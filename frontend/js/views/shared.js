import { h, icon } from "../dom.js";
import { CONFIDENCE_LEVELS } from "../api.js";

export function tierBadge(t, level, { compact = false } = {}) {
  const safe = CONFIDENCE_LEVELS.includes(level) ? level : "UNKNOWN";
  return h(
    "span",
    { class: `tier tier--${safe.toLowerCase()}${compact ? " tier--compact" : ""}`, title: t(`tiers.${safe}.desc`) },
    h("span", { class: "tier__glyph", "aria-hidden": "true" }),
    h("span", { class: "tier__label", text: t(`tiers.${safe}.label`) }),
  );
}

export function errorText(t, err) {
  if (!err) return "";
  const kind = err.kind || "network";
  if (kind === "http") {
    const base = t("errors.http", { status: err.status || "?" });
    return err.detail ? `${base} ${err.detail}` : base;
  }
  return t(`errors.${kind}`) || String(err.message || err);
}

export function errorPanel(t, err, onRetry, label) {
  return h(
    "div",
    { class: "notice notice--error", role: "alert" },
    icon("alert", 18),
    h("p", null, label ? label.replace("{reason}", errorText(t, err)) : errorText(t, err)),
    onRetry ? h("button", { class: "btn btn--quiet btn--sm", type: "button", onclick: onRetry }, icon("retry", 16), t("common.retry")) : null,
  );
}

export function loadingBlock(text) {
  return h("div", { class: "loading-block", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { text }));
}

export function viewHeader({ eyebrow, title, intro, actions }) {
  return h(
    "header",
    { class: "view-header" },
    h(
      "div",
      { class: "view-header__text" },
      eyebrow ? h("p", { class: "eyebrow", text: eyebrow }) : null,
      h("h1", { class: "view-title", tabindex: "-1", "data-view-heading": "", text: title }),
      intro ? h("p", { class: "view-intro", text: intro }) : null,
    ),
    actions ? h("div", { class: "view-header__actions" }, actions) : null,
  );
}

/** Group trace items by confidence level, preserving backend order. */
export function groupByTier(trace) {
  const out = Object.fromEntries(CONFIDENCE_LEVELS.map((c) => [c, []]));
  for (const item of trace || []) out[item.confidence]?.push(item);
  return out;
}
