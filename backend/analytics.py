"""
SALAH — deterministic analytics.

Everything here is pure SQLite + Python. No LLM involvement. All financial
facts surfaced anywhere in the product originate from these functions.

Phase 1 fixes applied:
  * get_data_end() anchors every rolling window to MAX(ts) from SQLite, never
    to the runtime clock.
  * get_regulars() classifies customers behaviourally (8+ visits AND a 60+
    day active span, measured inside the pre-lapse window), not from a
    manually declared slice.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "merchant.db"

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Behavioural regular thresholds (Phase 1 spec).
REGULAR_MIN_VISITS = 8
REGULAR_MIN_SPAN_DAYS = 60
# Regulars are judged on behaviour BEFORE the final 8 weeks, so a customer who
# lapsed late still counts as a regular (and can then be flagged as lapsed).
LAPSE_WEEKS = 8


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_data_end() -> datetime:
    """Latest transaction timestamp in the DB. Single source of truth for the
    'end of observed data' anchor; replaces datetime.now() everywhere in
    analytics so windows match the synthetic 2024 dataset."""
    conn = get_conn()
    row = conn.execute("SELECT MAX(ts) AS m FROM transactions").fetchone()
    conn.close()
    if row is None or row["m"] is None:
        raise RuntimeError("no transactions in DB; run data/seed.py first")
    return datetime.fromisoformat(row["m"])


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _pre_lapse_start(data_end: datetime) -> datetime:
    """Start of the pre-lapse window: everything before the final 8 weeks."""
    return data_end - timedelta(weeks=LAPSE_WEEKS)


# ---------------------------------------------------------------------------
# Rolling-window analytics — all anchored on get_data_end().
# ---------------------------------------------------------------------------

def weekly_revenue(weeks: int = 8) -> list[dict]:
    """Weekly revenue for the trailing N weeks before the data end."""
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
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
    """Revenue by hour-of-day over the trailing window."""
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
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
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
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


def get_regulars(data_end: datetime | None = None) -> set[str]:
    """Behavioural classification: a customer is a regular if, inside the
    pre-lapse window (data_end - 8w excluded tail), they have:
      * >= 8 transactions, AND
      * an active span (MAX(ts) - MIN(ts)) of >= 60 days.
    No manually declared list is consulted."""
    end = data_end or get_data_end()
    pre_start = _pre_lapse_start(end)
    conn = get_conn()
    rows = conn.execute(
        "SELECT customer_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM transactions WHERE ts < ? GROUP BY customer_id",
        (_iso(end - timedelta(weeks=LAPSE_WEEKS)),),
    ).fetchall()
    conn.close()
    out: set[str] = set()
    for r in rows:
        if r["n"] < REGULAR_MIN_VISITS:
            continue
        span = (datetime.fromisoformat(r["last_ts"]) - datetime.fromisoformat(r["first_ts"])).days
        if span >= REGULAR_MIN_SPAN_DAYS:
            out.add(r["customer_id"])
    return out


def lapsed_regulars(weeks: int = 4) -> dict:
    """Regulars with no transactions in the trailing `weeks` before data end."""
    end = get_data_end()
    cutoff = end - timedelta(weeks=weeks)
    regulars = get_regulars(end)
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
    lapsed.sort(key=lambda x: x["last_visit"])
    return {"count": len(lapsed), "customers": lapsed}


def ticket_trend(weeks: int = 12) -> dict:
    """Average ticket size: recent half-window vs earlier half."""
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
    mid = end - timedelta(weeks=weeks // 2)
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
    """Share of revenue between 18:00-22:00."""
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
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
        "generated_at": get_data_end().isoformat(),
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
