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
        main.conversation_history.clear()


class HealthContractTests(_ClientCase):
    def test_health_reports_voice_unavailable_without_key(self):
        data = self.client.get("/api/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["llm_available"])
        self.assertEqual(data["voice"], {"available": False, "languages": ["en", "hi"]})
        self.assertEqual(set(data["languages"]), {"en", "hi", "es", "fr"})

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
        with mock.patch.object(main.analytics, "build_merchant_context", side_effect=RuntimeError("no db")):
            res = self.client.get("/insights")
        self.assertEqual(res.status_code, 500)


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

    def test_reset_clears_conversation(self):
        main.conversation_history.append({"role": "user", "text": "x"})
        res = self.client.post("/reset")
        self.assertEqual(res.json(), {"ok": True})
        self.assertEqual(main.conversation_history, [])
        main.memory.reset_conversation.assert_called_once()


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

    def test_api_routes_are_not_shadowed_by_static_mount(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/context").headers["content-type"], "application/json")


if __name__ == "__main__":
    unittest.main()
