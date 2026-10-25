// Evidence: the deterministic trace as an audit surface, plus the single
// recommended experiment — explicitly marked as not yet measured.

import { h, icon, replace } from "../dom.js";
import * as fmt from "../format.js";
import { CONFIDENCE_LEVELS } from "../api.js";
import { tierBadge, errorPanel, loadingBlock, viewHeader, groupByTier } from "./shared.js";

export function createEvidence({ store, t, actions }) {
  const el = h("div", { class: "view-body view-body--evidence" });

  function legend() {
    return h(
      "section",
      { class: "tier-legend", "aria-labelledby": "tier-legend-title" },
      h("h2", { class: "section-title", id: "tier-legend-title", text: t("evidence.legend") }),
      h(
        "dl",
        { class: "tier-legend__list" },
        CONFIDENCE_LEVELS.map((c) => h("div", { class: "tier-legend__item" }, h("dt", null, tierBadge(t, c)), h("dd", { text: t(`tiers.${c}.desc`) }))),
      ),
    );
  }

  function filters(trace, active) {
    const counts = groupByTier(trace);
    const option = (key, label, n) =>
      h(
        "button",
        {
          type: "button",
          id: `filter-${key.toLowerCase()}`,
          class: "filter-chip",
          "aria-pressed": String(active === key),
          onclick: () => store.set({ evidenceFilter: key }),
        },
        label,
        h("span", { class: "filter-chip__n", text: String(n) }),
      );
    return h(
      "div",
      { class: "filter-row", role: "group", "aria-label": t("evidence.filterLabel") },
      option("ALL", t("evidence.all"), trace.length),
      CONFIDENCE_LEVELS.map((c) => option(c, t(`tiers.${c}.label`), counts[c].length)),
    );
  }

  function traceRow(item, index) {
    const id = `trace-${index}`;
    return h(
      "li",
      { class: `trace-row trace-row--${item.confidence.toLowerCase()}` },
      h(
        "details",
        null,
        h(
          "summary",
          { "aria-describedby": `${id}-value` },
          tierBadge(t, item.confidence),
          h("span", { class: "trace-row__signal", text: fmt.humanizeSignal(item.signal) }),
          h("span", { class: "trace-row__value", id: `${id}-value`, lang: "en", text: item.value }),
          h("span", { class: "trace-row__chev", "aria-hidden": "true" }, icon("chevron", 16)),
        ),
        h(
          "dl",
          { class: "trace-row__detail" },
          h("div", null, h("dt", { text: t("evidence.value") }), h("dd", { class: "mono", lang: "en", text: item.value })),
          h("div", null, h("dt", { text: t("evidence.implication") }), h("dd", { lang: "en", text: item.implication || "—" })),
          item.window ? h("div", null, h("dt", { text: t("evidence.window") }), h("dd", { lang: "en", text: item.window })) : null,
          h("div", null, h("dt", { text: t("evidence.source") }), h("dd", null, h("code", { text: item.source || "—" }))),
        ),
      ),
    );
  }

  function traceList(trace, filter) {
    const items = filter === "ALL" ? trace : trace.filter((x) => x.confidence === filter);
    if (!items.length) return h("p", { class: "empty-note", text: t("evidence.empty") });
    return h("ol", { class: "trace-list" }, items.map((item) => traceRow(item, trace.indexOf(item))));
  }

  function recommendation(action, ctx, lang) {
    if (!action) {
      return h("aside", { class: "rec rec--na" }, h("h2", { class: "section-title", text: t("evidence.rec.title") }), h("p", { class: "muted", text: t("evidence.rec.unavailable") }));
    }
    const row = (label, text) =>
      text ? h("div", { class: "rec__row" }, h("dt", { text: label }), h("dd", { lang: "en", text })) : null;
    const lapsed = action.priority === "lapsed_regulars" ? ctx?.lapsed_regulars : null;
    return h(
      "aside",
      { class: "rec", "aria-labelledby": "rec-title" },
      h("p", { class: "eyebrow", text: t("evidence.rec.title") }),
      h("h2", { class: "rec__title", id: "rec-title", text: t(`evidence.rec.priority.${action.priority}`) || action.priority }),
      h(
        "p",
        { class: "rec__status" },
        h("span", { class: "status-dot status-dot--pending", "aria-hidden": "true" }),
        h("strong", { text: t("evidence.rec.status") }),
      ),
      h("p", { class: "rec__status-note", text: t("evidence.rec.statusNote") }),
      // Lifecycle: SALAH records no experiment runs or results, so only the
      // first stage can ever be current here.
      h(
        "ol",
        { class: "rec__stages", "aria-label": t("evidence.rec.stages.label") },
        ["proposed", "running", "measured"].map((s, i) =>
          h("li", { class: `rec__stage${i === 0 ? " is-current" : ""}`, "aria-current": i === 0 ? "step" : null, text: t(`evidence.rec.stages.${s}`) }),
        ),
      ),
      h(
        "dl",
        { class: "rec__list" },
        row(t("evidence.rec.observed"), action.observed),
        row(t("evidence.rec.hypothesis"), action.hypothesis),
        row(t("evidence.rec.experiment"), action.experiment),
        row(t("evidence.rec.baseline"), action.baseline),
        row(t("evidence.rec.measure"), action.measure),
        row(t("evidence.rec.confounders"), action.confounders),
        row(t("evidence.rec.unknown"), action.unknown),
        row(t("evidence.rec.basis"), action.basis),
      ),
      Number.isInteger(action.weeks) && action.weeks > 0
        ? h("p", { class: "rec__review" }, icon("check", 16), t("evidence.rec.review", { weeks: action.weeks }))
        : null,
      lapsed && lapsed.customers.length
        ? h(
            "details",
            { class: "rec__customers" },
            h("summary", null, t("evidence.lapsed.title", { n: lapsed.count })),
            h(
              "table",
              { class: "data-table" },
              h("thead", null, h("tr", null, h("th", { scope: "col", text: t("evidence.lapsed.customer") }), h("th", { scope: "col", text: t("evidence.lapsed.lastVisit") }))),
              h(
                "tbody",
                null,
                lapsed.customers.map((c) => h("tr", null, h("td", null, h("code", { text: c.customer_id })), h("td", { text: fmt.dateTime(c.last_visit, lang) }))),
              ),
            ),
            h("p", { class: "muted small", text: t("evidence.lapsed.note") }),
          )
        : null,
    );
  }

  function render() {
    const { insights, context, lang, evidenceFilter } = store.get();
    const header = viewHeader({
      eyebrow: t("shell.synthetic"),
      title: t("evidence.title"),
      intro: t("evidence.intro"),
    });

    if (insights.status === "loading" && !insights.data) {
      replace(el, header, loadingBlock(t("common.loading")));
      return;
    }
    if (!insights.data) {
      replace(el, header, errorPanel(t, insights.error, actions.reloadData, t("evidence.error")));
      return;
    }
    const { trace, action } = insights.data;
    replace(
      el,
      header,
      h(
        "div",
        { class: "evidence-grid" },
        h(
          "div",
          { class: "evidence-main" },
          legend(),
          trace
            ? h(
                "section",
                { class: "trace", "aria-labelledby": "trace-title" },
                h(
                  "div",
                  { class: "trace__head" },
                  h("h2", { class: "section-title", id: "trace-title", text: t("evidence.count", { n: trace.length }) }),
                  lang !== "en" ? h("p", { class: "muted small", text: t("evidence.englishNote") }) : null,
                ),
                filters(trace, evidenceFilter),
                traceList(trace, evidenceFilter),
              )
            : h("p", { class: "empty-note", text: t("common.unavailable") }),
        ),
        recommendation(action, context.data, lang),
      ),
      h("p", { class: "disclaimer" }, icon("info", 16), t("disclaimer.short")),
    );
  }

  return { el, render };
}
