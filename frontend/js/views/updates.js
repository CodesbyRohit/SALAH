// Proactive updates over POST /nudge — generated on request, in this session.
// No background service or delivery is implied.

import { h, icon, replace } from "../dom.js";
import * as fmt from "../format.js";
import { viewHeader, errorPanel } from "./shared.js";
import { LANGUAGES } from "../i18n.js";

export function createUpdates({ store, t, actions }) {
  const el = h("div", { class: "view-body view-body--updates" });

  function render() {
    const { nudges, nudgeStatus, nudgeError, lang } = store.get();
    const loading = nudgeStatus === "loading";
    const button = h(
      "button",
      { type: "button", class: "btn btn--primary", disabled: loading || undefined, "aria-busy": String(loading), onclick: actions.nudge },
      loading ? h("span", { class: "spinner spinner--sm", "aria-hidden": "true" }) : icon("updates", 18),
      loading ? t("updates.generating") : t("updates.generate"),
    );
    replace(
      el,
      viewHeader({ eyebrow: t("shell.synthetic"), title: t("updates.title"), intro: t("updates.intro"), actions: button }),
      h("p", { class: "notice notice--info" }, icon("info", 18), h("span", { text: t("updates.honesty") })),
      nudgeStatus === "error" ? errorPanel(t, nudgeError, actions.nudge, t("updates.error")) : null,
      nudges.length
        ? h(
            "ol",
            { class: "nudge-list", "aria-live": "polite" },
            nudges.map((n) =>
              h(
                "li",
                { class: "nudge", lang: n.language },
                h(
                  "div",
                  { class: "nudge__meta" },
                  h("span", { class: "nudge__icon", "aria-hidden": "true" }, icon("updates", 16)),
                  h("time", { datetime: n.at.toISOString(), text: t("updates.generatedAt", { time: fmt.clock(n.at, lang) }) }),
                  h("span", { class: "chip", text: LANGUAGES[n.language]?.native || n.language }),
                ),
                h("p", { class: "nudge__text", text: n.text }),
              ),
            ),
          )
        : h("div", { class: "empty-state" }, icon("updates", 26), h("p", { text: t("updates.empty") })),
    );
  }

  store.subscribe(["nudges", "nudgeStatus"], () => store.get().view === "updates" && render());
  return { el, render };
}
