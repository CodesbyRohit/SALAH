"""
Unit tests for SALAH Multilingual capabilities.
"""
import os
import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import main
import cache as demo_cache
import llm


class TestMultilingual(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")

    def test_ask_request_model(self):
        req = main.AskRequest(question="test question")
        self.assertEqual(req.language, "en")

        req_es = main.AskRequest(question="test question", language="es")
        self.assertEqual(req_es.language, "es")

    def test_cache_bypassed_for_non_english(self):
        demo_cache.put_cached("is hafte kitna kamaya", "Cached English Answer")
        
        hit_en = demo_cache.get_cached("is hafte kitna kamaya", language="en")
        self.assertIsNotNone(hit_en)
        self.assertEqual(hit_en["answer"], "Cached English Answer")

        hit_es = demo_cache.get_cached("is hafte kitna kamaya", language="es")
        self.assertIsNone(hit_es)

        hit_fr = demo_cache.get_cached("is hafte kitna kamaya", language="fr")
        self.assertIsNone(hit_fr)

        hit_hi = demo_cache.get_cached("is hafte kitna kamaya", language="hi")
        self.assertIsNone(hit_hi)

    def test_deterministic_explanation_multilingual(self):
        ctx = {
            "weekly_revenue": [{"week": "2024-W51", "revenue": 1000.0}],
            "weakest_weekday": {
                "weekday": "Tuesday",
                "avg_daily_revenue": 46.62,
                "overall_avg_daily_revenue": 137.09,
            },
            "lapsed_regulars": {"count": 2},
            "ticket_trend": {"early_avg_ticket": 35.0, "recent_avg_ticket": 30.0, "change_pct": -14.3},
            "evening_share": {"evening_share_pct": 38.3},
        }

        exp_en = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language="en")
        self.assertIn("Total revenue", exp_en)

        exp_es = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language="es")
        self.assertIn("El ingreso total", exp_es)

        exp_fr = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language="fr")
        self.assertIn("Le revenu total", exp_fr)

        exp_hi = llm.deterministic_explanation(ctx, [], {"priority": "none"}, language="hi")
        self.assertIn("कुल कमाई", exp_hi)

    def test_build_llm_payload_includes_language(self):
        ctx = {"weekly_revenue": [{"week": "2024-W51", "revenue": 1000.0}]}
        payload_es = llm.build_llm_payload(ctx, [], {"priority": "none"}, question="hola", language="es")
        user_text = payload_es["contents"][-1]["parts"][0]["text"]
        self.assertIn("TARGET_LANGUAGE: Answer in Spanish (es)", user_text)

        payload_fr = llm.build_llm_payload(ctx, [], {"priority": "none"}, question="bonjour", language="fr")
        user_text_fr = payload_fr["contents"][-1]["parts"][0]["text"]
        self.assertIn("TARGET_LANGUAGE: Answer in French (fr)", user_text_fr)

    def test_ask_endpoint_multilingual(self):
        res_en = self.client.post("/ask", json={"question": "is hafte kitna kamaya", "language": "en"})
        self.assertEqual(res_en.status_code, 200)
        self.assertIn("answer", res_en.json())

        res_es = self.client.post("/ask", json={"question": "¿cuánto gané esta semana?", "language": "es"})
        self.assertEqual(res_es.status_code, 200)
        self.assertIn("answer", res_es.json())

        res_fr = self.client.post("/ask", json={"question": "combien ai-je gagné cette semaine ?", "language": "fr"})
        self.assertEqual(res_fr.status_code, 200)
        self.assertIn("answer", res_fr.json())

        res_hi = self.client.post("/ask", json={"question": "इस हफ्ते कितना कमाया?", "language": "hi"})
        self.assertEqual(res_hi.status_code, 200)
        self.assertIn("answer", res_hi.json())

    def test_voice_tts_unsupported_language_graceful_fallback(self):
        res = self.client.post("/voice/tts", json={"text": "Hola", "language": "es"})
        # Sarvam doesn't support Spanish, so returns 503 HTTP status
        self.assertEqual(res.status_code, 503)


if __name__ == "__main__":
    unittest.main()
