"""
SALAH — per-visitor conversation sessions (no login).

A random session id travels in an HttpOnly cookie. Conversation history is
kept per session, in memory, bounded in size and age, so one visitor's
questions never reach another visitor's model context, and /reset clears only
the caller's own conversation.

Capacity never costs an existing visitor their history: only expired
sessions are removed. When the store is full, or one client already holds
``max_per_client`` live sessions, a NEW session is refused (admit() says why)
and the caller is answered without memory instead.

On serverless each instance has its own memory: isolation holds, persistence
does not (a request served by another instance starts with an empty history),
and the per-client cap is counted per instance, not globally. Persistent,
globally enforced sessions would need a managed store.
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
MAX_PER_CLIENT = 20
SESSION_TTL_SECONDS = 2 * 3600
MAX_ITEMS = 20

ADMITTED = "ok"
CLIENT_LIMIT = "client_limit"  # this client already holds MAX_PER_CLIENT live sessions
CAPACITY = "capacity"          # the store holds MAX_SESSIONS live sessions

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

    Live sessions are never evicted for capacity. Sessions idle for longer
    than ``ttl`` seconds are dropped; each history keeps its last
    ``max_items`` turns; new sessions are refused (not made room for) when the
    store or the requesting client is at its limit."""

    def __init__(self, max_sessions: int = MAX_SESSIONS, ttl: float = SESSION_TTL_SECONDS,
                 max_items: int = MAX_ITEMS, max_per_client: int = MAX_PER_CLIENT, clock=time.monotonic):
        self.max_sessions = max_sessions
        self.ttl = ttl
        self.max_items = max_items
        self.max_per_client = max_per_client
        self._clock = clock
        self._lock = threading.Lock()
        # session id -> [last touched, client key, history]
        self._data: OrderedDict[str, list] = OrderedDict()

    def _prune_expired(self, now: float) -> None:
        for sid in [sid for sid, (touched, _, _) in self._data.items() if now - touched > self.ttl]:
            del self._data[sid]

    def admit(self, session_id: str, client: str) -> str:
        """Make sure the session exists. An existing session is always
        admitted (and refreshed); a new one only while both the store and the
        client are under their limits. Returns ADMITTED, CLIENT_LIMIT or
        CAPACITY. Expired sessions are cleared first."""
        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            entry = self._data.get(session_id)
            if entry is not None:
                entry[0] = now
                return ADMITTED
            if sum(1 for _, c, _ in self._data.values() if c == client) >= self.max_per_client:
                return CLIENT_LIMIT
            if len(self._data) >= self.max_sessions:
                return CAPACITY
            self._data[session_id] = [now, client, []]
            return ADMITTED

    def history(self, session_id: str) -> list[dict]:
        """A copy of the session's history (empty for unknown sessions)."""
        with self._lock:
            self._prune_expired(self._clock())
            entry = self._data.get(session_id)
            return [dict(item) for item in entry[2]] if entry else []

    def append(self, session_id: str, *items: dict) -> bool:
        """Append to an admitted session; False if it is not (or no longer)
        in the store. Never creates a session."""
        with self._lock:
            entry = self._data.get(session_id)
            if entry is None:
                return False
            entry[0] = self._clock()
            entry[2] = (entry[2] + [dict(i) for i in items])[-self.max_items:]
            return True

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            self._prune_expired(self._clock())
            return len(self._data)
