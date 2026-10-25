// Executive overview: what is happening, what supports it, what to consider
// next, what remains uncertain. Every number is a backend value, rendered.

import { h, icon, replace } from "../dom.js";
import { columnChart } from "../charts.js";
import * as fmt from "../format.js";
import { coreFallbackSvg } from "../scene/stage.js";
import { tierBadge, errorPanel, loadingBlock, groupByTier } from "./shared.js";
import { CONFIDENCE_LEVELS } from "../api.js";

const WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const EVENING = { from: 18, to: 22 }; // mirrors analytics.evening_share (18:00 ≤ h < 22:00)

export function createOverview({ store, t, actions }) {
  const el = h("div", { class: "view-body view-body--overview" });
  const anchor = h("div", { class: "hero__core", "data-core-anchor": "" });
  let charts = [];

  function headlineFor(ctx, action, lang) {
    if (!action) return t("overview.headline.unavailable");
    const p = action.priority;
    if (p === "lapsed_regulars" && ctx?.lapsed_regulars) {
      return t("overview.headline.lapsed_regulars", { count: ctx.lapsed_regulars.count });
    }
    if (p === "weak_weekday" && ctx?.weakest_weekday) {
      return t("overview.headline.weak_weekday", { day: fmt.weekday(ctx.weakest_weekday.weekday, lang) });
    }
    if (p === "ticket_decline") return t("overview.headline.ticket_decline");
    if (p === "none") return t("overview.headline.none");
    return t("overview.headline.unavailable");
  }

  function hero(ctx, ins, lang) {
    const action = ins?.action;
    const weeks = ctx?.weekday_profile?.weeks;
    const eyebrow =
      ctx?.generated_at && Number.isInteger(weeks)
        ? t("overview.eyebrow", { weeks, date: fmt.date(ctx.generated_at, lang) })
        : t("overview.eyebrowNoDate");
    const legendItems = CONFIDENCE_LEVELS.map((c) => h("li", null, tierBadge(t, c, { compact: true })));

    replace(
      anchor,
      coreFallbackSvg(),
      h(
        "details",
        { class: "core-legend" },
        h("summary", null, icon("info", 16), t("overview.core.what")),
        h("p", { text: t("overview.core.explain") }),
        h("ul", { class: "core-legend__tiers" }, legendItems),
        h("p", { class: "core-legend__fallback", text: t("overview.core.fallback") }),
      ),
    );

    return h(
      "section",
      { class: "hero", "aria-labelledby": "overview-headline" },
      h(
        "div",
        { class: "hero__text" },
        h("p", { class: "eyebrow" }, h("span", { class: "chip chip--synthetic", text: t("shell.synthetic") }), eyebrow),
        h("h1", {
          class: "hero__headline",
          id: "overview-headline",
          tabindex: "-1",
          "data-view-heading": "",
          text: headlineFor(ctx, action, lang),
        }),
        action?.observed
          ? h(
              "div",
              { class: "hero__lede", lang: "en" },
              h("span", { class: "lede-label", text: t("overview.ledeLabel") }),
              h("p", { text: action.observed }),
            )
          : null,
        h(
          "div",
          { class: "hero__ctas" },
          h("a", { class: "btn btn--primary", href: "#/evidence" }, t("overview.ctaEvidence"), icon("arrow", 18)),
          h("a", { class: "btn btn--ghost", href: "#/copilot" }, icon("copilot", 18), t("overview.ctaAsk")),
        ),
      ),
      anchor,
    );
  }

  function questions(ctx, ins, lang) {
    const facts = [];
    if (ctx?.weakest_weekday) {
      const w = ctx.weakest_weekday;
      facts.push(
        t("overview.facts.weekday", {
          day: fmt.weekday(w.weekday, lang),
          avg: fmt.money(w.avg_daily_revenue, lang),
          overall: fmt.money(w.overall_avg_daily_revenue, lang),
        }),
      );
    }
    if (ctx?.lapsed_regulars) facts.push(t("overview.facts.lapsed", { count: ctx.lapsed_regulars.count }));
    if (ctx?.ticket_trend && Number.isInteger(ctx.ticket_trend.weeks)) {
      const tt = ctx.ticket_trend;
      facts.push(
        t("overview.facts.ticket", {
          early: fmt.money(tt.early_avg_ticket, lang),
          recent: fmt.money(tt.recent_avg_ticket, lang),
          change: fmt.percent(tt.change_pct, lang, { signed: true }),
          half: tt.weeks / 2,
        }),
      );
    }
    if (ctx?.evening_share) facts.push(t("overview.facts.evening", { share: fmt.percent(ctx.evening_share.evening_share_pct, lang) }));

    const tiers = groupByTier(ins?.trace);
    const uncertain = [...tiers.HYPOTHESIS, ...tiers.UNKNOWN];
    const action = ins?.action;
    const unavailable = h("p", { class: "muted", text: t("common.unavailable") });

    const card = (n, title, body, link) =>
      h(
        "article",
        { class: "q-card" },
        h("p", { class: "q-card__index", "aria-hidden": "true", text: `0${n}` }),
        h("h2", { class: "q-card__title", text: title }),
        h("div", { class: "q-card__body" }, body),
        link || null,
      );

    return h(
      "section",
      { class: "questions", "aria-label": t("nav.overview") },
      card(1, t("overview.q1"), facts.length ? h("ul", { class: "fact-list" }, facts.map((f) => h("li", { text: f }))) : unavailable),
      card(
        2,
        t("overview.q2"),
        ins?.trace
          ? [
              h(
                "ul",
                { class: "tier-counts" },
                CONFIDENCE_LEVELS.map((c) => h("li", null, tierBadge(t, c), h("strong", { class: "tier-counts__n", text: String(tiers[c].length) }))),
              ),
              h("p", { class: "muted small", text: t("overview.q2Note", { n: ins.trace.length }) }),
            ]
          : unavailable,
        h("a", { class: "link-arrow", href: "#/evidence" }, t("overview.ctaEvidence"), icon("arrow", 16)),
      ),
      card(
        3,
        t("overview.q3"),
        action
          ? [
              h("p", { class: "q-card__strong", text: t(`evidence.rec.priority.${action.priority}`) || action.priority }),
              action.experiment ? h("p", { class: "engine-text", lang: "en", text: action.experiment }) : null,
              action.measure
                ? h("p", { class: "q-card__measure" }, h("strong", { text: `${t("overview.q3Measure")}: ` }), h("span", { lang: "en", text: action.measure }))
                : null,
              Number.isInteger(action.weeks) && action.weeks > 0
                ? h("p", { class: "muted small", text: t("overview.q3Review", { weeks: action.weeks }) })
                : null,
            ]
          : unavailable,
      ),
      card(
        4,
        t("overview.q4"),
        ins?.trace
          ? uncertain.length
            ? h(
                "ul",
                { class: "uncertain-list" },
                uncertain.map((u) => h("li", null, tierBadge(t, u.confidence, { compact: true }), h("span", { lang: "en", text: u.implication || u.value }))),
              )
            : h("p", { class: "muted", text: t("overview.q4Empty") })
          : unavailable,
      ),
    );
  }

  function tile(label, value, sub, extra) {
    return h(
      "article",
      { class: `kpi${value === null ? " kpi--na" : ""}` },
      h("h3", { class: "kpi__label", text: label }),
      h("p", { class: "kpi__value", text: value === null ? t("common.unavailable") : value }),
      sub ? h("p", { class: "kpi__sub" }, extra || null, sub) : null,
    );
  }

  function kpis(ctx, lang) {
    const weekly = ctx?.weekly_revenue;
    const latest = weekly?.length ? weekly[weekly.length - 1] : null;
    const tt = ctx?.ticket_trend;
    const es = ctx?.evening_share;
    const ww = ctx?.weakest_weekday;
    const lr = ctx?.lapsed_regulars;
    const weeks = ctx?.weekday_profile?.weeks;
    let ticketDelta = null;
    if (tt) {
      const dir = tt.change_pct < 0 ? "down" : tt.change_pct > 0 ? "up" : null;
      ticketDelta = dir
        ? h("span", { class: `delta delta--${dir}` }, h("span", { "aria-hidden": "true", text: dir === "down" ? "↓ " : "↑ " }), h("span", { class: "sr-only", text: `${t(`overview.kpi.${dir}`)} ` }))
        : null;
    }
    return h(
      "section",
      { class: "kpis", "aria-label": t("overview.q1") },
      tile(
        t("overview.kpi.revenue"),
        es ? fmt.money(es.total_revenue, lang) : null,
        Number.isInteger(weeks) ? t("overview.kpi.revenueSub", { weeks }) : null,
      ),
      tile(t("overview.kpi.latestWeek"), latest ? fmt.money(latest.revenue, lang) : null, latest ? fmt.weekLabel(latest.week, lang) : null),
      tile(
        t("overview.kpi.ticket"),
        tt ? fmt.money(tt.recent_avg_ticket, lang) : null,
        // Without the window the comparison has no stated period: omit it.
        tt && Number.isInteger(tt.weeks)
          ? t("overview.kpi.ticketSub", { half: tt.weeks / 2, early: fmt.money(tt.early_avg_ticket, lang), change: fmt.percent(tt.change_pct, lang, { signed: true }) })
          : null,
        tt && Number.isInteger(tt.weeks) ? ticketDelta : null,
      ),
      tile(
        t("overview.kpi.evening"),
        es ? fmt.percent(es.evening_share_pct, lang) : null,
        es && Number.isInteger(es.weeks) ? t("overview.kpi.eveningSub", { amount: fmt.money(es.evening_revenue, lang), weeks: es.weeks }) : null,
      ),
      tile(
        t("overview.kpi.weakest"),
        ww ? fmt.weekday(ww.weekday, lang) : null,
        ww ? t("overview.kpi.weakestSub", { avg: fmt.money(ww.avg_daily_revenue, lang), overall: fmt.money(ww.overall_avg_daily_revenue, lang) }) : null,
      ),
      tile(
        t("overview.kpi.lapsed"),
        lr ? fmt.number(lr.count, lang, 0) : null,
        lr
          ? lr.regular_min_visits && lr.regular_min_span_days && lr.weeks
            ? t("overview.kpi.lapsedRule", { visits: lr.regular_min_visits, days: lr.regular_min_span_days, weeks: lr.weeks })
            : t("overview.kpi.lapsedSub")
          : null,
      ),
    );
  }

  function chartStrings() {
    return { show: t("common.showTable"), hide: t("common.hideTable"), empty: t("overview.charts.empty") };
  }

  function unavailableChart(title) {
    return h("figure", { class: "chart chart--na" }, h("h3", { class: "chart__title", text: title }), h("p", { class: "empty-note", text: t("common.unavailable") }));
  }

  function weeklyChart(ctx, lang) {
    const title = t("overview.charts.weeklyTitle");
    const weekly = ctx?.weekly_revenue;
    if (!weekly) return unavailableChart(title);
    const dayMonth = new Intl.DateTimeFormat(fmt.localeFor(lang), { day: "numeric", month: "short" });
    const partialFirst = weekly.length > 1 && fmt.windowStartsMidWeek(ctx.generated_at, ctx.weekday_profile?.weeks || 8);
    const data = weekly.map((w, i) => {
      const range = fmt.isoWeekRange(w.week);
      const partial = partialFirst && i === 0;
      return {
        key: w.week,
        label: range ? dayMonth.format(range.start) : w.week,
        detail: fmt.weekLabel(w.week, lang),
        value: w.revenue,
        muted: partial,
        tag: partial ? t("overview.charts.partial") : "",
        directLabel: i === weekly.length - 1,
      };
    });
    const complete = data.filter((d) => !d.muted);
    let summary = t("overview.charts.empty");
    if (complete.length) {
      const max = complete.reduce((a, b) => (b.value > a.value ? b : a));
      const min = complete.reduce((a, b) => (b.value < a.value ? b : a));
      summary = t("overview.charts.weeklySummary", {
        n: data.length,
        max: fmt.money(max.value, lang),
        maxWeek: max.detail,
        min: fmt.money(min.value, lang),
        minWeek: min.detail,
      });
      if (partialFirst) summary += ` ${t("overview.charts.partialNote")}`;
    }
    return columnChart({
      data,
      title,
      subtitle: t("overview.charts.weeklySub"),
      summary,
      format: (v) => fmt.money(v, lang),
      tickFormat: (v) => fmt.moneyCompact(v, lang),
      legend: partialFirst
        ? [
            { label: title, swatch: "accent" },
            { label: t("overview.charts.partial"), swatch: "muted" },
          ]
        : null,
      table: {
        headers: [t("overview.charts.colWeek"), t("overview.charts.colRevenue")],
        rows: data.map((d) => [d.tag ? `${d.detail} (${d.tag})` : d.detail, fmt.money(d.value, lang)]),
      },
      strings: chartStrings(),
      height: 240,
    });
  }

  function hourlyChart(ctx, lang) {
    const title = t("overview.charts.hourlyTitle");
    const hourly = ctx?.revenue_by_hour;
    if (!hourly) return unavailableChart(title);
    const data = hourly.map((r) => ({
      key: String(r.hour),
      label: String(r.hour).padStart(2, "0"),
      detail: `${fmt.hourLabel(r.hour)}–${fmt.hourLabel(r.hour + 1)}`,
      value: r.revenue,
      muted: !(r.hour >= EVENING.from && r.hour < EVENING.to),
    }));
    const peak = data.length ? data.reduce((a, b) => (b.value > a.value ? b : a)) : null;
    if (peak) peak.directLabel = true;
    return columnChart({
      data,
      title,
      subtitle: t("overview.charts.hourlySub"),
      summary: peak ? t("overview.charts.hourlySummary", { hour: peak.detail, value: fmt.money(peak.value, lang) }) : t("overview.charts.empty"),
      format: (v) => fmt.money(v, lang),
      tickFormat: (v) => fmt.moneyCompact(v, lang),
      legend: [
        { label: t("overview.charts.evening"), swatch: "accent" },
        { label: t("overview.charts.otherHours"), swatch: "muted" },
      ],
      table: {
        headers: [t("overview.charts.colHour"), t("overview.charts.colRevenue")],
        rows: data.map((d) => [d.detail, fmt.money(d.value, lang)]),
      },
      strings: chartStrings(),
      height: 220,
    });
  }

  function weekdayChart(ctx, lang) {
    const title = t("overview.charts.weekdayTitle");
    const wp = ctx?.weekday_profile;
    if (!wp) return unavailableChart(title);
    const weakest = ctx.weakest_weekday?.weekday;
    const days = WEEKDAY_ORDER.filter((d) => wp.days[d]);
    const data = days.map((d) => ({
      key: d,
      label: fmt.weekday(d, lang, "short"),
      detail: fmt.weekday(d, lang),
      value: wp.days[d].avg_daily_revenue,
      muted: d !== weakest,
      directLabel: d === weakest,
      tag: d === weakest ? t("overview.charts.weakest") : "",
    }));
    return columnChart({
      data,
      title,
      subtitle: t("overview.charts.weekdaySub"),
      summary: weakest && wp.days[weakest]
        ? t("overview.charts.weekdaySummary", {
            day: fmt.weekday(weakest, lang),
            value: fmt.money(wp.days[weakest].avg_daily_revenue, lang),
            overall: fmt.money(wp.overall_avg_daily_revenue, lang),
          })
        : t("overview.charts.empty"),
      format: (v) => fmt.money(v, lang),
      tickFormat: (v) => fmt.moneyCompact(v, lang),
      reference: { value: wp.overall_avg_daily_revenue, label: t("overview.charts.overall", { value: fmt.money(wp.overall_avg_daily_revenue, lang) }) },
      legend: [
        { label: t("overview.charts.weakest"), swatch: "accent" },
        { label: t("overview.charts.otherDays"), swatch: "muted" },
      ],
      table: {
        headers: [t("overview.charts.colDay"), t("overview.charts.colAvg"), t("overview.charts.colBelow"), t("overview.charts.colTx")],
        rows: days.map((d) => {
          const x = wp.days[d];
          return [
            fmt.weekday(d, lang),
            fmt.money(x.avg_daily_revenue, lang),
            Number.isInteger(x.below_avg_days) ? t("overview.charts.belowOf", { below: x.below_avg_days, total: x.days_in_window }) : "—",
            fmt.number(x.avg_tx_per_active_day, lang),
          ];
        }),
      },
      strings: chartStrings(),
      height: 220,
    });
  }

  function render() {
    const { context, insights, lang } = store.get();
    charts.forEach((c) => c._dispose?.());
    charts = [];

    if (context.status === "loading" && !context.data) {
      replace(el, hero(null, null, lang), loadingBlock(t("overview.loading")));
      return;
    }
    if (context.status === "error" && !context.data) {
      replace(el, hero(null, insights.data, lang), errorPanel(t, context.error, actions.reloadData, t("overview.error")));
      return;
    }
    const ctx = context.data;
    const ins = insights.data;
    charts = [weeklyChart(ctx, lang), hourlyChart(ctx, lang), weekdayChart(ctx, lang)];
    replace(
      el,
      hero(ctx, ins, lang),
      insights.status === "error" ? errorPanel(t, insights.error, actions.reloadData, t("evidence.error")) : null,
      questions(ctx, ins, lang),
      kpis(ctx, lang),
      h(
        "section",
        { class: "charts", "aria-label": t("overview.charts.weeklyTitle") },
        h("div", { class: "charts__wide" }, charts[0]),
        charts[1],
        charts[2],
      ),
      h("p", { class: "disclaimer" }, icon("info", 16), t("disclaimer.short")),
    );
  }

  return { el, render, anchor: () => anchor };
}
