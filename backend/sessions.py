"""
SALAH — per-visitor conversation sessions (no login).

A random session id travels in an HttpOnly cookie. Conversation history is
kept per session, in memory, bounded in size and age, so one visitor's
questions never reach another visitor's model context, and /reset clears only
the caller's own conversation.

On serverless each instance has its own memory: isolation holds, persistence
does not (a request served by another instance starts with an empty history).
Persistent history would need a managed store.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import OrderedDict

COOKIE_NAME = "salah_sid"
COOKIE_MAX_AGE = 7 * 24 * 3600
MAX_SESSIONS = 1000
SESSION_TTL_SECONDS = 2 * 3600
MAX_ITEMS = 20

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def new_id() -> str:
    return secrets.token_urlsafe(24)  # 32 URL-safe characters, 192 bits


def valid(session_id: str | None) -> bool:
    return isinstance(session_id, str) and bool(_ID_RE.match(session_id))


def log_key(session_id: str) -> str:
    """Pseudonymous key for logs: lets a session's entries be found and
    removed without writing the cookie value (a bearer token) to disk."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


class SessionStore:
    """Bounded, thread-safe per-session conversation histories.

    Least-recently-used sessions are evicted beyond ``max_sessions``; sessions
    idle for longer than ``ttl`` seconds are dropped; each history keeps its
    last ``max_items`` turns."""

    def __init__(self, max_sessions: int = MAX_SESSIONS, ttl: float = SESSION_TTL_SECONDS,
                 max_items: int = MAX_ITEMS, clock=time.monotonic):
        self.max_sessions = max_sessions
        self.ttl = ttl
        self.max_items = max_items
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [sid for sid, (touched, _) in self._data.items() if now - touched > self.ttl]
        for sid in expired:
            del self._data[sid]
        while len(self._data) > self.max_sessions:
            self._data.popitem(last=False)

    def history(self, session_id: str) -> list[dict]:
        """A copy of the session's history (empty for unknown sessions)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            entry = self._data.get(session_id)
            return [dict(item) for item in entry[1]] if entry else []

    def append(self, session_id: str, *items: dict) -> None:
        with self._lock:
            now = self._clock()
            _, history = self._data.pop(session_id, (now, []))
            history = (history + [dict(i) for i in items])[-self.max_items:]
            self._data[session_id] = (now, history)  # most recently used last
            self._prune(now)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._data)
