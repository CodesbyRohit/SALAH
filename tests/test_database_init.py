"""
First-run database initialisation (backend/database.py).

Every test runs against its own temporary directory via SALAH_DB_PATH, so the
developer's data/merchant.db is never read, modified or deleted. Covers a
missing database, a missing parent directory, fresh / repeated / concurrent
(threads and processes) initialisation, refusal to overwrite unexpected files,
serverless path resolution, the seed CLI, and every API endpoint both on a
freshly created database and when initialisation is impossible.

Run:  python -m unittest discover -s tests -v
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import analytics  # noqa: E402
import database  # noqa: E402
import dataset  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402

# SHA-256 of the dataset produced by the original data/seed.py (verified
# against an untouched local database before the generator was moved).
GOLDEN_DIGEST = "2d6548355c79e9e728c69d4064112c3c4e581b86ef36a99ce908a7e15920e3d8"


def digest(path: Path) -> str:
    conn = sqlite3.connect(path)
    h = hashlib.sha256()
    for row in conn.execute("SELECT customer_id, amount, items, ts FROM transactions ORDER BY id"):
        h.update(repr(row).encode())
    for row in conn.execute("SELECT id, first_visit, spend_history FROM customers ORDER BY id"):
        h.update(repr(row).encode())
    conn.close()
    return h.hexdigest()


class _IsolatedDb(unittest.TestCase):
    """Points SALAH_DB_PATH at a fresh temp dir and resets the ready-cache."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "nested" / "data" / "merchant.db"
        self.set_db_path(self.path)

    def set_db_path(self, path):
        p = mock.patch.dict(os.environ, {"SALAH_DB_PATH": str(path)})
        p.start()
        self.addCleanup(p.stop)
        database._ready.clear()
        self.addCleanup(database._ready.clear)

    def leftovers(self):
        return [p.name for p in self.path.parent.glob(".*.tmp")] if self.path.parent.exists() else []


class InitialisationTests(_IsolatedDb):
    def test_missing_database_and_parent_directory_are_created(self):
        self.assertFalse(self.path.parent.exists())
        path = database.ensure_database()
        self.assertEqual(path, self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(database.validate(self.path), [])
        self.assertEqual(self.leftovers(), [])

    def test_fresh_database_is_the_original_seed_dataset(self):
        database.ensure_database()
        self.assertEqual(digest(self.path), GOLDEN_DIGEST)

    def test_schema_tables_and_columns_are_validated(self):
        database.ensure_database()
        conn = sqlite3.connect(self.path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        conn.close()
        self.assertTrue({"transactions", "customers", "meta"} <= tables)
        self.assertEqual(version, dataset.SCHEMA_VERSION)

    def test_repeated_initialisation_never_regenerates(self):
        with mock.patch.object(dataset, "populate", wraps=dataset.populate) as populate:
            database.ensure_database()
            mtime = self.path.stat().st_mtime_ns
            database.ensure_database()
            database._ready.clear()  # a new process / cold start finding the file
            database.ensure_database()
        self.assertEqual(populate.call_count, 1)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_concurrent_threads_initialise_once_and_consistently(self):
        barrier = threading.Barrier(8)
        results, errors = [], []

        def worker():
            barrier.wait()
            try:
                results.append(database.ensure_database())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch.object(dataset, "populate", wraps=dataset.populate) as populate:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {self.path})
        self.assertEqual(populate.call_count, 1)
        self.assertEqual(digest(self.path), GOLDEN_DIGEST)
        self.assertEqual(self.leftovers(), [])

    def test_concurrent_processes_initialise_safely(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import database; "
            "p = database.ensure_database(); print(p)"
        )
        env = {**os.environ, "SALAH_DB_PATH": str(self.path)}
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(ROOT / "backend")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(5)
        ]
        outputs = [p.communicate(timeout=60) for p in procs]
        for proc, (out, err) in zip(procs, outputs):
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(database.validate(self.path), [])
        self.assertEqual(digest(self.path), GOLDEN_DIGEST)
        self.assertEqual(self.leftovers(), [])

    def test_zero_byte_file_is_initialised(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"")
        database.ensure_database()
        self.assertEqual(digest(self.path), GOLDEN_DIGEST)

    def test_unexpected_database_is_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(database.DatabaseUnavailable) as ctx:
            database.ensure_database()
        self.assertIn("missing table transactions", str(ctx.exception))
        conn = sqlite3.connect(self.path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(tables, {"other"}, "the existing file must be left untouched")

    def test_non_sqlite_file_is_reported_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(database.DatabaseUnavailable):
            database.ensure_database()
        self.assertTrue(self.path.read_bytes().startswith(b"this is not a database"))

    def test_unwritable_location_raises_database_unavailable(self):
        blocker = self.dir / "not-a-directory"
        blocker.write_text("x")
        self.set_db_path(blocker / "merchant.db")
        with self.assertRaises(database.DatabaseUnavailable):
            database.ensure_database()

    def test_database_removed_after_initialisation_is_recreated(self):
        analytics.get_data_end()
        self.path.unlink()
        self.assertEqual(analytics.get_data_end().isoformat(), "2024-12-22T16:45:00")

    def test_analytics_reads_never_create_empty_files(self):
        conn = database.connect()
        conn.close()
        self.assertGreater(self.path.stat().st_size, 0)
        self.assertEqual(database.validate(self.path), [])

    def test_seed_cli_regenerates_at_the_resolved_path(self):
        env = {**os.environ, "SALAH_DB_PATH": str(self.path)}
        out = subprocess.run([sys.executable, str(ROOT / "data" / "seed.py")], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("seeded 1874 transactions", out.stdout)
        self.assertEqual(digest(self.path), GOLDEN_DIGEST)


class LocationTests(unittest.TestCase):
    def clean_env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SALAH_DB_PATH", "SALAH_DATA_DIR", "VERCEL", "AWS_LAMBDA_FUNCTION_NAME")}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_vercel_uses_the_writable_temp_directory(self):
        with self.clean_env(VERCEL="1"):
            expected = Path(tempfile.gettempdir()) / "salah"
            self.assertEqual(database.data_dir(), expected)
            self.assertEqual(database.db_path(), expected / "merchant.db")
            self.assertEqual(database.location_kind(), "ephemeral")

    def test_lambda_runtime_is_also_treated_as_serverless(self):
        with self.clean_env(AWS_LAMBDA_FUNCTION_NAME="salah"):
            self.assertTrue(database.is_serverless())

    def test_local_development_keeps_project_data_directory(self):
        with self.clean_env():
            self.assertEqual(database.db_path(), database.PROJECT_DATA_DIR / "merchant.db")
            self.assertEqual(database.location_kind(), "project")

    def test_explicit_overrides_win(self):
        with self.clean_env(VERCEL="1", SALAH_DATA_DIR="/srv/salah"):
            self.assertEqual(database.db_path(), Path("/srv/salah") / "merchant.db")
        with self.clean_env(SALAH_DB_PATH="/x/y.db", SALAH_DATA_DIR="/srv/salah"):
            self.assertEqual(database.db_path(), Path("/x/y.db"))

    def test_memory_log_lives_next_to_the_database(self):
        import memory
        self.assertEqual(memory.DATA_DIR, database.data_dir())


class _Endpoints(_IsolatedDb):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)
        for p in (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "SARVAM_API_KEY": ""}),
            mock.patch.object(llm, "GEMINI_API_KEY", ""),
            mock.patch.object(main.memory, "store"),
            mock.patch.object(main.memory, "reset_conversation"),
        ):
            p.start()
            self.addCleanup(p.stop)
        main.conversation_history.clear()


class EndpointsOnFreshDatabaseTests(_Endpoints):
    def test_first_request_initialises_and_context_is_real(self):
        self.assertFalse(self.path.exists())
        res = self.client.get("/context")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.path.exists())
        data = res.json()
        self.assertEqual(data["generated_at"], "2024-12-22T16:45:00")
        self.assertEqual(data["weakest_weekday"]["weekday"], "Tuesday")
        self.assertEqual(data["evening_share"]["total_revenue"], 7677.0)

    def test_health_reports_database_ready(self):
        data = self.client.get("/api/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["database"], {"ready": True, "location": "custom", "error": None})

    def test_insights_match_the_deterministic_engine(self):
        data = self.client.get("/insights").json()
        ctx = analytics.build_merchant_context()
        self.assertEqual(data["trace"], analytics.build_trace(ctx))
        self.assertEqual(data["action"], analytics.get_recommended_action(ctx))
        levels = {t["confidence"] for t in data["trace"]}
        self.assertEqual(levels, {"FACT", "PATTERN", "HYPOTHESIS", "UNKNOWN"})
        self.assertEqual(data["action"]["priority"], "lapsed_regulars")

    def test_ask_answers_from_the_fresh_database(self):
        res = self.client.post("/ask", json={"question": "kya karun?", "language": "en"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("7677.0", data["answer"])
        self.assertEqual(data["source"], "deterministic")
        self.assertEqual(data["action"]["priority"], "lapsed_regulars")

    def test_nudge_uses_real_data(self):
        res = self.client.post("/nudge", json={"force": True, "language": "en"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Tuesday", res.json()["nudge"])

    def test_voice_and_reset_are_unaffected(self):
        self.assertEqual(self.client.post("/voice/tts", json={"text": "hi", "language": "en"}).status_code, 503)
        self.assertEqual(self.client.post("/voice/stt", files={"file": ("a.webm", b"", "audio/webm")}).status_code, 400)
        self.assertEqual(self.client.post("/reset").json(), {"ok": True})


class EndpointsWhenInitialisationFailsTests(_Endpoints):
    def setUp(self):
        super().setUp()
        blocker = self.dir / "not-a-directory"
        blocker.write_text("x")
        self.set_db_path(blocker / "merchant.db")

    def test_health_stays_up_and_reports_the_failure(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        db = res.json()["database"]
        self.assertFalse(db["ready"])
        self.assertTrue(db["error"])

    def test_data_endpoints_return_503_instead_of_degraded_answers(self):
        for method, path, body in (
            ("get", "/context", None),
            ("get", "/insights", None),
            ("post", "/ask", {"question": "kya karun?", "language": "en"}),
            ("post", "/nudge", {"force": True, "language": "en"}),
        ):
            res = getattr(self.client, method)(path, json=body) if body else getattr(self.client, method)(path)
            self.assertEqual(res.status_code, 503, path)
            self.assertIn("Merchant data unavailable", res.json()["detail"], path)


if __name__ == "__main__":
    unittest.main()
