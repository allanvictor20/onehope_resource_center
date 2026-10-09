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
    4. Saves all chunks + metadata to search_index.json (written atomically)

After running, main.py uses TF-IDF search over search_index.json to find
relevant chunks per question — no ChromaDB, no sentence-transformers,
no compilation required.
"""

import os
import sys
import time
import json
import logging
from dotenv import load_dotenv
from drive_reader import get_drive_service, extract_text_from_drive_file
from sheet_utils import (
    fetch_sheet, find_link_column, find_name_column, find_folder_column, fix_mojibake,
)

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
INDEX_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_index.json")
# Smaller chunks mean more distinct passages fit in the model's context budget.
CHUNK_SIZE    = int(os.environ.get("CHUNK_SIZE_WORDS", "300"))     # words per chunk
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP_WORDS", "60"))   # words of overlap


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

def fetch_sheet_rows():
    logger.info("Fetching Google Sheet...")
    df = fetch_sheet(SHEET_CSV_URL, timeout=30)
    logger.info(f"Sheet loaded: {len(df)} rows, columns: {list(df.columns)}")
    return df


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
        file_name  = fix_mojibake(str(row.get(name_col, "")).strip())
        drive_url  = str(row.get(link_col, "")).strip()
        folder     = fix_mojibake(str(row.get(folder_col, "")).strip()) if folder_col else "General"
        if folder.lower() in ("nan", "none"):
            folder = "General"

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

    # 4. Save index to JSON — write to a temp file then swap it in, so the
    #    running server never reads a half-written index.
    if not all_chunks:
        raise RuntimeError("Indexer produced no chunks; keeping the existing index.")
    tmp_path = INDEX_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, INDEX_PATH)

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
