"""
drive_sync.py
─────────────
Keeps the document store in step with the OneHope Google Drive folder.

Each sync:
  1. Lists every file in the folder (and its subfolders) — a handful of cheap
     Drive API calls, no downloads.
  2. Compares that list with what is already stored.
  3. Downloads and extracts text only for files that are new or changed,
     updates renamed/moved files without re-downloading, and removes files
     that were deleted from the folder.

So adding a document to Drive is all anyone has to do: the next scheduled sync
(every few minutes) makes it searchable.

Run once by hand with:   python indexer.py          (or --full to re-read everything)
"""

import os
import json
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from doc_store import DocStore, INDEXED, MEDIA, UNSUPPORTED, TOO_LARGE, EMPTY, ERROR
from drive_reader import (
    _retry_call, extract_file_id, extract_text, get_drive_service, is_extractable, is_media,
)

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = "id, name, mimeType, modifiedTime, size, webViewLink, shortcutDetails"

MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "60"))
MIN_TEXT_CHARS = 50
ERROR_RETRY_HOURS = 6       # failed files are retried at most this often
REBUILD_EVERY = 20          # refresh search after this many processed files


@dataclass
class SyncResult:
    source: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    total_files: int = 0
    added: int = 0
    updated: int = 0
    renamed: int = 0
    removed: int = 0
    failed: int = 0
    unchanged: int = 0
    added_names: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.renamed or self.removed)


# ── LISTING ───────────────────────────────────────────────────────────────────

def _file_record(f: dict, folder: str) -> dict:
    size = f.get("size")
    return {
        "file_id": f["id"],
        "name": f.get("name", "Untitled"),
        "mime_type": f.get("mimeType", ""),
        "modified_time": f.get("modifiedTime", ""),
        "size_bytes": int(size) if size else None,
        "drive_url": f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view",
        "folder": folder or "General",
    }


def list_folder_files(service, root_folder_id: str) -> list:
    """
    Every file under the folder, recursively. Subfolder names become the
    'folder' label (e.g. "Training / Spark"). Shortcuts are followed.
    Works for My Drive folders and Shared Drives.
    """
    found: dict = {}
    queue = [(root_folder_id, "")]
    seen_folders = {root_folder_id}

    while queue:
        folder_id, path = queue.pop(0)
        page_token = None
        while True:
            resp = _retry_call(service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=f"nextPageToken, files({FILE_FIELDS})",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute)

            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == SHORTCUT_MIME:
                    details = f.get("shortcutDetails") or {}
                    target_id = details.get("targetId")
                    if not target_id:
                        continue
                    if details.get("targetMimeType") == FOLDER_MIME:
                        f = {"id": target_id, "name": f.get("name"), "mimeType": FOLDER_MIME}
                        mime = FOLDER_MIME
                    else:
                        try:
                            f = _retry_call(service.files().get(
                                fileId=target_id, fields=FILE_FIELDS, supportsAllDrives=True).execute)
                            mime = f.get("mimeType", "")
                        except Exception as e:
                            logger.warning(f"Skipping broken shortcut {f.get('name')}: {e}")
                            continue

                if mime == FOLDER_MIME:
                    if f["id"] not in seen_folders:
                        seen_folders.add(f["id"])
                        sub = f"{path} / {f['name']}" if path else f["name"]
                        queue.append((f["id"], sub))
                    continue

                found[f["id"]] = _file_record(f, path)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    logger.info(f"Drive folder listing: {len(found)} files in {len(seen_folders)} folders")
    return list(found.values())


def list_sheet_files(service, sheet_url: str) -> list:
    """Legacy source: the files linked from the resource Google Sheet."""
    from sheet_utils import fetch_sheet, sheet_to_catalog

    found = {}
    for entry in sheet_to_catalog(fetch_sheet(sheet_url, timeout=30)):
        file_id = extract_file_id(entry["drive_url"])
        if not file_id or file_id in found:
            continue
        try:
            f = _retry_call(service.files().get(
                fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True).execute)
        except Exception as e:
            logger.warning(f"Can't read sheet link for {entry['file_name']}: {e}")
            continue
        if f.get("mimeType") == FOLDER_MIME:
            continue
        record = _file_record(f, entry["folder"])
        record["name"] = entry["file_name"] or record["name"]
        found[file_id] = record
    return list(found.values())


# ── SYNC ──────────────────────────────────────────────────────────────────────

def _process_file(record: dict, service) -> dict:
    """Decides what to store for one new/changed file, downloading if needed."""
    mime = record["mime_type"]
    doc = {**record, "text": None, "error": None}

    if is_media(mime):
        doc["status"] = MEDIA
    elif not is_extractable(mime):
        doc["status"] = UNSUPPORTED
    elif record.get("size_bytes") and record["size_bytes"] > MAX_FILE_MB * 1024 * 1024:
        doc["status"] = TOO_LARGE
    else:
        try:
            text = extract_text(record["file_id"], mime, record["name"], service)
            if len(text.strip()) < MIN_TEXT_CHARS:
                doc["status"] = EMPTY
            else:
                doc["status"], doc["text"] = INDEXED, text
        except Exception as e:
            logger.error(f"  → Extraction failed for {record['name']}: {e}")
            doc["status"], doc["error"] = ERROR, str(e)[:500]
    return doc


def _needs_processing(record: dict, stored: Optional[dict], full: bool) -> bool:
    if stored is None or full:
        return True
    if stored["modified_time"] != record["modified_time"]:
        return True
    if stored["status"] == ERROR:
        synced = stored.get("synced_at")
        if synced is not None and synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        return synced is None or datetime.now(timezone.utc) - synced > timedelta(hours=ERROR_RETRY_HOURS)
    return False


def run_sync(store: DocStore, service, list_files: Callable[[], list], source: str = "drive",
             full: bool = False, on_batch: Optional[Callable[[], None]] = None) -> SyncResult:
    result = SyncResult(source=source, started_at=datetime.now(timezone.utc).isoformat())
    t0 = time.time()

    remote = list_files()
    result.total_files = len(remote)
    stored = store.snapshot()

    # Safety: an empty listing almost always means the folder was unshared
    # or the ID is wrong — never wipe the library because of that.
    if not remote and stored:
        raise RuntimeError("The Drive listing came back empty, so nothing was changed. "
                           "Check DRIVE_FOLDER_ID and that the folder is shared with the service account.")

    remote_ids = {r["file_id"] for r in remote}
    gone = [fid for fid in stored if fid not in remote_ids]
    result.removed = store.delete(gone)
    if result.removed:
        logger.info(f"Removed {result.removed} files no longer in Drive")

    # New files first, so fresh uploads become searchable soonest.
    todo = [r for r in remote if _needs_processing(r, stored.get(r["file_id"]), full)]
    todo.sort(key=lambda r: r["file_id"] in stored)
    todo_ids = {r["file_id"] for r in todo}

    for r in remote:
        prev = stored.get(r["file_id"])
        if r["file_id"] in todo_ids or prev is None:
            continue
        if (prev["name"], prev["folder"], prev["drive_url"]) != (r["name"], r["folder"], r["drive_url"]):
            store.update_metadata(r["file_id"], r["name"], r["folder"], r["drive_url"])
            result.renamed += 1
        else:
            result.unchanged += 1

    if todo:
        logger.info(f"Sync: {len(todo)} files to read ({len(remote)} in Drive)")
    since_rebuild = 0
    for i, record in enumerate(todo, 1):
        is_new = record["file_id"] not in stored
        logger.info(f"[{i}/{len(todo)}] {record['name']}")
        doc = _process_file(record, service)
        store.upsert(doc)
        if doc["status"] == ERROR:
            result.failed += 1
        elif is_new:
            result.added += 1
            if len(result.added_names) < 20:
                result.added_names.append(record["name"])
        else:
            result.updated += 1

        since_rebuild += 1
        if on_batch and since_rebuild >= REBUILD_EVERY and i < len(todo):
            on_batch()
            since_rebuild = 0

    result.finished_at = datetime.now(timezone.utc).isoformat()
    result.seconds = round(time.time() - t0, 1)
    logger.info(f"Sync done in {result.seconds}s: +{result.added} new, {result.updated} updated, "
                f"{result.renamed} renamed, -{result.removed} removed, {result.failed} failed")
    return result


# ── CONFIGURED ENTRY POINT ────────────────────────────────────────────────────

def configured_source() -> Optional[str]:
    if os.environ.get("DRIVE_FOLDER_ID", "").strip():
        return "folder"
    if os.environ.get("SHEET_CSV_URL", "").strip():
        return "sheet"
    return None


def sync_from_env(store: DocStore, full: bool = False,
                  on_batch: Optional[Callable[[], None]] = None) -> SyncResult:
    """Runs one sync using the environment's settings. Raises on failure."""
    source = configured_source()
    if source is None:
        raise EnvironmentError("Set DRIVE_FOLDER_ID (recommended) or SHEET_CSV_URL to sync documents.")
    service = get_drive_service()

    if source == "folder":
        folder_id = os.environ["DRIVE_FOLDER_ID"].strip()
        lister = lambda: list_folder_files(service, folder_id)  # noqa: E731
    else:
        sheet_url = os.environ["SHEET_CSV_URL"].strip()
        lister = lambda: list_sheet_files(service, sheet_url)  # noqa: E731

    try:
        result = run_sync(store, service, lister, source=source, full=full, on_batch=on_batch)
    except Exception as e:
        result = SyncResult(source=source, started_at=datetime.now(timezone.utc).isoformat(),
                            finished_at=datetime.now(timezone.utc).isoformat(), error=str(e)[:500])
        store.set_state("last_sync", json.dumps(asdict(result)))
        raise
    # Only write when something happened, so quiet syncs don't wake the database.
    if result.changed or result.failed:
        store.set_state("last_sync", json.dumps(asdict(result)))
    return result
