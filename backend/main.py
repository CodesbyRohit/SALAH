"""
SALAH — backend entrypoint (FastAPI).
"""

import base64
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make backend modules importable when running from the project root.
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import analytics
import cache as demo_cache
import llm
import memory


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Salah",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = PROJECT_ROOT / "frontend"

MAX_HISTORY_ITEMS = 20
MAX_QUESTION_LENGTH = 1000

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
        "time": datetime.now().isoformat(),
    }


@app.get("/context")
def context():
    """Return the current merchant context."""
    try:
        return analytics.build_merchant_context()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build merchant context: {exc}",
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
    except Exception:
        merchant_context = {}

    trace = None

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

        if not answer:
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
        "language": language,
    }


@app.post("/nudge")
def nudge(req: NudgeRequest):
    """Generate a proactive merchant update."""
    language = normalize_language(req.language)

    try:
        merchant_context = analytics.build_merchant_context()
    except Exception:
        merchant_context = {}

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
    audio = await file.read()

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

if FRONTEND_DIR.exists():
    app.mount(
        "/",
        StaticFiles(
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