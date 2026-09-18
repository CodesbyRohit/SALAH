"""
SALAH — synthetic merchant data seeder.

Planted representative patterns (honest framing: representative synthetic data,
NOT claims about a real merchant):
  * Tuesday revenue weakness        (recurring weekday dip)
  * Evening revenue concentration   (18:00-22:00 share)
  * Lapsed regular customers        (regulars that went quiet in the last 8 weeks)
  * Average ticket decline          (recent tickets below earlier baseline)

BASELINE (pre-Phase-1) BEHAVIOUR — intentionally kept so the phase plan has
real work to do:
  * REGULARS = CUSTOMER_POOL[:40]   (manual slice, not behavioral)
  * first_visit stores a spend list, not the first transaction timestamp

Phase 1: first_visit now stores the customer's actual first transaction
ISO timestamp. The REGULARS list is kept only as the *generator's* activity
model (who behaves like a regular); analytics classifies regulars
behaviourally from transactions — see backend/analytics.py.
"""
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "merchant.db"

DATA_END = datetime(2024, 12, 22, 21, 30)   # last synthetic transaction
WEEKS = 52                                   # ~1 year of history

CUSTOMER_POOL = [f"CUST_{i:03d}" for i in range(1, 161)]  # 160 customers

# BASELINE FLAW (spec P0): manually declared regulars instead of behavioral
# classification. Phase 1 replaces this with visits/span logic.
REGULARS = CUSTOMER_POOL[:40]

# ~30% of regulars lapse in the final 8 weeks of the dataset.
LAPSED = REGULARS[::3]

ITEMS = [
    ("Chai", 15), ("Samosa", 20), ("Vada Pav", 18), ("Poha", 25),
    ("Coffee", 25), ("Sandwich", 45), ("Dosa", 60), ("Idli", 40),
]
ITEM_WEIGHTS = [30, 22, 18, 12, 8, 5, 3, 2]

LAPSE_START = DATA_END - timedelta(weeks=8)


def _pick_items(rng: random.Random, n: int) -> list[str]:
    return [rng.choices([i[0] for i in ITEMS], weights=ITEM_WEIGHTS)[0] for _ in range(n)]


def _is_active(rng: random.Random, customer: str, day: datetime) -> bool:
    """Rough activity model per customer per day."""
    if customer in REGULARS:
        # Regulars: frequent, then lapse late in the window.
        if customer in LAPSED and day >= LAPSE_START:
            return rng.random() < 0.06          # mostly gone
        return rng.random() < 0.55              # 3-4 visits/week
    # Walk-ins: occasional
    return rng.random() < 0.04


def _day_multiplier(day: datetime) -> float:
    """Deterministic demand shape: Tuesday dip, evening concentration,
    general growth over the year."""
    base = 1.0
    # Tuesday weakness — planted pattern
    if day.weekday() == 1:
        base *= 0.55
    # Weekend lift
    if day.weekday() in (5, 6):
        base *= 1.25
    # Mild secular growth
    weeks_from_start = (day - (DATA_END - timedelta(weeks=WEEKS))).days / 7
    base *= (1.0 + 0.004 * weeks_from_start)
    return base


HOUR_WEIGHTS = {
    8: 6, 9: 8, 10: 7, 11: 6, 12: 8, 13: 9, 14: 6, 15: 5, 16: 6, 17: 8,
    18: 12, 19: 15, 20: 16, 21: 14, 22: 8,
}


def seed(verbose: bool = True) -> dict:
    rng = random.Random(42)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id TEXT NOT NULL,
        amount REAL NOT NULL,
        items TEXT NOT NULL,
        ts TEXT NOT NULL
    );
    CREATE TABLE customers (
        id TEXT PRIMARY KEY,
        first_visit TEXT,
        spend_history TEXT
    );
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    cur.execute(
        "INSERT INTO meta VALUES ('schema_version', '1');"
    )

    rows: list[tuple] = []
    customer_spend: dict[str, list[float]] = {}
    customer_first: dict[str, str] = {}

    start = DATA_END - timedelta(weeks=WEEKS)
    day = start
    while day <= DATA_END:
        mult = _day_multiplier(day)
        n_tx_target = int(28 * mult)
        for _ in range(n_tx_target):
            cust = rng.choice(CUSTOMER_POOL)
            if not _is_active(rng, cust, day):
                continue
            hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
            minute = rng.randrange(0, 60, 5)
            ts = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if ts > DATA_END:
                continue
            # Ticket decline: later transactions skew to cheaper items slightly
            # in the last 10 weeks.
            if ts >= DATA_END - timedelta(weeks=10) and rng.random() < 0.35:
                items = _pick_items(rng, 1)
            else:
                items = _pick_items(rng, rng.choices([1, 2, 3], weights=[45, 40, 15])[0])
            prices = {name: price for name, price in ITEMS}
            amount = sum(prices[i] for i in items)
            rows.append((cust, float(amount), json.dumps(items), ts.isoformat()))
            customer_spend.setdefault(cust, []).append(amount)
            # Track the TRUE earliest ts (same-day rows are generated in random
            # hour order, so "first generated" != "earliest").
            prev = customer_first.get(cust)
            if prev is None or ts < datetime.fromisoformat(prev):
                customer_first[cust] = ts.isoformat()
        day += timedelta(days=1)

    cur.executemany(
        "INSERT INTO transactions (customer_id, amount, items, ts) VALUES (?, ?, ?, ?)",
        rows,
    )

    # first_visit = actual first transaction timestamp (Phase 1 fix);
    # spend_history remains the running per-visit spend list.
    for cust, spends in customer_spend.items():
        cur.execute(
            "INSERT OR REPLACE INTO customers (id, first_visit, spend_history) VALUES (?, ?, ?)",
            (cust, customer_first.get(cust), json.dumps(spends)),
        )

    conn.commit()

    n_tx = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    n_cust = cur.execute("SELECT COUNT(DISTINCT customer_id) FROM transactions").fetchone()[0]
    min_ts, max_ts = cur.execute("SELECT MIN(ts), MAX(ts) FROM transactions").fetchone()
    conn.close()

    if verbose:
        print(f"seeded {n_tx} transactions, {n_cust} customers")
        print(f"range: {min_ts} -> {max_ts}")
    return {"transactions": n_tx, "customers": n_cust, "min_ts": min_ts, "max_ts": max_ts}


if __name__ == "__main__":
    seed()
