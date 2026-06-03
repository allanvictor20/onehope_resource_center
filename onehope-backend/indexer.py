"""
indexer.py
──────────
Run this ONCE to index all your OneHope documents into a local search index.
Re-run whenever you add new files to your Drive folder.

Usage:
    python indexer.py

What it does:
    1. Fetches your Google Sheet to get all file names and Drive links
    2. For each file, extracts text (skipping videos, images, audio)
    3. Breaks text into overlapping chunks of ~500 words
    4. Saves all chunks + metadata to index.json (local file, no C++ needed)

After running, main.py uses TF-IDF search over index.json to find
relevant chunks per question — no ChromaDB, no sentence-transformers,
no compilation required.
"""

import os
import sys
import time
import json
import logging
import requests
import pandas as pd
from io import StringIO
from dotenv import load_dotenv
from drive_reader import get_drive_service, extract_text_from_drive_file, extract_file_id

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "")
INDEX_PATH    = "./search_index.json"   # where the index is saved
CHUNK_SIZE    = 500   # words per chunk
CHUNK_OVERLAP = 50    # words of overlap between chunks


# ── TEXT CHUNKING ─────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    """
    Splits text into overlapping chunks of ~chunk_size words.
    Overlap helps avoid splitting context across chunk boundaries.
    """
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


# ── SHEET FETCHER ─────────────────────────────────────────────────────────────

def fetch_sheet_rows() -> pd.DataFrame:
    logger.info("Fetching Google Sheet...")
    if not SHEET_CSV_URL:
        raise EnvironmentError("SHEET_CSV_URL is not set in .env")
    res = requests.get(SHEET_CSV_URL, timeout=30)
    res.raise_for_status()
    df = pd.read_csv(StringIO(res.text))
    logger.info(f"Sheet loaded: {len(df)} rows, columns: {list(df.columns)}")
    return df


def find_link_column(df: pd.DataFrame) -> str:
    for col in df.columns:
        sample = df[col].dropna().astype(str)
        if sample.str.contains("drive.google.com", na=False).any():
            return col
    raise ValueError("Could not find a column with Google Drive links in the sheet.")


def find_name_column(df: pd.DataFrame) -> str:
    for candidate in ["File Name", "Name", "Title", "Filename", "file_name", "name"]:
        if candidate in df.columns:
            return candidate
    return df.columns[0]


def find_folder_column(df: pd.DataFrame):
    for candidate in ["Folder", "Category", "Section", "Type", "folder", "category"]:
        if candidate in df.columns:
            return candidate
    return None


# ── MAIN INDEXER ──────────────────────────────────────────────────────────────

def run_indexer():
    logger.info("=" * 60)
    logger.info("OneHope Resource Centre — Document Indexer")
    logger.info("=" * 60)

    # 1. Load sheet
    df = fetch_sheet_rows()
    link_col   = find_link_column(df)
    name_col   = find_name_column(df)
    folder_col = find_folder_column(df)

    logger.info(f"Columns → name: '{name_col}' | link: '{link_col}' | folder: '{folder_col or 'none'}'")

    # 2. Authenticate with Google Drive
    logger.info("Authenticating with Google Drive...")
    drive_service = get_drive_service()

    # 3. Process each row and collect chunks
    all_chunks = []   # list of dicts: {text, file_name, drive_url, folder}
    total   = len(df)
    indexed = 0
    skipped = 0

    for i, row in df.iterrows():
        file_name  = str(row.get(name_col, "")).strip()
        drive_url  = str(row.get(link_col, "")).strip()
        folder     = str(row.get(folder_col, "")).strip() if folder_col else "General"

        if not drive_url or drive_url.lower() in ("nan", "none", ""):
            logger.warning(f"  [{i+1}/{total}] No link for: {file_name} — skipping")
            skipped += 1
            continue

        logger.info(f"\n[{i+1}/{total}] {file_name}")

        text = extract_text_from_drive_file(drive_url, drive_service)

        if text is None:
            logger.info("  → Skipped (unsupported or media file)")
            skipped += 1
            continue

        if len(text.strip()) < 50:
            logger.info("  → Skipped (too little text extracted)")
            skipped += 1
            continue

        chunks = chunk_text(text)
        logger.info(f"  → {len(chunks)} chunks from {len(text)} chars")

        for chunk in chunks:
            all_chunks.append({
                "text":      chunk,
                "file_name": file_name,
                "drive_url": drive_url,
                "folder":    folder,
            })

        indexed += 1
        time.sleep(0.5)  # avoid hitting Drive rate limits

    # 4. Save index to JSON
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    # 5. Summary
    logger.info("\n" + "=" * 60)
    logger.info("Indexing complete!")
    logger.info(f"  ✅ Indexed:  {indexed} files")
    logger.info(f"  ⏭  Skipped:  {skipped} files")
    logger.info(f"  📄 Total chunks saved: {len(all_chunks)}")
    logger.info(f"  💾 Index saved to: {INDEX_PATH}")
    logger.info("=" * 60)
    logger.info("You can now start the backend with: uvicorn main:app --reload")


if __name__ == "__main__":
    run_indexer()
