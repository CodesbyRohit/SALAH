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

Phase 2 additions (all deterministic, no LLM calls):
  * weekday_profile()       — recurrence evidence for the weak weekday
  * build_trace(ctx)        — glass-box evidence list behind eventual answers
  * get_recommended_action(ctx) — ONE merchant-controlled experiment

Evidence-honesty contract for build_trace():
  FACT       = a number directly computed from the transaction table
  PATTERN    = a relationship that recurs across independent data groups
  HYPOTHESIS = possible explanation, must be explicitly framed as possible
               and must be grounded in evidence present in the context
  UNKNOWN    = payment data cannot establish the answer
  No cause (motivation, staffing, inventory, hours, pricing, availability,
  weather, competition) may ever be asserted without such evidence existing
  in merchant_context.
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


def weekday_profile(weeks: int = 8) -> dict:
    """Per-weekday decomposition of the trailing window. Deterministic input
    for recurrence assessment of the weak weekday — NOT a cause.
    Each weekday entry carries avg daily revenue, how many of that weekday's
    days in the window fell below the overall daily average, the average gap
    on below-average days, and the average transaction count per active day.

    NOTE: only weekdays that actually occur in the window are present in
    ``days_seen``; a short window (e.g. 2 weeks) sees fewer occurrences."""
    end = get_data_end()
    start = end - timedelta(weeks=weeks)
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, amount FROM transactions WHERE ts >= ?", (_iso(start),)
    ).fetchall()
    conn.close()
    rev_by_day: dict[str, float] = {}
    cnt_by_day: dict[str, int] = {}
    for r in rows:
        d = datetime.fromisoformat(r["ts"]).date().isoformat()
        rev_by_day[d] = rev_by_day.get(d, 0.0) + r["amount"]
        cnt_by_day[d] = cnt_by_day.get(d, 0) + 1
    overall = (sum(rev_by_day.values()) / len(rev_by_day)) if rev_by_day else 0.0
    per_wd: dict[int, list[float]] = {i: [] for i in range(7)}
    cnt_per_wd: dict[int, list[float]] = {i: [] for i in range(7)}
    for d, rev in rev_by_day.items():
        wd = datetime.fromisoformat(d).weekday()
        per_wd[wd].append(rev)
        cnt_per_wd[wd].append(float(cnt_by_day.get(d, 0)))
    days_seen = {}
    for wd in range(7):
        vals = per_wd[wd]
        if not vals:
            continue
        below = [v for v in vals if v < overall]
        days_seen[WEEKDAYS[wd]] = {
            "days_in_window": len(vals),
            "avg_daily_revenue": round(sum(vals) / len(vals), 2),
            "below_avg_days": len(below),
            "avg_gap_on_below_days_pct": round(
                (sum((overall - v) / overall for v in below) / len(below) * 100)
                if below else 0.0, 1),
            "avg_tx_per_active_day": round(sum(cnt_per_wd[wd]) / len(cnt_per_wd[wd]), 1),
        }
    return {"weeks": weeks, "overall_avg_daily_revenue": round(overall, 2), "days": days_seen}


# ---------------------------------------------------------------------------
# Phase 2 — deterministic trace (glass box) and action/experiment engine.
# Both consume ONLY the merchant context dict; no DB access, no LLM calls.
# Thresholds below are generic data-shape rules, not seed-specific knowledge.
# ---------------------------------------------------------------------------

# Generic signal thresholds (data-shape rules, not dataset knowledge)
WEAK_WEEKDAY_MAX_GAP_PCT = 15.0   # weak day must trail overall avg by >15%
WEAK_WEEKDAY_MIN_RECURRENCE = 0.6 # and be below avg on >=60% of its days
TICKET_DECLINE_MIN_PCT = 10.0     # 'significant' ticket decline threshold


def _pct_of_average(value: float, average: float) -> float:
    """value as % of average (100 = exactly at average)."""
    if average <= 0:
        return 0.0
    return round(value / average * 100, 1)


def _action_lapsed(lr: dict) -> dict:
    """Priority 1 — lapsed regulars. Payment data shows they stopped but
    cannot establish why; the reactivation action stays generic because the
    dataset contains no evidence of channels/discounts/staff."""
    n = lr["count"]
    most_recent = lr["customers"][-1]["last_visit"] if lr["customers"] else None
    oldest = lr["customers"][0]["last_visit"] if lr["customers"] else None
    return {
        "priority": "lapsed_regulars",
        "observed": (
            f"{n} previously regular customers have had no transactions in the "
            f"last 4 weeks (last visits between {oldest} and {most_recent})."
        ),
        "unknown": "Payment data shows they stopped coming but cannot establish why.",
        "experiment": (
            "Run one controlled reactivation attempt toward this group using a "
            "channel and offer the merchant already has available, and hold a "
            "comparable group of still-regular customers unchanged as control; "
            "compare return-visit rates after 4 weeks."
        ),
        "measure": (
            "How many of the lapsed customers transact again within 4 weeks, "
            "versus return rate in the unchanged control group."
        ),
        "basis": f"lapsed_regulars ({n} customers, trailing-4-weeks silence)",
        "weeks": 4,
    }


def _action_weak_weekday(ww: dict, wp: dict) -> dict:
    """Priority 2 — recurring weak weekday. Cause is UNKNOWN; the action is a
    generic weekday-controlled test, not a solution claim."""
    day = ww["weekday"]
    weak = wp["days"][day]
    return {
        "priority": "weak_weekday",
        "observed": (
            f"{day} revenue averaged {weak['avg_daily_revenue']} vs an overall "
            f"daily average of {wp['overall_avg_daily_revenue']}; {weak['below_avg_days']} "
            f"of {weak['days_in_window']} recent {day}s fell below average."
        ),
        "unknown": "Payment data does not establish why this weekday underperforms.",
        "experiment": (
            f"Test one merchant-controlled change on {day}s only for the next "
            "2-4 occurrences, keep all other days unchanged, and compare those "
            f"{day}s against the recent {day} baseline."
        ),
        "measure": (
            f"Average daily revenue on the tested {day}s versus the recent "
            f"{day} baseline of {weak['avg_daily_revenue']}."
        ),
        "basis": f"weakest_weekday + weekday_profile (gap {weak['avg_gap_on_below_days_pct']}% on below-average days)",
        "weeks": 4,
    }


def _action_ticket_decline(tt: dict) -> dict:
    """Priority 3 — significant average-ticket decline."""
    return {
        "priority": "ticket_decline",
        "observed": (
            f"Average ticket moved from {tt['early_avg_ticket']} (first half of "
            f"the window) to {tt['recent_avg_ticket']} (second half), a "
            f"{abs(tt['change_pct'])}% decline."
        ),
        "unknown": "Payment data does not establish whether the cause is fewer items per visit, cheaper items, or a changed customer mix.",
        "experiment": (
            "Test one merchant-controlled change that could raise average ticket "
            "(e.g. a paired-item offer the merchant already sells) and compare "
            "the next 2 weeks of average ticket against the recent baseline."
        ),
        "measure": (
            f"Average ticket versus the recent baseline of {tt['recent_avg_ticket']} "
            "over the next 2 weeks."
        ),
        "basis": f"ticket_trend ({tt['change_pct']}% change over trailing 12 weeks)",
        "weeks": 2,
    }


def get_recommended_action(merchant_context: dict) -> dict:
    """Return ONE merchant-controlled experiment from deterministic context.

    Priority: 1 lapsed regulars, 2 recurring weak weekday, 3 significant
    ticket decline, 4 none. Reads only the context dict — no DB, no seed
    knowledge, no hardcoded customer IDs — so it keeps working if the
    underlying transaction dataset changes."""
    # --- Priority 1: lapsed regulars -------------------------------------
    lr = merchant_context.get("lapsed_regulars") or {}
    if lr.get("count", 0) > 0:
        return _action_lapsed(lr)

    # --- Priority 2: recurring weak weekday -------------------------------
    ww = merchant_context.get("weakest_weekday") or {}
    wp = merchant_context.get("weekday_profile") or {}
    day = ww.get("weekday")
    weak = (wp.get("days") or {}).get(day)
    if day and weak:
        gap = weak["avg_gap_on_below_days_pct"]
        recurrence = weak["below_avg_days"] / weak["days_in_window"] if weak["days_in_window"] else 0.0
        if gap >= WEAK_WEEKDAY_MAX_GAP_PCT and recurrence >= WEAK_WEEKDAY_MIN_RECURRENCE:
            return _action_weak_weekday(ww, wp)

    # --- Priority 3: significant ticket decline ---------------------------
    tt = merchant_context.get("ticket_trend") or {}
    if tt.get("change_pct", 0.0) <= -TICKET_DECLINE_MIN_PCT:
        return _action_ticket_decline(tt)

    # --- Priority 4: nothing strong enough to act on -----------------------
    return {
        "priority": "none",
        "observed": "No signal currently meets the action thresholds.",
        "unknown": "Whether any of the observed metrics are trending toward a problem.",
        "experiment": "No experiment recommended; keep measuring.",
        "measure": "Continue the existing weekly and weekday baselines.",
        "basis": "all signals below thresholds",
        "weeks": 0,
    }


def build_trace(merchant_context: dict) -> list[dict]:
    """Deterministic glass-box evidence behind Salah's eventual answer.

    Pure function of the context dict: same context -> same trace, no LLM,
    no second Gemini call. Every numerical value quoted is copied verbatim
    from merchant_context. Causal gaps are reported as UNKNOWN; HYPOTHESIS
    items only appear when the context itself contains the grounding
    evidence, and are always framed as possible, not established."""
    trace: list[dict] = []

    def add(signal: str, value, implication: str, source: str, confidence: str):
        trace.append({
            "signal": signal,
            "value": value,
            "implication": implication,
            "source": source,
            "confidence": confidence,
        })

    # 1. Weekly revenue — raw observed numbers.
    wr = merchant_context.get("weekly_revenue") or []
    if wr:
        total = round(sum(w["revenue"] for w in wr), 2)
        vals = [w["revenue"] for w in wr]
        avg = round(total / len(wr), 2)
        add("weekly_revenue",
            f"{len(wr)} weeks tracked, total {total}, avg {avg}/week",
            "Baseline revenue level for the trailing window.",
            "weekly_revenue", "FACT")

    # 2. Weak weekday — level is fact; recurrence is pattern; cause unknown.
    ww = merchant_context.get("weakest_weekday") or {}
    wp = merchant_context.get("weekday_profile") or {}
    day = ww.get("weekday")
    if day:
        weak = (wp.get("days") or {}).get(day, {})
        gap = _pct_of_average(ww.get("avg_daily_revenue", 0.0),
                              ww.get("overall_avg_daily_revenue", 0.0))
        add("weak_weekday_level",
            f"{day} averages {ww.get('avg_daily_revenue')} vs overall {ww.get('overall_avg_daily_revenue')} daily ({gap}% of average)",
            f"{day} is the weakest weekday in the trailing window.",
            "weakest_weekday", "FACT")
        if weak:
            recurrence = weak["below_avg_days"] / weak["days_in_window"] if weak["days_in_window"] else 0.0
            add("weak_weekday_recurrence",
                f"{weak['below_avg_days']} of {weak['days_in_window']} recent {day}s below overall daily average (gap {weak['avg_gap_on_below_days_pct']}% on those days)",
                f"The {day} shortfall recurs across independent weeks rather than being one bad day.",
                "weekday_profile", "PATTERN")
            if weak.get("avg_tx_per_active_day") is not None and weak["avg_tx_per_active_day"] > 0:
                overall_tx = _overall_tx_hint(merchant_context)
                if overall_tx:
                    add("weak_weekday_transaction_count",
                        f"{day} averages {weak['avg_tx_per_active_day']} transactions per active day",
                        "Fewer transactions on this weekday point to fewer visits rather than smaller tickets — possible, not established.",
                        "weekday_profile", "HYPOTHESIS")
            add("weak_weekday_cause", "not determinable from payment data",
                f"Payment data shows when revenue dips, not why.",
                "weakest_weekday", "UNKNOWN")

    # 3. Lapsed regulars — behaviour is fact; motivation unknown.
    lr = merchant_context.get("lapsed_regulars") or {}
    if lr.get("count", 0) > 0:
        custs = lr.get("customers") or []
        most_recent = custs[-1]["last_visit"] if custs else None
        add("lapsed_regulars",
            f"{lr['count']} previously regular customers silent for 4+ weeks (most recent visit {most_recent})",
            "A known group of repeat visitors has stopped transacting.",
            "lapsed_regulars", "FACT")
        add("lapse_reason", "not determinable from payment data",
            "Why they stopped is not recorded in the data.",
            "lapsed_regulars", "UNKNOWN")

    # 4. Ticket trend — observed averages are fact; driver unknown.
    tt = merchant_context.get("ticket_trend") or {}
    if tt:
        direction = "decline" if tt.get("change_pct", 0) < 0 else "increase" if tt.get("change_pct", 0) > 0 else "no change"
        add("ticket_trend",
            f"Average ticket {tt.get('early_avg_ticket')} -> {tt.get('recent_avg_ticket')} ({tt.get('change_pct')}%, {direction})",
            "Observed shift in spend per transaction across the window.",
            "ticket_trend", "FACT")
        add("ticket_trend_driver", "not determinable from payment data",
            "Whether fewer items, cheaper items, or different customers drove this is not recorded.",
            "ticket_trend", "UNKNOWN")

    # 5. Evening concentration — observed split is fact.
    es = merchant_context.get("evening_share") or {}
    if es:
        add("evening_share",
            f"{es.get('evening_share_pct')}% of revenue between 18:00-22:00 ({es.get('evening_revenue')} of {es.get('total_revenue')})",
            "Revenue concentrates in evening hours in the trailing window.",
            "evening_share", "FACT")

    return trace


def _overall_tx_hint(merchant_context: dict) -> float | None:
    """Non-zero average transactions/day across the window, or None.
    Derived from context fields only; used to sanity-check the HYPOTHESIS."""
    wp = merchant_context.get("weekday_profile") or {}
    vals = [d["avg_tx_per_active_day"] for d in (wp.get("days") or {}).values()
            if d.get("avg_tx_per_active_day")]
    return (sum(vals) / len(vals)) if vals else None


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
        "weekday_profile": weekday_profile(),
    }


if __name__ == "__main__":
    ctx = build_merchant_context()
    print(json.dumps(ctx, indent=2))
