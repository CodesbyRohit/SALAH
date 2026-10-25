// Merchant copilot: conversation over POST /ask, voice capture over
// /voice/stt, spoken answers over /voice/tts. All text is rendered with
// textContent; failures are explained, never simulated.

import { h, icon, replace } from "../dom.js";
import * as fmt from "../format.js";
import { coreFallbackSvg } from "../scene/stage.js";
import { tierBadge, errorText, viewHeader } from "./shared.js";

export const MAX_QUESTION = 1000; // mirrors backend MAX_QUESTION_LENGTH

export function createCopilot({ store, t, actions }) {
  const el = h("div", { class: "view-body view-body--copilot" });
  const nodes = new Map(); // message id -> { msg, node }
  const openEvidence = new Set(); // message ids whose evidence is expanded

  // --- static skeleton (built once, relabelled on language change) -------
  const list = h("ol", { class: "messages", role: "log", "aria-live": "polite", "aria-relevant": "additions" });
  // Shown only while a request is in flight; removed from the log otherwise.
  const thinking = h("li", { class: "msg msg--thinking", "aria-busy": "true" });
  const empty = h("div", { class: "conversation__empty" });
  const scroller = h("div", { class: "conversation__scroll" }, empty, list);

  const input = h("textarea", {
    id: "composer-input",
    class: "composer__input",
    rows: 1,
    maxlength: MAX_QUESTION,
    autocomplete: "off",
    spellcheck: "true",
  });
  const inputLabel = h("label", { class: "sr-only", for: "composer-input" });
  const counter = h("span", { class: "composer__counter", "aria-live": "off" });
  const micBtn = h("button", { type: "button", class: "icon-btn icon-btn--mic" });
  const sendBtn = h("button", { type: "submit", class: "btn btn--primary composer__send" });
  const form = h(
    "form",
    { class: "composer__form" },
    inputLabel,
    input,
    h("div", { class: "composer__actions" }, micBtn, counter, sendBtn),
  );
  const voiceBar = h("div", { class: "voice-bar", hidden: true, role: "status" });
  const voiceNote = h("p", { class: "composer__note" });
  const suggestions = h("div", { class: "suggestions" });
  const composer = h("div", { class: "composer" }, voiceBar, form, voiceNote);

  const header = h("div");
  const readAloud = h("input", { type: "checkbox", id: "read-aloud", class: "switch__input" });
  const readAloudLabel = h("label", { class: "switch", for: "read-aloud" });
  const resetBtn = h("button", { type: "button", class: "btn btn--ghost btn--sm" });

  // Fit radius 2.6: frame the engine and its evidence rings, not the full week arc.
  const sideAnchor = h("div", { class: "side-core", "data-core-anchor": "", "data-core-fit": "2.6" }, coreFallbackSvg());
  const sideStatus = h("p", { class: "side-status", role: "status" });
  const sideMeta = h("dl", { class: "side-meta" });

  el.append(
    header,
    h(
      "div",
      { class: "copilot" },
      h("section", { class: "conversation", "aria-labelledby": "copilot-title" }, scroller, suggestions, composer),
      h("aside", { class: "copilot__side" }, sideAnchor, sideStatus, sideMeta),
    ),
  );

  // --- behaviour ----------------------------------------------------------
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  input.addEventListener("input", () => {
    store.set({ draft: input.value });
    autosize();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || store.get().asking) return;
    actions.ask(q);
  });
  micBtn.addEventListener("click", () => {
    const s = store.get().voice.state;
    if (s === "recording") actions.voice.stop();
    else if (s === "requesting" || s === "processing") actions.voice.cancel();
    else actions.voice.start();
  });
  readAloud.addEventListener("change", () => actions.setReadAloud(readAloud.checked));
  resetBtn.addEventListener("click", actions.confirmReset);

  function scrollToEnd() {
    requestAnimationFrame(() => {
      // Desktop scrolls inside the panel; on mobile the page itself scrolls.
      if (getComputedStyle(scroller).overflowY === "visible") {
        list.lastElementChild?.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      } else {
        scroller.scrollTop = scroller.scrollHeight;
      }
    });
  }

  function voiceSupportedFor(lang) {
    const { health } = store.get();
    return Boolean(health.data?.voice_available && health.data.voice_languages.includes(lang));
  }

  function sourceChip(meta) {
    const key = meta.source === "llm" ? "llm" : meta.source === "cache" ? "cache" : "deterministic";
    return h("span", { class: `source-chip source-chip--${key}` }, icon(key === "llm" ? "spark" : "bolt", 14), t(`copilot.source.${key}`));
  }

  function reasonText(reason) {
    if (!reason) return "";
    if (reason.startsWith("rejected:")) return t("copilot.reason.rejected", { reason: reason.slice(9) });
    const known = t(`copilot.reason.${reason}`);
    return known.startsWith("copilot.") ? reason : known;
  }

  function evidenceBlock(meta, id) {
    const trace = meta.trace || [];
    const action = meta.action;
    return h(
      "div",
      { class: "msg__evidence", id, hidden: true },
      h(
        "ul",
        { class: "mini-trace" },
        trace.map((item) =>
          h(
            "li",
            null,
            tierBadge(t, item.confidence, { compact: true }),
            h("span", { class: "mini-trace__signal", text: fmt.humanizeSignal(item.signal) }),
            h("span", { class: "mini-trace__value", lang: "en", text: item.value }),
          ),
        ),
      ),
      action && action.priority !== "none"
        ? h(
            "p",
            { class: "mini-trace__action" },
            h("strong", { text: `${t(`evidence.rec.priority.${action.priority}`) || action.priority}: ` }),
            h("span", { lang: "en", text: action.experiment }),
          )
        : null,
      h("a", { class: "link-arrow", href: "#/evidence" }, t("nav.evidence"), icon("arrow", 14)),
    );
  }

  function messageNode(msg) {
    const { lang, playingId, playLoading } = store.get();
    if (msg.role === "user") {
      return h(
        "li",
        { class: "msg msg--user" },
        h("p", { class: "msg__who sr-only", text: t("copilot.you") }),
        h("p", { class: "msg__text", text: msg.text }),
      );
    }
    if (msg.role === "error") {
      return h(
        "li",
        { class: "msg msg--error", role: "alert" },
        h("p", { class: "msg__who" }, icon("alert", 16), t("copilot.errorTitle")),
        h("p", { class: "msg__text", text: t("copilot.errorBody", { reason: errorText(t, msg.error) }) }),
        h(
          "div",
          { class: "msg__actions" },
          h("button", { type: "button", class: "btn btn--quiet btn--sm", onclick: () => actions.retry(msg.id) }, icon("retry", 16), t("common.retry")),
        ),
      );
    }
    const meta = msg.meta || {};
    const evId = `ev-${msg.id}`;
    const traceCount = meta.trace?.length || 0;
    const evBtn = traceCount
      ? h(
          "button",
          { type: "button", class: "btn btn--quiet btn--sm", "aria-expanded": "false", "aria-controls": evId },
          icon("evidence", 16),
          t("copilot.evidence", { n: traceCount }),
        )
      : null;
    const block = traceCount ? evidenceBlock(meta, evId) : null;
    evBtn?.addEventListener("click", () => {
      const open = block.hidden;
      block.hidden = !open;
      evBtn.setAttribute("aria-expanded", String(open));
      if (open) openEvidence.add(msg.id);
      else openEvidence.delete(msg.id);
    });
    if (openEvidence.has(msg.id) && block) {
      block.hidden = false;
      evBtn.setAttribute("aria-expanded", "true");
    }
    const msgLang = meta.language || lang;
    const canSpeak = voiceSupportedFor(msgLang);
    const isPlaying = playingId === msg.id;
    const listenBtn = h(
      "button",
      {
        type: "button",
        class: `btn btn--quiet btn--sm${isPlaying ? " is-playing" : ""}`,
        disabled: !canSpeak || undefined,
        title: canSpeak ? null : store.get().health.data?.voice_available ? t("voice.ttsLanguage") : t("voice.unavailableServer"),
        "aria-pressed": String(isPlaying),
        onclick: () => (isPlaying ? actions.voice.stopPlayback() : actions.voice.play(msg.id, msg.text, msgLang)),
      },
      playLoading === msg.id ? h("span", { class: "spinner spinner--sm", "aria-hidden": "true" }) : icon(isPlaying ? "stop" : "speaker", 16),
      isPlaying ? t("copilot.stop") : t("copilot.listen"),
    );
    const reason = meta.source === "deterministic" ? reasonText(meta.fallback_reason) : "";
    return h(
      "li",
      { class: "msg msg--assistant", lang: msgLang },
      h("div", { class: "msg__head" }, h("span", { class: "msg__who", text: t("copilot.salah") }), sourceChip(meta)),
      h("p", { class: "msg__text", text: msg.text }),
      reason ? h("p", { class: "msg__reason" }, icon("info", 14), reason) : null,
      h("div", { class: "msg__actions" }, evBtn, listenBtn),
      block,
    );
  }

  function renderMessages() {
    const { messages, asking } = store.get();
    const seen = new Set();
    let added = false;
    messages.forEach((msg, i) => {
      seen.add(msg.id);
      let entry = nodes.get(msg.id);
      if (!entry || entry.msg !== msg || entry.lang !== store.get().lang || entry.dirty) {
        const node = messageNode(msg);
        if (entry) entry.node.replaceWith(node);
        else added = true;
        entry = { msg, node, lang: store.get().lang };
        nodes.set(msg.id, entry);
      }
      if (list.children[i] !== entry.node) list.insertBefore(entry.node, list.children[i] || null);
    });
    thinking.remove();
    for (const [id, entry] of nodes) {
      if (!seen.has(id)) {
        entry.node.remove();
        nodes.delete(id);
        openEvidence.delete(id);
      }
    }
    replace(thinking, h("p", { class: "msg__who", text: t("copilot.salah") }), h("p", { class: "msg__text typing" }, h("span", { class: "typing__dots", "aria-hidden": "true" }), t("copilot.thinking")));
    if (asking) list.append(thinking);
    else thinking.remove();
    empty.hidden = messages.length > 0 || asking;
    suggestions.hidden = messages.length > 0 || asking;
    if (added || asking) scrollToEnd();
  }

  function markAllDirty() {
    for (const entry of nodes.values()) entry.dirty = true;
  }

  function renderVoice() {
    const { voice, health, lang, playingId } = store.get();
    const s = voice.state;
    const recording = s === "recording";
    const busy = s === "requesting" || s === "processing";
    micBtn.replaceChildren(icon(recording ? "stop" : "mic", 20));
    micBtn.classList.toggle("is-recording", recording);
    micBtn.disabled = s === "unavailable";
    micBtn.setAttribute("aria-label", recording || busy ? t("copilot.micStop") : t("copilot.micStart"));
    micBtn.title = s === "unavailable" ? unavailableText(voice) : recording || busy ? t("copilot.micStop") : t("copilot.micStart");
    micBtn.setAttribute("aria-pressed", String(recording));

    let bar = null;
    if (s === "requesting" || s === "processing") {
      bar = [h("span", { class: "spinner spinner--sm", "aria-hidden": "true" }), h("span", { text: t(`voice.${s}`) }), cancelButton()];
    } else if (recording) {
      bar = [
        h("span", { class: "rec-dot", "aria-hidden": "true" }),
        h("span", null, t("voice.recording"), " ", h("span", { class: "mono", "data-voice-timer": "", text: fmt.duration(Date.now() - (voice.startedAt || Date.now())) })),
        h("span", { class: "level", role: "meter", "aria-label": t("voice.level"), "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0", "data-voice-level": "" }, h("span", { class: "level__fill" })),
        h("button", { type: "button", class: "btn btn--danger btn--sm", onclick: actions.voice.stop }, icon("stop", 14), t("copilot.micStop")),
      ];
    } else if (s === "transcribed") {
      bar = [icon("check", 16), h("span", { text: t("voice.transcribed") }), h("button", { type: "button", class: "btn btn--quiet btn--sm", onclick: actions.voice.dismiss }, t("voice.discard"))];
    } else if (s === "playing" || playingId) {
      bar = [icon("speaker", 16), h("span", { text: t("voice.playing") }), h("button", { type: "button", class: "btn btn--quiet btn--sm", onclick: actions.voice.stopPlayback }, icon("stop", 14), t("copilot.stop"))];
    } else if (s === "error") {
      const msg =
        voice.message === "denied"
          ? t("voice.denied")
          : voice.message === "browser"
            ? t("voice.unavailableBrowser")
            : voice.message === "empty"
              ? t("voice.empty")
              : voice.message === "tts"
                ? t("voice.ttsFailed", { reason: errorText(t, voice.error) })
                : t("voice.sttFailed", { reason: errorText(t, voice.error) });
      bar = [icon("alert", 16), h("span", { text: msg }), h("button", { type: "button", class: "btn btn--quiet btn--sm", onclick: actions.voice.dismiss }, t("common.close"))];
    }
    voiceBar.hidden = !bar;
    voiceBar.dataset.state = s;
    if (bar) replace(voiceBar, bar);

    // Persistent, honest note about voice capability.
    let note = "";
    if (s === "unavailable") note = unavailableText(voice);
    else if (health.data?.voice_available && !health.data.voice_languages.includes(lang)) note = t("voice.ttsLanguage");
    voiceNote.textContent = note;
    voiceNote.hidden = !note;

    const canSpeak = voiceSupportedFor(lang);
    readAloud.disabled = !canSpeak;
    readAloud.checked = canSpeak && store.get().readAloud;
    sideStatus.textContent = sideStatusText();
  }

  function cancelButton() {
    return h("button", { type: "button", class: "btn btn--quiet btn--sm", onclick: actions.voice.cancel }, t("common.cancel"));
  }

  function unavailableText(voice) {
    if (voice.reason === "insecure") return t("voice.unavailableInsecure");
    if (voice.reason === "browser") return t("voice.unavailableBrowser");
    if (voice.reason === "unknown") return t("voice.unknown");
    return t("voice.unavailableServer");
  }

  function sideStatusText() {
    const { asking, voice, playingId } = store.get();
    if (asking) return t("copilot.thinking");
    if (voice.state === "recording") return t("voice.recording");
    if (voice.state === "processing") return t("voice.processing");
    if (playingId) return t("voice.playing");
    return voice.state === "unavailable" ? unavailableText(voice) : t("voice.ready");
  }

  function renderSide() {
    const { health, lang } = store.get();
    const hd = health.data;
    replace(
      sideMeta,
      h("div", null, h("dt", { text: t("shell.language") }), h("dd", { text: fmtLang(lang) })),
      h("div", null, h("dt", { text: t("system.ai") }), h("dd", { text: hd ? (hd.llm_available ? t("common.on") : t("shell.aiOff")) : "—" })),
      h("div", null, h("dt", { text: t("system.voice") }), h("dd", { text: hd ? (hd.voice_available ? hd.voice_languages.join(", ").toUpperCase() : t("system.voiceOff")) : "—" })),
    );
  }

  function fmtLang(lang) {
    return { en: "English", hi: "हिन्दी", es: "Español", fr: "Français" }[lang] || lang;
  }

  function renderStatic() {
    const { lang } = store.get();
    replace(
      header,
      viewHeader({
        eyebrow: t("shell.synthetic"),
        title: t("copilot.title"),
        intro: t("copilot.intro"),
        actions: [h("div", { class: "switch-wrap" }, readAloud, readAloudLabel), resetBtn],
      }),
    );
    header.querySelector(".view-title").id = "copilot-title";
    replace(readAloudLabel, h("span", { class: "switch__track", "aria-hidden": "true" }, h("span", { class: "switch__thumb" })), t("copilot.readAloud"));
    replace(resetBtn, icon("reset", 16), t("copilot.newChat"));
    inputLabel.textContent = t("copilot.inputLabel");
    input.placeholder = t("copilot.placeholder");
    replace(sendBtn, h("span", { class: "composer__send-label", text: t("copilot.send") }), icon("send", 18));
    sendBtn.setAttribute("aria-label", t("copilot.send"));
    replace(
      empty,
      h("div", { class: "conversation__empty-mark", "aria-hidden": "true" }, icon("copilot", 28)),
      h("h2", { class: "conversation__empty-title", text: t("copilot.emptyTitle") }),
      h("p", { class: "muted", text: t("copilot.emptyBody") }),
    );
    replace(
      suggestions,
      h("p", { class: "suggestions__label", id: "sugg-label", text: t("copilot.suggestionsLabel") }),
      h(
        "ul",
        { class: "suggestions__list", "aria-labelledby": "sugg-label" },
        t("copilot.suggestions").map((q) =>
          h("li", null, h("button", { type: "button", class: "suggestion", lang, onclick: () => !store.get().asking && actions.ask(q) }, q)),
        ),
      ),
    );
  }

  function renderComposer() {
    const { asking, draft } = store.get();
    if (input.value !== draft) {
      input.value = draft;
      autosize();
    }
    sendBtn.disabled = asking || !draft.trim();
    counter.textContent = draft.length > MAX_QUESTION * 0.8 ? t("copilot.counter", { n: draft.length, max: MAX_QUESTION }) : "";
    form.setAttribute("aria-busy", String(asking));
  }

  // Cheap high-frequency updates (no store traffic).
  function setLevel(v) {
    const meter = voiceBar.querySelector("[data-voice-level]");
    if (meter) {
      meter.style.setProperty("--level", v.toFixed(3));
      meter.setAttribute("aria-valuenow", String(Math.round(v * 100)));
    }
    const timer = voiceBar.querySelector("[data-voice-timer]");
    const started = store.get().voice.startedAt;
    if (timer && started) timer.textContent = fmt.duration(Date.now() - started);
  }

  store.subscribe(["messages", "asking"], () => {
    renderMessages();
    renderVoice();
  });
  // Listen/Stop buttons reflect playback state, so playback changes rebuild them.
  store.subscribe(["playingId", "playLoading"], () => {
    markAllDirty();
    renderMessages();
    renderVoice();
  });
  store.subscribe(["voice", "health"], () => {
    markAllDirty();
    renderMessages();
    renderVoice();
    renderSide();
  });
  store.subscribe(["draft", "asking"], renderComposer);
  store.subscribe(["readAloud"], renderVoice);

  function render() {
    renderStatic();
    markAllDirty();
    renderMessages();
    renderComposer();
    renderVoice();
    renderSide();
  }

  return {
    el,
    render,
    anchor: () => sideAnchor,
    setLevel,
    focusInput: () => input.focus(),
  };
}
