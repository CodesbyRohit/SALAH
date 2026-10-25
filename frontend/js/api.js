// Centralized API service. Same-origin by default (the FastAPI app serves
// both the frontend and the API). Every response is shape-checked; sections
// that fail validation become `null` so the UI shows an honest unavailable
// state instead of a plausible-looking number.

export const CONFIDENCE_LEVELS = ["FACT", "PATTERN", "HYPOTHESIS", "UNKNOWN"];

export class ApiError extends Error {
  /**
   * @param {"network"|"http"|"shape"|"aborted"|"timeout"} kind
   */
  constructor(kind, message, { status = 0, detail = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const isStr = (v) => typeof v === "string";

function rows(value, check) {
  if (!Array.isArray(value)) return null;
  return value.filter((r) => isObj(r) && check(r));
}

export function normalizeContext(raw) {
  if (!isObj(raw)) throw new ApiError("shape", "context is not an object");
  const out = {
    generated_at: isStr(raw.generated_at) ? raw.generated_at : null,
    weekly_revenue: rows(raw.weekly_revenue, (r) => isStr(r.week) && isNum(r.revenue)),
    revenue_by_hour: rows(raw.revenue_by_hour, (r) => Number.isInteger(r.hour) && isNum(r.revenue)),
    weakest_weekday: null,
    lapsed_regulars: null,
    ticket_trend: null,
    evening_share: null,
    weekday_profile: null,
  };

  const ww = raw.weakest_weekday;
  if (isObj(ww) && isStr(ww.weekday) && isNum(ww.avg_daily_revenue) && isNum(ww.overall_avg_daily_revenue)) {
    out.weakest_weekday = ww;
  }
  const lr = raw.lapsed_regulars;
  if (isObj(lr) && Number.isInteger(lr.count)) {
    out.lapsed_regulars = {
      count: lr.count,
      customers: rows(lr.customers, (c) => isStr(c.customer_id) && isStr(c.last_visit)) || [],
    };
  }
  const tt = raw.ticket_trend;
  if (isObj(tt) && isNum(tt.early_avg_ticket) && isNum(tt.recent_avg_ticket) && isNum(tt.change_pct)) {
    out.ticket_trend = tt;
  }
  const es = raw.evening_share;
  if (isObj(es) && isNum(es.evening_share_pct) && isNum(es.evening_revenue) && isNum(es.total_revenue)) {
    out.evening_share = es;
  }
  const wp = raw.weekday_profile;
  if (isObj(wp) && isNum(wp.overall_avg_daily_revenue) && isObj(wp.days)) {
    const days = {};
    for (const [name, d] of Object.entries(wp.days)) {
      if (isObj(d) && isNum(d.avg_daily_revenue) && Number.isInteger(d.days_in_window)) days[name] = d;
    }
    out.weekday_profile = { weeks: wp.weeks, overall_avg_daily_revenue: wp.overall_avg_daily_revenue, days };
  }
  // An empty array is real data ("no rows"), distinct from a missing field.
  return out;
}

export function normalizeTrace(raw) {
  if (!Array.isArray(raw)) return null;
  return raw
    .filter((t) => isObj(t) && isStr(t.signal) && t.signal.trim())
    .map((t) => ({
      signal: t.signal,
      value: t.value === null || t.value === undefined ? "" : String(t.value),
      implication: isStr(t.implication) ? t.implication : "",
      source: isStr(t.source) ? t.source : "",
      // An unrecognised level is shown as UNKNOWN: understating certainty is
      // the safe direction (mirrors backend/llm.py sanitize_trace).
      confidence: CONFIDENCE_LEVELS.includes(t.confidence) ? t.confidence : "UNKNOWN",
    }));
}

const ACTION_TEXT_FIELDS = ["observed", "unknown", "experiment", "measure", "basis"];

export function normalizeAction(raw) {
  if (!isObj(raw) || !isStr(raw.priority) || !raw.priority) return null;
  const out = { priority: raw.priority, weeks: Number.isInteger(raw.weeks) ? raw.weeks : null };
  for (const k of ACTION_TEXT_FIELDS) out[k] = isStr(raw[k]) ? raw[k] : "";
  return out;
}

export function normalizeInsights(raw) {
  if (!isObj(raw)) throw new ApiError("shape", "insights is not an object");
  return {
    generated_at: isStr(raw.generated_at) ? raw.generated_at : null,
    trace: normalizeTrace(raw.trace),
    action: normalizeAction(raw.action),
  };
}

export function normalizeAnswer(raw) {
  if (!isObj(raw) || !isStr(raw.answer)) throw new ApiError("shape", "answer missing");
  return {
    answer: raw.answer.trim(),
    trace: normalizeTrace(raw.trace),
    action: normalizeAction(raw.action),
    source: isStr(raw.source) ? raw.source : null,
    fallback_reason: isStr(raw.fallback_reason) ? raw.fallback_reason : null,
    language: isStr(raw.language) ? raw.language : null,
  };
}

export function normalizeHealth(raw) {
  if (!isObj(raw) || raw.status !== "ok") throw new ApiError("shape", "health is not ok");
  const voice = isObj(raw.voice) ? raw.voice : null;
  const memory = isObj(raw.memory) ? raw.memory : null;
  return {
    status: "ok",
    llm_available: raw.llm_available === true,
    // Older backends did not report voice; treat that as "not known to work".
    voice_available: voice ? voice.available === true : false,
    voice_reported: Boolean(voice),
    voice_languages: voice && Array.isArray(voice.languages) ? voice.languages.filter(isStr) : [],
    languages: isObj(raw.languages) ? Object.keys(raw.languages) : ["en", "hi", "es", "fr"],
    memory: memory
      ? {
          cognee_available: memory.cognee_available === true,
          local_log_entries: Number.isInteger(memory.local_log_entries) ? memory.local_log_entries : null,
        }
      : null,
    cache_entries: Number.isInteger(raw.cache_entries) ? raw.cache_entries : null,
    time: isStr(raw.time) ? raw.time : null,
  };
}

async function readDetail(res) {
  try {
    const data = await res.clone().json();
    if (isObj(data) && isStr(data.detail)) return data.detail;
  } catch {
    /* not JSON */
  }
  return "";
}

export function createApi({ fetchImpl = globalThis.fetch?.bind(globalThis), base = "", timeoutMs = 45000 } = {}) {
  async function request(path, { method = "GET", json, body, signal, timeout = timeoutMs, expect = "json" } = {}) {
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, timeout);
    const onAbort = () => ctrl.abort();
    if (signal) {
      if (signal.aborted) ctrl.abort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    const init = { method, signal: ctrl.signal, headers: {} };
    if (json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(json);
    } else if (body !== undefined) {
      init.body = body;
    }

    let res;
    try {
      res = await fetchImpl(base + path, init);
    } catch (err) {
      if (timedOut) throw new ApiError("timeout", "request timed out");
      if (ctrl.signal.aborted) throw new ApiError("aborted", "request cancelled");
      throw new ApiError("network", err?.message || "network error");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }

    if (!res.ok) {
      const detail = await readDetail(res);
      throw new ApiError("http", detail || `HTTP ${res.status}`, { status: res.status, detail });
    }
    if (expect === "blob") return res.blob();
    try {
      return await res.json();
    } catch {
      throw new ApiError("shape", "response is not JSON");
    }
  }

  return {
    request,
    health: async (opts) => normalizeHealth(await request("/api/health", { timeout: 10000, ...opts })),
    context: async (opts) => normalizeContext(await request("/context", opts)),
    insights: async (opts) => normalizeInsights(await request("/insights", opts)),
    ask: async (question, language, opts) =>
      normalizeAnswer(await request("/ask", { method: "POST", json: { question, language }, ...opts })),
    nudge: async (language, opts) => {
      const data = await request("/nudge", { method: "POST", json: { force: true, language }, ...opts });
      if (!isObj(data) || !isStr(data.nudge) || !data.nudge.trim()) throw new ApiError("shape", "empty update");
      return { text: data.nudge.trim(), language: isStr(data.language) ? data.language : language };
    },
    stt: async (blob, filename, opts) => {
      const form = new FormData();
      form.append("file", blob, filename);
      const data = await request("/voice/stt", { method: "POST", body: form, ...opts });
      if (!isObj(data) || !isStr(data.text)) throw new ApiError("shape", "no transcript");
      return data.text.trim();
    },
    tts: (text, language, opts) =>
      request("/voice/tts", { method: "POST", json: { text, language }, expect: "blob", ...opts }),
    reset: (opts) => request("/reset", { method: "POST", ...opts }),
  };
}
