"""
Shared test fixtures. Uses a throw-away SQLite store filled with made-up
sample documents, so tests never need Google, Groq or real OneHope files.
"""

import pytest

import main
from doc_store import DocStore, INDEXED, MEDIA

PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MP4 = "video/mp4"


def _filler(n: int, tag: str) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def _doc(file_id, name, text, mime=PDF, folder="Training", status=INDEXED):
    return {
        "file_id": file_id, "name": name, "folder": folder, "mime_type": mime,
        "drive_url": f"https://drive.google.com/file/d/{file_id}/view",
        "modified_time": "2026-01-01T00:00:00.000Z", "status": status, "text": text,
    }


MENTORSHIP_TEXT = " ".join(
    f"Section {i}. This sample mentorship manual explains how a mentor meets with young people "
    f"each week, listens well, and helps them grow. {_filler(120, f'm{i}x')}"
    for i in range(6)
)

SAMPLE_DOCS = [
    _doc("f-1for50-pdf", "📄 02_What is 1for50.pdf",
         "Sample text about the 1for50 approach used for testing search. "
         "The 1for50 model is described in this placeholder document. " + _filler(50, "a")),
    _doc("f-1for50-pptx", "📄 02_What is 1for50.pptx",
         "Slides version: 1for50 approach overview placeholder for tests. " + _filler(50, "b"), mime=PPTX),
    _doc("f-mentor", "📄 Mentorship Manual.pdf", MENTORSHIP_TEXT, folder="Mentorship"),
    _doc("f-mentor-copy", "📄 Mentorship Manual (1).pdf", MENTORSHIP_TEXT, folder="Mentorship"),
    _doc("f-spark", "📄 Spark Empowered Teacher.pptx",
         "Spark teacher training sample. A spark session prepares teachers to lead lessons. "
         + _filler(40, "c"), mime=PPTX),
    _doc("f-families", "📄 11_Engaging Families.pptx",
         "Engaging families sample text: parents and families are invited to take part. "
         + _filler(40, "d"), mime=PPTX),
    _doc("f-evangelism", "📄 Evangelism Guide.docx",
         "Evangelism guide sample: sharing the gospel with children clearly and kindly. "
         + _filler(40, "e"), mime=DOCX),
    _doc("f-film", "Jesus Film (Luganda).mp4", None, mime=MP4, folder="Movies", status=MEDIA),
]


@pytest.fixture
def sample_store(tmp_path):
    store = DocStore(f"sqlite:///{tmp_path / 'test.db'}")
    for doc in SAMPLE_DOCS:
        store.upsert(dict(doc))
    return store


@pytest.fixture(autouse=True)
def library(sample_store, monkeypatch):
    """Points the app at the sample store and builds search from it."""
    monkeypatch.setattr(main, "_store", sample_store)
    monkeypatch.setattr(main, "SHEET_CSV_URL", "")
    monkeypatch.setattr(main, "_last_sync", None)
    main._sheet_cache.update(entries=[], last_fetched=0.0, loaded=False)
    main.rebuild_index()
    assert main._index.ready
    return sample_store
