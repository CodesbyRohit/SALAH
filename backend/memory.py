"""
SALAH — interaction memory (Cognee with local fallback).

Persistent interaction memory so Salah can answer "pichli baar tune kya
suggest kiya tha?" even after a conversation reset.

BASELINE (pre-Phase-7) BEHAVIOUR — kept intentionally so Phase 7 has real work:
  * store() sends the answer text to Cognee only (no trace stored)
  * the local JSONL fallback log exists but store() does not write to it when
    Cognee succeeds, and retrieve() only consults Cognee
"""
import json
import os
from datetime import datetime
from pathlib import Path

try:
    import cognee  # type: ignore
    COGNEE_AVAILABLE = True
except Exception:
    cognee = None
    COGNEE_AVAILABLE = False

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_PATH = DATA_DIR / "interactions.jsonl"
_last_cognee_error: str | None = None


def _cognee_ok() -> bool:
    """Cognee is usable only if importable AND configured (needs an LLM key)."""
    global _last_cognee_error
    if not COGNEE_AVAILABLE:
        _last_cognee_error = "cognee not installed"
        return False
    if not os.getenv("GEMINI_API_KEY"):
        _last_cognee_error = "no GEMINI_API_KEY for cognee"
        return False
    return True


def store(question: str, answer: str, trace: dict | list | None = None) -> None:
    """Persist one interaction. BASELINE: trace not stored."""
    try:
        if _cognee_ok():
            cognee.add(f"Q: {question}\nA: {answer}")
        else:
            _append_local(question, answer, trace)
    except Exception as e:
        global _last_cognee_error
        _last_cognee_error = str(e)
        _append_local(question, answer, trace)


def _append_local(question: str, answer: str, trace) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "question": question,
        "answer": answer,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def retrieve(query: str, limit: int = 3) -> list[dict]:
    """BASELINE: Cognee-only retrieval; falls back to empty on failure."""
    if _cognee_ok():
        try:
            results = cognee.search(query)
            out = []
            for r in results or []:
                text = str(getattr(r, "__str__", lambda: str(r))())
                out.append({"source": "cognee", "text": text[:600]})
            return out[:limit]
        except Exception as e:
            global _last_cognee_error
            _last_cognee_error = str(e)
    return []


def last_interactions(limit: int = 3) -> list[dict]:
    """Direct read of the local log, newest first."""
    if not LOG_PATH.exists():
        return []
    entries = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return list(reversed(entries))[:limit]


def reset_conversation() -> None:
    """Conversation reset only — persistent interaction memory survives."""
    LOG_PATH.unlink(missing_ok=True)


def status() -> dict:
    return {
        "cognee_available": _cognee_ok(),
        "last_cognee_error": _last_cognee_error,
        "local_log": str(LOG_PATH),
        "local_log_entries": (
            sum(1 for _ in LOG_PATH.open(encoding="utf-8")) if LOG_PATH.exists() else 0
        ),
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
