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
import threading
from datetime import datetime

import database

try:
    import cognee  # type: ignore
    COGNEE_AVAILABLE = True
except Exception:
    cognee = None
    COGNEE_AVAILABLE = False

# Same resolved data directory as the database: data/ locally, /tmp/salah on
# serverless (the deployment bundle is read-only).
DATA_DIR = database.data_dir()
LOG_PATH = DATA_DIR / "interactions.jsonl"
_last_cognee_error: str | None = None
# Serialises appends and the reset rewrite within this process.
_log_lock = threading.Lock()


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


def store(question: str, answer: str, trace: dict | list | None = None,
          session: str | None = None) -> None:
    """Persist one interaction. BASELINE: trace not stored.

    ``session`` is a pseudonymous key (sessions.log_key), never the cookie
    value. The Cognee path is not session-scoped; it is unused unless cognee
    is installed and configured."""
    try:
        if _cognee_ok():
            cognee.add(f"Q: {question}\nA: {answer}")
        else:
            _append_local(question, answer, trace, session)
    except Exception as e:
        global _last_cognee_error
        _last_cognee_error = str(e)
        _append_local(question, answer, trace, session)


def _append_local(question: str, answer: str, trace, session: str | None = None) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "session": session,
        "question": question,
        "answer": answer,
    }
    with _log_lock, LOG_PATH.open("a", encoding="utf-8") as f:
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


def last_interactions(limit: int = 3, session: str | None = None) -> list[dict]:
    """Direct read of the local log, newest first; only ``session``'s entries
    when a session key is given."""
    if not LOG_PATH.exists():
        return []
    entries = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session is None or (isinstance(entry, dict) and entry.get("session") == session):
                    entries.append(entry)
    return list(reversed(entries))[:limit]


def reset_conversation(session: str) -> int:
    """Remove one session's entries from the local log; other visitors'
    entries are kept. Rewrites atomically. Returns how many were removed."""
    with _log_lock:
        if not LOG_PATH.exists():
            return 0
        kept, removed = [], 0
        with LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)  # not ours to judge: keep unparseable lines
                    continue
                if isinstance(entry, dict) and entry.get("session") == session:
                    removed += 1
                else:
                    kept.append(line)
        if removed:
            tmp = LOG_PATH.with_name(f".{LOG_PATH.name}.{os.getpid()}.tmp")
            tmp.write_text("".join(kept), encoding="utf-8")
            os.replace(tmp, LOG_PATH)
        return removed


def _count_local_log_entries() -> int:
    if not LOG_PATH.exists():
        return 0
    with LOG_PATH.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def status() -> dict:
    return {
        "cognee_available": _cognee_ok(),
        "last_cognee_error": _last_cognee_error,
        "local_log": LOG_PATH.name,  # file name only: no filesystem layout in /api/health
        "local_log_entries": _count_local_log_entries(),
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
