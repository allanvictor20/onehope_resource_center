"""
sheet_utils.py
──────────────
Shared helpers for reading the OneHope resource Google Sheet and for
normalising document titles. Used by both indexer.py and main.py.
"""

import re
import logging
from io import StringIO

import pandas as pd
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


# ── SHEET LOADING ─────────────────────────────────────────────────────────────

def fetch_sheet(url: str, timeout: int = 30) -> pd.DataFrame:
    """Downloads the published sheet CSV, always decoding it as UTF-8."""
    if not url:
        raise EnvironmentError("SHEET_CSV_URL is not set.")
    res = requests.get(url, timeout=timeout)
    res.raise_for_status()
    # Google serves the CSV without a charset, so requests guesses Latin-1
    # and mangles emoji/accents. Decode the raw bytes ourselves.
    text = res.content.decode("utf-8-sig", errors="replace")
    return pd.read_csv(StringIO(text))


def find_link_column(df: pd.DataFrame) -> str:
    for col in df.columns:
        sample = df[col].dropna().astype(str)
        if sample.str.contains("drive.google.com", na=False).any():
            return col
    raise ValueError("Could not find a column with Google Drive links in the sheet.")


def find_name_column(df: pd.DataFrame) -> str:
    for candidate in NAME_CANDIDATES:
        if candidate in df.columns:
            return candidate
    return df.columns[0]


def find_folder_column(df: pd.DataFrame):
    for candidate in FOLDER_CANDIDATES:
        if candidate in df.columns:
            return candidate
    return None


def _clean(value) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in ("nan", "none") else fix_mojibake(text)


def sheet_to_catalog(df: pd.DataFrame) -> list:
    """
    Turns the sheet into a list of catalogue entries:
    {file_name, drive_url, folder, details}
    'details' holds every other text column (language, audience, type…)
    so they are searchable too.
    """
    try:
        link_col = find_link_column(df)
    except ValueError:
        logger.warning("Sheet has no Drive link column; catalogue disabled.")
        return []
    name_col = find_name_column(df)
    folder_col = find_folder_column(df)
    other_cols = [c for c in df.columns if c not in (link_col, name_col, folder_col)]

    catalog = []
    for _, row in df.iterrows():
        name = _clean(row.get(name_col))
        url = _clean(row.get(link_col))
        if not name or not url:
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
