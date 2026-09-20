"""
Phase 1 / Phase 2 regression tests — the deterministic engine is untouched by
Phase 3 and must keep behaving exactly as verified in those phases.

These tests read the seeded SQLite database. If it is missing they are SKIPPED
with a clear message rather than failing:

    python data/seed.py

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import analytics  # noqa: E402
import llm  # noqa: E402

ANALYTICS_SRC = Path(analytics.__file__).read_text(encoding="utf-8")

try:
    analytics.get_data_end()
    DB_READY = True
except Exception as exc:  # pragma: no cover - depends on local DB
    DB_READY = False
    DB_ERROR = str(exc)

CONTEXT_KEYS = {
    "generated_at", "weekly_revenue", "revenue_by_hour", "weakest_weekday",
    "lapsed_regulars", "ticket_trend", "evening_share", "weekday_profile",
}
NUM_RE = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?")
CUSTOMER_ID_LITERAL_RE = re.compile(r"\bC\d{2,}\b")


def numbers(text: str) -> set[float]:
    out = set()
    for token in NUM_RE.findall(text):
        try:
            out.add(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


@unittest.skipUnless(DB_READY, "data/merchant.db missing — run: python data/seed.py")
class DeterministicEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = analytics.build_merchant_context()
        cls.trace = analytics.build_trace(cls.context)
        cls.action = analytics.get_recommended_action(cls.context)

    # --- Phase 1 ---------------------------------------------------------
    def test_context_exposes_the_declared_keys(self):
        self.assertTrue(CONTEXT_KEYS.issubset(set(self.context)))

    def test_generated_at_is_the_data_end_not_the_clock(self):
        self.assertEqual(
            self.context["generated_at"], analytics.get_data_end().isoformat()
        )

    # --- Phase 2: determinism -------------------------------------------
    def test_trace_is_deterministic(self):
        self.assertEqual(analytics.build_trace(self.context), self.trace)

    def test_action_is_deterministic(self):
        self.assertEqual(analytics.get_recommended_action(self.context), self.action)

    # --- Phase 2: trace structure ---------------------------------------
    def test_every_trace_item_has_the_five_fields(self):
        self.assertTrue(self.trace)
        for item in self.trace:
            self.assertEqual(
                set(item), {"signal", "value", "implication", "source", "confidence"}
            )
            self.assertIn(item["confidence"], llm.CONFIDENCE_LEVELS)

    def test_trace_sources_are_real_context_signals(self):
        signals = set(self.context) | {
            "weak_weekday_level", "weak_weekday_recurrence",
            "weak_weekday_transaction_count", "weak_weekday_cause", "lapse_reason",
            "ticket_trend_driver", "lapsed_regulars",
        }
        for item in self.trace:
            self.assertIn(item["source"], signals)

    def test_every_trace_number_is_present_in_context_or_a_declared_derivation(self):
        permitted = numbers(json.dumps(self.context, ensure_ascii=False))
        permitted |= {round(v) for v in permitted} | {round(v, 1) for v in permitted}

        weekly = [w["revenue"] for w in self.context["weekly_revenue"]]
        total = round(sum(weekly), 2)
        permitted |= {total, round(total / len(weekly), 2)}

        ww = self.context["weakest_weekday"]
        permitted.add(round(ww["avg_daily_revenue"] / ww["overall_avg_daily_revenue"] * 100, 1))

        for item in self.trace:
            for value in (item["value"], item["implication"]):
                for number in numbers(str(value)):
                    self.assertIn(
                        number, permitted,
                        f"{item['signal']} quoted {number}, absent from context",
                    )

    # --- Phase 2: evidence honesty --------------------------------------
    def test_no_fact_item_asserts_a_cause(self):
        facts = [t for t in self.trace if t["confidence"] == "FACT"]
        self.assertTrue(facts)
        for item in facts:
            text = f"{item['value']} {item['implication']}"
            self.assertIsNone(llm._ASSERTIVE_CAUSE_RE.search(text), item["signal"])
            self.assertIsNone(llm._REASON_MARKER_RE.search(text), item["signal"])

    def test_unknown_items_are_the_unanswerable_questions(self):
        unknowns = [t for t in self.trace if t["confidence"] == "UNKNOWN"]
        self.assertTrue(unknowns)
        for item in unknowns:
            self.assertIn("not determinable", str(item["value"]))

    def test_hypotheses_are_framed_as_possible(self):
        for item in self.trace:
            if item["confidence"] == "HYPOTHESIS":
                self.assertIn("possible", item["implication"].lower())

    # --- Phase 2: action engine -----------------------------------------
    def test_action_has_the_declared_fields(self):
        self.assertTrue(set(llm.ACTION_FIELDS).issubset(set(self.action)))

    def test_action_priority_is_justified_by_the_context(self):
        """The priority must follow from the data, not from seed knowledge."""
        priority = self.action["priority"]
        lapsed = self.context["lapsed_regulars"]["count"]
        tt = self.context["ticket_trend"]["change_pct"]
        ww = self.context["weakest_weekday"]
        weak = self.context["weekday_profile"]["days"].get(ww["weekday"], {})
        gap = weak.get("avg_gap_on_below_days_pct", 0.0)
        recurrence = (weak.get("below_avg_days", 0) / weak["days_in_window"]
                      if weak.get("days_in_window") else 0.0)
        gate = gap >= analytics.WEAK_WEEKDAY_MAX_GAP_PCT and \
            recurrence >= analytics.WEAK_WEEKDAY_MIN_RECURRENCE

        if priority == "lapsed_regulars":
            self.assertGreater(lapsed, 0)
        elif priority == "weak_weekday":
            self.assertEqual(lapsed, 0)
            self.assertTrue(gate)
        elif priority == "ticket_decline":
            self.assertEqual(lapsed, 0)
            self.assertFalse(gate)
            self.assertLessEqual(tt, -analytics.TICKET_DECLINE_MIN_PCT)
        else:
            self.assertEqual(priority, "none")
            self.assertEqual(lapsed, 0)
            self.assertFalse(gate)
            self.assertGreater(tt, -analytics.TICKET_DECLINE_MIN_PCT)

    def test_action_basis_names_a_context_signal(self):
        self.assertTrue(any(signal in self.action["basis"] for signal in
                            ("lapsed_regulars", "weakest_weekday", "weekday_profile",
                             "ticket_trend", "all signals")))

    def test_lapsed_count_matches_the_customer_list(self):
        lapsed = self.context["lapsed_regulars"]
        self.assertEqual(lapsed["count"], len(lapsed["customers"]))

    # --- Phase 2: no planted-pattern knowledge --------------------------
    def test_analytics_contains_no_hardcoded_customer_ids(self):
        self.assertEqual(CUSTOMER_ID_LITERAL_RE.findall(ANALYTICS_SRC), [])

    def test_analytics_contains_no_weekday_name_literals_in_the_action_engine(self):
        """build_trace/get_recommended_action must not name a planted weekday."""
        body = ANALYTICS_SRC.split("def get_recommended_action")[1].split("def build_merchant_context")[0]
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
            self.assertNotIn(f'"{day}"', body)

    # --- Phase 3 must not have broken the engine ------------------------
    def test_llm_layer_reads_the_live_context_without_mutating_it(self):
        before = json.dumps(self.context, sort_keys=True, default=str)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
                mock.patch.object(llm, "GEMINI_API_KEY", ""):
            out = llm.explain_recommendation("kya karun?", self.context)
        after = json.dumps(self.context, sort_keys=True, default=str)
        self.assertEqual(before, after)
        self.assertEqual(out["source"], "deterministic")
        self.assertEqual(out["action"]["priority"], self.action["priority"])


if __name__ == "__main__":
    if not DB_READY:
        print(f"database not ready: {DB_ERROR}")
    unittest.main(verbosity=2)
