"""
Per-visitor isolation: conversation history, model context, /reset and the
local interaction log are scoped to the caller's session cookie.

No network: the Gemini transport is replaced by a recorder, so the tests see
exactly what each visitor's model request would contain.

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import llm  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import sessions  # noqa: E402


class SessionStoreTests(unittest.TestCase):
    def test_histories_are_separate_and_bounded(self):
        store = sessions.SessionStore(max_items=4)
        store.admit("a", "ip-a")
        store.admit("b", "ip-b")
        store.append("a", {"text": "a1"}, {"text": "a2"})
        store.append("b", {"text": "b1"})
        self.assertEqual([i["text"] for i in store.history("a")], ["a1", "a2"])
        self.assertEqual([i["text"] for i in store.history("b")], ["b1"])
        store.append("a", *({"text": f"x{i}"} for i in range(5)))
        self.assertEqual(len(store.history("a")), 4)

    def test_history_is_a_copy(self):
        store = sessions.SessionStore()
        store.admit("a", "ip-a")
        store.append("a", {"text": "a1"})
        store.history("a")[0]["text"] = "tampered"
        self.assertEqual(store.history("a"), [{"text": "a1"}])

    def test_clear_touches_one_session(self):
        store = sessions.SessionStore()
        store.admit("a", "ip-a")
        store.admit("b", "ip-b")
        store.append("a", {"text": "a1"})
        store.append("b", {"text": "b1"})
        store.clear("b")
        self.assertEqual(store.history("a"), [{"text": "a1"}])
        self.assertEqual(store.history("b"), [])

    def test_a_flood_of_new_sessions_never_erases_an_active_history(self):
        # The exact failure found in review: 1,000 new sessions, each from a
        # different client, used to evict an active visitor.
        store = sessions.SessionStore(max_sessions=1000)
        self.assertEqual(store.admit("active", "client-0"), sessions.ADMITTED)
        store.append("active", {"text": "my earlier question"})
        outcomes = [store.admit(f"flood-{i}", f"ip-{i}") for i in range(1000)]
        self.assertEqual(outcomes.count(sessions.ADMITTED), 999)  # until the store is full
        self.assertEqual(outcomes.count(sessions.CAPACITY), 1)     # then refused, not made room for
        self.assertEqual(store.history("active"), [{"text": "my earlier question"}])
        self.assertEqual(len(store), 1000)

    def test_existing_sessions_keep_working_at_capacity(self):
        store = sessions.SessionStore(max_sessions=2)
        store.admit("a", "ip-a")
        store.admit("b", "ip-b")
        self.assertEqual(store.admit("c", "ip-c"), sessions.CAPACITY)
        self.assertEqual(store.admit("a", "ip-a"), sessions.ADMITTED)
        self.assertTrue(store.append("a", {"text": "still remembered"}))
        self.assertEqual(store.history("a"), [{"text": "still remembered"}])
        self.assertFalse(store.append("c", {"text": "x"}))  # append never creates a session
        self.assertEqual(store.history("c"), [])

    def test_one_client_cannot_hold_more_than_its_share(self):
        store = sessions.SessionStore(max_sessions=1000, max_per_client=20)
        results = [store.admit(f"s{i}", "same-ip") for i in range(25)]
        self.assertEqual(results.count(sessions.ADMITTED), 20)
        self.assertEqual(results[20:], [sessions.CLIENT_LIMIT] * 5)
        self.assertEqual(store.admit("other", "other-ip"), sessions.ADMITTED)  # others unaffected

    def test_expired_sessions_free_capacity_before_anything_is_refused(self):
        now = [0.0]
        store = sessions.SessionStore(max_sessions=2, ttl=60, clock=lambda: now[0])
        store.admit("old-1", "ip-1")
        store.admit("old-2", "ip-2")
        self.assertEqual(store.admit("new", "ip-3"), sessions.CAPACITY)
        now[0] += 61
        self.assertEqual(store.admit("new", "ip-3"), sessions.ADMITTED)
        self.assertEqual(len(store), 1)

    def test_idle_sessions_expire(self):
        now = [1000.0]
        store = sessions.SessionStore(ttl=60, clock=lambda: now[0])
        store.admit("a", "ip-a")
        store.append("a", {"text": "a"})
        now[0] += 61
        self.assertEqual(store.history("a"), [])
        self.assertEqual(len(store), 0)

    def test_concurrent_appends_never_cross_sessions(self):
        store = sessions.SessionStore(max_items=1000)

        def worker(sid):
            store.admit(sid, sid)
            for i in range(200):
                store.append(sid, {"sid": sid, "i": i})

        threads = [threading.Thread(target=worker, args=(f"s{n}",)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for n in range(8):
            items = store.history(f"s{n}")
            self.assertEqual(len(items), 200)
            self.assertEqual({i["sid"] for i in items}, {f"s{n}"})

    def test_ids_are_random_and_validated(self):
        a, b = sessions.new_id(), sessions.new_id()
        self.assertNotEqual(a, b)
        self.assertTrue(sessions.valid(a))
        for bad in (None, "", "short", "x" * 65, "a" * 31, "has spaces in it 0123456789abcdef", "a" * 31 + ";"):
            self.assertFalse(sessions.valid(bad), bad)
        self.assertNotIn(a, sessions.log_key(a))
        self.assertEqual(len(sessions.log_key(a)), 16)


class VisitorIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "interactions.jsonl"
        self.payloads: list[str] = []

        def record(url, payload, timeout):
            self.payloads.append(json.dumps(payload, ensure_ascii=False))
            return {"candidates": []}  # empty reply -> deterministic fallback

        for p in (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "SARVAM_API_KEY": ""}),
            mock.patch.object(llm, "GEMINI_API_KEY", "test-key"),
            mock.patch.object(llm, "_default_transport", record),
            mock.patch.object(memory, "LOG_PATH", self.log),
            mock.patch.object(memory, "_cognee_ok", return_value=False),
        ):
            p.start()
            self.addCleanup(p.stop)
        main.conversations.clear_all()
        self.alice = TestClient(main.app)
        self.bob = TestClient(main.app)

    def ask(self, client, question):
        res = client.post("/ask", json={"question": question, "language": "en"})
        self.assertEqual(res.status_code, 200, res.text)
        return self.payloads[-1]

    def log_entries(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_session_cookie_is_issued_once_with_safe_attributes(self):
        res = self.alice.post("/ask", json={"question": "kya karun?", "language": "en"})
        cookie = res.headers.get("set-cookie", "")
        self.assertIn(f"{sessions.COOKIE_NAME}=", cookie)
        for attr in ("HttpOnly", "Path=/", "SameSite=lax", "Max-Age="):
            self.assertIn(attr, cookie)
        self.assertNotIn("Secure", cookie)  # plain http test client
        again = self.alice.post("/ask", json={"question": "kya karun?", "language": "en"})
        self.assertNotIn("set-cookie", again.headers)  # valid cookie is kept

    def test_cookie_is_secure_behind_an_https_proxy(self):
        res = self.alice.post("/ask", json={"question": "kya karun?", "language": "en"},
                              headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", res.headers.get("set-cookie", ""))

    def test_malformed_cookie_is_replaced(self):
        client = TestClient(main.app, cookies={sessions.COOKIE_NAME: "forged;value"})
        res = client.post("/ask", json={"question": "kya karun?", "language": "en"})
        self.assertIn(f"{sessions.COOKIE_NAME}=", res.headers.get("set-cookie", ""))
        self.assertNotIn("forged", res.headers.get("set-cookie", ""))

    def test_one_visitors_questions_never_reach_another_visitors_model_context(self):
        self.ask(self.alice, "alpha private question about my shop")
        bob_payload = self.ask(self.bob, "beta question")
        self.assertNotIn("alpha private question", bob_payload)
        alice_payload = self.ask(self.alice, "follow-up")
        self.assertIn("alpha private question", alice_payload)  # her own history still works
        self.assertNotIn("beta question", alice_payload)

    def test_reset_by_one_visitor_leaves_the_other_untouched(self):
        self.ask(self.alice, "alpha private question")
        self.ask(self.bob, "beta question")
        self.assertEqual(self.bob.post("/reset").json(), {"ok": True})
        self.assertIn("alpha private question", self.ask(self.alice, "follow-up"))
        self.assertNotIn("beta question", self.ask(self.bob, "after reset"))

    def test_log_is_tagged_pseudonymously_and_reset_removes_only_the_callers_entries(self):
        self.ask(self.alice, "alpha one")
        self.ask(self.alice, "alpha two")
        self.ask(self.bob, "beta one")
        alice_sid = self.alice.cookies.get(sessions.COOKIE_NAME)
        bob_sid = self.bob.cookies.get(sessions.COOKIE_NAME)
        raw = self.log.read_text(encoding="utf-8")
        self.assertNotIn(alice_sid, raw)  # the cookie value (a bearer token) is never logged
        self.assertNotIn(bob_sid, raw)
        self.assertEqual({e["session"] for e in self.log_entries()},
                         {sessions.log_key(alice_sid), sessions.log_key(bob_sid)})
        self.bob.post("/reset")
        self.assertEqual([e["question"] for e in self.log_entries()], ["alpha one", "alpha two"])
        self.assertEqual(memory.last_interactions(10, session=sessions.log_key(bob_sid)), [])
        self.assertEqual(len(memory.last_interactions(10, session=sessions.log_key(alice_sid))), 2)

    def test_reset_without_a_session_removes_nothing(self):
        self.ask(self.alice, "alpha one")
        stranger = TestClient(main.app)
        self.assertEqual(stranger.post("/reset").json(), {"ok": True})
        self.assertEqual([e["question"] for e in self.log_entries()], ["alpha one"])
        self.assertIn("alpha one", self.ask(self.alice, "follow-up"))

    def test_reset_keeps_unparseable_and_legacy_lines(self):
        self.log.write_text('{"ts": "old", "question": "legacy, no session"}\nnot json\n', encoding="utf-8")
        self.ask(self.bob, "beta one")
        self.bob.post("/reset")
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ['{"ts": "old", "question": "legacy, no session"}', "not json"])


class CapacityOverApiTests(unittest.TestCase):
    """The flood scenario end to end: tiny limits, distinct client addresses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payloads: list[str] = []

        def record(url, payload, timeout):
            self.payloads.append(json.dumps(payload, ensure_ascii=False))
            return {"candidates": []}

        self.store = sessions.SessionStore(max_sessions=5, max_per_client=2, max_items=20)
        for p in (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "SARVAM_API_KEY": ""}),
            mock.patch.object(llm, "GEMINI_API_KEY", "test-key"),
            mock.patch.object(llm, "_default_transport", record),
            mock.patch.object(memory, "LOG_PATH", Path(self.tmp.name) / "interactions.jsonl"),
            mock.patch.object(memory, "_cognee_ok", return_value=False),
            mock.patch.object(main, "conversations", self.store),
            mock.patch.object(main, "trusted_proxy", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def ask(self, client, question, ip):
        res = client.post("/ask", json={"question": question, "language": "en"}, headers={"X-Real-IP": ip})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_flood_from_many_addresses_leaves_the_active_visitor_intact(self):
        alice = TestClient(main.app)
        self.assertTrue(self.ask(alice, "alpha private question", "10.0.0.1")["remembered"])
        flood = [self.ask(TestClient(main.app), f"flood {i}", f"10.9.0.{i}") for i in range(10)]
        self.assertEqual([r["remembered"] for r in flood], [True] * 4 + [False] * 6)
        self.assertTrue(all(r["answer"] for r in flood))  # memory refused, never the answer
        follow = self.ask(alice, "follow-up", "10.0.0.1")
        self.assertTrue(follow["remembered"])
        self.assertIn("alpha private question", self.payloads[-1])

    def test_one_address_cannot_exhaust_the_store(self):
        results = [self.ask(TestClient(main.app), f"q{i}", "10.1.1.1")["remembered"] for i in range(4)]
        self.assertEqual(results, [True, True, False, False])
        self.assertTrue(self.ask(TestClient(main.app), "someone else", "10.2.2.2")["remembered"])

    def test_unremembered_answers_carry_no_history_and_store_none(self):
        for i in range(5):
            self.ask(TestClient(main.app), f"fill {i}", f"10.3.0.{i}")
        late = TestClient(main.app)
        first = self.ask(late, "late question one", "10.4.0.1")
        self.assertFalse(first["remembered"])
        self.ask(late, "late question two", "10.4.0.1")
        self.assertNotIn("late question one", self.payloads[-1])
        self.assertEqual(self.store.history(late.cookies.get(sessions.COOKIE_NAME)), [])

    def test_client_address_headers_are_ignored_off_vercel(self):
        with mock.patch.object(main, "trusted_proxy", return_value=False):
            results = [TestClient(main.app).post("/ask", json={"question": "q", "language": "en"},
                                                 headers={"X-Real-IP": f"10.5.0.{i}"}).json()["remembered"]
                       for i in range(3)]
        self.assertEqual(results, [True, True, False])  # spoofed addresses buy no extra sessions


if __name__ == "__main__":
    unittest.main()
