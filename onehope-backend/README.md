# OneHope Resource Centre

AI-powered resource assistant for OneHope Uganda.
Built with FastAPI (Python), Google Drive, a Postgres document store, TF-IDF search and Groq-hosted LLMs.

**Adding a resource = putting the file in the OneHope Drive folder.** Within about ten
minutes the assistant can answer questions from it. Edits and deletions are picked up
the same way. Nobody runs an indexer or commits anything.

---

## How it works

```
                 every 10 min (and on startup)
Google Drive folder ──────────────────────────────► Drive sync
  (+ subfolders)       list files (cheap, no downloads)   │
                       read only new / changed files       │ store text + metadata
                       drop deleted files                  ▼
                                                     Postgres (Neon / Supabase)
                                                           │ rebuilt in memory
                                                           ▼ when something changes
User question ──► POST /chat ──► search passages + catalogue ──► Groq LLM ──► answer + source links
```

- **Sync** (`drive_sync.py`): lists the folder tree; compares it with what's stored;
  downloads only what changed. When nothing changed it doesn't even touch the database,
  so a free database can sleep. New uploads are processed first.
- **Store** (`doc_store.py`): one row per Drive file with its extracted text. Survives
  restarts and redeploys, so the server is ready again in seconds.
- **Search** (`search.py`): passages of every document (typo-tolerant), plus a title
  catalogue of every file — including videos and images, which have no text.
- **Optional sheet**: if `SHEET_CSV_URL` is set, extra columns from the resource sheet
  (language, audience…) are attached to matching files to improve search. The sheet is
  no longer needed for anything to appear.

---

## One-time setup

### 1. Share the Drive folder with the service account
Open `GOOGLE_SERVICE_ACCOUNT_JSON` and copy the `client_email`
(…@….iam.gserviceaccount.com). In Google Drive, **Share** the resources folder with that
address as **Viewer**. Everything inside it — including subfolders — is then included.

### 2. Copy the folder ID
Open the folder in the browser. The ID is the last part of the URL:
`https://drive.google.com/drive/folders/`**`1AbCdEfGhIjK…`** → set `DRIVE_FOLDER_ID` to it.

### 3. Create a free Postgres database
**[Neon](https://neon.com)** (recommended): sign up → create a project → copy the
connection string (`postgresql://…neon.tech/neondb?sslmode=require`) → set it as
`DATABASE_URL`. The app creates its tables itself.

> Supabase works too: use the **Session pooler** connection string (Connect → Session pooler),
> because Render can't reach Supabase's direct IPv6 address.

Without `DATABASE_URL` the app uses a local SQLite file. That's fine on your laptop,
but on Render's free tier the disk is wiped on every restart, so all documents would be
re-read from Drive each time.

### 4. Keep the server awake (Render free tier)
Render's free tier puts the server to sleep after 15 minutes without visitors, and a
sleeping server can't sync. Create a free job at **[cron-job.org](https://cron-job.org)**
that requests `https://<your-app>.onrender.com/` every 10 minutes.
This uses ~744 of the 750 free Render hours a month, so keep other free Render services
off the same account (or let this one sleep — it then syncs whenever someone opens it).

### 5. Set the environment variables on Render
See the table below, then deploy. The first sync starts immediately; documents become
searchable in batches as they're read (the first full load takes a few minutes).

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | ✅ | Groq API key |
| `DRIVE_FOLDER_ID` | ✅ | The Drive folder holding the resources (shared with the service account) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | Service-account key, as one line of JSON |
| `DATABASE_URL` | ✅ in production | Postgres connection string (Neon / Supabase) |
| `GOOGLE_CLIENT_ID` | ✅ in production | OAuth client ID for Google Sign-In. If missing, the API refuses requests |
| `ADMIN_EMAILS` | recommended | Comma-separated emails that see the **Sync from Drive** button |
| `ALLOWED_ORIGINS` | recommended | Comma-separated frontend URLs allowed by CORS |
| `ALLOWED_DOMAIN` | optional | Only allow accounts from this domain, e.g. `onehope.net` |
| `SHEET_CSV_URL` | optional | Resource sheet CSV, for extra search details (or as the file list if `DRIVE_FOLDER_ID` is unset) |
| `SYNC_INTERVAL_MINUTES` | optional | How often to check Drive (default 10) |
| `MAX_FILE_MB` | optional | Skip files larger than this (default 60) |
| `RATE_LIMIT_PER_MINUTE` | optional | Messages per user per minute (default 10, `0` disables) |
| `GROQ_MODELS` | optional | Comma-separated model fallback order |
| `AUTH_DISABLED` | local dev only | `true` skips sign-in entirely |

---

## Local development

```bash
cd onehope-backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env              # fill in the values; AUTH_DISABLED=true locally
uvicorn main:app --reload         # http://localhost:8000 — syncs on startup
pytest                            # tests: no keys or network needed
```

`python indexer.py` runs a single sync from the command line (add `--full` to re-read
every file) — useful to check Drive access or do the first load before deploying.

**To get `GOOGLE_SERVICE_ACCOUNT_JSON` as one line:**
```bash
python3 -c "import json; print(json.dumps(json.load(open('service_account.json'))))"
```

---

## What gets read

| Type | Result |
|---|---|
| PDF, .docx, .pptx, .xlsx, Google Docs / Slides / Sheets | Full text searchable |
| Video, audio, images | Found by title and folder (e.g. "what films do we have in Luganda?") |
| Scanned PDFs (no text layer) | Found by title only |
| Old .doc / .ppt, other formats | Found by title only |
| Files over `MAX_FILE_MB` | Found by title only |
| A file that made the server run out of memory | Skipped after the restart, found by title only, retried when edited |

Files that fail to read are retried every 6 hours, and immediately when they're edited.

---

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | public | Health check (also the keep-awake URL) |
| GET | `/me` | signed in | Current user's email and whether they are an admin |
| GET | `/index-status` | signed in | Library size, files by status, last sync result |
| POST | `/chat` | signed in | Main chat endpoint |
| POST | `/reindex` | admin | Sync with Drive now; `?full=true` re-reads every file |

### POST /chat

Request:
```json
{
  "message": "Where can I find the Africa Manual?",
  "conversation_history": [
    {"role": "user", "content": "Hello"},
    {"role": "model", "content": "Hi! How can I help?"}
  ]
}
```

Response:
```json
{
  "answer": "The OneHope Africa Manual v2.1 covers...",
  "sources": [{"file_name": "OneHope Africa Manual v2.1.pdf", "drive_url": "https://drive.google.com/...", "folder": "Manuals"}],
  "suggestions": ["...", "...", "..."],
  "needs_clarification": false
}
```

---

## Troubleshooting

Check `GET /index-status` (signed in) — `last_sync.error` says what went wrong.

**"The Drive listing came back empty"** → the folder isn't shared with the service account, or `DRIVE_FOLDER_ID` is wrong. Nothing is deleted when this happens.

**"Not configured: GOOGLE_SERVICE_ACCOUNT_JSON…"** → set the service-account key on the server.

**A new file isn't showing up** → wait for the next sync (or click **Sync from Drive** as an admin), then check `files_by_status`: it may be a scanned PDF (`empty`) or too large (`too_large`).

**Render emails "exceeded its memory limit"** → the free server has 512 MB. The app normally uses ~100 MB at rest and ~250 MB during a full sync (check `memory_mb` in `/index-status`). If one file ever makes the server run out of memory, it is skipped automatically after the restart and shows up as `too_large` in `files_by_status` — it stays findable by title, and is retried if the file is edited.

**`Sign-in is not configured on the server` (503)** → set `GOOGLE_CLIENT_ID` (or `AUTH_DISABLED=true` locally).

**`The AI service is busy`** → every Groq model failed; check the server logs and your Groq quota.
