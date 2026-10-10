"""
search.py
─────────
In-memory search over the synced documents, using only numpy.

  • SearchIndex — TF-IDF over document passages (words and word pairs), with
    a spelling corrector so typos ("sparck", "mentorhsip") still match.
  • Catalog     — title-level search over every file in the library,
    including videos and images that have no text.

Why not scikit-learn? Importing it (with scipy) costs ~130 MB, a quarter of
Render's free 512 MB. The few pieces we need — tokenising, feature hashing,
TF-IDF weighting and cosine similarity — are small and implemented here with
the same formulas (smooth idf, sublinear tf, L2-normalised vectors).

Memory notes:
  • each document's text is kept once, as compact UTF-8 bytes, and a passage
    is just (document, start byte, end byte);
  • documents are consumed one at a time from an iterator;
  • TF-IDF weighting is done in place, block by block, so building the index
    needs little more memory than the finished index.
"""

import math
import re
import unicodedata
from array import array
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Iterable

import numpy as np

from sheet_utils import doc_key, display_name, fix_mojibake

TOP_K_CHUNKS       = 6
MAX_CHUNKS_PER_DOC = 2
MIN_SCORE          = 0.03
MAX_CONTEXT_CHARS  = 12000
CATALOG_MATCHES    = 8
HASH_BITS          = 20
CHUNK_SIZE_WORDS   = 300
CHUNK_OVERLAP_WORDS = 60

_HASH_MASK = (1 << HASH_BITS) - 1
_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")

STOP_WORDS = frozenset("""
a about above after again against all almost also am among an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does doesn't doing don't
down during each either else ever every few for from further get got had hadn't has hasn't have haven't
having he her here hers herself him himself his how however i if in into is isn't it its itself just
least less let like many may me might more most much must my myself neither no nor not now of off often
on once only or other our ours ourselves out over own per perhaps rather same shall she should shouldn't
since so some such than that the their theirs them themselves then there these they this those though
through thus to too under until up upon us very was wasn't we were weren't what when where whether which
while who whom whose why will with within without would wouldn't yet you your yours yourself yourselves
""".split())


# ── TEXT HELPERS ──────────────────────────────────────────────────────────────

def _strip_accents(text: str) -> str:
    if text.isascii():
        return text
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokenize(text: str) -> list:
    """Lower-cased words of 2+ characters, accents removed, stop words dropped."""
    return [t for t in _TOKEN_RE.findall(_strip_accents(text.lower())) if t not in STOP_WORDS]


def _features(tokens: list) -> Counter:
    """Hashed counts of words and adjacent word pairs. Python's hash() is
    stable within one process, and the index is always built and queried in
    the same process, so this is safe."""
    feats = Counter(hash(t) & _HASH_MASK for t in tokens)
    feats.update(hash((a, b)) & _HASH_MASK for a, b in zip(tokens, tokens[1:]))
    return feats


def _chunk_ranges(n_words: int, size: int = CHUNK_SIZE_WORDS, overlap: int = CHUNK_OVERLAP_WORDS):
    """(first_word, end_word) for overlapping passages of ~size words."""
    start = 0
    while start < n_words:
        yield start, min(start + size, n_words)
        if start + size >= n_words:
            break
        start += size - overlap


def chunk_text(text: str, size: int = CHUNK_SIZE_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list:
    """Splits text into overlapping passages of ~size words."""
    words = text.split()
    return [" ".join(words[a:b]) for a, b in _chunk_ranges(len(words), size, overlap)]


def _byte_offsets(text: str, spans):
    """Maps the character offsets used by spans to UTF-8 byte offsets, in a
    single pass over the text (only needed for non-ASCII documents)."""
    positions = sorted({p for span in spans for p in span})
    mapping, char_pos, byte_pos = {}, 0, 0
    for p in positions:
        byte_pos += len(text[char_pos:p].encode("utf-8"))
        char_pos = p
        mapping[p] = byte_pos
    return mapping.__getitem__


_KIND_BY_MIME_PREFIX = {"video/": "video", "audio/": "audio", "image/": "image"}
_KIND_BY_MIME = {
    "application/pdf": "PDF document",
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
}


def kind_of(mime_type: str) -> str:
    mime_type = mime_type or ""
    for prefix, kind in _KIND_BY_MIME_PREFIX.items():
        if mime_type.startswith(prefix):
            return kind
    return _KIND_BY_MIME.get(mime_type, "file")


# ── SPELLING CORRECTOR ────────────────────────────────────────────────────────

class TermCorrector:
    """Adds the closest library word for each query word the library doesn't
    contain. Candidates must share the first letter and be within two letters
    in length — typos almost always keep the first letter, and this stops e.g.
    "Luganda" being "corrected" to "uganda"."""

    def __init__(self, vocabulary: Iterable[str] = ()):
        self.known = {t for t in vocabulary if len(t) >= 4 and t.isalpha()}
        self.by_shape = defaultdict(list)
        for t in self.known:
            self.by_shape[(t[0], len(t))].append(t)

    def expand(self, query: str, min_sim: float = 0.8) -> str:
        if not self.known:
            return query
        extra = []
        unknown = [t for t in _TOKEN_RE.findall(_strip_accents(query.lower()))
                   if len(t) >= 5 and t.isalpha() and t not in self.known and t not in STOP_WORDS]
        for token in unknown[:8]:
            best, best_sim = None, min_sim
            for length in range(len(token) - 2, len(token) + 3):
                for term in self.by_shape.get((token[0], length), ()):
                    sim = SequenceMatcher(None, token, term).ratio()
                    if sim > best_sim or (sim == best_sim and best is not None and term < best):
                        best, best_sim = term, sim
            if best:
                extra.append(best)
        return f"{query} {' '.join(extra)}" if extra else query


# ── PASSAGE SEARCH ────────────────────────────────────────────────────────────

class SearchIndex:
    def __init__(self, docs: Iterable[dict] = ()):
        self.doc_texts: list = []          # UTF-8 bytes, whitespace normalised
        self.doc_names: list = []
        self.doc_urls: list = []
        self.doc_folders: list = []
        self.doc_keys: list = []
        self.corrector = TermCorrector()
        self.idf = None

        chunk_doc, chunk_start, chunk_end = array("i"), array("q"), array("q")
        feat_ids, feat_vals, row_ptr = array("i"), array("f"), array("q", [0])
        vocabulary: set = set()

        for doc in docs:
            text = doc.pop("text", None)         # let the caller's copy go
            words = text.split() if text else []
            del text
            if not words:
                continue
            name = fix_mojibake(doc.get("name") or doc.get("file_name") or "Unknown")
            title = display_name(name)
            title_tokens = tokenize(title)
            d = len(self.doc_texts)
            normalised = " ".join(words)
            encoded = normalised.encode("utf-8")

            starts, pos = [], 0
            for w in words:
                starts.append(pos)
                pos += len(w) + 1
            spans = [(starts[a], starts[b - 1] + len(words[b - 1])) for a, b in _chunk_ranges(len(words))]
            byte_of = (lambda c: c) if len(encoded) == len(normalised) else _byte_offsets(normalised, spans)

            for cs, ce in spans:
                tokens = tokenize(normalised[cs:ce])
                vocabulary.update(tokens)
                # Title words are included so questions naming a resource find it.
                feats = _features(title_tokens + tokens)
                feat_ids.extend(feats.keys())
                feat_vals.extend(feats.values())
                row_ptr.append(len(feat_ids))
                chunk_doc.append(d)
                chunk_start.append(byte_of(cs))
                chunk_end.append(byte_of(ce))

            vocabulary.update(title_tokens)
            self.doc_texts.append(encoded)
            self.doc_names.append(name)
            self.doc_urls.append(doc.get("drive_url", ""))
            self.doc_folders.append(fix_mojibake(doc.get("folder") or "General"))
            self.doc_keys.append(doc_key(name))
            del words, starts, spans, normalised

        self.chunk_doc = np.frombuffer(chunk_doc, dtype=np.int32)
        self.chunk_start = np.frombuffer(chunk_start, dtype=np.int64)
        self.chunk_end = np.frombuffer(chunk_end, dtype=np.int64)
        if len(self.chunk_doc):
            self._build_matrix(np.frombuffer(feat_ids, dtype=np.int32),
                               np.frombuffer(feat_vals, dtype=np.float32),
                               np.frombuffer(row_ptr, dtype=np.int64))
            self.corrector = TermCorrector(vocabulary)

    BLOCK = 1 << 20     # entries processed at a time, to avoid big temporary arrays

    def _build_matrix(self, cols, vals, row_ptr) -> None:
        """TF-IDF weights + L2 normalisation, done in place and in blocks."""
        n_rows, nnz = len(row_ptr) - 1, len(cols)
        df = np.bincount(cols, minlength=_HASH_MASK + 1)
        self.idf = (np.log((1.0 + n_rows) / (1.0 + df)) + 1.0).astype(np.float32)   # smooth idf
        del df
        lengths = np.diff(row_ptr)
        norms = np.zeros(n_rows, dtype=np.float32)
        r0 = 0
        while r0 < n_rows:
            # A block of whole rows holding roughly BLOCK entries.
            r1 = int(np.searchsorted(row_ptr, row_ptr[r0] + self.BLOCK, side="right"))
            r1 = min(max(r1 - 1, r0 + 1), n_rows)
            a, b = row_ptr[r0], row_ptr[r1]
            seg = vals[a:b]
            np.log(seg, out=seg)
            seg += 1.0                                              # sublinear tf
            seg *= self.idf[cols[a:b]]
            local_rows = np.repeat(np.arange(r1 - r0, dtype=np.int32), lengths[r0:r1])
            norms[r0:r1] = np.sqrt(np.bincount(local_rows, weights=seg * seg, minlength=r1 - r0))
            r0 = r1
        norms[norms == 0] = 1.0
        r0 = 0
        while r0 < n_rows:
            r1 = int(np.searchsorted(row_ptr, row_ptr[r0] + self.BLOCK, side="right"))
            r1 = min(max(r1 - 1, r0 + 1), n_rows)
            a, b = row_ptr[r0], row_ptr[r1]
            vals[a:b] /= np.repeat(norms[r0:r1], lengths[r0:r1])
            r0 = r1
        # Kept row-wise: a query scans for its few terms (fast enough, and no
        # big re-sort of the whole matrix is needed).
        self.cols, self.vals, self.row_ptr = cols, vals, row_ptr
        assert len(self.vals) == nnz

    @property
    def ready(self) -> bool:
        return self.idf is not None

    @property
    def num_chunks(self) -> int:
        return len(self.chunk_doc)

    @property
    def num_docs(self) -> int:
        return len(set(self.doc_keys))

    def chunk(self, i: int, score: float = 0.0) -> dict:
        d = int(self.chunk_doc[i])
        raw = self.doc_texts[d][int(self.chunk_start[i]):int(self.chunk_end[i])]
        return {
            "text": raw.decode("utf-8", errors="ignore"),
            "file_name": self.doc_names[d],
            "drive_url": self.doc_urls[d],
            "folder": self.doc_folders[d],
            "doc_key": self.doc_keys[d],
            "score": score,
        }

    def scores(self, query: str) -> np.ndarray:
        feats = _features(tokenize(self.corrector.expand(query)))
        if not feats:
            return np.zeros(self.num_chunks, dtype=np.float32)
        q_feats = np.array(sorted(feats), dtype=np.int32)
        q_weights = np.array([(1.0 + math.log(feats[f])) * float(self.idf[f]) for f in q_feats],
                             dtype=np.float32)
        q_weights /= float(np.linalg.norm(q_weights)) or 1.0
        hits = np.flatnonzero(np.isin(self.cols, q_feats))
        rows = np.searchsorted(self.row_ptr, hits, side="right") - 1
        weights = q_weights[np.searchsorted(q_feats, self.cols[hits])] * self.vals[hits]
        return np.bincount(rows, weights=weights, minlength=self.num_chunks).astype(np.float32)

    def search(self, query: str, top_k: int = TOP_K_CHUNKS) -> list:
        if not self.ready or not query.strip():
            return []
        scores = self.scores(query)
        candidates = np.flatnonzero(scores >= MIN_SCORE)
        candidates = candidates[np.argsort(-scores[candidates], kind="stable")]

        results, per_doc, first_file, seen_text = [], defaultdict(int), {}, set()
        for idx in candidates:
            if len(results) >= top_k:
                break
            chunk = self.chunk(idx, float(scores[idx]))
            key = chunk["doc_key"]
            fingerprint = hash(chunk["text"][:300])
            # Skip exact duplicates (same file listed twice), the other format
            # of the same lesson (PDF vs PPTX), and cap passages per document
            # so one manual can't crowd out the rest.
            if fingerprint in seen_text:
                continue
            if first_file.setdefault(key, chunk["file_name"]) != chunk["file_name"]:
                continue
            if per_doc[key] >= MAX_CHUNKS_PER_DOC:
                continue
            seen_text.add(fingerprint)
            per_doc[key] += 1
            results.append(chunk)
        return results


# ── TITLE CATALOGUE ───────────────────────────────────────────────────────────

def _char_ngrams(text: str) -> Counter:
    """Character 3–5-grams within each word, padded with spaces."""
    grams = Counter()
    for word in _strip_accents(text.lower()).split():
        padded = f" {word} "
        for n in range(3, 6):
            for i in range(len(padded) - n + 1):
                grams[padded[i:i + n]] += 1
    return grams


class Catalog:
    """Title-level search over every file in the library, including media
    (videos, images) that have no extractable text. Entries are
    {file_name, drive_url, folder, details}."""

    def __init__(self, entries: list):
        self.entries = entries
        self.vectors = []
        self.idf = {}
        if not entries:
            return
        grams = [_char_ngrams(f"{display_name(e['file_name'])} {e['folder']} {e['details']}") for e in entries]
        df = Counter(g for doc in grams for g in doc)
        n = len(entries)
        self.idf = {g: math.log((1 + n) / (1 + c)) + 1 for g, c in df.items()}
        self.vectors = [self._normalise({g: c * self.idf[g] for g, c in doc.items()}) for doc in grams]

    @staticmethod
    def _normalise(vec: dict) -> dict:
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {g: v / norm for g, v in vec.items()}

    def search(self, query: str, top_k: int = CATALOG_MATCHES, min_score: float = 0.12) -> list:
        if not self.vectors:
            return []
        q = self._normalise({g: c * self.idf[g] for g, c in _char_ngrams(query).items() if g in self.idf})
        if not q:
            return []
        scores = [sum(w * vec.get(g, 0.0) for g, w in q.items()) for vec in self.vectors]
        out, seen = [], set()
        for idx in sorted(range(len(scores)), key=lambda i: -scores[i]):
            if scores[idx] < min_score or len(out) >= top_k:
                break
            entry = self.entries[idx]
            key = doc_key(entry["file_name"])
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
        return out


def build_context(chunks: list, max_chars: int = MAX_CONTEXT_CHARS):
    """Returns (context_text, chunks_actually_included)."""
    parts, used, total = [], [], 0
    for chunk in chunks:
        block = f"[From: {display_name(chunk['file_name'])}]\n{chunk['text']}"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 500:
                parts.append(block[:remaining] + "…")
                used.append(chunk)
            break
        parts.append(block)
        used.append(chunk)
        total += len(block)
    return "\n\n".join(parts), used
