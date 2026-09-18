"""
SALAH — deterministic analytics.

Everything here is pure SQLite + Python. No LLM involvement. All financial
facts surfaced anywhere in the product originate from these functions.

BASELINE (pre-Phase-1) BEHAVIOUR — kept intentionally so Phase 1 has real work:
  * rolling windows are anchored to datetime.now() (WRONG: data ends 2024,
    runtime clock is 2026). Phase 1 replaces these anchors with get_data_end().
  * get_data_end() does not exist yet in the baseline.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "merchant.db"

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Baseline analytics — all windows anchored on datetime.now() (the known P0).
# ---------------------------------------------------------------------------

def weekly_revenue(weeks: int = 8) -> list[dict]:
    """Weekly revenue for the trailing N weeks before the runtime clock.

    BASELINE BUG: anchors to datetime.now(); against a 2024-dated dataset the
    recent windows are empty/zero. Phase 1 re-anchors to MAX(ts).
    """
    now = datetime.now()
    start = now - timedelta(weeks=weeks)
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, amount FROM transactions WHERE ts >= ? ORDER BY ts",
        (_iso(start),),
    ).fetchall()
    conn.close()
    buckets: dict[str, float] = {}
    for r in rows:
        dt = datetime.fromisoformat(r["ts"])
        year, week, _ = dt.isocalendar()
        key = f"{year}-W{week:02d}"
        buckets[key] = buckets.get(key, 0.0) + r["amount"]
    return [{"week": k, "revenue": round(v, 2)} for k, v in sorted(buckets.items())]


def revenue_by_hour(weeks: int = 8) -> list[dict]:
    """Revenue by hour-of-day over the trailing window (baseline: now-based)."""
    now = datetime.now()
    start = now - timedelta(weeks=weeks)
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, amount FROM transactions WHERE ts >= ?", (_iso(start),)
    ).fetchall()
    conn.close()
    buckets: dict[int, float] = {}
    for r in rows:
        h = datetime.fromisoformat(r["ts"]).hour
        buckets[h] = buckets.get(h, 0.0) + r["amount"]
    return [{"hour": h, "revenue": round(buckets.get(h, 0.0), 2)} for h in range(8, 23)]


def weakest_weekday(weeks: int = 8) -> dict:
    """Weekday with lowest average daily revenue over the trailing window."""
    now = datetime.now()
    start = now - timedelta(weeks=weeks)
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, amount FROM transactions WHERE ts >= ?", (_iso(start),)
    ).fetchall()
    conn.close()
    per_day: dict[str, float] = {}
    for r in rows:
        dt = datetime.fromisoformat(r["ts"])
        key = dt.date().isoformat()
        per_day[key] = per_day.get(key, 0.0) + r["amount"]
    by_weekday_sum: dict[int, float] = {}
    by_weekday_n: dict[int, int] = {}
    for day_str, rev in per_day.items():
        wd = datetime.fromisoformat(day_str).weekday()
        by_weekday_sum[wd] = by_weekday_sum.get(wd, 0.0) + rev
        by_weekday_n[wd] = by_weekday_n.get(wd, 0) + 1
    if not by_weekday_n:
        return {"weekday": None, "avg_daily_revenue": 0.0, "overall_avg_daily_revenue": 0.0}
    avgs = {wd: by_weekday_sum[wd] / by_weekday_n[wd] for wd in by_weekday_n}
    overall = sum(by_weekday_sum.values()) / sum(by_weekday_n.values())
    worst = min(avgs, key=avgs.get)
    return {
        "weekday": WEEKDAYS[worst],
        "avg_daily_revenue": round(avgs[worst], 2),
        "overall_avg_daily_revenue": round(overall, 2),
    }


def get_regulars() -> set[str]:
    """BASELINE: reads the customers table's first_visit field which (bug) holds
    a spend list, not a timestamp. Regular classification here is naive —
    derived from spend_history length. Phase 1 replaces with behavioral
    classification computed from the pre-lapse period."""
    conn = get_conn()
    rows = conn.execute("SELECT id, spend_history FROM customers").fetchall()
    conn.close()
    out = set()
    for r in rows:
        try:
            spends = json.loads(r["spend_history"])
        except (TypeError, json.JSONDecodeError):
            continue
        if len(spends) >= 8:
            out.add(r["id"])
    return out


def lapsed_regulars(weeks: int = 4) -> dict:
    """Regulars with no transactions in the trailing window (baseline: now)."""
    now = datetime.now()
    cutoff = now - timedelta(weeks=weeks)
    regulars = get_regulars()
    conn = get_conn()
    rows = conn.execute(
        "SELECT customer_id, MAX(ts) AS last_ts FROM transactions GROUP BY customer_id"
    ).fetchall()
    conn.close()
    lapsed = []
    for r in rows:
        if r["customer_id"] in regulars:
            last = datetime.fromisoformat(r["last_ts"])
            if last < cutoff:
                lapsed.append({"customer_id": r["customer_id"], "last_visit": r["last_ts"]})
    return {"count": len(lapsed), "customers": lapsed}


def ticket_trend(weeks: int = 12) -> dict:
    """Average ticket size: recent half-window vs earlier half (baseline: now)."""
    now = datetime.now()
    start = now - timedelta(weeks=weeks)
    mid = now - timedelta(weeks=weeks // 2)
    conn = get_conn()
    early = conn.execute(
        "SELECT AVG(amount) AS a FROM transactions WHERE ts >= ? AND ts < ?",
        (_iso(start), _iso(mid)),
    ).fetchone()["a"]
    recent = conn.execute(
        "SELECT AVG(amount) AS a FROM transactions WHERE ts >= ?", (_iso(mid),)
    ).fetchone()["a"]
    conn.close()
    early = early or 0.0
    recent = recent or 0.0
    change_pct = ((recent - early) / early * 100) if early else 0.0
    return {
        "early_avg_ticket": round(early, 2),
        "recent_avg_ticket": round(recent, 2),
        "change_pct": round(change_pct, 1),
    }


def evening_share(weeks: int = 8) -> dict:
    """Share of revenue between 18:00-22:00 (baseline: now-anchored)."""
    now = datetime.now()
    start = now - timedelta(weeks=weeks)
    conn = get_conn()
    total = conn.execute(
        "SELECT SUM(amount) AS s FROM transactions WHERE ts >= ?", (_iso(start),)
    ).fetchone()["s"] or 0.0
    evening = conn.execute(
        "SELECT SUM(amount) AS s FROM transactions WHERE ts >= ? AND "
        "CAST(strftime('%H', ts) AS INTEGER) >= 18 AND "
        "CAST(strftime('%H', ts) AS INTEGER) < 22",
        (_iso(start),),
    ).fetchone()["s"] or 0.0
    conn.close()
    share = (evening / total * 100) if total else 0.0
    return {"evening_revenue": round(evening, 2), "total_revenue": round(total, 2),
            "evening_share_pct": round(share, 1)}


def build_merchant_context() -> dict:
    """Aggregate deterministic context handed to the LLM and the frontend."""
    return {
        "generated_at": datetime.now().isoformat(),
        "weekly_revenue": weekly_revenue(),
        "revenue_by_hour": revenue_by_hour(),
        "weakest_weekday": weakest_weekday(),
        "lapsed_regulars": lapsed_regulars(),
        "ticket_trend": ticket_trend(),
        "evening_share": evening_share(),
    }


if __name__ == "__main__":
    ctx = build_merchant_context()
    print(json.dumps(ctx, indent=2))
