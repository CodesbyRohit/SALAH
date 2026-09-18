"""
SALAH — backend entrypoint (FastAPI).

BASELINE (pre-Phase-5) BEHAVIOUR — kept intentionally so Phase 5 has real work:
  * /ask pipeline is cache -> LLM, no merchant-context pre-build step wired
    into the response, no trace in responses
  * /context returns merchant context only (no recommended action, no trace)
  * minimal exception handling; Cognee/voice failures are not yet wrapped
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analytics
import cache as demo_cache
import llm
import memory

app = FastAPI(title="Salah", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

conversation_history: list[dict] = []


class AskRequest(BaseModel):
    question: str


class NudgeRequest(BaseModel):
    force: bool = False


# ---------------------------------------------------------------------------
# Voice (Sarvam REST)
# ---------------------------------------------------------------------------

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"


def sarvam_stt(audio_bytes: bytes) -> str | None:
    if not SARVAM_API_KEY:
        return None
    try:
        resp = requests.post(
            SARVAM_STT_URL,
            headers={"api-subscription-key": SARVAM_API_KEY},
            files={"file": ("audio.webm", audio_bytes, "audio/webm")},
            data={"model": "saarika:v2"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("transcript")
    except Exception:
        return None


def sarvam_tts(text: str) -> bytes | None:
    if not SARVAM_API_KEY:
        return None
    try:
        resp = requests.post(
            SARVAM_TTS_URL,
            headers={
                "api-subscription-key": SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": "hi-IN",
                "speaker": "anushka",
                "model": "bulbul:v2",
            },
            timeout=30,
        )
        resp.raise_for_status()
        import base64
        audio = resp.json().get("audios", [None])[0]
        return base64.b64decode(audio) if audio else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Endpoints
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
    return analytics.build_merchant_context()


@app.post("/ask")
def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")

    # cache-first for scripted demo questions
    hit = demo_cache.get_cached(question)
    if hit:
        answer = hit["answer"]
    else:
        ctx = analytics.build_merchant_context()
        answer = llm.explain(question, ctx, conversation_history) or ""
        if not answer:
            answer = "Sorry, jawab generate nahi ho paya. Dobara poochhiye."

    conversation_history.append({"role": "user", "text": question})
    conversation_history.append({"role": "assistant", "text": answer})
    memory.store(question, answer)

    return {"answer": answer, "trace": None}


@app.post("/nudge")
def nudge(req: NudgeRequest):
    ctx = analytics.build_merchant_context()
    wd = ctx["weakest_weekday"]
    lr = ctx["lapsed_regulars"]
    msg = (
        f"Namaste! {wd['weekday']} ka revenue average se neeche raha "
        f"({wd['avg_daily_revenue']} vs {wd['overall_avg_daily_revenue']}). "
        f"{lr['count']} purane regular customers dikh rahe hain jo kam aa rahe hain."
    )
    answer = llm.explain("Aaj ka short proactive update do", ctx) or msg
    return {"nudge": answer}


@app.post("/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
    audio = await file.read()
    text = sarvam_stt(audio)
    if text is None:
        raise HTTPException(status_code=503, detail="STT unavailable")
    return {"text": text}


@app.post("/voice/tts")
def voice_tts(payload: dict):
    text = (payload or {}).get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    audio = sarvam_tts(text)
    if audio is None:
        raise HTTPException(status_code=503, detail="TTS unavailable")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(audio)
    tmp.close()
    return FileResponse(tmp.name, media_type="audio/mpeg")


@app.post("/reset")
def reset():
    conversation_history.clear()
    memory.reset_conversation()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
