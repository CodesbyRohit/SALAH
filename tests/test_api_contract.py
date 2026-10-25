"""
API contract tests for the endpoints the frontend depends on.

Covers the additive fields the merchant workspace reads (``/insights``, the
``action``/``source``/``fallback_reason`` fields of ``/ask``, voice status in
``/api/health``), the honest-failure paths of the voice endpoints, and that the
production frontend is served with every asset it references.

No network: Gemini and Sarvam keys are forced off, and the memory layer is
patched so these tests never write to ``data/interactions.jsonl``.

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import analytics  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402

FRONTEND = ROOT / "frontend"

try:
    analytics.get_data_end()
    DB_READY = True
except Exception:  # pragma: no cover - depends on local DB
    DB_READY = False

NO_KEYS = {"GEMINI_API_KEY": "", "SARVAM_API_KEY": ""}


class _ClientCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        patches = [
            mock.patch.dict(os.environ, NO_KEYS),
            mock.patch.object(llm, "GEMINI_API_KEY", ""),
            mock.patch.object(main.memory, "store"),
            mock.patch.object(main.memory, "reset_conversation"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.conversations.clear_all()


class HealthContractTests(_ClientCase):
    def test_health_reports_voice_unavailable_without_key(self):
        data = self.client.get("/api/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["llm_available"])
        self.assertEqual(data["voice"], {"available": False, "languages": ["en", "hi"]})
        self.assertEqual(set(data["languages"]), {"en", "hi", "es", "fr"})

    def test_health_does_not_expose_filesystem_paths(self):
        memory = self.client.get("/api/health").json()["memory"]
        self.assertEqual(memory["local_log"], "interactions.jsonl")
        self.assertNotIn(str(ROOT), json.dumps(self.client.get("/api/health").json()))

    def test_health_reports_voice_available_with_key(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "test-key"}):
            data = self.client.get("/api/health").json()
        self.assertTrue(data["voice"]["available"])


@unittest.skipUnless(DB_READY, "data/merchant.db missing — run: python data/seed.py")
class InsightsContractTests(_ClientCase):
    def test_insights_match_the_deterministic_engine(self):
        res = self.client.get("/insights")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        ctx = analytics.build_merchant_context()
        self.assertEqual(data["generated_at"], ctx["generated_at"])
        self.assertEqual(data["trace"], analytics.build_trace(ctx))
        self.assertEqual(data["action"], analytics.get_recommended_action(ctx))

    def test_insights_confidence_levels_are_the_four_allowed(self):
        trace = self.client.get("/insights").json()["trace"]
        self.assertTrue(trace)
        for item in trace:
            self.assertIn(item["confidence"], llm.CONFIDENCE_LEVELS)

    def test_insights_failure_is_an_honest_500(self):
        with mock.patch.object(main.analytics, "build_merchant_context", side_effect=RuntimeError("no db")), \
                self.assertLogs("salah", level="ERROR"):
            res = self.client.get("/insights")
        self.assertEqual(res.status_code, 500)

    def test_analytics_failure_is_a_500_everywhere_without_internal_detail(self):
        # Before: /ask and /nudge answered from an empty context ("0 lapsed
        # regular customers"), and 500 bodies echoed the exception text.
        secret = "sqlite at /srv/private/merchant.db exploded"
        with mock.patch.object(main.analytics, "build_merchant_context", side_effect=RuntimeError(secret)):
            for method, path, body in (
                ("get", "/context", None),
                ("get", "/insights", None),
                ("post", "/ask", {"question": "kya karun?", "language": "en"}),
                ("post", "/nudge", {"force": True, "language": "en"}),
            ):
                with self.assertLogs("salah", level="ERROR"):
                    res = getattr(self.client, method)(path, json=body) if body else getattr(self.client, method)(path)
                self.assertEqual(res.status_code, 500, path)
                self.assertNotIn("exploded", res.text, path)
                self.assertNotIn("/srv/private", res.text, path)
        main.memory.store.assert_not_called()


class DeterministicRevenueWordingTests(unittest.TestCase):
    def test_average_uses_the_window_length_not_the_calendar_bucket_count(self):
        ctx = {
            "weekly_revenue": [{"week": "2024-W43", "revenue": 300.0}]
            + [{"week": f"2024-W{w}", "revenue": 925.0} for w in range(44, 52)],
            "weekday_profile": {"weeks": 8},
        }
        text = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language="en")
        self.assertIn("past 8 weeks is 7700.0 (average 962.5 per week)", text)
        self.assertNotIn("9 weeks", text)

    def test_without_a_window_length_no_average_is_invented(self):
        ctx = {"weekly_revenue": [{"week": "2024-W50", "revenue": 500.0}, {"week": "2024-W51", "revenue": 700.0}]}
        for lang in ("en", "hi", "es", "fr", "hinglish"):
            text = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language=lang)
            self.assertIn("1200.0", text, lang)
            self.assertNotIn("600.0", text, lang)

    @unittest.skipUnless(DB_READY, "data/merchant.db missing — run: python data/seed.py")
    def test_canonical_dataset_wording(self):
        text = llm.deterministic_explanation(analytics.build_merchant_context(), [], {"priority": "none"}, language="en")
        self.assertIn("Total revenue for the past 8 weeks is 7677.0 (average 959.62 per week).", text)


class GeminiTransportTests(unittest.TestCase):
    def test_api_key_is_sent_in_a_header_not_the_url(self):
        # Regression: the key was dropped entirely when the safety layer was
        # added, so every configured Gemini call failed and fell back.
        fake = mock.Mock()
        fake.json.return_value = {"candidates": []}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k-123"}),                 mock.patch.object(llm.requests, "post", return_value=fake) as post:
            llm._default_transport("https://example.invalid/model:generateContent", {"x": 1}, 5)
        url = post.call_args.args[0]
        self.assertNotIn("k-123", url)
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "k-123")
        fake.raise_for_status.assert_called_once()


@unittest.skipUnless(DB_READY, "data/merchant.db missing — run: python data/seed.py")
class AskContractTests(_ClientCase):
    def test_ask_exposes_action_and_deterministic_source(self):
        res = self.client.post("/ask", json={"question": "what should I do next?", "language": "en"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["answer"])
        self.assertEqual(data["source"], "deterministic")
        self.assertEqual(data["fallback_reason"], "llm_unavailable")
        self.assertEqual(data["language"], "en")
        expected = analytics.get_recommended_action(analytics.build_merchant_context())
        self.assertEqual(data["action"]["priority"], expected["priority"])
        self.assertIsInstance(data["trace"], list)

    def test_ask_unknown_language_falls_back_to_english(self):
        data = self.client.post("/ask", json={"question": "hello", "language": "xx"}).json()
        self.assertEqual(data["language"], "en")

    def test_ask_rejects_empty_question(self):
        res = self.client.post("/ask", json={"question": "   ", "language": "en"})
        self.assertEqual(res.status_code, 400)

    def test_ask_reports_validated_llm_source(self):
        ctx = analytics.build_merchant_context()
        action = analytics.get_recommended_action(ctx)
        reply = {
            "answer": "Salah ka sujhav: ek controlled test karke dekhiye.",
            "facts_used": ["lapsed_regulars"],
            "priority": action["priority"],
            "caveats": [],
        }
        fake = {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch.object(llm, "_default_transport", return_value=fake):
            data = self.client.post("/ask", json={"question": "kya karun?", "language": "hi"}).json()
        self.assertEqual(data["source"], "llm")
        self.assertIsNone(data["fallback_reason"])
        self.assertEqual(data["answer"], reply["answer"])

    def test_ask_reports_rejected_llm_output(self):
        ctx = analytics.build_merchant_context()
        action = analytics.get_recommended_action(ctx)
        reply = {
            "answer": "Your staff is late, so revenue fell by 99999.",
            "facts_used": [],
            "priority": action["priority"],
        }
        fake = {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), \
                mock.patch.object(llm, "_default_transport", return_value=fake):
            data = self.client.post("/ask", json={"question": "why?", "language": "en"}).json()
        self.assertEqual(data["source"], "deterministic")
        self.assertTrue(data["fallback_reason"].startswith("rejected:"))
        self.assertNotIn("99999", data["answer"])


class VoiceAndResetContractTests(_ClientCase):
    def test_stt_rejects_empty_audio(self):
        res = self.client.post("/voice/stt", files={"file": ("a.webm", b"", "audio/webm")})
        self.assertEqual(res.status_code, 400)

    def test_stt_without_key_is_unavailable(self):
        res = self.client.post("/voice/stt", files={"file": ("a.webm", b"\x1aE\xdf\xa3", "audio/webm")})
        self.assertEqual(res.status_code, 503)

    def test_tts_without_key_is_unavailable(self):
        res = self.client.post("/voice/tts", json={"text": "hello", "language": "en"})
        self.assertEqual(res.status_code, 503)

    def test_tts_rejects_empty_text(self):
        res = self.client.post("/voice/tts", json={"text": " ", "language": "en"})
        self.assertEqual(res.status_code, 400)

    # Voice is forwarded to a paid API: oversized or non-audio requests must
    # be refused before any provider call, even when a key is configured.
    def test_stt_rejects_non_audio_uploads_before_calling_the_provider(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "k"}), \
                mock.patch.object(main.requests, "post") as post:
            res = self.client.post("/voice/stt", files={"file": ("a.txt", b"hello", "text/plain")})
        self.assertEqual(res.status_code, 415)
        post.assert_not_called()

    def test_stt_rejects_oversized_audio_before_calling_the_provider(self):
        big = b"\0" * (main.MAX_AUDIO_BYTES + 1)
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "k"}), \
                mock.patch.object(main.requests, "post") as post:
            res = self.client.post("/voice/stt", files={"file": ("a.webm", big, "audio/webm")})
        self.assertEqual(res.status_code, 413)
        post.assert_not_called()

    def test_tts_rejects_overlong_text_before_calling_the_provider(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "k"}), \
                mock.patch.object(main.requests, "post") as post:
            res = self.client.post("/voice/tts", json={"text": "a" * (main.MAX_TTS_CHARS + 1), "language": "en"})
        self.assertEqual(res.status_code, 413)
        post.assert_not_called()

    def test_cross_origin_requests_get_no_cors_grant_by_default(self):
        res = self.client.get("/api/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("access-control-allow-origin", res.headers)
        pre = self.client.options("/ask", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", pre.headers)

    def test_reset_clears_only_the_callers_conversation(self):
        self.client.post("/ask", json={"question": "kya karun?", "language": "en"})
        sid = self.client.cookies.get(main.sessions.COOKIE_NAME)
        main.conversations.admit("someone-elses-session-id-0123456789ab", "another-client")
        main.conversations.append("someone-elses-session-id-0123456789ab", {"role": "user", "text": "theirs"})
        res = self.client.post("/reset")
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(main.conversations.history(sid), [])
        self.assertEqual(main.conversations.history("someone-elses-session-id-0123456789ab"),
                         [{"role": "user", "text": "theirs"}])
        main.memory.reset_conversation.assert_called_once_with(session=main.sessions.log_key(sid))


class FrontendServingTests(_ClientCase):
    LOCAL_REF_RE = re.compile(r'(?:src|href)="(?!https?:|#|data:)([^"]+)"')
    IMPORT_RE = re.compile(r'(?:import|from)\s*\(?\s*["\'](\.{1,2}/[^"\']+)["\']')

    def test_index_is_served_with_its_entry_module(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn('type="module"', res.text)
        self.assertIn("synthetic", res.text.lower())

    def test_every_local_asset_in_index_is_served(self):
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        refs = set(self.LOCAL_REF_RE.findall(html))
        self.assertTrue(refs)
        for ref in refs:
            res = self.client.get("/" + ref.lstrip("./"))
            self.assertEqual(res.status_code, 200, ref)

    def test_every_relative_js_import_resolves(self):
        for path in (FRONTEND / "js").rglob("*.js"):
            src = path.read_text(encoding="utf-8")
            for ref in self.IMPORT_RE.findall(src):
                target = (path.parent / ref).resolve()
                self.assertTrue(target.is_file(), f"{path.name} imports missing {ref}")

    def test_vendored_three_is_gzip_compressed(self):
        res = self.client.get("/vendor/three/three.module.min.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("content-encoding"), "gzip")

    def test_static_files_are_edge_cacheable_but_api_responses_are_not(self):
        for path in ("/", "/js/app.js", "/vendor/three/three.module.min.js"):
            cc = self.client.get(path).headers.get("cache-control", "")
            self.assertIn("s-maxage=", cc, path)
            self.assertIn("max-age=0", cc, path)  # browsers still revalidate
        for path in ("/api/health", "/context", "/insights"):
            self.assertNotIn("s-maxage", self.client.get(path).headers.get("cache-control", ""), path)
        self.assertNotIn("s-maxage", self.client.get("/no-such-file.js").headers.get("cache-control", ""))

    def test_api_routes_are_not_shadowed_by_static_mount(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/context").headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
