"""
sheet_utils.py
──────────────
Shared helpers for reading the OneHope resource Google Sheet and for
normalising document titles. Used by both indexer.py and main.py.
"""

import csv
import io
import re
import logging

import requests

logger = logging.getLogger(__name__)

NAME_CANDIDATES = ["File Name", "Name", "Title", "Filename", "file_name", "name"]
FOLDER_CANDIDATES = ["Folder", "Category", "Section", "Type", "folder", "category"]


# ── TEXT CLEAN-UP ─────────────────────────────────────────────────────────────

def fix_mojibake(value: str) -> str:
    """
    Repairs UTF-8 text that was wrongly decoded as Latin-1
    (e.g. 'ð\\x9f\\x93\\x84 Manual.pdf' → '📄 Manual.pdf').
    Returns the input unchanged if it isn't mojibake.
    """
    if not isinstance(value, str) or not value:
        return value
    if not re.search(r"[À-ÿ][\u0080-¿]", value):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def doc_key(file_name: str) -> str:
    """
    A normalised identity for a document title, so that the PDF and PPTX
    exports of the same lesson (or 'X (1).pdf' copies) count as one document.
    """
    name = fix_mojibake(file_name or "").lower()
    name = re.sub(r"\.(pdf|pptx?|docx?|xlsx?|csv|txt)$", "", name.strip())
    name = re.sub(r"\s*-\s*read-only\s*$", "", name)
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)
    name = re.sub(r"\[\d+\]", "", name)
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return name.strip()


def display_name(file_name: str) -> str:
    """Title as shown to users: mojibake fixed, leading emoji/markers stripped."""
    name = fix_mojibake(file_name or "").strip()
    return re.sub(r"^[^\w(\[]+", "", name).strip() or name


# Operating-system clutter that gets uploaded along with real folders.
_JUNK_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})


def is_junk_file(name: str) -> bool:
    """True for files nobody ever wants as a resource (.DS_Store, ._ copies,
    Office lock files), with or without a leading 📄 marker."""
    base = fix_mojibake(name or "").strip().rsplit("/", 1)[-1].lower()
    base = re.sub(r"^[^\w.~]+", "", base)     # strip 📄 markers but keep a leading dot
    return base in _JUNK_NAMES or base.startswith("._") or base.startswith("~$")


# ── SHEET LOADING ─────────────────────────────────────────────────────────────
# Plain csv instead of pandas: pandas adds ~40 MB of memory just to read a
# small spreadsheet, and the server only has 512 MB.

class Sheet:
    """A parsed CSV: column names plus rows as dicts."""

    def __init__(self, columns: list, rows: list):
        self.columns = columns
        self.rows = rows

    @classmethod
    def from_csv(cls, text: str) -> "Sheet":
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
        header = next(reader, [])
        columns, seen = [], {}
        for i, name in enumerate(header):
            name = name.strip() or f"Column {i + 1}"
            if name in seen:                   # duplicate headers → "Name.1", like pandas
                seen[name] += 1
                name = f"{name}.{seen[name]}"
            else:
                seen[name] = 0
            columns.append(name)
        rows = [dict(zip(columns, r)) for r in reader if any(cell.strip() for cell in r)]
        return cls(columns, rows)

    @classmethod
    def from_columns(cls, data: dict) -> "Sheet":
        columns = list(data)
        n = max((len(v) for v in data.values()), default=0)
        return cls(columns, [{c: data[c][i] for c in columns} for i in range(n)])


def fetch_sheet(url: str, timeout: int = 30) -> Sheet:
    """Downloads the published sheet CSV, always decoding it as UTF-8."""
    if not url:
        raise EnvironmentError("SHEET_CSV_URL is not set.")
    res = requests.get(url, timeout=timeout)
    res.raise_for_status()
    # Google serves the CSV without a charset, so requests guesses Latin-1
    # and mangles emoji/accents. Decode the raw bytes ourselves.
    return Sheet.from_csv(res.content.decode("utf-8-sig", errors="replace"))


def find_link_column(sheet: Sheet) -> str:
    for col in sheet.columns:
        if any("drive.google.com" in str(row.get(col) or "") for row in sheet.rows):
            return col
    raise ValueError("Could not find a column with Google Drive links in the sheet.")


def find_name_column(sheet: Sheet) -> str:
    for candidate in NAME_CANDIDATES:
        if candidate in sheet.columns:
            return candidate
    return sheet.columns[0]


def find_folder_column(sheet: Sheet):
    for candidate in FOLDER_CANDIDATES:
        if candidate in sheet.columns:
            return candidate
    return None


def _clean(value) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in ("nan", "none") else fix_mojibake(text)


# ── FOLDER-TREE SHEETS ────────────────────────────────────────────────────────
# The "OneHope Resource Centre Folder Structure" sheet lists the Drive tree:
#   Hierarchy | Name                              | Type   | Size | Link
#   2.1.1.3.  |             📁 The God Man Movie   | Folder | -    | …
#             |                 📄 English ….mp4    | File   | 259 MB | …
# File rows have no Hierarchy value; their folder is the indentation.

_HIERARCHY_RE = re.compile(r"^\d+(?:\.\d+)*\.?$")
_MARKERS_RE = re.compile(r"^[\s 📁📂📄]+")


def find_tree_type_column(sheet: Sheet):
    """The Folder/File column, if this sheet is a folder-tree listing."""
    for col in sheet.columns:
        values = {_clean(r.get(col)).lower() for r in sheet.rows} - {""}
        if "folder" in values and values <= {"folder", "file"}:
            return col
    return None


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip("  \t"))


def _tree_to_catalog(sheet: Sheet, name_col: str, link_col: str, type_col: str) -> list:
    hier_col = next((c for c in sheet.columns if c.strip().lower() == "hierarchy"), None)
    indents = [_indent(str(r.get(name_col) or "")) for r in sheet.rows]
    unit = min((i for i in indents if i > 0), default=0)

    stack: list = []          # (depth, folder name) of the folders we're inside
    catalog = []
    for row, indent in zip(sheet.rows, indents):
        raw = fix_mojibake(str(row.get(name_col) or ""))
        name = _MARKERS_RE.sub("", raw).strip()
        if not name:
            continue
        depth = indent // unit if unit else None

        if _clean(row.get(type_col)).lower() == "folder":
            hier = _clean(row.get(hier_col)) if hier_col else ""
            if _HIERARCHY_RE.match(hier):
                depth = len([p for p in hier.split(".") if p]) - 1
            depth = depth if depth is not None else 0
            stack = [(d, n) for d, n in stack if d < depth] + [(depth, name)]
            continue

        url = _clean(row.get(link_col))
        if not url or is_junk_file(name):
            continue
        parents = [n for d, n in stack if depth is None or d < depth]
        catalog.append({
            "file_name": name,
            "drive_url": url,
            "folder": " / ".join(parents) or "General",
            "details": "",
        })
    return catalog


def sheet_to_catalog(sheet: Sheet) -> list:
    """
    Turns the sheet into a list of catalogue entries:
    {file_name, drive_url, folder, details}
    'details' holds every other text column (language, audience, type…)
    so they are searchable too.
    """
    if not sheet.columns:
        return []
    try:
        link_col = find_link_column(sheet)
    except ValueError:
        logger.warning("Sheet has no Drive link column; catalogue disabled.")
        return []
    name_col = find_name_column(sheet)

    type_col = find_tree_type_column(sheet)
    if type_col:
        return _tree_to_catalog(sheet, name_col, link_col, type_col)

    folder_col = find_folder_column(sheet)
    other_cols = [c for c in sheet.columns if c not in (link_col, name_col, folder_col)]

    catalog = []
    for row in sheet.rows:
        name = _clean(row.get(name_col))
        url = _clean(row.get(link_col))
        if not name or not url or is_junk_file(name):
            continue
        details = "; ".join(
            f"{col}: {val}" for col in other_cols
            if (val := _clean(row.get(col))) and "http" not in val
        )
        catalog.append({
            "file_name": name,
            "drive_url": url,
            "folder": _clean(row.get(folder_col)) if folder_col else "General",
            "details": details[:200],
        })
    return catalog
