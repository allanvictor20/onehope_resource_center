"""
Tests for the Drive → store sync, using an in-memory fake of the Drive API.
Set TEST_DATABASE_URL=postgresql://... to also run the store tests on Postgres.
"""

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import drive_sync
import main
from doc_store import DocStore, INDEXED, MEDIA, UNSUPPORTED, TOO_LARGE, EMPTY, ERROR

FOLDER = drive_sync.FOLDER_MIME
SHORTCUT = drive_sync.SHORTCUT_MIME
PDF = "application/pdf"


# ── FAKE DRIVE ────────────────────────────────────────────────────────────────

class _Call:
    def __init__(self, fn):
        self.execute = fn


class FakeDrive:
    """Just enough of service.files().list/get for the sync code."""

    PAGE_SIZE = 2   # small, so pagination is exercised

    def __init__(self):
        self.items = {}
        self.text = {}
        self.extract_calls = []

    def add(self, id, name, mime=PDF, parent="root", modified="2026-01-01T00:00:00Z",
            size=1000, text=None, target=None, target_mime=None):
        item = {"id": id, "name": name, "mimeType": mime, "parents": [parent],
                "modifiedTime": modified, "webViewLink": f"https://drive.google.com/file/d/{id}/view"}
        if size is not None and mime != FOLDER:
            item["size"] = str(size)
        if target:
            item["shortcutDetails"] = {"targetId": target, "targetMimeType": target_mime}
        self.items[id] = item
        if text is not None:
            self.text[id] = text
        return item

    def files(self):
        return self

    def list(self, q, pageToken=None, **kwargs):
        parent = re.match(r"'([^']+)' in parents", q).group(1)
        children = sorted((i for i in self.items.values() if parent in i["parents"]), key=lambda i: i["id"])
        start = int(pageToken or 0)
        page = children[start:start + self.PAGE_SIZE]
        resp = {"files": [dict(i) for i in page]}
        if start + self.PAGE_SIZE < len(children):
            resp["nextPageToken"] = str(start + self.PAGE_SIZE)
        return _Call(lambda: resp)

    def get(self, fileId, **kwargs):
        def run():
            if fileId not in self.items:
                raise RuntimeError("not found")
            return dict(self.items[fileId])
        return _Call(run)

    def extract(self, file_id, mime_type, name, service):
        self.extract_calls.append(file_id)
        text = self.text.get(file_id)
        if isinstance(text, Exception):
            raise text
        return text or ""


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(drive_sync, "extract_text", fake.extract)
    return fake


@pytest.fixture
def store(tmp_path):
    return DocStore(f"sqlite:///{tmp_path / 'sync.db'}")


def sync(store, drive, **kwargs):
    return drive_sync.run_sync(store, drive, lambda: drive_sync.list_folder_files(drive, "root"), **kwargs)


LONG = "This is a sample OneHope training document with plenty of readable text in it. " * 3


# ── LISTING ───────────────────────────────────────────────────────────────────

def test_lists_nested_folders_with_paths_pagination_and_shortcuts(drive):
    drive.add("fold-a", "Training", FOLDER, size=None)
    drive.add("fold-b", "Spark", FOLDER, parent="fold-a", size=None)
    drive.add("d1", "Root doc.pdf")
    drive.add("d2", "Training doc.pdf", parent="fold-a")
    drive.add("d3", "Spark doc.pdf", parent="fold-b")
    drive.add("d4", "Another root.pdf")
    drive.add("d5", "Third root.pdf")
    # A shortcut in the root pointing at a file kept elsewhere.
    drive.add("elsewhere", "Shared manual.pdf", parent="other-place")
    drive.add("sc1", "Shared manual (shortcut)", SHORTCUT, size=None, target="elsewhere", target_mime=PDF)
    # A shortcut loop back to the root must not recurse forever.
    drive.add("sc2", "Loop", SHORTCUT, parent="fold-b", size=None, target="root", target_mime=FOLDER)

    files = {f["file_id"]: f for f in drive_sync.list_folder_files(drive, "root")}
    assert set(files) == {"d1", "d2", "d3", "d4", "d5", "elsewhere"}
    assert files["d1"]["folder"] == "General"
    assert files["d2"]["folder"] == "Training"
    assert files["d3"]["folder"] == "Training / Spark"
    assert files["elsewhere"]["name"] == "Shared manual.pdf"
    assert files["d1"]["drive_url"].endswith("/d1/view")


# ── SYNC BEHAVIOUR ────────────────────────────────────────────────────────────

def test_first_sync_reads_everything_and_classifies(store, drive, monkeypatch):
    monkeypatch.setattr(drive_sync, "MAX_FILE_MB", 1)
    drive.add("doc", "Manual.pdf", text=LONG)
    drive.add("vid", "Film.mp4", "video/mp4")
    drive.add("img", "Poster.png", "image/png")
    drive.add("zip", "Archive.zip", "application/zip")
    drive.add("big", "Huge.pdf", size=5 * 1024 * 1024, text=LONG)
    drive.add("scan", "Scanned.pdf", text="   ")
    drive.add("bad", "Broken.pdf", text=ValueError("corrupt PDF"))

    result = sync(store, drive)
    statuses = {fid: d["status"] for fid, d in store.snapshot().items()}
    assert statuses == {"doc": INDEXED, "vid": MEDIA, "img": MEDIA, "zip": UNSUPPORTED,
                        "big": TOO_LARGE, "scan": EMPTY, "bad": ERROR}
    assert (result.added, result.failed, result.total_files) == (6, 1, 7)
    # Media, unsupported and oversized files are never downloaded.
    assert set(drive.extract_calls) == {"doc", "scan", "bad"}


def test_second_sync_only_touches_changes(store, drive):
    drive.add("a", "A.pdf", text=LONG)
    drive.add("b", "B.pdf", text=LONG)
    drive.add("c", "C.pdf", text=LONG)
    sync(store, drive)
    drive.extract_calls.clear()

    drive.add("new", "New upload.pdf", text=LONG)                                 # added
    drive.add("a", "A.pdf", modified="2026-02-01T00:00:00Z", text=LONG + " v2")  # edited
    drive.items["b"]["name"] = "B renamed.pdf"                                    # renamed only
    del drive.items["c"]                                                         # deleted

    result = sync(store, drive)
    assert drive.extract_calls == ["new", "a"]          # new files first, rename not re-read
    assert (result.added, result.updated, result.renamed, result.removed, result.unchanged) == (1, 1, 1, 1, 0)
    assert result.added_names == ["New upload.pdf"]
    snap = store.snapshot()
    assert set(snap) == {"a", "b", "new"}
    assert snap["b"]["name"] == "B renamed.pdf"
    assert "v2" in next(d["text"] for d in store.all_documents() if d["file_id"] == "a")

    drive.extract_calls.clear()
    result = sync(store, drive)
    assert drive.extract_calls == [] and result.unchanged == 3 and not result.changed


def test_quiet_sync_never_touches_the_database(store, drive, monkeypatch):
    """Lets a free Neon/Supabase database sleep between real changes."""
    from sqlalchemy import event
    monkeypatch.setenv("DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(drive_sync, "get_drive_service", lambda: drive)
    drive.add("a", "A.pdf", text=LONG)
    drive_sync.sync_from_env(store)

    queries = []
    event.listen(store.engine, "before_cursor_execute", lambda *a, **k: queries.append(a[2]))
    for _ in range(3):
        drive_sync.sync_from_env(store)
    assert queries == []

    drive.add("b", "B.pdf", text=LONG)                   # a real change does write
    drive_sync.sync_from_env(store)
    assert queries


def test_full_sync_rereads_everything(store, drive):
    drive.add("a", "A.pdf", text=LONG)
    sync(store, drive)
    drive.extract_calls.clear()
    sync(store, drive, full=True)
    assert drive.extract_calls == ["a"]


def test_failed_files_are_retried_later_not_every_run(store, drive):
    drive.add("bad", "Broken.pdf", text=ValueError("timeout"))
    sync(store, drive)
    drive.extract_calls.clear()

    sync(store, drive)
    assert drive.extract_calls == []                     # too soon to retry

    with store.engine.begin() as conn:                   # pretend it failed 7 hours ago
        from doc_store import documents
        conn.execute(documents.update().values(
            synced_at=datetime.now(timezone.utc) - timedelta(hours=drive_sync.ERROR_RETRY_HOURS + 1)))
    store._snapshot = None                               # edited behind the store's back
    drive.text["bad"] = LONG
    sync(store, drive)
    assert drive.extract_calls == ["bad"]
    assert store.snapshot()["bad"]["status"] == INDEXED


def test_empty_listing_never_wipes_the_library(store, drive):
    drive.add("a", "A.pdf", text=LONG)
    sync(store, drive)
    drive.items.clear()                                  # e.g. folder unshared
    with pytest.raises(RuntimeError, match="empty"):
        sync(store, drive)
    assert set(store.snapshot()) == {"a"}


def test_search_refreshes_in_batches_during_big_syncs(store, drive, monkeypatch):
    monkeypatch.setattr(drive_sync, "REBUILD_EVERY", 3)
    for i in range(7):
        drive.add(f"d{i}", f"Doc {i}.pdf", text=LONG)
    batches = []
    sync(store, drive, on_batch=lambda: batches.append(len(store.snapshot())))
    assert batches == [3, 6]


def test_sync_from_env_records_result_and_errors(store, drive, monkeypatch):
    monkeypatch.setenv("DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(drive_sync, "get_drive_service", lambda: drive)
    drive.add("a", "A.pdf", text=LONG)
    drive_sync.sync_from_env(store)
    assert '"added": 1' in store.get_state("last_sync")

    drive.items.clear()
    with pytest.raises(RuntimeError):
        drive_sync.sync_from_env(store)
    assert "empty" in store.get_state("last_sync")


def test_configured_source_prefers_folder(monkeypatch):
    monkeypatch.delenv("DRIVE_FOLDER_ID", raising=False)
    monkeypatch.delenv("SHEET_CSV_URL", raising=False)
    assert drive_sync.configured_source() is None
    monkeypatch.setenv("SHEET_CSV_URL", "https://example.com/sheet.csv")
    assert drive_sync.configured_source() == "sheet"
    monkeypatch.setenv("DRIVE_FOLDER_ID", "abc")
    assert drive_sync.configured_source() == "folder"


# ── END TO END: new upload becomes answerable ────────────────────────────────

def test_new_drive_upload_becomes_searchable(store, drive, monkeypatch):
    monkeypatch.setenv("DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(drive_sync, "get_drive_service", lambda: drive)
    monkeypatch.setattr(main, "_store", store)
    drive.add("a", "Existing.pdf", text=LONG)
    main.run_sync()
    assert main._index.search("zanzibar outreach") == []

    drive.add("z", "Zanzibar Outreach Report.pdf",
              text="The Zanzibar outreach report describes the island programme in detail. " * 3)
    main.run_sync()
    hits = main._index.search("zanzibar outreach")
    assert hits and hits[0]["file_name"] == "Zanzibar Outreach Report.pdf"
    assert not main._sync_progress["running"]


def test_overlapping_syncs_are_skipped(store, drive, monkeypatch):
    monkeypatch.setattr(main, "_store", store)
    called = []
    monkeypatch.setattr(main, "sync_from_env", lambda *a, **k: called.append(1))
    main._sync_lock.acquire()
    try:
        main.run_sync()
    finally:
        main._sync_lock.release()
    assert called == []


# ── STORE (SQLite always; Postgres when TEST_DATABASE_URL is set) ────────────

STORE_URLS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(params=STORE_URLS)
def any_store(request, tmp_path):
    if request.param == "sqlite":
        yield DocStore(f"sqlite:///{tmp_path / 's.db'}")
        return
    s = DocStore(os.environ["TEST_DATABASE_URL"])
    from doc_store import metadata
    yield s
    metadata.drop_all(s.engine)


def test_store_roundtrip(any_store):
    doc = {"file_id": "x", "name": "Doc.pdf", "folder": "F", "mime_type": PDF,
           "drive_url": "https://drive.google.com/file/d/x/view", "modified_time": "t1",
           "status": INDEXED, "text": "hello\x00world"}         # NUL from PDF extraction
    any_store.upsert(doc)
    any_store.upsert({**doc, "modified_time": "t2", "text": "updated"})
    docs = any_store.all_documents()
    assert len(docs) == 1 and docs[0]["text"] == "updated" and docs[0]["modified_time"] == "t2"

    any_store.upsert({**doc, "file_id": "y", "text": "a\x00b"})
    assert next(d["text"] for d in any_store.all_documents() if d["file_id"] == "y") == "ab"

    any_store.update_metadata("x", "Renamed.pdf", "G", "https://drive.google.com/file/d/x/view")
    assert any_store.snapshot()["x"]["name"] == "Renamed.pdf"
    assert any_store.counts() == {INDEXED: 2}
    assert any_store.delete(["x", "missing"]) == 1

    any_store.set_state("k", "v1")
    any_store.set_state("k", "v2")
    assert any_store.get_state("k") == "v2" and any_store.get_state("nope") is None


def test_database_url_normalisation():
    from doc_store import normalise_database_url as n
    assert n("postgres://u:p@h/db").startswith("postgresql+psycopg://")
    assert n("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert n("").startswith("sqlite:///")
