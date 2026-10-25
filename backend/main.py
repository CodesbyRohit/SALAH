"""
SALAH — backend entrypoint (FastAPI).
"""

import base64
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make backend modules importable when running from the project root.
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import analytics
import cache as demo_cache
import database
import llm
import memory


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Salah",
    version="0.1.0",
)

# The frontend is served by this app (same origin), so browsers need no
# cross-origin access. Allowing any origin let any website spend this API
# (including a configured paid model) from its visitors' browsers.
# SALAH_CORS_ORIGINS, a comma-separated list, opts specific origins in.
CORS_ORIGINS = [o.strip() for o in os.getenv("SALAH_CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

# Compress text responses (the frontend ships an unbundled Three.js module).
app.add_middleware(GZipMiddleware, minimum_size=1024)

FRONTEND_DIR = PROJECT_ROOT / "frontend"

log = logging.getLogger("salah")

MAX_HISTORY_ITEMS = 20
MAX_QUESTION_LENGTH = 1000
# Voice requests are forwarded to a paid API: bound what one request can send.
# 60 s of browser-recorded Opus audio is well under 1 MB.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_TTS_CHARS = 2500

conversation_history: list[dict] = []

SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "es": "Spanish",
    "fr": "French",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_language(code: str | None) -> str:
    """Return a supported language code, defaulting to English."""
    if not isinstance(code, str):
        return "en"

    value = code.strip().lower()

    return value if value in SUPPORTED_LANGUAGES else "en"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str
    language: str | None = "en"


class NudgeRequest(BaseModel):
    force: bool = False
    language: str | None = "en"


# ---------------------------------------------------------------------------
# Voice — Sarvam REST
# ---------------------------------------------------------------------------

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

SARVAM_LANG_MAP = {
    "hi": "hi-IN",
    "en": "en-IN",
}


def get_sarvam_key() -> str:
    """Read the Sarvam API key at request time."""
    return os.getenv("SARVAM_API_KEY", "").strip()


def sarvam_stt(audio_bytes: bytes) -> str | None:
    """Convert uploaded audio to text using Sarvam STT."""
    api_key = get_sarvam_key()

    if not api_key or not audio_bytes:
        return None

    try:
        response = requests.post(
            SARVAM_STT_URL,
            headers={
                "api-subscription-key": api_key,
            },
            files={
                "file": (
                    "audio.webm",
                    audio_bytes,
                    "audio/webm",
                )
            },
            data={
                "model": "saarika:v2",
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()
        transcript = data.get("transcript")

        return transcript if isinstance(transcript, str) else None

    except Exception:
        return None


def sarvam_tts(text: str, language: str = "en") -> bytes | None:
    """Convert text to speech using Sarvam TTS."""
    api_key = get_sarvam_key()

    if not api_key or not text.strip():
        return None

    target_language = SARVAM_LANG_MAP.get(language)

    # Sarvam voice is currently configured only for these languages.
    if not target_language:
        return None

    try:
        response = requests.post(
            SARVAM_TTS_URL,
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": target_language,
                "speaker": "anushka",
                "model": "bulbul:v2",
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()
        audios = data.get("audios")

        if not isinstance(audios, list) or not audios:
            return None

        encoded_audio = audios[0]

        if not encoded_audio:
            return None

        return base64.b64decode(encoded_audio)

    except Exception:
        return None


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Return backend health and integration status."""
    return {
        "status": "ok",
        "llm_available": llm.llm_available(),
        "memory": memory.status(),
        "cache_entries": demo_cache.cache_size(),
        "database": database.status(),
        "voice": {
            "available": bool(get_sarvam_key()),
            "languages": sorted(SARVAM_LANG_MAP),
        },
        "languages": SUPPORTED_LANGUAGES,
        "time": datetime.now().isoformat(),
    }


@app.get("/context")
def context():
    """Return the current merchant context."""
    try:
        return analytics.build_merchant_context()
    except database.DatabaseUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Merchant data unavailable: {exc}",
        ) from exc
    except Exception as exc:
        # Details go to the server log, not to the client.
        log.exception("building merchant context failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to build merchant context",
        ) from exc


@app.get("/insights")
def insights():
    """Return the deterministic evidence trace and recommended experiment.

    Both come straight from analytics.py (no LLM), so the frontend can show
    the evidence behind SALAH's recommendation before any question is asked.
    """
    try:
        merchant_context = analytics.build_merchant_context()
        return {
            "generated_at": merchant_context.get("generated_at"),
            "trace": analytics.build_trace(merchant_context),
            "action": analytics.get_recommended_action(merchant_context),
        }
    except database.DatabaseUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Merchant data unavailable: {exc}",
        ) from exc
    except Exception as exc:
        log.exception("building merchant insights failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to build merchant insights",
        ) from exc


@app.post("/ask")
def ask(req: AskRequest):
    """Answer a merchant question using SALAH's intelligence layer."""
    global conversation_history

    question = req.question.strip()[:MAX_QUESTION_LENGTH]
    language = normalize_language(req.language)

    if not question:
        raise HTTPException(
            status_code=400,
            detail="empty question",
        )

    try:
        merchant_context = analytics.build_merchant_context()
    except database.DatabaseUnavailable as exc:
        # No data means no honest answer: report it instead of a degraded one.
        raise HTTPException(
            status_code=503,
            detail=f"Merchant data unavailable: {exc}",
        ) from exc
    except Exception as exc:
        # Same for a broken analytics step: an empty context would produce an
        # answer with invented zeros.
        log.exception("building merchant context for /ask failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to build merchant context",
        ) from exc

    trace = None
    action = None
    source = "cache"
    fallback_reason = None

    # Cache-first for scripted/demo questions.
    cached = demo_cache.get_cached(
        question,
        language=language,
    )

    if cached:
        answer = cached.get("answer") or ""
        trace = cached.get("trace")

    else:
        recommendation = llm.explain_recommendation(
            question,
            merchant_context,
            history=conversation_history,
            language=language,
        )

        answer = recommendation.get("answer") or ""
        trace = recommendation.get("trace")
        action = recommendation.get("action")
        source = recommendation.get("source") or "deterministic"
        fallback_reason = recommendation.get("fallback_reason")

        if not answer:
            source = "deterministic"
            answer = llm.deterministic_explanation(
                merchant_context,
                [],
                {"priority": "none"},
                language=language,
            )

    # Keep conversation history bounded.
    conversation_history.append(
        {
            "role": "user",
            "text": question,
        }
    )

    conversation_history.append(
        {
            "role": "assistant",
            "text": answer,
        }
    )

    if len(conversation_history) > MAX_HISTORY_ITEMS:
        conversation_history = conversation_history[-MAX_HISTORY_ITEMS:]

    # Persist the interaction in SALAH's memory layer.
    memory.store(
        question,
        answer,
        trace=trace,
    )

    return {
        "answer": answer,
        "trace": trace,
        "action": action,
        "source": source,
        "fallback_reason": fallback_reason,
        "language": language,
    }


@app.post("/nudge")
def nudge(req: NudgeRequest):
    """Generate a proactive merchant update."""
    language = normalize_language(req.language)

    try:
        merchant_context = analytics.build_merchant_context()
    except database.DatabaseUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Merchant data unavailable: {exc}",
        ) from exc
    except Exception as exc:
        log.exception("building merchant context for /nudge failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to build merchant context",
        ) from exc

    weakest_weekday = merchant_context.get(
        "weakest_weekday",
        {},
    )

    lapsed_regulars = merchant_context.get(
        "lapsed_regulars",
        {},
    )

    fallback_message = (
        f"Update: "
        f"{weakest_weekday.get('weekday', 'Day')} revenue below average "
        f"({weakest_weekday.get('avg_daily_revenue')} vs "
        f"{weakest_weekday.get('overall_avg_daily_revenue')}). "
        f"{lapsed_regulars.get('count', 0)} "
        f"lapsed regular customers identified."
    )

    answer = llm.explain(
        "Give a short proactive update",
        merchant_context,
        language=language,
    )

    if not answer:
        answer = fallback_message

    return {
        "nudge": answer,
        "language": language,
    }


@app.post("/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
    """Transcribe uploaded audio using Sarvam STT."""
    if not (file.content_type or "").lower().startswith("audio/"):
        raise HTTPException(
            status_code=415,
            detail="Upload must be an audio file",
        )

    audio = await file.read(MAX_AUDIO_BYTES + 1)

    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Audio file too large",
        )

    if not audio:
        raise HTTPException(
            status_code=400,
            detail="Empty audio file",
        )

    text = sarvam_stt(audio)

    if text is None:
        raise HTTPException(
            status_code=503,
            detail="STT unavailable",
        )

    return {
        "text": text,
    }


@app.post("/voice/tts")
def voice_tts(payload: dict):
    """Generate speech audio using Sarvam TTS."""
    payload = payload or {}

    text = payload.get("text", "")
    language = normalize_language(payload.get("language"))

    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=400,
            detail="empty text",
        )

    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(
            status_code=413,
            detail="Text too long to read aloud",
        )

    audio = sarvam_tts(
        text,
        language=language,
    )

    if audio is None:
        raise HTTPException(
            status_code=503,
            detail="TTS unavailable for selected language",
        )

    return Response(
        content=audio,
        media_type="audio/mpeg",
    )


@app.post("/reset")
def reset():
    """Clear the current conversation."""
    global conversation_history

    conversation_history.clear()
    memory.reset_conversation()

    return {
        "ok": True,
    }


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

# Static files only change with a deployment. s-maxage lets a CDN in front of
# the app (Vercel's edge) serve them near the user instead of forwarding every
# asset to the function; max-age=0 keeps browsers revalidating, so a new
# deployment is picked up on the next load. API responses are not affected.
STATIC_CACHE_CONTROL = "public, max-age=0, s-maxage=86400, stale-while-revalidate=300"


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        return response


if FRONTEND_DIR.exists():
    app.mount(
        "/",
        FrontendFiles(
            directory=str(FRONTEND_DIR),
            html=True,
        ),
        name="static",
    )


# ---------------------------------------------------------------------------
# Local development entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )