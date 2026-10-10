"""
doc_store.py
────────────
Durable storage for the documents synced from Google Drive.

One table holds every file found in the Drive folder — its metadata, and the
extracted text for files we can read. The search index is rebuilt in memory
from this table, so nothing has to be committed to the repository and a
server restart only takes a few seconds to be ready again.

DATABASE_URL picks the database:
  • unset             → a local SQLite file (fine for development; on Render's
                         free tier it is wiped whenever the server restarts)
  • postgresql://...  → a hosted Postgres such as Neon or Supabase (production)
"""

import os
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import (
    Column, DateTime, Integer, MetaData, String, Table, Text,
    create_engine, delete, func, insert, select, update,
)

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource_store.db")

# status values
INDEXED = "indexed"          # text extracted and searchable
MEDIA = "media"              # video/audio/image: findable by title only
UNSUPPORTED = "unsupported"  # file type we can't read
TOO_LARGE = "too_large"      # over MAX_FILE_MB
EMPTY = "empty"              # readable, but no text (e.g. scanned PDF)
ERROR = "error"              # extraction failed

metadata = MetaData()

documents = Table(
    "documents", metadata,
    Column("file_id", String(128), primary_key=True),
    Column("name", Text, nullable=False),
    Column("folder", Text, nullable=False, default="General"),
    Column("drive_url", Text, nullable=False),
    Column("mime_type", String(255), nullable=False, default=""),
    Column("modified_time", String(64), nullable=False, default=""),
    Column("size_bytes", Integer, nullable=True),
    Column("status", String(32), nullable=False),
    Column("error", Text, nullable=True),
    Column("text", Text, nullable=True),
    Column("synced_at", DateTime(timezone=True), nullable=False),
)

sync_state = Table(
    "sync_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=True),
)


def normalise_database_url(url: str) -> str:
    """Accepts the connection strings Neon/Supabase hand out and points
    SQLAlchemy at the psycopg (v3) driver."""
    url = (url or "").strip()
    if not url:
        return f"sqlite:///{DEFAULT_SQLITE_PATH}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _clean_text(value: Optional[str]) -> Optional[str]:
    # Postgres rejects NUL characters, which PDF extraction sometimes produces.
    return value.replace("\x00", "") if isinstance(value, str) else value


class DocStore:
    def __init__(self, url: Optional[str] = None):
        self.url = normalise_database_url(url if url is not None else os.environ.get("DATABASE_URL", ""))
        self.is_sqlite = self.url.startswith("sqlite")
        kwargs = {"future": True}
        if self.is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # Neon/Supabase close idle connections; check before use.
            kwargs.update(pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=2)
        self.engine = create_engine(self.url, **kwargs)
        metadata.create_all(self.engine)
        # This process is the only writer, so the file list can be cached in
        # memory. A sync where nothing changed then never touches the database,
        # which lets a free Neon/Supabase database sleep between changes.
        self._snapshot: Optional[dict] = None
        logger.info(f"Document store: {'SQLite (local file)' if self.is_sqlite else 'Postgres'}")

    # ── documents ─────────────────────────────────────────────────────────────

    _SNAPSHOT_KEYS = ("file_id", "name", "folder", "drive_url", "modified_time", "status", "synced_at")

    def snapshot(self) -> dict:
        """file_id → metadata (no text) for every stored file; used to diff against Drive."""
        if self._snapshot is None:
            cols = [documents.c[k] for k in self._SNAPSHOT_KEYS]
            with self.engine.connect() as conn:
                self._snapshot = {r.file_id: dict(r._mapping) for r in conn.execute(select(*cols))}
        return {fid: dict(meta) for fid, meta in self._snapshot.items()}

    def upsert(self, doc: dict) -> None:
        row = {
            "file_id": doc["file_id"],
            "name": _clean_text(doc["name"]),
            "folder": _clean_text(doc.get("folder") or "General"),
            "drive_url": doc["drive_url"],
            "mime_type": doc.get("mime_type", ""),
            "modified_time": doc.get("modified_time", ""),
            "size_bytes": doc.get("size_bytes"),
            "status": doc["status"],
            "error": _clean_text(doc.get("error")),
            "text": _clean_text(doc.get("text")),
            "synced_at": datetime.now(timezone.utc),
        }
        # Portable upsert (works the same on SQLite and Postgres).
        with self.engine.begin() as conn:
            exists = conn.execute(
                select(documents.c.file_id).where(documents.c.file_id == row["file_id"])
            ).first()
            if exists:
                conn.execute(update(documents).where(documents.c.file_id == row["file_id"]).values(**row))
            else:
                conn.execute(insert(documents).values(**row))
        if self._snapshot is not None:
            self._snapshot[row["file_id"]] = {k: row[k] for k in self._SNAPSHOT_KEYS}

    def update_metadata(self, file_id: str, name: str, folder: str, drive_url: str) -> None:
        """Rename/move without re-extracting the text."""
        with self.engine.begin() as conn:
            conn.execute(update(documents).where(documents.c.file_id == file_id)
                         .values(name=_clean_text(name), folder=_clean_text(folder), drive_url=drive_url))
        if self._snapshot is not None and file_id in self._snapshot:
            self._snapshot[file_id].update(name=_clean_text(name), folder=_clean_text(folder), drive_url=drive_url)

    def delete(self, file_ids: Iterable[str]) -> int:
        ids = list(file_ids)
        if not ids:
            return 0
        with self.engine.begin() as conn:
            removed = conn.execute(delete(documents).where(documents.c.file_id.in_(ids))).rowcount
        if self._snapshot is not None:
            for fid in ids:
                self._snapshot.pop(fid, None)
        return removed

    def all_documents(self) -> list:
        """Every stored file, with text, as plain dicts."""
        cols = [documents.c.file_id, documents.c.name, documents.c.folder, documents.c.drive_url,
                documents.c.mime_type, documents.c.modified_time, documents.c.status, documents.c.text]
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(*cols).order_by(documents.c.name))]

    def iter_documents(self, batch: int = 20):
        """Yields stored files one at a time (with text), so the whole library
        never has to be in memory at once."""
        cols = [documents.c.file_id, documents.c.name, documents.c.folder, documents.c.drive_url,
                documents.c.mime_type, documents.c.status]
        with self.engine.connect() as conn:
            ids = [r.file_id for r in conn.execute(select(documents.c.file_id).order_by(documents.c.name))]
        for i in range(0, len(ids), batch):
            part = ids[i:i + batch]
            with self.engine.connect() as conn:
                rows = conn.execute(select(*cols, documents.c.text)
                                    .where(documents.c.file_id.in_(part)).order_by(documents.c.name))
                found = [dict(r._mapping) for r in rows]
            yield from found
            del found

    def counts(self) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(documents.c.status, func.count()).group_by(documents.c.status))
            return {status: n for status, n in rows}

    def recent(self, limit: int = 10) -> list:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(documents.c.name, documents.c.folder, documents.c.status, documents.c.synced_at)
                .order_by(documents.c.synced_at.desc()).limit(limit))
            return [{**dict(r._mapping), "synced_at": r.synced_at.isoformat() if r.synced_at else None}
                    for r in rows]

    # ── sync state ────────────────────────────────────────────────────────────

    def get_state(self, key: str) -> Optional[str]:
        with self.engine.connect() as conn:
            row = conn.execute(select(sync_state.c.value).where(sync_state.c.key == key)).first()
            return row.value if row else None

    def set_state(self, key: str, value: Optional[str]) -> None:
        with self.engine.begin() as conn:
            if conn.execute(select(sync_state.c.key).where(sync_state.c.key == key)).first():
                conn.execute(update(sync_state).where(sync_state.c.key == key).values(value=value))
            else:
                conn.execute(insert(sync_state).values(key=key, value=value))
