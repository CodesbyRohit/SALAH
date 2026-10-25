"""
SALAH browser end-to-end checks (Playwright + Chromium).

Starts the real FastAPI app with uvicorn on a free port, drives the real
frontend against the real backend, and reports each check. Not part of the
unittest suite because it needs a browser:

    python -m pip install playwright && python -m playwright install chromium
    python tests/e2e/e2e_ui.py              # add --shots DIR to save screenshots

Against a deployment instead of a local server (same checks, real remote API):

    python tests/e2e/e2e_ui.py --base-url https://<deployment>.vercel.app

For a deployment behind Vercel Deployment Protection, run it under
`vercel env run` from a directory linked to the project, so VERCEL_OIDC_TOKEN
is set. It is sent as x-vercel-trusted-oidc-idp-token on requests to that
origin only (never to third-party origins such as Google Fonts) and is never
printed.

What is real and what is mocked:
  * /context, /insights, /ask, /nudge, /api/health — real backend responses.
  * Voice success paths (/voice/stt, /voice/tts) need a Sarvam key, so they are
    exercised with Playwright route mocks plus Chromium's fake microphone. The
    real "voice not configured" path is tested unmocked.
  * /reset is mocked in the browser so the run does not delete the developer's
    local interaction log; the real endpoint is covered by tests/test_api_contract.py.
  * /ask failure is injected with a route to test the error + retry path.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import struct
import subprocess
import sys
import time
import traceback
import urllib.request
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
# Windows consoles default to cp1252; checks print Devanagari text.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        def run(*a, **kw):
            try:
                fn(*a, **kw)
                RESULTS.append((name, True, ""))
                print(f"  PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - report every failure
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"  FAIL  {name}\n        {type(exc).__name__}: {str(exc)[:400]}")
                if os.getenv("E2E_TRACE"):
                    traceback.print_exc()
        return run
    return wrap


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "SARVAM_API_KEY")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.15)
    proc.kill()
    raise RuntimeError("server did not start")


def tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(8000 * 5)))  # 5 s of silence
    return buf.getvalue()


def api_json(api, path: str):
    resp = api.get(path, timeout=30000)
    assert resp.ok, f"{path}: HTTP {resp.status}"
    return resp.json()


class Run:
    def __init__(self, browser, base, shots, auth=None):
        self.browser = browser
        self.base = base
        self.auth = auth or {}
        self.shots = Path(shots) if shots else None
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)

    def context(self, **ctx):
        context = self.browser.new_context(**ctx)
        if self.auth:
            context.route(f"{self.base}/**", lambda route: route.fallback(headers=self.headers(route.request)))
        return context

    def headers(self, request):
        """Request headers plus deployment auth, for handlers that fetch the
        real response themselves (route.fetch bypasses context routes)."""
        return {**request.headers, **self.auth}

    def page(self, viewport=(1440, 900), **ctx):
        context = self.context(viewport={"width": viewport[0], "height": viewport[1]}, **ctx)
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(f"pageerror: {e}"))
        # HTTP failures are tracked by status below; the browser's matching
        # "Failed to load resource" console line would double-report them.
        page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
                and page.errors.append(f"console: {m.text}"))
        page.on("response", lambda r: r.status >= 400 and not getattr(page, "allow_http_errors", False)
                and page.errors.append(f"HTTP {r.status} {r.url}"))
        return page

    def shot(self, page, name):
        if self.shots:
            page.screenshot(path=str(self.shots / f"{name}.png"), full_page=True)

    def ready(self, page, route="overview"):
        page.goto(f"{self.base}/#/{route}")
        page.wait_for_selector(".kpi__value", state="attached", timeout=15000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None)
    ap.add_argument("--base-url", default=None, help="test a deployment instead of starting a local server")
    args = ap.parse_args()

    auth = {}
    if args.base_url:
        server = None
        base = args.base_url.rstrip("/")
        token = os.getenv("VERCEL_OIDC_TOKEN")
        if token:
            auth = {"x-vercel-trusted-oidc-idp-token": token}
    else:
        port = free_port()
        server = start_server(port)
        base = f"http://127.0.0.1:{port}"
    print(f"SALAH E2E against {base}" + (" (Vercel OIDC auth)" if auth else ""))
    try:
        with sync_playwright() as p:
            api = p.request.new_context(base_url=base, extra_http_headers=auth)
            ctx, ins = (api_json(api, path) for path in ("/context", "/insights"))
            api.dispose()
            browser = p.chromium.launch(args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            ])
            run = Run(browser, base, args.shots, auth)
            suite(run, ctx, ins)
            browser.close()
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} browser checks passed")
    sys.exit(0 if passed == len(RESULTS) else 1)


def suite(run: Run, ctx: dict, ins: dict):
    page = run.page()

    @check("app initializes, loads /context and /insights, no console errors")
    def _():
        run.ready(page)
        expect(page.locator("#overview-headline")).to_contain_text(f"{ctx['lapsed_regulars']['count']} regular customers")
        page.wait_for_timeout(800)
        assert not page.errors, page.errors
    _()

    @check("KPI tiles render backend values exactly (no invented metrics)")
    def _():
        values = page.locator(".kpi__value").all_inner_texts()
        assert "₹7,677" in values[0] and f"{ctx['evening_share']['total_revenue']:.0f}" in values[0].replace(",", ""), values
        assert values[1] == "₹1,116", values
        assert values[2] == "₹31.06", values
        assert values[3] == "38.3%", values
        assert values[4] == "Tuesday", values
        assert values[5] == str(ctx["lapsed_regulars"]["count"]), values
    _()

    @check("synthetic-data disclosure is visible")
    def _():
        expect(page.locator(".topbar .chip--synthetic")).to_be_visible()
        expect(page.locator("main .disclaimer").first).to_contain_text("not live Paytm transactions")
    _()

    @check("three charts render with honest partial-week labelling and table twins")
    def _():
        assert page.locator(".chart").count() == 3
        expect(page.locator(".chart").first).to_contain_text("Partial week")
        btn = page.locator(".chart").first.get_by_role("button", name="Show table")
        btn.click()
        rows = page.locator(".chart").first.locator("tbody tr")
        assert rows.count() == len(ctx["weekly_revenue"]), rows.count()
        expect(page.locator(".chart").first.get_by_role("button", name="Hide table")).to_have_attribute("aria-expanded", "true")
    _()

    @check("chart tooltip works from the keyboard")
    def _():
        plot = page.locator(".chart__plot").nth(1)
        plot.focus()
        page.keyboard.press("Home")
        tip = page.locator(".chart__tooltip.is-visible")
        expect(tip).to_contain_text("₹226")
        page.keyboard.press("End")
        expect(tip).to_contain_text("₹630")
        page.keyboard.press("Escape")
    _()

    @check("evidence view lists every trace item with filters and expandable rows")
    def _():
        page.goto(f"{run.base}/#/evidence")
        rows = page.locator(".trace-row")
        expect(rows).to_have_count(len(ins["trace"]))
        unknown = sum(1 for t in ins["trace"] if t["confidence"] == "UNKNOWN")
        page.locator("#filter-unknown").click()
        expect(rows).to_have_count(unknown)
        expect(page.locator("#filter-unknown")).to_have_attribute("aria-pressed", "true")
        assert page.evaluate("document.activeElement.id") == "filter-unknown"
        page.locator("#filter-all").click()
        rows.first.locator("summary").click()
        expect(rows.first.locator(".trace-row__detail")).to_be_visible()
        expect(page.locator(".rec")).to_contain_text("Not yet measured")
        expect(page.locator(".rec")).to_contain_text(ins["action"]["experiment"][:40])
        run.shot(page, "evidence-desktop")
    _()

    @check("KPI subtitles state their period and definition")
    def _():
        run.ready(page)
        subs = page.locator(".kpi__sub").all_inner_texts()
        joined = " | ".join(subs)
        tt, es, lr = ctx["ticket_trend"], ctx["evening_share"], ctx["lapsed_regulars"]
        assert f"Last {tt['weeks'] // 2} weeks" in joined, subs
        assert f"of {es['weeks']}-week revenue" in joined, subs
        assert "per trading day" in joined, subs
        rule = f"{lr['regular_min_visits']}+ payments over {lr['regular_min_span_days']}+ days, then none for {lr['weeks']} weeks"
        assert rule in joined, subs
        expect(page.locator(".q-card__measure")).to_contain_text("How to judge it")
    _()

    @check("evidence states each signal's period; experiment plan is complete and only 'Proposed'")
    def _():
        page.goto(f"{run.base}/#/evidence")
        first = page.locator(".trace-row").first
        expect(first).to_contain_text("959.62")
        assert "853" not in first.inner_text()
        first.locator("summary").click()
        expect(first.locator(".trace-row__detail")).to_contain_text("Period and sample")
        expect(first.locator(".trace-row__detail")).to_contain_text(f"8 weeks to {ctx['generated_at'][:10]}")
        rec = page.locator(".rec")
        for label in ("Hypothesis to test", "Baseline", "How to judge it", "What else could move the result"):
            expect(rec).to_contain_text(label)
        expect(rec).to_contain_text(ins["action"]["baseline"][:40])
        current = page.locator(".rec__stage[aria-current='step']")
        expect(current).to_have_count(1)
        expect(current).to_have_text("Proposed")
    _()

    @check("copilot answers via real /ask with source and fallback reason shown")
    def _():
        page.goto(f"{run.base}/#/copilot")
        with page.expect_response(lambda r: r.url.endswith("/ask")) as resp:
            page.get_by_role("button", name="What should I try next?").click()
        body = resp.value.request.post_data_json
        assert body == {"question": "What should I try next?", "language": "en"}, body
        msg = page.locator(".msg--assistant").last
        expect(msg).to_contain_text("Deterministic explanation")
        expect(msg).to_contain_text("not configured on this server")
        msg.get_by_role("button", name="Evidence").click()
        expect(msg.locator(".mini-trace li")).to_have_count(len(ins["trace"]))
        run.shot(page, "copilot-desktop")
    _()

    @check("composer: Enter submits, empty input cannot submit, no duplicate sends")
    def _():
        box = page.locator("#composer-input")
        expect(page.locator(".composer__send")).to_be_disabled()
        before = page.locator(".msg--user").count()
        box.fill("How much did I earn this week?")
        calls = []
        page.on("request", lambda r: r.url.endswith("/ask") and calls.append(1))
        box.press("Enter")
        box.press("Enter")
        page.wait_for_selector(".msg--thinking", state="detached")
        expect(page.locator(".msg--user")).to_have_count(before + 1)
        page.wait_for_timeout(300)
        assert len(calls) == 1, calls
    _()

    @check("model-like output is rendered as text, never HTML")
    def _():
        page.route("**/ask", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"answer": "<img src=x onerror=window.__pwned=1><b>bold</b>", "trace": [], "action": None,
                             "source": "llm", "fallback_reason": None, "language": "en"})))
        page.locator("#composer-input").fill("xss probe")
        page.locator("#composer-input").press("Enter")
        expect(page.locator(".msg--assistant").last).to_contain_text("<b>bold</b>")
        assert page.evaluate("window.__pwned === undefined")
        assert page.locator(".msg--assistant img").count() == 0
        page.unroute("**/ask")
    _()

    @check("API failure shows an honest error with a working retry")
    def _():
        page.allow_http_errors = True
        page.route("**/ask", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"boom"}'))
        page.locator("#composer-input").fill("Why was Tuesday so slow?")
        page.locator("#composer-input").press("Enter")
        err = page.locator(".msg--error").last
        expect(err).to_contain_text("500")
        expect(err).to_contain_text("not answered")
        page.unroute("**/ask")
        page.allow_http_errors = False
        err.get_by_role("button", name="Retry").click()
        expect(page.locator(".msg--error")).to_have_count(0)
        expect(page.locator(".msg--assistant").last).to_contain_text("Tuesday")
    _()

    @check("rate limit (429) is explained, and the session cookie is HttpOnly")
    def _():
        cookies = {c["name"]: c for c in page.context.cookies()}
        sid = cookies.get("salah_sid")
        assert sid and sid["httpOnly"] and sid["sameSite"] == "Lax" and sid["path"] == "/", cookies
        assert page.evaluate("document.cookie.includes('salah_sid')") is False  # unreadable from JS
        page.allow_http_errors = True
        page.route("**/ask", lambda route: route.fulfill(status=429, content_type="text/plain", body="Too Many Requests"))
        page.locator("#composer-input").fill("rate limit probe")
        page.locator("#composer-input").press("Enter")
        expect(page.locator(".msg--error").last).to_contain_text("Too many requests right now")
        page.unroute("**/ask")
        page.allow_http_errors = False
        page.locator(".msg--error").last.get_by_role("button", name="Retry").click()
        expect(page.locator(".msg--error")).to_have_count(0)
        page.wait_for_selector(".msg--thinking", state="detached")  # let the retried answer land
    _()

    @check("an answer the server could not remember says so (capacity)")
    def _():
        def not_remembered(route):
            resp = route.fetch(headers=run.headers(route.request))
            data = resp.json()
            data["remembered"] = False
            route.fulfill(response=resp, json=data)
        page.wait_for_selector(".msg--thinking", state="detached")
        page.route("**/ask", not_remembered)
        try:
            page.locator("#composer-input").fill("capacity probe")
            page.locator("#composer-input").press("Enter")
            msg = page.locator(".msg--assistant").last
            expect(msg).to_contain_text("Total revenue")
            expect(msg.locator(".msg__reason--memory")).to_contain_text("won't be remembered")
        finally:
            page.unroute("**/ask")
        page.locator("#composer-input").fill("normal question")
        page.locator("#composer-input").press("Enter")
        page.wait_for_selector(".msg--thinking", state="detached")
        expect(page.locator(".msg--assistant").last.locator(".msg__reason--memory")).to_have_count(0)
    _()

    @check("voice is honestly unavailable without a Sarvam key (real backend)")
    def _():
        mic = page.locator(".icon-btn--mic")
        expect(mic).to_be_disabled()
        expect(page.locator(".composer__note")).to_contain_text("Sarvam API key")
        expect(page.locator(".msg--assistant").first.get_by_role("button", name="Listen")).to_be_disabled()
    _()

    @check("conversation reset asks for confirmation, then clears")
    def _():
        page.route("**/reset", lambda route: route.fulfill(status=200, content_type="application/json", body='{"ok": true}'))
        page.get_by_role("button", name="New conversation").click()
        dialog = page.locator("#reset-dialog")
        expect(dialog).to_be_visible()
        page.locator("#reset-cancel").click()
        assert page.locator(".msg--user").count() > 0
        page.get_by_role("button", name="New conversation").click()
        with page.expect_request("**/reset"):
            page.locator("#reset-confirm").click()
        expect(page.locator(".msg")).to_have_count(0)
        expect(page.locator(".conversation__empty")).to_be_visible()
        page.unroute("**/reset")
    _()

    @check("language switch relabels UI and sends the language to /ask and /nudge")
    def _():
        page.select_option("#lang-select", "hi")
        expect(page.locator('.nav a[data-nav="copilot"]')).to_contain_text("कोपायलट")
        assert page.evaluate("document.documentElement.lang") == "hi"
        with page.expect_response(lambda r: r.url.endswith("/ask")) as resp:
            page.get_by_role("button", name="अब क्या करूँ?").click()
        assert resp.value.request.post_data_json["language"] == "hi"
        assert resp.value.json()["language"] == "hi"
        expect(page.locator(".msg--assistant").last).to_contain_text("कुल कमाई")
        page.goto(f"{run.base}/#/updates")
        with page.expect_response(lambda r: r.url.endswith("/nudge")) as resp:
            page.get_by_role("button", name="अपडेट बनाएँ").click()
        assert resp.value.request.post_data_json == {"force": True, "language": "hi"}
        expect(page.locator(".nudge")).to_have_count(1)
        page.select_option("#lang-select", "en")
        expect(page.locator(".view-title").filter(has_text="Proactive updates")).to_be_visible()
    _()

    @check("proactive update renders the real /nudge text, labelled as on-demand")
    def _():
        with page.expect_response(lambda r: r.url.endswith("/nudge")) as resp:
            page.get_by_role("button", name="Generate update").click()
        text = resp.value.json()["nudge"]
        expect(page.locator(".nudge").first).to_contain_text(text[:60])
        expect(page.locator(".notice--info")).to_contain_text("doesn't run in the background")
        run.shot(page, "updates-desktop")
    _()

    @check("system view reports real integration status")
    def _():
        page.goto(f"{run.base}/#/system")
        cards = page.locator(".status-card")
        expect(cards.nth(0)).to_contain_text("Online")
        expect(cards.nth(1)).to_contain_text("Not configured")
        expect(cards.nth(2)).to_contain_text("Not configured")
        assert "interactions.jsonl" not in page.content(), "internal path leaked"
    _()

    @check("keyboard: skip link, nav focus and heading focus on route change")
    def _():
        run.ready(page)
        page.reload()
        page.wait_for_selector(".kpi__value")
        page.keyboard.press("Tab")
        active = page.evaluate("document.activeElement.className")
        assert active == "skip-link", f"first Tab focused {active!r}"
        page.keyboard.press("Enter")
        active = page.evaluate("document.activeElement.id")
        assert active == "main", f"skip link moved focus to {active!r}"
        page.locator('.nav a[data-nav="evidence"]').focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        active = page.evaluate("document.activeElement.outerHTML.slice(0, 120)")
        assert page.evaluate("document.activeElement.matches('[data-view-heading]')"), f"focus on {active}"
        assert page.evaluate("document.querySelector('.nav a[aria-current=page]').dataset.nav") == "evidence"
    _()

    @check("no unexpected console errors or failed requests during the session")
    def _():
        assert not page.errors, page.errors
    _()
    page.context.close()

    # --- voice success path (mocked Sarvam + fake microphone) -----------------
    vpage = run.page()

    @check("voice flow: record → transcript for review → send → listen → stop (Sarvam mocked)")
    def _():
        def health(route):
            resp = route.fetch(headers=run.headers(route.request))
            data = resp.json()
            data["voice"] = {"available": True, "languages": ["en", "hi"]}
            route.fulfill(response=resp, json=data)
        vpage.route("**/api/health", health)
        stt_types = []
        def stt(route):
            stt_types.append(route.request.headers.get("content-type", ""))
            route.fulfill(status=200, content_type="application/json", body='{"text": "is hafte kitna kamaya"}')
        vpage.route("**/voice/stt", stt)
        vpage.route("**/voice/tts", lambda route: route.fulfill(status=200, content_type="audio/wav", body=tiny_wav()))
        run.ready(vpage, "copilot")
        mic = vpage.locator(".icon-btn--mic")
        expect(mic).to_be_enabled()
        mic.click()
        expect(vpage.locator(".voice-bar[data-state='recording']")).to_be_visible()
        expect(vpage.locator(".voice-bar")).to_contain_text("Recording")
        vpage.wait_for_timeout(1500)
        mic.click()
        expect(vpage.locator(".voice-bar[data-state='transcribed']")).to_be_visible()
        expect(vpage.locator("#composer-input")).to_have_value("is hafte kitna kamaya")
        assert stt_types and stt_types[0].startswith("multipart/form-data"), stt_types
        vpage.locator(".composer__send").click()
        msg = vpage.locator(".msg--assistant").last
        expect(msg).to_contain_text("Total revenue")
        listen = msg.get_by_role("button", name="Listen")
        expect(listen).to_be_enabled()
        listen.click()
        stop = msg.get_by_role("button", name="Stop")
        expect(stop).to_be_visible()
        stop.click()
        expect(msg.get_by_role("button", name="Listen")).to_be_visible()
    _()

    @check("voice: microphone permission denied is explained")
    def _():
        context = run.context(viewport={"width": 1280, "height": 800}, permissions=[])
        pg = context.new_page()
        pg.add_init_script("""navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('denied', 'NotAllowedError'));""")
        def health(route):
            resp = route.fetch(headers=run.headers(route.request))
            data = resp.json()
            data["voice"] = {"available": True, "languages": ["en", "hi"]}
            route.fulfill(response=resp, json=data)
        pg.route("**/api/health", health)
        pg.goto(f"{run.base}/#/copilot")
        pg.locator(".icon-btn--mic").click()
        expect(pg.locator(".voice-bar[data-state='error']")).to_contain_text("permission was denied")
        context.close()
    _()

    @check("voice: STT failure (503) is reported, not faked")
    def _():
        vpage.allow_http_errors = True
        vpage.unroute("**/voice/stt")
        vpage.route("**/voice/stt", lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":"STT unavailable"}'))
        mic = vpage.locator(".icon-btn--mic")
        mic.click()
        # Time the recording from when it starts, not from the click: under
        # load the permission step alone can take most of a second, which
        # left a near-empty clip ("No speech was captured") intermittently.
        expect(vpage.locator(".voice-bar[data-state='recording']")).to_be_visible()
        vpage.wait_for_timeout(1200)
        mic.click()
        expect(vpage.locator(".voice-bar[data-state='error']")).to_contain_text("STT unavailable")
        vpage.allow_http_errors = False
    _()
    vpage.context.close()

    @check("data load failure: honest error panel, no numbers, retry recovers")
    def _():
        pg = run.page()
        pg.allow_http_errors = True
        pg.route("**/context", lambda r: r.fulfill(status=503, content_type="application/json",
                                                   body='{"detail":"Merchant data unavailable: test"}'))
        pg.goto(f"{run.base}/#/overview")
        err = pg.locator(".error-panel, [role='alert']").first
        expect(err).to_contain_text("Couldn't load merchant data")
        expect(err).to_contain_text("503")
        assert pg.locator(".kpi__value").count() == 0, "numbers shown despite failed load"
        pg.unroute("**/context")
        pg.allow_http_errors = False
        pg.get_by_role("button", name="Retry").first.click()
        expect(pg.locator(".kpi__value").first).to_have_text("₹7,677")
        pg.context.close()
    _()

    # --- rendering modes ------------------------------------------------------
    @check("reduced motion: 3D renders a single static frame")
    def _():
        pg = run.page(reduced_motion="reduce")
        run.ready(pg)
        pg.wait_for_function("document.documentElement.dataset.render === 'static'", timeout=10000)
        info = pg.evaluate("window.__salah.stage.info()")
        assert info["mode"] == "static" and info["reducedMotion"], info
        pg.context.close()
    _()

    @check("WebGL unavailable: static diagram fallback, analytics still usable")
    def _():
        pg = run.page()
        pg.add_init_script("""
          const orig = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function(type, ...rest) {
            if (String(type).startsWith('webgl')) return null;
            return orig.call(this, type, ...rest);
          };""")
        run.ready(pg)
        pg.wait_for_function("document.documentElement.dataset.render === 'off'")
        expect(pg.locator(".hero__core .core-fallback")).to_be_visible()
        expect(pg.locator(".kpi__value").first).to_have_text("₹7,677")
        run.shot(pg, "overview-no-webgl")
        pg.context.close()
    _()

    @check("render mode setting switches 3D off and back on")
    def _():
        pg = run.page()
        run.ready(pg, "system")
        pg.select_option("#render-mode", "off")
        pg.wait_for_function("document.documentElement.dataset.render === 'off'")
        pg.select_option("#render-mode", "low")
        pg.wait_for_function("document.documentElement.dataset.render === 'low'", timeout=10000)
        pg.evaluate("localStorage.removeItem('salah_render')")
        pg.context.close()
    _()

    # --- responsive layouts -----------------------------------------------------
    for w, h in [(320, 640), (360, 740), (390, 844), (768, 1024), (1280, 800), (1440, 900)]:
        @check(f"responsive {w}px: no horizontal overflow on any view")
        def _(w=w, h=h):
            pg = run.page(viewport=(w, h))
            run.ready(pg)
            for view in ["overview", "evidence", "copilot", "updates", "system"]:
                pg.goto(f"{run.base}/#/{view}")
                pg.wait_for_timeout(350)
                sw, iw = pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
                assert sw <= iw, f"{view}: scrollWidth {sw} > {iw}"
                if view in ("overview", "evidence", "copilot") and w in (320, 390, 768):
                    run.shot(pg, f"{view}-{w}")
            if w <= 760:
                expect(pg.locator(".tabbar")).to_be_visible()
                expect(pg.locator(".rail")).to_be_hidden()
            assert not pg.errors, pg.errors
            pg.context.close()
        _()


if __name__ == "__main__":
    main()
