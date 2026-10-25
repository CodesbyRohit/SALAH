"""
SALAH deployed-API smoke test.

Makes real HTTP requests against a deployed SALAH (Vercel preview or
production) and compares the data endpoints with the canonical synthetic
dataset, regenerated locally from version-controlled code into a temp
directory. Nothing is mocked and nothing in the repository's data/ is used.

    python tests/smoke/deployed_smoke.py https://<deployment>.vercel.app --via vercel
    python tests/smoke/deployed_smoke.py https://salah-liart.vercel.app

--via oidc     plain HTTPS plus the short-lived VERCEL_OIDC_TOKEN as
               x-vercel-trusted-oidc-idp-token, sent only to the target host.
               Run under `npx vercel env run` from a directory linked to the
               project. Preferred for protected previews: unlike `vercel curl`
               it creates no deployment-protection bypass credential.
--via vercel   send every request through `npx vercel curl`. Works with just
               `npx vercel login`, but the CLI creates an automation bypass
               credential on the project, which should be revoked afterwards.
Default: plain HTTPS (public deployments).

The POST checks write to the target's ephemeral interaction log and
in-process conversation history, exactly like a real user would.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RESULTS: list[tuple[str, bool, str]] = []


class Client:
    def __init__(self, base: str, via: str, scratch: Path):
        self.base = base.rstrip("/")
        self.via = via
        self.scratch = scratch
        self.npx = shutil.which("npx.cmd") or shutil.which("npx")
        self.headers = {}
        if via == "oidc":
            token = os.environ.get("VERCEL_OIDC_TOKEN")
            if not token:
                raise SystemExit("--via oidc needs VERCEL_OIDC_TOKEN (run under `npx vercel env run`)")
            self.headers = {"x-vercel-trusted-oidc-idp-token": token}

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str = "application/json") -> tuple[int, dict, bytes]:
        url = self.base + path
        if self.via in ("direct", "oidc"):
            req = urllib.request.Request(url, data=body, method=method, headers=self.headers)
            if body is not None:
                req.add_header("Content-Type", content_type)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as e:
                return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
        # HEAD uses --head: the CLI wrapper stalls piping large bodies on Windows.
        verb = ["--head"] if method == "HEAD" else ["--include", "--request", method]
        args = [self.npx, "--yes", "vercel", "curl", url, "--", "--silent", "--show-error",
                "--path-as-is", *verb]
        if body is not None:
            data = self.scratch / "body.bin"
            data.write_bytes(body)
            args += ["--header", f"Content-Type: {content_type}", "--data-binary", f"@{data}"]
        out = subprocess.run(args, capture_output=True, timeout=120).stdout
        return parse_http(out)


def parse_http(raw: bytes) -> tuple[int, dict, bytes]:
    # Skip interim responses (e.g. "HTTP/1.1 100 Continue") and any CLI preamble.
    start = raw.find(b"HTTP/")
    if start < 0:
        raise RuntimeError(f"no HTTP response in CLI output: {raw[:300]!r}")
    raw = raw[start:]
    while True:
        head, _, rest = raw.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        if status != 100 or not rest.startswith(b"HTTP/"):
            break
        raw = rest
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, rest


def check(name, fn):
    try:
        detail = fn() or ""
        RESULTS.append((name, True, detail))
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    except Exception as exc:  # noqa: BLE001 - report every failure
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  FAIL  {name}\n        {type(exc).__name__}: {str(exc)[:500]}")


def canonical(scratch: Path) -> tuple[dict, dict, dict]:
    """Regenerate the synthetic dataset in a temp dir and return the expected
    /context and /insights, plus figures computed with raw SQL."""
    os.environ["SALAH_DB_PATH"] = str(scratch / "canonical.db")
    sys.path.insert(0, str(ROOT / "backend"))
    import analytics  # noqa: E402
    import database  # noqa: E402

    path = database.ensure_database()
    ctx = analytics.build_merchant_context()
    ins = {"generated_at": ctx["generated_at"], "trace": analytics.build_trace(ctx),
           "action": analytics.get_recommended_action(ctx)}
    conn = sqlite3.connect(path)
    try:
        max_ts = conn.execute("SELECT MAX(ts) FROM transactions").fetchone()[0]
        start = (datetime.fromisoformat(max_ts) - timedelta(weeks=8)).isoformat()
        total_8w = conn.execute("SELECT SUM(amount) FROM transactions WHERE ts >= ?", (start,)).fetchone()[0]
        counts = conn.execute("SELECT (SELECT COUNT(*) FROM transactions), (SELECT COUNT(*) FROM customers)").fetchone()
    finally:
        conn.close()
    raw = {"max_ts": max_ts, "total_8w": round(total_8w, 2), "transactions": counts[0], "customers": counts[1]}
    return ctx, ins, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--via", choices=["direct", "oidc", "vercel"], default="direct")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="salah-smoke-"))
    c = Client(args.url, args.via, scratch)
    exp_ctx, exp_ins, raw = canonical(scratch)
    print(f"SALAH deployed smoke against {c.base} (via {args.via})")
    print(f"canonical dataset: {raw['transactions']} transactions, {raw['customers']} customers, "
          f"last ts {raw['max_ts']}, 8-week revenue {raw['total_8w']}")

    def get_json(path, status=200):
        code, headers, body = c.request("GET", path)
        assert code == status, f"HTTP {code}: {body[:200]!r}"
        assert headers.get("content-type", "").startswith("application/json"), headers.get("content-type")
        return json.loads(body)

    def post(path, payload, raw_body=None):
        body = raw_body if raw_body is not None else json.dumps(payload).encode()
        code, headers, data = c.request("POST", path, body)
        try:
            parsed = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = None
        return code, parsed, data

    seen = {}

    def health():
        h = get_json("/api/health")
        assert h["status"] == "ok", h
        assert h["database"] == {"ready": True, "location": "ephemeral", "error": None}, h["database"]
        text = json.dumps(h)
        for leak in ("/tmp", "/var/task", "\\\\", "API_KEY", "C:/", "C:\\\\"):
            assert leak not in text, f"health exposes {leak!r}: {text}"
        seen["health"] = h
        return f"database={h['database']}, llm_available={h['llm_available']}, voice={h['voice']['available']}"
    check("GET /api/health -> 200, database ready (ephemeral), no paths or secrets", health)

    def context():
        a, b = get_json("/context"), get_json("/context")
        assert a == b, "two /context calls differ"
        assert a["generated_at"] == raw["max_ts"] == "2024-12-22T16:45:00", a["generated_at"]
        assert a == exp_ctx, "deployed /context differs from the canonical dataset"
        total = round(sum(w["revenue"] for w in a["weekly_revenue"]), 2)
        assert total == raw["total_8w"] == a["evening_share"]["total_revenue"], (total, raw["total_8w"])
        assert round(total) == 7677, total
        return f"generated_at={a['generated_at']}, 8w revenue={total}, lapsed={a['lapsed_regulars']['count']}"
    check("GET /context -> 200, equals canonical dataset, stable across calls", context)

    def insights():
        a, b = get_json("/insights"), get_json("/insights")
        assert a == b, "two /insights calls differ"
        assert a == exp_ins, "deployed /insights differs from the deterministic engine"
        assert a["action"]["priority"] == "lapsed_regulars", a["action"]["priority"]
        tiers = sorted({t["confidence"] for t in a["trace"]})
        assert set(tiers) <= {"FACT", "PATTERN", "HYPOTHESIS", "UNKNOWN"}, tiers
        return f"priority={a['action']['priority']}, trace items={len(a['trace'])}, tiers={tiers}"
    check("GET /insights -> 200, equals deterministic engine, priority lapsed_regulars", insights)

    def ask_valid():
        code, j, data = post("/ask", {"question": "What should I try next?", "language": "en"})
        assert code == 200, (code, data[:300])
        assert set(j) >= {"answer", "trace", "action", "source", "fallback_reason", "language"}, sorted(j)
        assert j["answer"].strip() and j["language"] == "en"
        assert j["trace"] == exp_ins["trace"], "ask trace differs from /insights"
        assert j["action"]["priority"] == "lapsed_regulars", j["action"]
        if not seen.get("health", {}).get("llm_available"):
            assert j["source"] == "deterministic" and j["fallback_reason"] == "llm_unavailable", (j["source"], j["fallback_reason"])
        assert str(raw["total_8w"]) in j["answer"].replace(",", ""), "answer lacks the canonical revenue"
        return f"source={j['source']}, fallback_reason={j['fallback_reason']}"
    check("POST /ask (en) -> 200, deterministic answer from canonical data", ask_valid)

    def ask_hi():
        code, j, data = post("/ask", {"question": "kya karun?", "language": "hi"})
        assert code == 200 and j["language"] == "hi" and j["answer"].strip(), (code, data[:200])
    check("POST /ask (hi) -> 200, language hi", ask_hi)

    def ask_unsupported_language():
        code, j, data = post("/ask", {"question": "What should I try next?", "language": "de"})
        assert code == 200 and j["language"] == "en", (code, data[:200])
    check("POST /ask unsupported language -> 200, normalised to en", ask_unsupported_language)

    def ask_empty():
        code, j, data = post("/ask", {"question": "   ", "language": "en"})
        assert code == 400 and j == {"detail": "empty question"}, (code, data[:200])
    check("POST /ask empty question -> 400", ask_empty)

    def ask_missing():
        code, j, data = post("/ask", {"language": "en"})
        assert code == 422 and j["detail"][0]["loc"][-1] == "question", (code, data[:200])
    check("POST /ask missing question -> 422", ask_missing)

    def ask_malformed():
        code, j, data = post("/ask", None, raw_body=b'{"question": "hi"')
        assert code == 422, (code, data[:200])
    check("POST /ask malformed JSON -> 422", ask_malformed)

    def ask_wrong_type():
        code, j, data = post("/ask", {"question": 42})
        assert code == 422, (code, data[:200])
    check("POST /ask non-string question -> 422", ask_wrong_type)

    def nudge():
        code, j, data = post("/nudge", {"force": True, "language": "en"})
        assert code == 200 and set(j) == {"nudge", "language"} and j["language"] == "en", (code, data[:300])
        ww, lr = exp_ctx["weakest_weekday"], exp_ctx["lapsed_regulars"]
        if not seen.get("health", {}).get("llm_available"):
            assert ww["weekday"] in j["nudge"] and f"{lr['count']} regular" in j["nudge"], j["nudge"]
        return j["nudge"][:110]
    check("POST /nudge -> 200, built from canonical data", nudge)

    def nudge_defaults():
        code, j, data = post("/nudge", {})
        assert code == 200 and j["language"] == "en", (code, data[:200])
    check("POST /nudge {} -> 200 with defaults", nudge_defaults)

    def nudge_malformed():
        code, j, data = post("/nudge", None, raw_body=b"{nope")
        assert code == 422, (code, data[:200])
    check("POST /nudge malformed JSON -> 422", nudge_malformed)

    def tts_unconfigured():
        if seen.get("health", {}).get("voice", {}).get("available"):
            return "skipped: voice configured"
        code, j, data = post("/voice/tts", {"text": "hello", "language": "en"})
        assert code == 503, (code, data[:200])
    check("POST /voice/tts without Sarvam -> 503 (no fake audio)", tts_unconfigured)

    def frontend():
        code, headers, body = c.request("GET", "/")
        assert code == 200 and b"<html" in body.lower(), code
        for asset in ("/js/app.js", "/css/tokens.css", "/vendor/three/three.module.min.js"):
            code, headers, _ = c.request("HEAD", asset)
            assert code == 200 and int(headers.get("content-length", "1")) > 0, (asset, code)
    check("GET / and core assets -> 200", frontend)

    def no_private_files():
        for path in ("/data/merchant.db", "/backend/main.py", "/../backend/main.py",
                     "/%2e%2e/backend/main.py", "/.env", "/pyproject.toml"):
            code, _, body = c.request("GET", path)
            # 404 from the app, or 400 when the edge rejects a traversal path outright.
            assert code in (400, 404) and b"SQLite" not in body and b"FastAPI" not in body, (path, code)
    check("database, source and env files are not served", no_private_files)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} deployed smoke checks passed")
    shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
