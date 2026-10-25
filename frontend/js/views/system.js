// System status: what this server actually reports, plus display settings
// and live-measured rendering info. Internal paths are never displayed.

import { h, icon, replace } from "../dom.js";
import * as fmt from "../format.js";
import { viewHeader, errorText } from "./shared.js";
import { RENDER_MODES } from "../scene/stage.js";

export function createSystem({ store, t, actions }) {
  const el = h("div", { class: "view-body view-body--system" });

  function card(title, ok, value, note) {
    const state = ok === null ? "neutral" : ok ? "ok" : "off";
    return h(
      "article",
      { class: `status-card status-card--${state}` },
      h("h2", { class: "status-card__title" }, h("span", { class: `status-dot status-dot--${state}`, "aria-hidden": "true" }), title),
      h("p", { class: "status-card__value", text: value }),
      note ? h("p", { class: "status-card__note", text: note }) : null,
    );
  }

  function render() {
    const { health, context, lang, renderPref, renderInfo } = store.get();
    const hd = health.data;
    const down = health.status === "error";
    const ri = renderInfo || {};
    const select = h(
      "select",
      { id: "render-mode", class: "select", onchange: (e) => actions.setRenderPref(e.target.value) },
      RENDER_MODES.map((m) => h("option", { value: m, selected: m === renderPref || undefined, text: t(`system.modes.${m}`) })),
    );

    replace(
      el,
      viewHeader({
        title: t("system.title"),
        intro: t("system.intro"),
        actions: h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: actions.refreshHealth }, icon("retry", 16), t("system.refresh")),
      }),
      h(
        "section",
        { class: "status-grid", "aria-live": "polite" },
        card(
          t("system.api"),
          hd ? true : down ? false : null,
          hd ? t("system.apiOk") : down ? t("system.apiDown") : t("shell.checking"),
          hd?.time ? t("system.checkedAt", { time: fmt.clock(new Date(hd.time), lang) }) : down ? errorText(t, health.error) : "",
        ),
        card(t("system.ai"), hd ? hd.llm_available : null, hd ? (hd.llm_available ? t("common.on") : t("common.off")) : "—", hd ? (hd.llm_available ? t("system.aiOn") : t("system.aiOff")) : ""),
        card(
          t("system.voice"),
          hd ? hd.voice_available : null,
          hd ? (hd.voice_available ? t("system.voiceOn", { langs: hd.voice_languages.join(", ").toUpperCase() }) : t("system.voiceOff")) : "—",
          hd && !hd.voice_reported ? t("system.voiceUnknown") : hd?.voice_available ? t("voice.ttsLanguage") : "",
        ),
        card(
          t("system.memory"),
          hd?.memory ? true : null,
          hd?.memory ? (hd.memory.cognee_available ? t("system.memoryCognee") : t("system.memoryLocal", { n: hd.memory.local_log_entries ?? "—" })) : "—",
        ),
        card(
          t("system.data"),
          context.data ? true : context.status === "error" ? false : null,
          t("shell.synthetic"),
          context.data?.generated_at ? t("system.dataEnd", { date: fmt.dateTime(context.data.generated_at, lang) }) : "",
        ),
        card(t("system.cache"), null, hd && hd.cache_entries !== null ? t("system.cacheEntries", { n: hd.cache_entries }) : "—"),
      ),
      h(
        "section",
        { class: "panel", "aria-labelledby": "display-title" },
        h("h2", { class: "section-title", id: "display-title", text: t("system.display") }),
        h("div", { class: "field" }, h("label", { for: "render-mode", text: t("system.renderLabel") }), select),
        h(
          "dl",
          { class: "kv" },
          h("div", null, h("dt", { text: t("system.webgl") }), h("dd", { text: ri.webgl ? t("system.supported") : t("system.notSupported") })),
          h("div", null, h("dt", { text: t("system.reducedMotion") }), h("dd", { text: ri.reducedMotion ? t("common.on") : t("common.off") })),
          h("div", null, h("dt", { text: t("system.activeMode") }), h("dd", { text: t(`system.modes.${ri.mode || "off"}`) })),
          h(
            "div",
            null,
            h("dt", { text: t("system.frameTime") }),
            h("dd", { class: "mono", text: typeof ri.avgMs === "number" && ri.mode !== "static" && ri.mode !== "off" ? `${ri.avgMs.toFixed(1)} ms` : t("system.notMeasured") }),
          ),
        ),
      ),
      h(
        "section",
        { class: "panel", "aria-labelledby": "data-title" },
        h("h2", { class: "section-title", id: "data-title", text: t("system.dataStatement") }),
        h("p", { text: t("disclaimer.long") }),
      ),
    );
  }

  store.subscribe(["health", "renderInfo", "renderPref", "context"], () => store.get().view === "system" && render());
  return { el, render };
}
