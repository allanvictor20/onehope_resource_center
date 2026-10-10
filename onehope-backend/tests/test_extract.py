"""Text extraction from real (small) sample files, and the memory-safe index."""

import os

import pytest

import drive_reader
from search import SearchIndex

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.parametrize("kind, expected", [
    ("pdf", ["Engaging families: parents are invited", "Page two about outreach"]),
    ("pptx", ["Spark Teacher Training", "Teachers prepare lessons & invite children", "Café “quotes” stay intact"]),
    ("docx", ["Mentorship Manual", "Mentors meet weekly with young people.", "Naïve café test — done."]),
    ("xlsx", ["[Sheet: Budget]", "Bibles\t1200"]),
])
def test_extracts_text_from_each_format(kind, expected):
    text = drive_reader.extract_text_from_path(os.path.join(FIX, f"sample.{kind}"), kind)
    for snippet in expected:
        assert snippet in text, (kind, snippet, text)


def test_pptx_keeps_slide_order_and_paragraphs():
    text = drive_reader.extract_text_from_path(os.path.join(FIX, "sample.pptx"), "pptx")
    assert text.index("Spark Teacher Training") < text.index("Second slide")
    assert "Spark Teacher Training\nTeachers prepare" in text


def test_text_is_capped(monkeypatch):
    monkeypatch.setattr(drive_reader, "MAX_CHARS_PER_SHEET", 20)
    text = drive_reader.extract_text_from_path(os.path.join(FIX, "sample.xlsx"), "xlsx")
    assert len(text) == 20


def test_drive_download_goes_to_disk_and_is_cleaned_up(tmp_path, monkeypatch):
    seen = {}

    def fake_download(file_id, service, path):
        seen["path"] = path
        with open(os.path.join(FIX, "sample.docx"), "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())

    monkeypatch.setattr(drive_reader, "_download_to_file", fake_download)
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    text = drive_reader.extract_text("id1", mime, "Manual.docx", service=None)
    assert "Mentors meet weekly" in text
    assert not os.path.exists(seen["path"])               # temp file removed


def test_google_docs_are_exported_as_text():
    class Export:
        def files(self):
            return self

        def export(self, fileId, mimeType):
            assert mimeType == "text/plain"
            return type("C", (), {"execute": lambda self: "Hello\x00 world".encode()})()

    text = drive_reader.extract_text("id", "application/vnd.google-apps.document", "Doc", Export())
    assert text == "Hello world"


# ── index ─────────────────────────────────────────────────────────────────────

def test_index_slices_non_ascii_passages_exactly():
    text = "Café ’quotes’ naïve 📄 emoji " * 400 + " the mentorship programme"
    idx = SearchIndex([{"name": "Doc.pdf", "drive_url": "u", "text": text},
                       {"name": "Plain.pdf", "drive_url": "u2", "text": "plain ascii words here " * 300}])
    assert idx.num_docs == 2 and idx.num_chunks > 5
    for i in range(idx.num_chunks):
        passage = idx.chunk(i)["text"]
        assert passage == passage.strip() and "�" not in passage
    assert idx.search("mentorship programme")[0]["file_name"] == "Doc.pdf"
    assert idx.search("naive cafe")[0]["file_name"] == "Doc.pdf"          # accents ignored


def test_index_consumes_document_text():
    """The index keeps its own compact copy, so callers' copies can be freed."""
    docs = [{"name": "A.pdf", "drive_url": "u", "text": "alpha beta gamma " * 50}]
    SearchIndex(iter(docs))
    assert "text" not in docs[0]


def test_empty_index():
    idx = SearchIndex([])
    assert not idx.ready and idx.search("anything") == [] and idx.num_chunks == 0
