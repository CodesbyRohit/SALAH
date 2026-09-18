"""
SALAH — LLM explanation layer (baseline).

Single Gemini REST call. The LLM receives the deterministic merchant context as
JSON and produces a merchant-facing Hindi answer. No DB access, no tool use.

BASELINE (pre-Phase-4) BEHAVIOUR — kept intentionally so Phase 4 has real work:
  * generic system prompt (not evidence-aware, no FACT/PATTERN/HYPOTHESIS/
    UNKNOWN discipline, no sentence cap)
  * returns plain text answer; the structured {answer, trace} contract and
    deterministic trace injection arrive in Phase 2/4.
"""
import json
import os

import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)

SYSTEM_PROMPT = (
    "Tum 'Salah' ho — ek chota business assistant jo ek Indian merchant ke "
    "payments data ki simple Hindi vyakhya karta hai. User Hindi/Hinglish "
    "mein sawal poochega. Simple Hindi mein, seedha jawab do. Sirf MERCHANT_DATA "
    "mein diye gaye numbers use karo, naye numbers mat banao."
)


def llm_available() -> bool:
    return bool(GEMINI_API_KEY)


def explain(question: str, merchant_context: dict, history: list | None = None) -> str | None:
    """Ask Gemini for a short Hindi explanation. Returns None on any failure so
    callers can fall back to cache/analytics-only behavior."""
    if not llm_available():
        return None
    contents = []
    if history:
        for h in history[-6:]:
            role = "user" if h.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": str(h.get("text", ""))[:500]}]})
    contents.append({"role": "user", "parts": [{
        "text": (
            f"MERCHANT_DATA:\n{json.dumps(merchant_context, ensure_ascii=False)}\n\n"
            f"USER_QUESTION: {question}"
        )
    }]})
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
    }
    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None
