"""
drive_reader.py
───────────────
Fetches files from Google Drive with a service account and extracts their text.
Supports: PDF, Google Docs / Slides / Sheets, .pptx, .docx, .xlsx

Built to stay small in memory (the server has 512 MB):
  • files are streamed to a temporary file on disk, never held whole in memory;
  • PDFs are read page by page with pdfium (fast, and far lighter than
    pdfplumber);
  • .pptx/.docx text is read straight from the XML inside the file, so
    embedded images and videos are never loaded;
  • each document's text is capped at MAX_CHARS_PER_DOC (spreadsheets lower).
"""

import os
import re
import json
import time
import shutil
import logging
import tempfile
import zipfile
from html import unescape
from typing import Optional
from xml.etree import ElementTree

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# ── MIME TYPE ROUTING ─────────────────────────────────────────────────────────

EXTRACTABLE_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.document": "gdoc",
    "application/vnd.google-apps.presentation": "gslides",
    "application/vnd.google-apps.spreadsheet": "gsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

SKIP_TYPES = {"application/octet-stream"}

# Upper bound on text kept per document (whole manuals, not just page 1).
MAX_CHARS_PER_DOC = int(os.environ.get("MAX_CHARS_PER_DOC", "400000"))
# Spreadsheets are mostly numbers and grids; keep less of them.
MAX_CHARS_PER_SHEET = int(os.environ.get("MAX_CHARS_PER_SHEET", "50000"))
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds; doubles each retry
DOWNLOAD_CHUNK = 4 * 1024 * 1024


# ── SERVICE ACCOUNT SETUP ─────────────────────────────────────────────────────

def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    creds_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_raw:
        raise EnvironmentError(
            "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set. "
            "See .env.example for instructions."
        )

    try:
        creds_dict = json.loads(creds_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── FILE ID EXTRACTION ────────────────────────────────────────────────────────

_FILE_ID_PATTERNS = [
    r"/file/d/([a-zA-Z0-9_-]+)",
    r"/document/d/([a-zA-Z0-9_-]+)",
    r"/presentation/d/([a-zA-Z0-9_-]+)",
    r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
    r"[?&]id=([a-zA-Z0-9_-]+)",
]


def extract_file_id(drive_url: str) -> Optional[str]:
    for pattern in _FILE_ID_PATTERNS:
        match = re.search(pattern, drive_url or "")
        if match:
            return match.group(1)
    logger.warning(f"Could not extract file ID from URL: {drive_url}")
    return None


# ── RETRY HELPER ─────────────────────────────────────────────────────────────

def _retry_call(fn, *args, max_retries=MAX_RETRIES, **kwargs):
    """Calls fn and retries on Drive 429/5xx errors with exponential backoff."""
    delay = RETRY_BACKOFF
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except HttpError as e:
            status = e.resp.status if hasattr(e, "resp") else 0
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                logger.warning(f"Drive API error {status}, retry {attempt}/{max_retries} in {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise


# ── ENTRY POINTS ──────────────────────────────────────────────────────────────

def is_media(mime_type: str) -> bool:
    return mime_type in SKIP_TYPES or mime_type.split("/")[0] in ("video", "audio", "image")


def is_extractable(mime_type: str) -> bool:
    return mime_type in EXTRACTABLE_TYPES


def extract_text(file_id: str, mime_type: str, name: str, service) -> str:
    """
    Extracts text from a Drive file whose metadata is already known.
    Raises on failure so the caller can record the error.
    """
    kind = EXTRACTABLE_TYPES[mime_type]
    if kind in ("gdoc", "gslides", "gsheet"):
        export_type = "text/csv" if kind == "gsheet" else "text/plain"
        logger.info(f"  → Exporting {kind}: {name}")
        content = _retry_call(service.files().export(fileId=file_id, mimeType=export_type).execute)
        limit = MAX_CHARS_PER_SHEET if kind == "gsheet" else MAX_CHARS_PER_DOC
        return _clean(content.decode("utf-8", errors="replace"), limit)

    tmp_dir = tempfile.mkdtemp(prefix="onehope-")
    try:
        path = os.path.join(tmp_dir, "file")
        logger.info(f"  → Downloading {kind}: {name}")
        _download_to_file(file_id, service, path)
        return extract_text_from_path(path, kind)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def extract_text_from_path(path: str, kind: str) -> str:
    """Extracts text from a local file. kind is pdf, pptx, docx or xlsx."""
    extractor = {"pdf": _pdf_text, "pptx": _pptx_text, "docx": _docx_text, "xlsx": _xlsx_text}[kind]
    limit = MAX_CHARS_PER_SHEET if kind == "xlsx" else MAX_CHARS_PER_DOC
    return _clean(extractor(path, limit), limit)


def _clean(text: str, limit: int) -> str:
    return (text or "").replace("\x00", "")[:limit]


def _download_to_file(file_id: str, service, path: str) -> None:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=DOWNLOAD_CHUNK)
        done = False
        while not done:
            _, done = _retry_call(downloader.next_chunk)


# ── EXTRACTORS ────────────────────────────────────────────────────────────────

def _pdf_text(path: str, limit: int) -> str:
    import pypdfium2 as pdfium

    parts, total = [], 0
    pdf = pdfium.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            if text:
                parts.append(text)
                total += len(text)
            if total >= limit:
                break
    finally:
        pdf.close()
    return "\n".join(parts)


_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_PARAGRAPH_RE = re.compile(r"<a:p[ >].*?</a:p>", re.DOTALL)
_RUN_TEXT_RE = re.compile(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", re.DOTALL)


def _pptx_text(path: str, limit: int) -> str:
    """Slide text in slide order, read from the XML only (no images/media)."""
    parts, total = [], 0
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            (int(m.group(1)), name) for name in zf.namelist() if (m := _SLIDE_RE.match(name))
        )
        for _, name in slides:
            xml = zf.read(name).decode("utf-8", errors="replace")
            for para in _PARAGRAPH_RE.findall(xml):
                line = "".join(unescape(t) for t in _RUN_TEXT_RE.findall(para)).strip()
                if line:
                    parts.append(line)
                    total += len(line) + 1
            if total >= limit:
                break
    return "\n".join(parts)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(path: str, limit: int) -> str:
    """Paragraph text streamed from word/document.xml (no images loaded)."""
    parts, total, current = [], 0, []
    with zipfile.ZipFile(path) as zf, zf.open("word/document.xml") as fh:
        for event, elem in ElementTree.iterparse(fh, events=("end",)):
            if elem.tag == f"{_W}t" and elem.text:
                current.append(elem.text)
            elif elem.tag == f"{_W}tab":
                current.append("\t")
            elif elem.tag == f"{_W}p":
                line = "".join(current).strip()
                current = []
                if line:
                    parts.append(line)
                    total += len(line) + 1
                elem.clear()
                if total >= limit:
                    break
    return "\n".join(parts)


def _xlsx_text(path: str, limit: int) -> str:
    import openpyxl

    lines, total = [], 0
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in wb.worksheets:
            lines.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                row_text = "\t".join(str(cell) for cell in row if cell is not None)
                if row_text.strip():
                    lines.append(row_text)
                    total += len(row_text) + 1
                if total >= limit:
                    break
            if total >= limit:
                break
    finally:
        wb.close()
    return "\n".join(lines)
