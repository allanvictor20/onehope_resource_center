"""
main.py
───────
OneHope Resource Centre — FastAPI Backend

Changes from v1:
  - Fixed health check: returns docs_indexed (was chunks_indexed) so frontend
    status display works correctly
  - Added request timeout (30 s) on Groq call to prevent indefinite hangs
  - Added Google Sign-In token verification with domain restriction
  - Added /reindex-status endpoint so the UI can poll progress
  - Added last_reindex_time tracking
  - Added Groq model fallback list
  - Added retry logic for transient errors
"""

import os
import json
import logging
import time
import threading
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests
import pandas as pd
from io import StringIO
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Google auth (only needed when GOOGLE_CLIENT_ID is set)
try:
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
SHEET_CSV_URL    = os.environ.get("SHEET_CSV_URL", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ALLOWED_DOMAIN   = os.environ.get("ALLOWED_DOMAIN", "")   # e.g. "onehope.net"
INDEX_PATH       = "./search_index.json"
TOP_K_CHUNKS     = 5
SHEET_CACHE_TTL  = 1800   # 30 minutes
MAX_CHUNK_CHARS  = 6000   # hard cap on total context sent to Groq
GROQ_TIMEOUT     = 30     # seconds

# Groq model preference list — first available is used
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
]


# ── TF-IDF SEARCH INDEX ───────────────────────────────────────────────────────

_search_index: list = []
_tfidf_vectorizer = None
_tfidf_matrix = None
_last_reindex_time: Optional[str] = None
_reindex_in_progress: bool = False


def load_search_index():
    global _search_index, _tfidf_vectorizer, _tfidf_matrix

    if not os.path.exists(INDEX_PATH):
        logger.warning("search_index.json not found. Run 'python indexer.py' first.")
        _search_index = []
        _tfidf_vectorizer = None
        _tfidf_matrix = None
        return

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        _search_index = json.load(f)

    if not _search_index:
        logger.warning("search_index.json is empty.")
        return

    texts = [chunk["text"] for chunk in _search_index]
    _tfidf_vectorizer = TfidfVectorizer(
        strip_accents="unicode",
        lowercase=True,
        ngram_range=(1, 2),
        min_df=1,
        max_df=0.95,
        sublinear_tf=True,
    )
    _tfidf_matrix = _tfidf_vectorizer.fit_transform(texts)
    logger.info(f"Search index loaded: {len(_search_index)} chunks, vocab: {len(_tfidf_vectorizer.vocabulary_)}")


def search_index(query: str, top_k: int = TOP_K_CHUNKS) -> list:
    if _tfidf_vectorizer is None or _tfidf_matrix is None or not _search_index:
        return []

    query_vec = _tfidf_vectorizer.transform([query])
    scores = cosine_similarity(query_vec, _tfidf_matrix).flatten()
    top_indices = scores.argsort()[::-1][:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] > 0:
            chunk = _search_index[idx].copy()
            chunk["score"] = float(scores[idx])
            results.append(chunk)

    if results:
        logger.info(f"TF-IDF search: {len(results)} relevant chunks (top score: {results[0]['score']:.3f})")
    else:
        logger.info("TF-IDF search: no relevant chunks found")
    return results


def build_context(chunks: list, max_chars: int = MAX_CHUNK_CHARS) -> str:
    parts = []
    total = 0
    for i, chunk in enumerate(chunks):
        text = chunk["text"]
        source = chunk.get("file_name", "Unknown")
        block = f"[Excerpt {i+1} — {source}]:\n{text}"
        if total + len(block) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                parts.append(block[:remaining] + "…")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


# ── SHEET CACHE ───────────────────────────────────────────────────────────────

_sheet_cache: dict = {"df": None, "last_fetched": 0}


def get_sheet_data() -> pd.DataFrame:
    now = time.time()
    if _sheet_cache["df"] is not None and (now - _sheet_cache["last_fetched"]) < SHEET_CACHE_TTL:
        return _sheet_cache["df"]

    logger.info("Refreshing Google Sheet data...")
    try:
        res = requests.get(SHEET_CSV_URL, timeout=20)
        res.raise_for_status()
        df = pd.read_csv(StringIO(res.text))
        _sheet_cache["df"] = df
        _sheet_cache["last_fetched"] = now
        logger.info(f"Sheet refreshed: {len(df)} rows")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch sheet: {e}")
        if _sheet_cache["df"] is not None:
            logger.warning("Serving stale sheet cache")
            return _sheet_cache["df"]
        raise HTTPException(status_code=503, detail="Could not load resource index.")


# ── SCHEDULED RE-INDEX ────────────────────────────────────────────────────────

def run_scheduled_reindex():
    global _last_reindex_time, _reindex_in_progress
    if _reindex_in_progress:
        logger.warning("Re-index already running, skipping.")
        return
    _reindex_in_progress = True
    logger.info("⏰ Re-index starting...")
    try:
        from indexer import run_indexer
        run_indexer()
        load_search_index()
        _last_reindex_time = datetime.now(timezone.utc).isoformat()
        logger.info("✅ Re-index complete.")
    except Exception as e:
        logger.error(f"❌ Re-index failed: {e}")
    finally:
        _reindex_in_progress = False


# ── GOOGLE SIGN-IN VERIFICATION ───────────────────────────────────────────────

def verify_google_token(authorization: str = Header(default=None)):
    """
    Dependency — call with Depends(verify_google_token) on protected routes.

    If GOOGLE_CLIENT_ID is not configured, auth is disabled and every request
    is allowed (useful during local development).

    If ALLOWED_DOMAIN is set (e.g. "onehope.net"), only emails from that domain
    are accepted after the token is verified.
    """
    if not GOOGLE_CLIENT_ID:
        # Auth not configured — allow all requests
        return {"email": "anonymous", "auth_disabled": True}

    if not GOOGLE_AUTH_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="google-auth library not installed. Run: pip install google-auth"
        )

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing.")

    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token missing.")

    try:
        info = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    email = info.get("email", "")
    if not info.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Email not verified by Google.")

    if ALLOWED_DOMAIN and not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(
            status_code=403,
            detail=f"Access restricted to @{ALLOWED_DOMAIN} accounts."
        )

    return info


# ── GROQ ──────────────────────────────────────────────────────────────────────

def get_groq_client():
    if not GROQ_API_KEY:
        raise EnvironmentError("GROQ_API_KEY is not set in .env")
    return Groq(api_key=GROQ_API_KEY)


def call_groq_with_fallback(client: Groq, messages: list, max_tokens: int = 1200, temperature: float = 0.7) -> str:
    """
    Tries each model in GROQ_MODELS in order, returning on first success.
    Raises HTTPException only if all models fail.
    """
    last_error = None
    for model in GROQ_MODELS:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=GROQ_TIMEOUT,
            )
            if model != GROQ_MODELS[0]:
                logger.info(f"Used fallback Groq model: {model}")
            return response.choices[0].message.content.strip()
        except Exception as e:
            last_error = e
            logger.warning(f"Groq model {model} failed: {e}")
            continue
    raise HTTPException(status_code=502, detail=f"All Groq models failed. Last error: {str(last_error)}")


# ── MODELS ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    conversation_history: Optional[list] = []


class Source(BaseModel):
    file_name: str
    drive_url: str
    folder: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    suggestions: list[str] = []
    needs_clarification: bool = False


# ── LIFESPAN ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting OneHope Resource Centre backend...")
    load_search_index()

    if SHEET_CSV_URL:
        try:
            get_sheet_data()
        except Exception:
            pass

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_scheduled_reindex,
        trigger="cron",
        hour=2,
        minute=0,
        id="nightly_reindex",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("⏰ Nightly re-index scheduled at 2:00 AM.")
    app.state.scheduler = scheduler
    logger.info("Backend ready.")
    yield

    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown(wait=False)
    logger.info("Backend shutting down.")


# ── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="OneHope Resource Centre API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    """
    FIX: renamed chunks_indexed → docs_indexed so frontend status pill works.
    Also exposes last_reindex_time so the UI can show "last updated" info.
    """
    return {
        "status": "ok",
        "docs_indexed": len(_search_index),          # ← fixed key name
        "chunks_indexed": len(_search_index),         # kept for backwards compat
        "search_ready": _tfidf_vectorizer is not None,
        "sheet_loaded": _sheet_cache["df"] is not None,
        "last_reindex_time": _last_reindex_time,
        "auth_enabled": bool(GOOGLE_CLIENT_ID),
    }


@app.get("/index-status")
def index_status():
    if not _search_index:
        return {"indexed": False, "message": "No index found. Run 'python indexer.py' first."}
    return {
        "indexed": True,
        "total_chunks": len(_search_index),
        "in_progress": _reindex_in_progress,
        "last_reindex_time": _last_reindex_time,
    }


@app.post("/reindex")
def trigger_reindex():
    """Manually trigger a re-index. Safe to call while server is live."""
    if _reindex_in_progress:
        return {"status": "already_running", "message": "Re-index is already in progress."}
    thread = threading.Thread(target=run_scheduled_reindex, daemon=True)
    thread.start()
    return {"status": "reindex_started", "message": "Re-index started in background. Poll /index-status for progress."}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user=Depends(verify_google_token)):
    """
    Main chat endpoint.
    Protected by Google Sign-In when GOOGLE_CLIENT_ID env var is set.
    Auth is disabled automatically during local development (no GOOGLE_CLIENT_ID).
    """
    user_message = req.message.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    logger.info(f"Question from {user.get('email', 'anon')}: {user_message[:80]}")

    # ── STEP 1: TF-IDF search ──
    results = search_index(user_message, top_k=TOP_K_CHUNKS)

    # ── STEP 2: Build lean context ──
    doc_context = build_context(results)

    # ── STEP 3: Collect sources ──
    sources = []
    seen_urls = set()
    for chunk in results:
        url = chunk.get("drive_url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(Source(
                file_name=chunk.get("file_name", "Unknown"),
                drive_url=url,
                folder=chunk.get("folder", "General"),
            ))

    # ── STEP 4: Keep sheet cache warm ──
    try:
        get_sheet_data()
    except Exception:
        pass

    # ── STEP 5: Build prompt ──
    if doc_context:
        context_block = f"""=== RELEVANT DOCUMENT EXCERPTS ===
{doc_context}

Use the excerpts above to answer the question. Always mention which resource a piece of information comes from."""
    else:
        context_block = "No matching documents were found in the index for this query. Answer from general OneHope knowledge if possible, and let the user know you couldn't find a specific resource."

    system_prompt = f"""You are the OneHope Uganda Resource Centre assistant.
You help staff, volunteers, and partners find and understand OneHope's programmes,
materials, and resources. You are warm, helpful, and conversational.

OneHope works to provide every child with God's Word and help them make a decision for Christ.

When answering:
- Be conversational and friendly
- Use the document excerpts below to give accurate answers
- Always mention which resource information comes from
- If you don't know something, say so honestly — don't make things up
- Handle typos, abbreviations, and informal English gracefully
- If a user seems to be asking about something with a slight spelling variation (e.g. "catalyse" vs "catalyze", "sparck" vs "spark"), figure out what they meant and answer accordingly
- Never say you don't know something just because of a spelling mistake — make your best guess at what they meant

{context_block}

=== RESPONSE FORMAT ===
You MUST respond with a JSON object only. No markdown fences, no preamble, just raw JSON.
Use this exact structure:
{{
  "needs_clarification": false,
  "clarification_question": "",
  "clarification_options": [],
  "answer": "your full answer here",
  "suggestions": ["follow-up question 1", "follow-up question 2", "follow-up question 3"]
}}

Rules:
- If the query is too vague to answer meaningfully (e.g. just "training" or "resources" with no context), set needs_clarification to true, write a short clarification_question, and provide 3-4 short clarification_options as button labels. Leave answer empty.
- If the query is clear enough, set needs_clarification to false, leave clarification fields empty, write the full answer, and provide 3 short follow-up suggestions (max 8 words each).
- Never return anything outside the JSON object.
"""

    # ── STEP 6: Build message history (last 10 turns max) ──
    messages = [{"role": "system", "content": system_prompt}]
    history = (req.conversation_history or [])[-20:]
    for turn in history:
        role = "assistant" if turn.get("role") == "model" else "user"
        content = turn.get("content", "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    # ── STEP 7: Groq call with model fallback & timeout ──
    try:
        client = get_groq_client()
        raw = call_groq_with_fallback(client, messages)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    # ── STEP 8: Parse JSON response ──
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("Groq did not return valid JSON, using plain text fallback")
        parsed = {
            "answer": raw,
            "needs_clarification": False,
            "suggestions": [],
            "clarification_question": "",
            "clarification_options": [],
        }

    needs_clarification = parsed.get("needs_clarification", False)

    if needs_clarification:
        answer = parsed.get("clarification_question", "Could you be more specific?")
        suggestions = parsed.get("clarification_options", [])[:4]
    else:
        answer = parsed.get("answer", "Sorry, I could not generate a response.")
        suggestions = parsed.get("suggestions", [])[:3]

    logger.info(f"Responded. Sources: {len(sources)} | Clarification: {needs_clarification} | Suggestions: {len(suggestions)}")
    return ChatResponse(
        answer=answer,
        sources=sources,
        suggestions=suggestions,
        needs_clarification=needs_clarification,
    )
