# OneHope Resource Centre — Deployment Guide

## What changed in v2

| File | What changed |
|------|-------------|
| `main.py` | Fixed `docs_indexed` key, added Google Sign-In auth, Groq timeout, model fallback, reindex guard |
| `drive_reader.py` | Added retry on Drive 429 rate limits, added .xlsx and Google Sheets extraction |
| `requirements.txt` | Added `openpyxl` |
| `app.js` | Google Sign-In flow, re-index button, localStorage cap (20 chats), token expiry handling, all bug fixes |
| `index.html` | Added auth overlay, sign-in button, re-index button in sidebar |
| `style.css` | Added styles for auth overlay, user badge, re-index button |
| `Procfile` | New — needed by Render/Railway |
| `render.yaml` | New — one-click Render deploy config |
| `.env.example` | Updated with new GOOGLE_CLIENT_ID and ALLOWED_DOMAIN vars |

---

## Quick Start (Local)

```bash
cd onehope-backend
pip install -r requirements.txt
cp .env.example .env
# Fill in your values in .env
python indexer.py          # first-time index
uvicorn main:app --reload  # starts on http://localhost:8000
```

Open `onehope-frontend/index.html` in your browser.

---

## Hosting — Backend on Render (free)

1. Push this folder to a GitHub repo.
2. Go to [render.com](https://render.com) → New → Web Service → connect your repo.
3. Set Root Directory to `onehope-backend`.
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Add these Environment Variables in the Render dashboard:
   - `GROQ_API_KEY`
   - `SHEET_CSV_URL`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`
   - `GOOGLE_CLIENT_ID` (optional — leave blank to disable auth)
   - `ALLOWED_DOMAIN` (optional — e.g. `onehope.net`)
7. Deploy. Copy the URL (e.g. `https://onehope-xxx.onrender.com`).

> ⚠️ **search_index.json on Render**: Render's free tier has an ephemeral filesystem — it resets on every deploy. You have two options:
> - **Option A (easiest)**: Run `python indexer.py` locally, commit `search_index.json` to your repo, and let Render deploy it.
> - **Option B**: Use a free [Supabase Storage](https://supabase.com) bucket and update `INDEX_PATH` in main.py and indexer.py to read/write from the bucket URL.

---

## Hosting — Frontend on Netlify (free, unlimited)

1. In `onehope-frontend/app.js`, change:
   ```js
   const BACKEND_URL = 'http://localhost:8000';
   // → to your Render URL:
   const BACKEND_URL = 'https://your-app.onrender.com';
   ```
2. Go to [netlify.com](https://netlify.com) → Add new site → Deploy manually.
3. Drag and drop the `onehope-frontend` folder.
4. Done — you get a free `*.netlify.app` URL.

---

## Setting Up Google Sign-In

1. Go to [Google Cloud Console](https://console.cloud.google.com).
2. Create a project (or use your existing one).
3. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID.
4. Application type: **Web application**.
5. Authorised JavaScript origins:
   - `http://localhost` (for local dev)
   - `https://your-site.netlify.app` (your deployed frontend)
6. Copy the Client ID.
7. In `onehope-frontend/index.html`: replace `YOUR_GOOGLE_CLIENT_ID` in the meta tag.
8. In `onehope-frontend/app.js`: replace `YOUR_GOOGLE_CLIENT_ID` in the constant.
9. In Render dashboard: set `GOOGLE_CLIENT_ID` to the same value.
10. Optionally set `ALLOWED_DOMAIN=onehope.net` to restrict sign-in to your org.

> 💡 If you leave `GOOGLE_CLIENT_ID` blank in both the frontend and backend `.env`, auth is fully disabled and the app works without sign-in — great for internal local use.

---

## Running the Indexer

| Method | Command |
|--------|---------|
| Direct (recommended) | `cd onehope-backend && python indexer.py` |
| HTTP (server running) | `curl -X POST http://localhost:8000/reindex` |
| UI button | Click **Refresh Index** in the sidebar |
| Automatic | Every night at 2:00 AM server time |

Poll progress: `GET /index-status` → `{ "in_progress": true/false, "total_chunks": N }`
