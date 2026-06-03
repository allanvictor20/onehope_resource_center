# OneHope Resource Centre — Backend

AI-powered resource assistant for OneHope Uganda.  
Built with FastAPI (Python) + search index + Google Drive API + Gemini.

---

## How it works

```
User message (HTML frontend)
        ↓
POST /chat  (FastAPI backend)
        ↓
search index vector search → top 5 relevant document chunks  [FREE, no API call]
        ↓
Google Sheet summary fetched (cached 30 min)             [FREE, no API call]
        ↓
Gemini API called with question + chunks + sheet index   [1 API call]
        ↓
Answer + source links returned to frontend
```

**Result: 1 Gemini API call per user message, regardless of library size.**

---

## Project structure

```
onehope-backend/
├── main.py                      # FastAPI server (run this to serve requests)
├── indexer.py                   # One-time script to index all Drive documents
├── drive_reader.py              # Google Drive file fetcher and text extractor
├── requirements.txt             # Python dependencies
├── .env.example                 # Copy to .env and fill in your values
├── .env                         # Your secrets (NEVER commit to GitHub)
├── service_account.json         # Downloaded from Google Cloud (NEVER commit)
├── search_index.json                   # Created by indexer.py (auto-generated)
└── onehope_assistant_frontend.html  # Updated frontend (talks to this backend)
```

---

## Setup (step by step)

### 1. Clone / download this folder

```bash
cd onehope-backend
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> Note: `scikit-learn` installs cleanly on Windows with no C++ build tools required.
> Total install size is under 50MB.

### 4. Set up your .env file

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
GEMINI_API_KEY=your_gemini_api_key_here
SHEET_CSV_URL=https://docs.google.com/spreadsheets/...
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

**To get `GOOGLE_SERVICE_ACCOUNT_JSON`:**  
On Windows (PowerShell):
```powershell
Get-Content service_account.json | python -c "import sys,json; print(json.dumps(json.load(sys.stdin)))"
```
On Mac/Linux:
```bash
python3 -c "import json; print(json.dumps(json.load(open('service_account.json'))))"
```
Paste the single-line output as the value of `GOOGLE_SERVICE_ACCOUNT_JSON` in your `.env`.

### 5. Index your documents (run once)

```bash
python indexer.py
```

This will:
- Fetch your Google Sheet to get all file names and Drive links
- Download and extract text from every supported file (PDF, PPTX, DOCX, Google Docs/Slides)
- Skip videos, audio, and images automatically
- Store everything in `search_index.json` locally

This takes a few minutes depending on how many files you have.  
**Re-run whenever you add new files to your Drive folder.**

### 6. Start the backend server

```bash
uvicorn main:app --reload
```

The server runs at `http://localhost:8000`.  
Open `http://localhost:8000` in your browser — you should see a JSON health check.

### 7. Open the frontend

Open `onehope_assistant_frontend.html` directly in your browser.  
It will connect to `http://localhost:8000` automatically.

---

## Deploying to production (Railway)

1. Push this folder to a GitHub repository  
   (Make sure `.env` and `service_account.json` are in `.gitignore`)

2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub

3. Add your environment variables in Railway's dashboard (same keys as your `.env`)

4. Railway will run `uvicorn main:app --host 0.0.0.0 --port $PORT` automatically

5. Once deployed, update `BACKEND_URL` in `onehope_assistant_frontend.html`:
   ```js
   const BACKEND_URL = 'https://your-app.railway.app';
   ```

---

## Supported file types

| Type | Supported |
|---|---|
| PDF | ✅ |
| Google Docs | ✅ |
| Google Slides | ✅ |
| .pptx | ✅ |
| .docx | ✅ |
| MP4 / MOV / video | ⏭ Skipped |
| MP3 / audio | ⏭ Skipped |
| PNG / JPG / images | ⏭ Skipped |
| .DS_Store | ⏭ Skipped |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Health check — shows docs indexed count |
| GET | `/index-status` | Detailed search index index info |
| POST | `/chat` | Main chat endpoint |
| POST | `/reindex` | Manually trigger a full re-index of Drive documents |

### POST /chat request body

```json
{
  "message": "Where can I find the Africa Manual?",
  "conversation_history": [
    {"role": "user", "content": "Hello"},
    {"role": "model", "content": "Hi! How can I help?"}
  ]
}
```

### POST /chat response

```json
{
  "answer": "The OneHope Africa Manual v2.1 covers...",
  "sources": [
    {
      "file_name": "OneHope Africa Manual v2.1.pdf",
      "drive_url": "https://drive.google.com/...",
      "folder": "Manuals"
    }
  ]
}
```

---

## Keeping the index up to date

The backend automatically re-indexes your Drive documents every night at **2:00 AM server time**.  
This means new files you upload to Drive will be searchable by the next morning — no action needed.

### Trigger a manual re-index immediately

If you upload a new file and don't want to wait until 2 AM, hit this endpoint:

```bash
curl -X POST http://localhost:8000/reindex
```

Or just open `http://localhost:8000/reindex` in a REST client (Postman, Insomnia, etc).  
The re-index runs in the background — check your server terminal logs for progress.

### What updates automatically vs. what needs a re-index

| Change | Auto-updates? |
|---|---|
| New row added to Google Sheet | ✅ Within 30 minutes (sheet is cached) |
| New file uploaded to Drive | ⏰ Next night at 2 AM (or manual `/reindex`) |
| File content changed in Drive | ⏰ Next night at 2 AM (or manual `/reindex`) |
| File deleted from Drive | ⏰ Next night at 2 AM (or manual `/reindex`) |

---

## Troubleshooting

**`GOOGLE_SERVICE_ACCOUNT_JSON is not set`**  
→ Make sure your `.env` file exists and has the variable set.

**`search index not found. Run python indexer.py first`**  
→ You need to run `python indexer.py` before starting the server.

**`Quota exceeded` from Gemini**  
→ You've hit your API rate limit. Wait for the quota to reset or enable billing in Google AI Studio.

**`Could not reach backend` in the frontend**  
→ Make sure `uvicorn main:app --reload` is running in a terminal.

**Files not being indexed**  
→ Confirm the service account email has **Viewer** access to your Drive folder.  
→ Check that the Google Drive API is enabled in your Google Cloud project.
