"""
SALAH — synthetic merchant data seeder (command line).

Regenerates the representative synthetic dataset with planted patterns (Tuesday
weakness, evening concentration, lapsed regulars, ticket decline). It is NOT
data from a real merchant.

The generation logic lives in backend/dataset.py so the server can create the
database automatically on first use (including on fresh serverless instances).
You only need this script to force a clean regeneration:

    python data/seed.py

This replaces the database at the resolved location (data/merchant.db locally;
see backend/database.py for SALAH_DB_PATH / SALAH_DATA_DIR / serverless rules).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import database  # noqa: E402


def seed(verbose: bool = True) -> dict:
    path = database.db_path()
    summary = database.rebuild_database(path)
    if verbose:
        print(f"seeded {summary['transactions']} transactions, {summary['customers']} customers")
        print(f"range: {summary['min_ts']} -> {summary['max_ts']}")
        print(f"database: {path}")
    return summary


if __name__ == "__main__":
    seed()
