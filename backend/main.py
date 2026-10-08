"""
SALAH — backend entrypoint (FastAPI).
"""

import os
import sys
import base64
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analytics
import cache as demo_cache
import llm
import memory


app = FastAPI(title="Salah", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

MAX_HISTORY_ITEMS = 20
MAX_QUESTION_LENGTH = 1000

conversation_history: list[dict] = []


SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "es": "Spanish",
    "fr": "French",
}


def normalize_language(code: str | None) -> str:
    if not isinstance(code, str):
        return "en"

    value = code.strip().lower()

    return value if value in SUPPORTED_LANGUAGES else "en"


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


# Sarvam voice support currently configured for these languages.
SARVAM_LANG_MAP = {
    "hi": "hi-IN",
    "en": "en-IN",
}


def get_sarvam_key() -> str:
    return os.getenv("SARVAM_API_KEY", "")


def sarvam_stt(audio_bytes: bytes) -> str | None:
    api_key = get_sarvam_key()

    if not api_key or not audio_bytes:
        return None

    try:
        resp = requests.post(
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

        resp.raise_for_status()

        return resp.json().get("transcript")

    except Exception:
        return None


def sarvam_tts(text: str, language: str = "en") -> bytes | None:
    api_key = get_sarvam_key()

    if not api_key or not text.strip():
        return None

    target_lang = SARVAM_LANG_MAP.get(language)

    # Gracefully fail for languages not configured for Sarvam voice.
    if not target_lang:
        return None

    try:
        resp = requests.post(
            SARVAM_TTS_URL,
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": target_lang,
                "speaker": "anushka",
                "model": "bulbul:v2",
            },
            timeout=30,
        )

        resp.raise_for_status()

        audio = resp.json().get("audios", [None])[0]

        return base64.b64decode(audio) if audio else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "llm_available": llm.llm_available(),
        "memory": memory.status(),
        "cache_entries": demo_cache.cache_size(),
        "time": datetime.now().isoformat(),
    }


@app.get("/context")
def context():
    try:
        return analytics.build_merchant_context()

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build merchant context: {exc}",
        )


@app.post("/ask")
def ask(req: AskRequest):
    global conversation_history

    question = req.question.strip()[:MAX_QUESTION_LENGTH]
    lang = normalize_language(req.language)

    if not question:
        raise HTTPException(
            status_code=400,
            detail="empty question",
        )

    try:
        ctx = analytics.build_merchant_context()

    except Exception:
        ctx = {}

    trace = None

    # Cache-first for scripted/demo questions.
    try:
        hit = demo_cache.get_cached(
            question,
            language=lang,
        )
    except TypeError:
        # Backward compatibility if cache.py does not yet
        # support the language parameter.
        hit = demo_cache.get_cached(question)

    if hit:
        answer = hit["answer"]
        trace = hit.get("trace")

    else:
        try:
            rec = llm.explain_recommendation(
                question,
                ctx,
                history=conversation_history,
                language=lang,
            )

            answer = rec.get("answer") or ""
            trace = rec.get("trace")

        except TypeError:
            # Backward compatibility with an older llm.py.
            answer = llm.explain(
                question,
                ctx,
                conversation_history,
            ) or ""

        if not answer:
            try:
                answer = llm.deterministic_explanation(
                    ctx,
                    [],
                    {"priority": "none"},
                    language=lang,
                )
            except TypeError:
                answer = (
                    "Sorry, answer generate nahi ho paya. "
                    "Dobara poochhiye."
                )

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

    try:
        memory.store(
            question,
            answer,
            trace=trace,
        )
    except TypeError:
        # Backward compatibility with an older memory.py.
        memory.store(
            question,
            answer,
        )

    return {
        "answer": answer,
        "trace": trace,
        "language": lang,
    }


@app.post("/nudge")
def nudge(req: NudgeRequest):
    lang = normalize_language(req.language)

    try:
        ctx = analytics.build_merchant_context()

    except Exception:
        ctx = {}

    wd = ctx.get("weakest_weekday", {})
    lr = ctx.get("lapsed_regulars", {})

    fallback_msg = (
        f"Update: {wd.get('weekday', 'Day')} revenue below average "
        f"({wd.get('avg_daily_revenue')} vs "
        f"{wd.get('overall_avg_daily_revenue')}). "
        f"{lr.get('count', 0)} lapsed regular customers identified."
    )

    try:
        answer = (
            llm.explain(
                "Give a short proactive update",
                ctx,
                language=lang,
            )
            or fallback_msg
        )

    except TypeError:
        answer = (
            llm.explain(
                "Give a short proactive update",
                ctx,
            )
            or fallback_msg
        )

    return {
        "nudge": answer,
        "language": lang,
    }


@app.post("/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
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
    payload = payload or {}

    text = payload.get("text", "")
    lang = normalize_language(payload.get("language"))

    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="empty text",
        )

    audio = sarvam_tts(
        text,
        language=lang,
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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )
