"""
Every displayed metric, recomputed independently from raw SQL.

analytics.py is the source of truth for the product; these tests restate each
metric's definition as plain SQL over the canonical synthetic dataset, so a
change to a definition (window, denominator, inclusion rule) shows up as a
failing test instead of a silently different number on screen.

Run:  python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import analytics  # noqa: E402
import database  # noqa: E402


class MetricDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"SALAH_DB_PATH": str(Path(cls.tmp.name) / "merchant.db")}
        cls._old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        database._ready.clear()
        cls.ctx = analytics.build_merchant_context()
        cls.trace = analytics.build_trace(cls.ctx)
        cls.action = analytics.get_recommended_action(cls.ctx)
        cls.conn = sqlite3.connect(database.db_path())
        cls.end = datetime.fromisoformat(cls.q("SELECT MAX(ts) FROM transactions")[0][0])
        cls.start = cls.end - timedelta(weeks=8)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for k, v in cls._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        database._ready.clear()
        cls.tmp.cleanup()

    @classmethod
    def q(cls, sql, *args):
        return cls.conn.execute(sql, args).fetchall()

    def test_trailing_revenue_is_the_sum_of_the_last_56_days(self):
        (total,), = self.q("SELECT ROUND(SUM(amount), 2) FROM transactions WHERE ts >= ?", self.start.isoformat())
        self.assertEqual(round(sum(w["revenue"] for w in self.ctx["weekly_revenue"]), 2), total)
        self.assertEqual(self.ctx["evening_share"]["total_revenue"], total)
        self.assertEqual(total, 7677.0)

    def test_weekly_average_divides_by_the_window_not_the_bucket_count(self):
        self.assertEqual(len(self.ctx["weekly_revenue"]), 9)  # 8 weeks span 9 calendar weeks
        item = next(t for t in self.trace if t["signal"] == "weekly_revenue")
        self.assertIn("avg 959.62/week", item["value"])
        self.assertNotIn("853", item["value"])
        self.assertIn("first partial", item["window"])

    def test_daily_averages_use_whole_trading_days_only(self):
        # Whole days strictly after the window's partial first day, with at
        # least one payment (payment data cannot tell closed from no sales).
        day0 = (self.start + timedelta(days=1)).date().isoformat()
        rows = self.q("SELECT substr(ts, 1, 10) AS d, SUM(amount) FROM transactions WHERE ts >= ? GROUP BY d", day0)
        per_day = dict(rows)
        overall = round(sum(per_day.values()) / len(per_day), 2)
        wp = self.ctx["weekday_profile"]
        self.assertEqual(wp["overall_avg_daily_revenue"], overall)
        self.assertEqual(wp["trading_days"], len(per_day))
        self.assertEqual(self.ctx["weakest_weekday"]["overall_avg_daily_revenue"], overall)
        for name, d in wp["days"].items():
            self.assertLessEqual(d["days_in_window"], 8, name)  # never a 9th, partial day
        tuesdays = [v for d, v in per_day.items() if datetime.fromisoformat(d).weekday() == 1]
        self.assertEqual(self.ctx["weakest_weekday"]["avg_daily_revenue"], round(sum(tuesdays) / len(tuesdays), 2))

    def test_day_window_start_is_the_next_midnight(self):
        end = datetime(2024, 12, 22, 16, 45)
        self.assertEqual(analytics._day_window_start(end, 8), datetime(2024, 10, 28))
        self.assertEqual(analytics._day_window_start(datetime(2024, 12, 22), 8), datetime(2024, 10, 27))

    def test_average_ticket_compares_the_last_6_weeks_with_the_6_before(self):
        mid = self.end - timedelta(weeks=6)
        early_start = self.end - timedelta(weeks=12)
        (early,), = self.q("SELECT AVG(amount) FROM transactions WHERE ts >= ? AND ts < ?", early_start.isoformat(), mid.isoformat())
        (recent,), = self.q("SELECT AVG(amount) FROM transactions WHERE ts >= ?", mid.isoformat())
        tt = self.ctx["ticket_trend"]
        self.assertEqual((tt["early_avg_ticket"], tt["recent_avg_ticket"]), (round(early, 2), round(recent, 2)))
        self.assertEqual(tt["change_pct"], round((recent - early) / early * 100, 1))
        self.assertEqual(tt["weeks"], 12)

    def test_evening_share_is_18_to_22_over_the_8_week_window(self):
        (evening,), = self.q(
            "SELECT SUM(amount) FROM transactions WHERE ts >= ? AND CAST(substr(ts, 12, 2) AS INTEGER) BETWEEN 18 AND 21",
            self.start.isoformat())
        es = self.ctx["evening_share"]
        self.assertEqual(es["evening_revenue"], round(evening, 2))
        self.assertEqual(es["evening_share_pct"], round(evening / es["total_revenue"] * 100, 1))

    def test_lapsed_regulars_follow_the_stated_definition(self):
        lr = self.ctx["lapsed_regulars"]
        pre = (self.end - timedelta(weeks=8)).isoformat()
        cutoff = (self.end - timedelta(weeks=lr["weeks"])).isoformat()
        regulars = {
            cid for cid, n, first, last in self.q(
                "SELECT customer_id, COUNT(*), MIN(ts), MAX(ts) FROM transactions WHERE ts < ? GROUP BY customer_id", pre)
            if n >= lr["regular_min_visits"]
            and (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days >= lr["regular_min_span_days"]
        }
        silent = {cid for cid, last in self.q("SELECT customer_id, MAX(ts) FROM transactions GROUP BY customer_id")
                  if last < cutoff}
        self.assertEqual({c["customer_id"] for c in lr["customers"]}, regulars & silent)
        self.assertEqual(lr["count"], len(regulars & silent))

    def test_hypothesis_states_its_comparison_and_holds(self):
        item = next(t for t in self.trace if t["confidence"] == "HYPOTHESIS")
        wp = self.ctx["weekday_profile"]
        weak = wp["days"][self.ctx["weakest_weekday"]["weekday"]]
        self.assertLess(weak["avg_tx_per_active_day"], wp["overall_avg_tx_per_active_day"])
        self.assertIn(f"vs {wp['overall_avg_tx_per_active_day']} overall", item["value"])

    def test_every_measured_item_states_its_period(self):
        for t in self.trace:
            if t["confidence"] != "UNKNOWN":
                self.assertTrue(t["window"], t["signal"])
                self.assertIn("2024-12-22", t["window"], t["signal"])

    def test_experiment_plan_is_complete_and_claims_no_outcome(self):
        for field in ("hypothesis", "experiment", "baseline", "measure", "confounders", "unknown"):
            self.assertTrue(self.action.get(field), field)
        self.assertEqual(self.action["weeks"], 4)
        text = " ".join(str(v) for v in self.action.values()).lower()
        for claim in ("increased", "improved", "worked", "succeeded", "raised revenue"):
            self.assertNotIn(claim, text)


class HypothesisGroundingTests(unittest.TestCase):
    def ctx(self, weak_tx, overall_tx):
        return {
            "generated_at": "2024-12-22T16:45:00",
            "weakest_weekday": {"weekday": "Tuesday", "avg_daily_revenue": 46.62, "overall_avg_daily_revenue": 135.25},
            "weekday_profile": {
                "weeks": 8, "overall_avg_daily_revenue": 135.25, "overall_avg_tx_per_active_day": overall_tx,
                "trading_days": 55,
                "days": {"Tuesday": {"days_in_window": 8, "avg_daily_revenue": 46.62, "below_avg_days": 7,
                                     "avg_gap_on_below_days_pct": 76.6, "avg_tx_per_active_day": weak_tx}},
            },
        }

    def test_no_fewer_visits_hypothesis_when_the_weak_day_has_as_many_payments(self):
        trace = analytics.build_trace(self.ctx(weak_tx=4.5, overall_tx=4.3))
        self.assertFalse([t for t in trace if t["confidence"] == "HYPOTHESIS"])

    def test_hypothesis_present_when_payments_are_fewer(self):
        trace = analytics.build_trace(self.ctx(weak_tx=2.4, overall_tx=4.3))
        self.assertEqual([t["signal"] for t in trace if t["confidence"] == "HYPOTHESIS"],
                         ["weak_weekday_transaction_count"])


if __name__ == "__main__":
    unittest.main()
