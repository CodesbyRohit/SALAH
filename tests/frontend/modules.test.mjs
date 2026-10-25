// Unit tests for the frontend's pure modules (no DOM, no network).
// Run: node --test tests/frontend/

import { test } from "node:test";
import assert from "node:assert/strict";

import * as fmt from "../../frontend/js/format.js";
import {
  createApi,
  ApiError,
  normalizeContext,
  normalizeTrace,
  normalizeAction,
  normalizeAnswer,
  normalizeHealth,
} from "../../frontend/js/api.js";
import { transition, VOICE_STATES, pickRecordingMime, extensionFor, canRecord } from "../../frontend/js/voice-machine.js";
import { DICTIONARIES, translate, flattenKeys } from "../../frontend/js/i18n.js";
import { createStore } from "../../frontend/js/store.js";
import { resolveMode } from "../../frontend/js/scene/stage.js";

// --- format ---------------------------------------------------------------
test("money renders backend values without changing them", () => {
  assert.equal(fmt.money(7677.0, "en"), "₹7,677");
  assert.equal(fmt.money(31.06, "en"), "₹31.06");
  assert.equal(fmt.money(35.1, "en"), "₹35.10");
  assert.equal(fmt.money(null), "—");
  assert.equal(fmt.money(Number.NaN), "—");
});

test("percent keeps backend 0–100 units and signs on request", () => {
  assert.equal(fmt.percent(38.3, "en"), "38.3%");
  assert.equal(fmt.percent(-11.5, "en", { signed: true }), "-11.5%");
  assert.equal(fmt.percent(4, "en", { signed: true }), "+4%");
  assert.equal(fmt.percent(undefined), "—");
});

test("weekday names are localized for display only", () => {
  assert.equal(fmt.weekday("Tuesday", "en"), "Tuesday");
  assert.equal(fmt.weekday("Tuesday", "hi"), "मंगलवार");
  assert.equal(fmt.weekday("Tuesday", "fr"), "mardi");
  assert.equal(fmt.weekday("Someday", "en"), "Someday");
});

test("ISO week ranges are correct across a year boundary", () => {
  const w51 = fmt.isoWeekRange("2024-W51");
  assert.equal(w51.start.toDateString(), new Date(2024, 11, 16).toDateString());
  assert.equal(w51.end.toDateString(), new Date(2024, 11, 22).toDateString());
  const w01 = fmt.isoWeekRange("2025-W01");
  assert.equal(w01.start.toDateString(), new Date(2024, 11, 30).toDateString());
  assert.equal(fmt.isoWeekRange("bogus"), null);
});

test("partial first week is detected from the window start", () => {
  // 2024-12-22T16:45 minus 8 weeks = Sunday 2024-10-27 16:45 → mid-week start.
  assert.equal(fmt.windowStartsMidWeek("2024-12-22T16:45:00"), true);
  // Ending Monday 00:00 → window starts exactly on a Monday.
  assert.equal(fmt.windowStartsMidWeek("2024-12-23T00:00:00"), false);
  assert.equal(fmt.windowStartsMidWeek(null), false);
});

test("duration formats recording time", () => {
  assert.equal(fmt.duration(0), "0:00");
  assert.equal(fmt.duration(65_400), "1:05");
});

// --- api normalization ------------------------------------------------------
const CONTEXT = {
  generated_at: "2024-12-22T16:45:00",
  weekly_revenue: [{ week: "2024-W51", revenue: 1116.0 }, { week: 5, revenue: "x" }],
  revenue_by_hour: [{ hour: 8, revenue: 226.0 }],
  weakest_weekday: { weekday: "Tuesday", avg_daily_revenue: 46.62, overall_avg_daily_revenue: 137.09 },
  lapsed_regulars: { count: 8, customers: [{ customer_id: "CUST_031", last_visit: "2024-10-06T08:10:00" }] },
  ticket_trend: { early_avg_ticket: 35.1, recent_avg_ticket: 31.06, change_pct: -11.5 },
  evening_share: { evening_revenue: 2942.0, total_revenue: 7677.0, evening_share_pct: 38.3 },
  weekday_profile: { weeks: 8, overall_avg_daily_revenue: 137.09, days: { Tuesday: { days_in_window: 8, avg_daily_revenue: 46.62 } } },
};

test("normalizeContext keeps valid sections and drops malformed rows", () => {
  const c = normalizeContext(CONTEXT);
  assert.deepEqual(c.weekly_revenue, [{ week: "2024-W51", revenue: 1116.0 }]);
  assert.equal(c.evening_share.total_revenue, 7677.0);
  assert.equal(c.lapsed_regulars.count, 8);
});

test("normalizeContext turns a malformed section into null (honest unavailable)", () => {
  const c = normalizeContext({ ...CONTEXT, ticket_trend: { early_avg_ticket: "35" }, evening_share: null });
  assert.equal(c.ticket_trend, null);
  assert.equal(c.evening_share, null);
  assert.equal(c.weakest_weekday.weekday, "Tuesday");
  assert.throws(() => normalizeContext("nope"), ApiError);
});

test("normalizeTrace downgrades unknown confidence levels to UNKNOWN", () => {
  const t = normalizeTrace([
    { signal: "a", value: "1", implication: "x", source: "s", confidence: "FACT" },
    { signal: "b", value: 2, confidence: "CERTAIN" },
    { value: "no signal" },
  ]);
  assert.equal(t.length, 2);
  assert.equal(t[0].confidence, "FACT");
  assert.equal(t[1].confidence, "UNKNOWN");
  assert.equal(t[1].value, "2");
  assert.equal(normalizeTrace("x"), null);
});

test("normalizeAction requires a priority", () => {
  assert.equal(normalizeAction({ observed: "x" }), null);
  const a = normalizeAction({ priority: "weak_weekday", weeks: 4, experiment: "try" });
  assert.equal(a.priority, "weak_weekday");
  assert.equal(a.weeks, 4);
  assert.equal(a.measure, "");
});

test("normalizeAnswer rejects a response without an answer string", () => {
  assert.throws(() => normalizeAnswer({ trace: [] }), ApiError);
  const a = normalizeAnswer({ answer: " hi ", source: "deterministic", fallback_reason: "llm_unavailable" });
  assert.equal(a.answer, "hi");
  assert.equal(a.trace, null);
  assert.equal(a.fallback_reason, "llm_unavailable");
});

test("normalizeHealth treats an unreported voice status as unavailable", () => {
  const old = normalizeHealth({ status: "ok", llm_available: false });
  assert.equal(old.voice_available, false);
  assert.equal(old.voice_reported, false);
  const h = normalizeHealth({ status: "ok", llm_available: true, voice: { available: true, languages: ["en", "hi"] }, memory: { local_log: "C:/secret/path", local_log_entries: 3 } });
  assert.equal(h.voice_available, true);
  assert.deepEqual(h.voice_languages, ["en", "hi"]);
  assert.equal(JSON.stringify(h).includes("secret"), false, "internal paths must not survive normalization");
});

// --- api transport ------------------------------------------------------------
const jsonResponse = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("api.ask posts question + language and validates the reply", async () => {
  let seen;
  const api = createApi({
    fetchImpl: async (url, init) => {
      seen = { url, init };
      return jsonResponse(200, { answer: "ok", trace: [], action: null, source: "deterministic", fallback_reason: "llm_unavailable", language: "hi" });
    },
  });
  const res = await api.ask("kya karun?", "hi");
  assert.equal(seen.url, "/ask");
  assert.deepEqual(JSON.parse(seen.init.body), { question: "kya karun?", language: "hi" });
  assert.equal(res.language, "hi");
});

test("api surfaces HTTP errors with the server's detail", async () => {
  const api = createApi({ fetchImpl: async () => jsonResponse(503, { detail: "STT unavailable" }) });
  await assert.rejects(api.stt(new Blob(["x"]), "q.webm"), (e) => e instanceof ApiError && e.kind === "http" && e.status === 503 && e.detail === "STT unavailable");
});

test("api distinguishes network failure, timeout and cancellation", async () => {
  const down = createApi({ fetchImpl: async () => { throw new TypeError("Failed to fetch"); } });
  await assert.rejects(down.context(), (e) => e.kind === "network");

  const hang = (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
  const slow = createApi({ fetchImpl: hang, timeoutMs: 20 });
  await assert.rejects(slow.context(), (e) => e.kind === "timeout");

  const ctrl = new AbortController();
  const p = createApi({ fetchImpl: hang }).ask("q", "en", { signal: ctrl.signal });
  ctrl.abort();
  await assert.rejects(p, (e) => e.kind === "aborted");
});

test("api.nudge rejects an empty update instead of showing a blank card", async () => {
  const api = createApi({ fetchImpl: async () => jsonResponse(200, { nudge: "  ", language: "en" }) });
  await assert.rejects(api.nudge("en"), (e) => e.kind === "shape");
});

// --- voice state machine --------------------------------------------------------
test("voice machine follows the happy path", () => {
  let s = "ready";
  for (const [event, expected] of [
    ["start", "requesting"],
    ["granted", "recording"],
    ["stop", "processing"],
    ["transcript", "transcribed"],
    ["submit", "answering"],
    ["answered", "ready"],
    ["play", "playing"],
    ["ended", "ready"],
  ]) {
    s = transition(s, event);
    assert.equal(s, expected, `after ${event}`);
  }
});

test("voice machine handles denial, empty audio and invalid events", () => {
  assert.equal(transition("requesting", "denied"), "error");
  assert.equal(transition("processing", "empty"), "error");
  assert.equal(transition("error", "reset"), "ready");
  assert.equal(transition("ready", "stop"), "ready", "invalid events are ignored");
  assert.equal(transition("recording", "play"), "recording", "cannot play while recording");
  assert.equal(transition("playing", "unavailable"), "unavailable");
  assert.equal(transition("unavailable", "start"), "unavailable");
  assert.equal(transition("unavailable", "available"), "ready");
  assert.ok(canRecord("ready") && !canRecord("processing"));
  assert.ok(VOICE_STATES.includes("transcribed"));
});

test("recording format prefers webm (what the backend labels uploads as)", () => {
  assert.equal(pickRecordingMime((t) => t.startsWith("audio/webm")), "audio/webm;codecs=opus");
  assert.equal(pickRecordingMime((t) => t === "audio/mp4"), "audio/mp4");
  assert.equal(pickRecordingMime(() => false), "");
  assert.equal(extensionFor("audio/ogg;codecs=opus"), "ogg");
  assert.equal(extensionFor(""), "webm");
});

// --- i18n -------------------------------------------------------------------------
test("every language defines exactly the English key set", () => {
  const en = flattenKeys(DICTIONARIES.en).sort();
  for (const [lang, dict] of Object.entries(DICTIONARIES)) {
    assert.deepEqual(flattenKeys(dict).sort(), en, `${lang} keys differ`);
  }
});

test("placeholders survive translation in every language", () => {
  const en = DICTIONARIES.en;
  for (const key of flattenKeys(en)) {
    const base = translate("en", key);
    if (typeof base !== "string") continue;
    const vars = (base.match(/\{(\w+)\}/g) || []).sort();
    for (const lang of Object.keys(DICTIONARIES)) {
      const text = translate(lang, key);
      assert.deepEqual((text.match(/\{(\w+)\}/g) || []).sort(), vars, `${lang}:${key}`);
    }
  }
});

test("translate interpolates and falls back to English", () => {
  assert.equal(translate("en", "overview.headline.lapsed_regulars", { count: 8 }), "8 regular customers have gone quiet.");
  assert.equal(translate("xx", "nav.copilot"), "Copilot");
  assert.equal(translate("en", "no.such.key"), "no.such.key");
  assert.equal(translate("hi", "copilot.suggestions").length, 4);
});

// --- store ---------------------------------------------------------------------------
test("store notifies only subscribers of changed keys", () => {
  const store = createStore({ a: 1, b: 1 });
  const calls = [];
  store.subscribe(["a"], () => calls.push("a"));
  store.subscribe(["b"], () => calls.push("b"));
  store.set({ a: 2 });
  store.set({ a: 2 });
  assert.deepEqual(calls, ["a"]);
});

// --- render mode resolution -------------------------------------------------------------
test("render mode respects WebGL support and reduced motion", () => {
  const base = { webgl: true, reducedMotion: false, lowPower: false };
  assert.equal(resolveMode("auto", base), "full");
  assert.equal(resolveMode("auto", { ...base, lowPower: true }), "low");
  assert.equal(resolveMode("auto", { ...base, reducedMotion: true }), "static");
  assert.equal(resolveMode("full", { ...base, reducedMotion: true }), "static");
  assert.equal(resolveMode("full", { ...base, webgl: false }), "off");
  assert.equal(resolveMode("off", base), "off");
});
