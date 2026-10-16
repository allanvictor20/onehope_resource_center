"""
Questions about a type of resource — "What movies does OneHope have available?" —
must list the films with their links, even though no title contains the word
"movie". Uses real titles and folder paths from the OneHope Drive.
"""

import pytest
from fastapi.testclient import TestClient

import drive_sync
import main
import search
from doc_store import DocStore, INDEXED, MEDIA
from sheet_utils import Sheet, is_junk_file, sheet_to_catalog
from tests.test_sync import FakeDrive, FOLDER

PDF = "application/pdf"
MOVIE = "Partner Files / Print and Movie / Movie"
GODMAN = f"{MOVIE} / The God Man Movie / The God Man"
FILLER = " ".join(f"word{i}" for i in range(80))


def _doc(file_id, name, mime=PDF, folder="Internal Files", text=None):
    is_media = mime.startswith("video/") or mime == "application/octet-stream"
    return {
        "file_id": file_id, "name": name, "folder": folder, "mime_type": mime,
        "drive_url": f"https://drive.google.com/file/d/{file_id}/view",
        "modified_time": "2026-01-01T00:00:00.000Z",
        "status": MEDIA if is_media else INDEXED,
        "text": None if is_media else (text or f"OneHope Uganda sample text about {name}. {FILLER}"),
    }


LIBRARY = [
    _doc("cm101", "Children's Ministry 101 (CM 101).pdf", folder="Internal Files / Manuals & Guides"),
    _doc("years", "OVER THE YEARS AT ONEHOPE.pdf"),
    _doc("manual", "OneHope Africa Manual v 2.1 (1)-2.pdf"),
    _doc("distro", "OneHope Distribution Form.pdf"),
    _doc("godman-pdf", "English The GodMan Curriculum.pdf", folder=GODMAN,
         text=f"The God Man curriculum guides leaders through the film with children. {FILLER}"),
    _doc("bafk", "BAFK - promo.mp4", "video/mp4", f"{MOVIE} / Lumo / Trailers"),
    _doc("lumo", "Lumo Trailer.mp4", "video/mp4", f"{MOVIE} / Lumo / Trailers"),
    # Some uploads of .ts files come back from Drive without a video MIME type.
    _doc("rose", "American English 'Rose' (1).ts", "application/octet-stream", f"{MOVIE} / Rose"),
    _doc("gm-en", "English The GodMan African Version.mp4", "video/mp4", GODMAN),
    _doc("gm-luo", "Luo The GodMan African Version.mp4", "video/mp4", GODMAN),
    _doc("gm-story", "The GodMan African Version Live Storyteller's Release.mp4", "video/mp4", GODMAN),
    _doc("gm-lg", "Luganda The GodMan African Version.mp4", "video/mp4", GODMAN),
]
VIDEO_IDS = {"bafk", "lumo", "rose", "gm-en", "gm-luo", "gm-story", "gm-lg"}


@pytest.fixture(autouse=True)
def onehope_library(tmp_path, monkeypatch):
    store = DocStore(f"sqlite:///{tmp_path / 'media.db'}")
    for doc in LIBRARY:
        store.upsert(dict(doc))
    monkeypatch.setattr(main, "_store", store)
    monkeypatch.setattr(main, "SHEET_CSV_URL", "")
    main._sheet_cache.update(entries=[], last_fetched=0.0, loaded=False)
    main.rebuild_index()
    return store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 0)
    return TestClient(main.app)


@pytest.fixture
def fake_groq(monkeypatch):
    calls = []

    def fake(client, messages, **kwargs):
        calls.append(messages)
        return '{"needs_clarification": false, "answer": "Here are the films.", "suggestions": []}'

    fake.calls = calls
    monkeypatch.setattr(main, "call_groq_with_fallback", fake)
    monkeypatch.setattr(main, "get_groq_client", lambda: object())
    return fake


def _ids(sources):
    return {s["drive_url"].split("/d/")[1].split("/")[0] for s in sources}


# ── THE QUESTION FROM THE SCREENSHOT ──────────────────────────────────────────

def test_movies_question_returns_every_film_with_its_link(client, fake_groq):
    body = client.post("/chat", json={"message": "What movies does OneHope have available?"}).json()
    sources = body["sources"]
    # The seven films are the answer — every one, as a link, and nothing else.
    assert _ids(sources) == VIDEO_IDS and len(sources) == len(VIDEO_IDS)
    assert all(s["drive_url"].startswith("https://drive.google.com/") for s in sources)
    # Titles that merely contain "OneHope" are no longer offered as the answer.
    assert not {"years", "manual", "distro"} & _ids(sources)

    prompt = fake_groq.calls[0][0]["content"]
    assert "COMPLETE LIST — FILMS AND VIDEOS" in prompt
    for title in ("Lumo Trailer.mp4", "American English 'Rose' (1).ts", "Luganda The GodMan African Version.mp4"):
        assert title in prompt


def test_film_question_narrows_by_language(client, fake_groq):
    body = client.post("/chat", json={"message": "Do you have the God Man film in Luganda?"}).json()
    assert body["sources"][0]["file_name"] == "Luganda The GodMan African Version.mp4"


def test_follow_up_keeps_the_media_type(client, fake_groq):
    history = [{"role": "user", "content": "What movies does OneHope have available?"},
               {"role": "model", "content": "Here are the films."}]
    body = client.post("/chat", json={"message": "any in Luo?", "conversation_history": history}).json()
    assert body["sources"][0]["file_name"] == "Luo The GodMan African Version.mp4"


def test_trailers_narrow_to_the_trailers_folder():
    hits = main.get_catalog().list_kind("video", "do you have any trailers")
    assert {h["file_name"] for h in hits} == {"BAFK - promo.mp4", "Lumo Trailer.mp4"}


def test_document_questions_are_not_treated_as_media(client, fake_groq):
    client.post("/chat", json={"message": "What is in the OneHope Africa Manual?"})
    assert "=== COMPLETE LIST" not in fake_groq.calls[0][0]["content"]


def test_unmatched_narrowing_falls_back_to_normal_search():
    assert main.get_catalog().list_kind("video", "films about zzqqxx") == []


# ── BUILDING BLOCKS ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("query, kind", [
    ("What movies does OneHope have available?", "video"),
    ("any films in Luganda", "video"),
    ("show me the trailers", "video"),
    ("do we have audio recordings", "audio"),
    ("photos from the outreach", "image"),
    ("slides for Spark training", "slides"),
    ("What is 1for50?", None),
    ("the Distribution Training Manual", None),
])
def test_requested_kind(query, kind):
    assert search.requested_kind(query) == kind


def test_kind_falls_back_to_the_file_extension():
    assert search.kind_of("application/octet-stream", "Rose.ts") == "video"
    assert search.kind_of("", "Lumo Trailer.MP4") == "video"
    assert search.kind_of("video/mp4", "anything") == "video"
    assert search.kind_of("", "Manual.pdf") == "PDF document"
    assert search.kind_of("", "notes") == "file"


@pytest.mark.parametrize("name, junk", [
    (".DS_Store", True), ("📄 .DS_Store", True), ("Thumbs.db", True), ("._Lumo Trailer.mp4", True),
    ("~$Report.docx", True), ("Lumo Trailer.mp4", False), ("_CRM Workbooks Introduction.pptx", False),
])
def test_is_junk_file(name, junk):
    assert is_junk_file(name) is junk


def test_catalog_never_lists_junk_files():
    catalog = search.Catalog([
        {"file_name": ".DS_Store", "drive_url": "u1", "folder": "Print / 17 Stories", "details": ""},
        {"file_name": "17 Stories Cards – Africa (1).pdf", "drive_url": "u2",
         "folder": "Print / 17 Stories", "details": ""},
    ])
    assert [e["file_name"] for e in catalog.entries] == ["17 Stories Cards – Africa (1).pdf"]


def test_drive_listing_skips_junk_files():
    drive = FakeDrive()
    drive.add("stories", "17 Stories", FOLDER, size=None)
    drive.add("cards", "17 Stories Cards – Africa (1).pdf", parent="stories")
    drive.add("ds", ".DS_Store", "application/octet-stream", parent="stories")
    files = drive_sync.list_folder_files(drive, "root")
    assert [f["file_id"] for f in files] == ["cards"]


# ── THE FOLDER-STRUCTURE SHEET ────────────────────────────────────────────────

I = "    "
TREE_SHEET = Sheet.from_columns({
    "Hierarchy": ["1", "", "2", "2.1.", "2.1.1.", "2.1.1.1.", "", "2.1.1.1.1.", "", "2.1.1.3.", "2.1.1.3.1.", "",
                  "2.1.2.", "2.1.2.1.", "", ""],
    "Name": ["📁 Internal Files", I + "📄 OneHope Africa Manual v 2.1 (1)-2.pdf", "📁 Partner Files",
             I + "📁 Print and Movie", I * 2 + "📁 Movie", I * 3 + "📁 Lumo ",
             I * 4 + "📄 LUMO Training Manual_Jan 2019.pdf", I * 4 + "📁 Trailers", I * 5 + "📄 Lumo Trailer.mp4",
             I * 3 + "📁 The God Man Movie", I * 4 + "📁 The God Man",
             I * 5 + "📄 Luganda The GodMan African Version.mp4",
             I * 2 + "📁 Print", I * 3 + "📁 17 Stories", I * 4 + "📄 17 Stories Cards – Africa (1).pdf",
             I * 4 + "📄 .DS_Store"],
    "Type": ["Folder", "File", "Folder", "Folder", "Folder", "Folder", "File", "Folder", "File", "Folder",
             "Folder", "File", "Folder", "Folder", "File", "File"],
    "Size": ["-", "24 MB", "-", "-", "-", "-", "278 KB", "-", "29 MB", "-", "-", "246 MB", "-", "-", "1 MB", "6 KB"],
    "Link": [f"https://drive.google.com/file/d/id{i}/view" for i in range(16)],
})


def test_folder_tree_sheet_gets_full_paths_and_no_folders_or_junk():
    by_name = {e["file_name"]: e for e in sheet_to_catalog(TREE_SHEET)}
    assert set(by_name) == {
        "OneHope Africa Manual v 2.1 (1)-2.pdf", "LUMO Training Manual_Jan 2019.pdf", "Lumo Trailer.mp4",
        "Luganda The GodMan African Version.mp4", "17 Stories Cards – Africa (1).pdf",
    }
    assert by_name["OneHope Africa Manual v 2.1 (1)-2.pdf"]["folder"] == "Internal Files"
    assert by_name["LUMO Training Manual_Jan 2019.pdf"]["folder"] == "Partner Files / Print and Movie / Movie / Lumo"
    assert by_name["Lumo Trailer.mp4"]["folder"] == "Partner Files / Print and Movie / Movie / Lumo / Trailers"
    assert by_name["Luganda The GodMan African Version.mp4"]["folder"] == GODMAN
    assert by_name["17 Stories Cards – Africa (1).pdf"]["folder"] == "Partner Files / Print and Movie / Print / 17 Stories"


def test_folder_tree_sheet_answers_film_questions_on_its_own():
    catalog = search.Catalog(sheet_to_catalog(TREE_SHEET))
    names = [e["file_name"] for e in catalog.list_kind("video", "What movies does OneHope have available?")]
    assert names == ["Lumo Trailer.mp4", "Luganda The GodMan African Version.mp4"]
