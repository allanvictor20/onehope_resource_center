"""
search.py
─────────
In-memory search over the synced documents.

  • SearchIndex — word TF-IDF over document passages, with a spelling
    corrector so typos ("sparck", "mentorhsip") still match.
  • Catalog     — title-level search over every file in the library,
    including videos and images that have no text.
"""

import re
from collections import defaultdict

import numpy as np
from sklearn.feature_extraction.text import (
    ENGLISH_STOP_WORDS, HashingVectorizer, TfidfTransformer, TfidfVectorizer,
)

from sheet_utils import doc_key, display_name, fix_mojibake

TOP_K_CHUNKS       = 6
MAX_CHUNKS_PER_DOC = 2
MIN_SCORE          = 0.03
MAX_CONTEXT_CHARS  = 12000
CATALOG_MATCHES    = 8
HASH_FEATURES      = 2 ** 20
CHUNK_SIZE_WORDS   = 300
CHUNK_OVERLAP_WORDS = 60


def chunk_text(text: str, size: int = CHUNK_SIZE_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list:
    """Splits text into overlapping passages of ~size words."""
    words = text.split()
    chunks, start = [], 0
    while start < len(words):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def build_chunks(docs: list) -> list:
    """Turns stored documents (with text) into searchable passages."""
    chunks = []
    for doc in docs:
        if not doc.get("text"):
            continue
        for passage in chunk_text(doc["text"]):
            chunks.append({
                "text": passage,
                "file_name": doc["name"],
                "drive_url": doc["drive_url"],
                "folder": doc.get("folder") or "General",
                "file_id": doc.get("file_id"),
            })
    return chunks


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


# ── SEARCH INDEX ──────────────────────────────────────────────────────────────
# Word TF-IDF over the chunks, plus a small spelling corrector over the index
# vocabulary so that typos ("sparck", "mentorhsip") still find the right terms.
# The corrector works on unique words only, so memory stays small even when
# full documents are indexed.

_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")


def _vocabulary(texts) -> set:
    """Distinct words in the library, for the spelling corrector."""
    words = set()
    for text in texts:
        words.update(_TOKEN_RE.findall(text.lower()))
    return words


class TermCorrector:
    def __init__(self, vocabulary):
        self.terms = [t for t in vocabulary if " " not in t and len(t) >= 4 and t.isalpha()]
        self.known = set(self.terms)
        self.first_letters = np.array([t[0] for t in self.terms])
        self.lengths = np.array([len(t) for t in self.terms])
        self.vec = None
        self.matrix = None
        if self.terms:
            self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), lowercase=True)
            self.matrix = self.vec.fit_transform(self.terms)

    def expand(self, query: str, min_sim: float = 0.5) -> str:
        """Returns the query plus the closest known word for each unknown word."""
        if self.matrix is None:
            return query
        unknown = [t for t in _TOKEN_RE.findall(query.lower())
                   if len(t) >= 5 and t.isalpha() and t not in self.known and t not in ENGLISH_STOP_WORDS]
        extra = []
        for token in unknown[:8]:
            sims = (self.matrix @ self.vec.transform([token]).T).toarray().ravel()
            # Typos almost always keep the first letter; requiring it stops
            # e.g. "Luganda" being "corrected" to "uganda".
            sims[self.first_letters != token[0]] = 0
            sims[np.abs(self.lengths - len(token)) > 2] = 0
            best = int(sims.argmax())
            if sims[best] >= min_sim:
                extra.append(self.terms[best])
        return f"{query} {' '.join(extra)}" if extra else query


class SearchIndex:
    def __init__(self, chunks: list):
        self.chunks = chunks
        self.vec = None
        self.matrix = None
        self.corrector = TermCorrector([])
        if not chunks:
            return
        for chunk in chunks:
            chunk["file_name"] = fix_mojibake(chunk.get("file_name", "Unknown"))
            chunk["folder"] = fix_mojibake(chunk.get("folder", "General"))
            chunk["doc_key"] = doc_key(chunk["file_name"])
        # Title is included so questions naming a resource find it.
        texts = [f"{display_name(c['file_name'])}. {c['text']}" for c in chunks]
        # Feature hashing keeps memory flat as the library grows: there is no
        # vocabulary of every word pair held in memory (Render free = 512 MB).
        self.vec = HashingVectorizer(
            strip_accents="unicode", lowercase=True, ngram_range=(1, 2),
            stop_words="english", n_features=HASH_FEATURES, alternate_sign=False,
            norm=None, dtype=np.float32,
        )
        self.tfidf = TfidfTransformer(sublinear_tf=True)
        self.matrix = self.tfidf.fit_transform(self.vec.transform(texts)).astype(np.float32)
        self.corrector = TermCorrector(_vocabulary(texts))

    @property
    def ready(self) -> bool:
        return self.matrix is not None

    def search(self, query: str, top_k: int = TOP_K_CHUNKS) -> list:
        if not self.ready or not query.strip():
            return []
        q = self.tfidf.transform(self.vec.transform([self.corrector.expand(query)]))
        scores = (self.matrix @ q.T).toarray().ravel()

        results, per_doc, first_file, seen_text = [], defaultdict(int), {}, set()
        for idx in scores.argsort()[::-1]:
            score = float(scores[idx])
            if score < MIN_SCORE or len(results) >= top_k:
                break
            chunk = self.chunks[idx]
            key = chunk["doc_key"]
            fingerprint = hash(chunk["text"][:300])
            # Skip exact duplicates (same file listed twice in the sheet) and
            # the other format of the same lesson (PDF vs PPTX), and cap
            # chunks per document so one manual can't crowd out the rest.
            if fingerprint in seen_text:
                continue
            if first_file.setdefault(key, chunk["file_name"]) != chunk["file_name"]:
                continue
            if per_doc[key] >= MAX_CHUNKS_PER_DOC:
                continue
            seen_text.add(fingerprint)
            per_doc[key] += 1
            results.append({**chunk, "score": score})
        return results


class Catalog:
    """Title-level search over every file in the library, including media
    (videos, images) that have no extractable text. Entries are
    {file_name, drive_url, folder, details}."""

    def __init__(self, entries: list):
        self.entries = entries
        self.vec = None
        self.matrix = None
        if entries:
            texts = [f"{display_name(e['file_name'])} {e['folder']} {e['details']}" for e in entries]
            self.vec = TfidfVectorizer(
                strip_accents="unicode", lowercase=True, analyzer="char_wb", ngram_range=(3, 5),
            )
            self.matrix = self.vec.fit_transform(texts)

    def search(self, query: str, top_k: int = CATALOG_MATCHES, min_score: float = 0.12) -> list:
        if self.matrix is None:
            return []
        scores = (self.matrix @ self.vec.transform([query]).T).toarray().ravel()
        out, seen = [], set()
        for idx in scores.argsort()[::-1]:
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
