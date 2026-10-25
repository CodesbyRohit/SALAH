// SALAH merchant workspace — bootstrap, routing, data loading and actions.

import { createApi, ApiError } from "./api.js";
import { createStore, resource } from "./store.js";
import { translate, LANGUAGES } from "./i18n.js";
import * as fmt from "./format.js";
import { $, h, icon, replace } from "./dom.js";
import { createStage } from "./scene/stage.js";
import { createVoice, voiceSupport } from "./voice.js";
import { transition } from "./voice-machine.js";
import { createOverview } from "./views/overview.js";
import { createEvidence } from "./views/evidence.js";
import { createCopilot } from "./views/copilot.js";
import { createUpdates } from "./views/updates.js";
import { createSystem } from "./views/system.js";
import { errorText } from "./views/shared.js";

const VIEWS = ["overview", "evidence", "copilot", "updates", "system"];

const storage = {
  get(key, fallback) {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* private mode etc. — preference just won't persist */
    }
  },
};

const initialLang = storage.get("salah_lang", "en");
const support = voiceSupport();

const store = createStore({
  lang: LANGUAGES[initialLang] ? initialLang : "en",
  view: "overview",
  health: resource("loading"),
  context: resource("loading"),
  insights: resource("loading"),
  messages: [],
  asking: false,
  draft: "",
  nudges: [],
  nudgeStatus: "idle",
  nudgeError: null,
  voice: { state: "unavailable", reason: support === "ok" ? "unknown" : support, message: "", error: null, startedAt: 0 },
  playingId: null,
  playLoading: null,
  readAloud: storage.get("salah_read_aloud", "0") === "1",
  renderPref: storage.get("salah_render", "auto"),
  renderInfo: null,
  evidenceFilter: "ALL",
});

const api = createApi();
const t = (key, vars) => translate(store.get().lang, key, vars);

// --- stage (3D) ---------------------------------------------------------
const stage = createStage({
  canvas: $("#core-canvas"),
  onInfo: (info) => store.set({ renderInfo: info }),
});

// --- actions --------------------------------------------------------------
let msgSeq = 0;
let askGeneration = 0;
let askAbort = null;

const actions = {
  async reloadData() {
    const { context, insights } = store.get();
    store.set({
      context: resource("loading", context.data),
      insights: resource("loading", insights.data),
    });
    const [ctx, ins] = await Promise.allSettled([api.context(), api.insights()]);
    store.set({
      context: ctx.status === "fulfilled" ? resource("ready", ctx.value) : resource("error", context.data, ctx.reason),
      insights: ins.status === "fulfilled" ? resource("ready", ins.value) : resource("error", insights.data, ins.reason),
    });
  },

  async refreshHealth() {
    store.set({ health: resource("loading", store.get().health.data) });
    try {
      const data = await api.health();
      store.set({ health: resource("ready", data) });
    } catch (err) {
      store.set({ health: resource("error", null, err) });
    }
    syncVoiceAvailability();
  },

  async ask(question, { retryOf } = {}) {
    const q = question.trim().slice(0, 1000);
    if (!q || store.get().asking) return;
    const lang = store.get().lang;
    const gen = ++askGeneration;
    askAbort = new AbortController();
    let messages = store.get().messages;
    if (retryOf) messages = messages.filter((m) => m.id !== retryOf);
    else messages = [...messages, { id: ++msgSeq, role: "user", text: q }];
    if (store.get().voice.state === "error") voiceSend("reset");
    voiceSend("submit");
    store.set({ messages, asking: true, draft: "" });
    stage.setState("thinking");
    try {
      const res = await api.ask(q, lang, { signal: askAbort.signal });
      if (gen !== askGeneration) return;
      if (!res.answer) throw new ApiError("shape", t("copilot.emptyAnswer"));
      const msg = {
        id: ++msgSeq,
        role: "assistant",
        text: res.answer,
        meta: { source: res.source, fallback_reason: res.fallback_reason, trace: res.trace, action: res.action, language: res.language || lang, remembered: res.remembered },
      };
      store.set({ messages: [...store.get().messages, msg], asking: false });
      voiceSend("answered");
      stage.setState("answered");
      if (res.trace) stage.setData({ trace: res.trace });
      const { readAloud, health } = store.get();
      if (readAloud && health.data?.voice_available && health.data.voice_languages.includes(msg.meta.language)) {
        voice.play(msg.id, msg.text, msg.meta.language);
      }
    } catch (err) {
      if (gen !== askGeneration) return;
      if (err instanceof ApiError && err.kind === "aborted") {
        store.set({ asking: false });
        return;
      }
      store.set({
        messages: [...store.get().messages, { id: ++msgSeq, role: "error", question: q, error: err }],
        asking: false,
      });
      voiceSend("fail");
      voiceSend("reset");
      stage.setState("error");
    } finally {
      if (gen === askGeneration) askAbort = null;
    }
  },

  retry(id) {
    const msg = store.get().messages.find((m) => m.id === id);
    if (msg?.question) actions.ask(msg.question, { retryOf: id });
  },

  confirmReset() {
    const dialog = $("#reset-dialog");
    replace($("#reset-title"), t("copilot.resetTitle"));
    replace($("#reset-body"), t("copilot.resetBody"));
    replace($("#reset-cancel"), t("common.cancel"));
    replace($("#reset-confirm"), t("copilot.resetConfirm"));
    if (typeof dialog.showModal === "function") dialog.showModal();
    else if (window.confirm(t("copilot.resetBody"))) actions.reset();
  },

  async reset() {
    askGeneration++;
    askAbort?.abort();
    voice.stopPlayback();
    try {
      await api.reset();
      store.set({ messages: [], asking: false, draft: "" });
      stage.setState("idle");
    } catch (err) {
      // The server still holds the conversation, so keep it visible here too.
      store.set({ asking: false });
      toast(t("copilot.resetFailed", { reason: errorText(t, err) }));
    }
    copilot.focusInput();
  },

  async nudge() {
    if (store.get().nudgeStatus === "loading") return;
    const lang = store.get().lang;
    store.set({ nudgeStatus: "loading", nudgeError: null });
    try {
      const res = await api.nudge(lang);
      store.set({ nudges: [{ ...res, at: new Date() }, ...store.get().nudges], nudgeStatus: "idle" });
    } catch (err) {
      store.set({ nudgeStatus: "error", nudgeError: err });
    }
  },

  setReadAloud(on) {
    storage.set("salah_read_aloud", on ? "1" : "0");
    store.set({ readAloud: on });
  },

  setRenderPref(mode) {
    storage.set("salah_render", mode);
    store.set({ renderPref: mode });
    stage.setPreference(mode);
  },

  voice: null,
};

function voiceSend(event) {
  const v = store.get().voice;
  const next = transition(v.state, event);
  if (next !== v.state) store.set({ voice: { ...v, state: next } });
}

function syncVoiceAvailability() {
  const v = store.get().voice;
  const hd = store.get().health.data;
  let reason = null;
  if (support !== "ok") reason = support;
  else if (!hd) reason = store.get().health.status === "error" ? "server" : "unknown";
  else if (!hd.voice_available) reason = "server";
  if (reason) {
    if (v.state !== "unavailable" || v.reason !== reason) store.set({ voice: { ...v, state: "unavailable", reason } });
  } else if (v.state === "unavailable") {
    store.set({ voice: { ...v, state: transition("unavailable", "available"), reason: null } });
  }
}

const voice = createVoice({
  store,
  api,
  onLevel: (v) => {
    stage.setLevel(v);
    copilot.setLevel(v);
  },
  onTranscript: (text) => {
    store.set({ draft: text });
    copilot.focusInput();
  },
});
actions.voice = voice;

function toast(text) {
  const region = $("#toast");
  region.textContent = text;
  region.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (region.hidden = true), 4000);
}

// --- views ----------------------------------------------------------------
const ctx = { store, t, actions };
const overview = createOverview(ctx);
const evidence = createEvidence(ctx);
const copilot = createCopilot(ctx);
const updates = createUpdates(ctx);
const system = createSystem(ctx);
const viewModules = { overview, evidence, copilot, updates, system };

for (const name of VIEWS) $(`section[data-view="${name}"]`).append(viewModules[name].el);

/** Re-render a view while keeping keyboard focus on the same control. */
function renderView(name) {
  const activeId = document.activeElement?.id;
  viewModules[name].render();
  if (activeId && !document.getElementById(activeId)?.isConnected) return;
  if (activeId && document.activeElement?.id !== activeId) document.getElementById(activeId)?.focus({ preventScroll: true });
}

store.subscribe(["context", "insights"], () => {
  renderView("overview");
  renderView("evidence");
  const { context, insights } = store.get();
  stage.setData({
    weekly: context.data?.weekly_revenue
      ? context.data.weekly_revenue.map((w, i) => ({
          revenue: w.revenue,
          partial: i === 0 && context.data.weekly_revenue.length > 1 && fmt.windowStartsMidWeek(context.data.generated_at),
        }))
      : [],
    trace: insights.data?.trace || [],
    action: insights.data?.action || null,
  });
  if (insights.status === "error" || context.status === "error") stage.setState("error");
  renderShell();
});
store.subscribe(["evidenceFilter"], () => renderView("evidence"));
store.subscribe(["health", "voice"], renderShell);
store.subscribe(["asking", "voice"], () => {
  const { asking, voice: v } = store.get();
  if (asking || v.state === "processing") stage.setState("thinking");
  else if (v.state === "recording") stage.setState("listening");
  else if (v.state === "error") stage.setState("error");
  else stage.setState("idle"); // an "answered" pulse is set explicitly after this
});

// --- shell ------------------------------------------------------------------
function renderShell() {
  const { lang, health, context, view } = store.get();
  document.documentElement.lang = lang;
  document.title = `SALAH — ${t(`nav.${view}`)} · ${t("app.descriptor")}`;
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-label]")) node.setAttribute("aria-label", t(node.dataset.i18nLabel));

  const hd = health.data;
  const pill = (key, state, label, iconName) =>
    h("a", { class: `pill pill--${state}`, href: "#/system", "data-pill": key }, h("span", { class: `status-dot status-dot--${state}`, "aria-hidden": "true" }), icon(iconName, 14), h("span", { class: "pill__label", text: label }));
  replace(
    $("#status-pills"),
    pill("api", hd ? "ok" : health.status === "error" ? "off" : "neutral", hd ? t("shell.apiOn") : health.status === "error" ? t("shell.apiOff") : t("shell.checking"), "system"),
    pill("ai", hd ? (hd.llm_available ? "ok" : "neutral") : "neutral", hd?.llm_available ? t("shell.aiOn") : t("shell.aiOff"), "spark"),
    pill("voice", hd?.voice_available ? "ok" : "neutral", hd?.voice_available ? t("shell.voiceOn") : t("shell.voiceOff"), "mic"),
  );

  const end = context.data?.generated_at;
  $("#data-through").textContent = end ? t("shell.dataThrough", { date: fmt.date(end, lang) }) : "";
  $("#lang-select").value = lang;
}

$("#lang-select").addEventListener("change", (e) => {
  const lang = LANGUAGES[e.target.value] ? e.target.value : "en";
  storage.set("salah_lang", lang);
  store.set({ lang });
  for (const name of VIEWS) renderView(name);
  renderShell();
});

$("#reset-dialog").addEventListener("close", (e) => {
  if (e.target.returnValue === "confirm") actions.reset();
});

// --- routing ------------------------------------------------------------------
function currentRoute() {
  // Only "#/name" hashes are routes; in-page anchors (e.g. #main) keep the view.
  if (location.hash && !location.hash.startsWith("#/")) return store.get().view;
  const name = location.hash.slice(2);
  return VIEWS.includes(name) ? name : "overview";
}

let firstRoute = true;
function route() {
  const name = currentRoute();
  store.set({ view: name });
  for (const section of document.querySelectorAll("section[data-view]")) section.hidden = section.dataset.view !== name;
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (name === "updates" || name === "system") renderView(name);
  stage.setAnchor(viewModules[name].anchor?.() || null);
  document.documentElement.dataset.route = name;
  renderShell();
  if (!firstRoute) {
    window.scrollTo({ top: 0 });
    const heading = $(`section[data-view="${name}"] [data-view-heading]`);
    heading?.focus({ preventScroll: true });
  }
  firstRoute = false;
}
window.addEventListener("hashchange", route);

$(".skip-link").addEventListener("click", (e) => {
  e.preventDefault();
  $("#main").focus();
});

// --- boot -----------------------------------------------------------------------
for (const slot of document.querySelectorAll("[data-icon]")) {
  slot.replaceWith(icon(slot.dataset.icon, slot.dataset.icon === "globe" ? 15 : 20));
}
for (const name of VIEWS) renderView(name);
route();
renderShell();
actions.refreshHealth();
actions.reloadData();
stage.init(store.get().renderPref);
document.documentElement.classList.add("is-ready");

// Exposed for diagnostics and end-to-end tests only.
window.__salah = { store, stage: { info: () => stage.info() } };
