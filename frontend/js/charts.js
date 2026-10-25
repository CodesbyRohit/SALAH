// Accessible, dependency-free SVG column charts.
// Values are plotted exactly as the backend returns them. Each chart ships a
// hover + keyboard tooltip, selective direct labels, a text summary and a
// table twin, so no value is ever reachable only by hovering.

import { h, svg, replace, nextId } from "./dom.js";

const M = { top: 26, right: 10, bottom: 34, left: 52 };
const MAX_BAR = 24;
const RADIUS = 4;

function niceMax(value) {
  if (!(value > 0)) return 1;
  const exp = 10 ** Math.floor(Math.log10(value));
  const f = value / exp;
  const step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return step * exp;
}

/** Column path: square at the baseline, 4px rounded data-end. */
function columnPath(x, y, w, hgt) {
  const r = Math.min(RADIUS, w / 2, hgt);
  if (hgt <= 0) return "";
  return [
    `M${x},${y + hgt}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + w - r}`,
    `Q${x + w},${y} ${x + w},${y + r}`,
    `V${y + hgt}`,
    "Z",
  ].join("");
}

/**
 * @param {object} opts
 * @param {{key:string,label:string,value:number,muted?:boolean,directLabel?:boolean,tag?:string,detail?:string}[]} opts.data
 *   Single series: bars use the accent; `muted` de-emphasizes ("highlight one, gray the rest").
 * @param {(v:number)=>string} opts.format       full value format (tooltip, labels)
 * @param {(v:number)=>string} opts.tickFormat   compact axis format
 * @param {string} opts.title
 * @param {string} [opts.subtitle]
 * @param {string} opts.summary                   visible one-line text summary
 * @param {{label:string, swatch:"accent"|"muted"}[]} [opts.legend]
 * @param {{value:number,label:string}} [opts.reference]
 * @param {{headers:string[], rows:string[][]}} opts.table
 * @param {{show:string, hide:string, empty:string}} opts.strings
 * @param {number} [opts.height]
 */
export function columnChart(opts) {
  const id = nextId("chart");
  const plot = h("div", { class: "chart__plot" });
  const tooltip = h("div", { class: "chart__tooltip", role: "presentation", "aria-hidden": "true" });
  const live = h("p", { class: "sr-only", "aria-live": "polite" });
  const tableWrap = h("div", { class: "chart__table", hidden: true });
  const toggle = h("button", {
    class: "btn btn--quiet btn--sm",
    type: "button",
    "aria-expanded": "false",
    "aria-controls": `${id}-table`,
    text: opts.strings.show,
  });
  tableWrap.id = `${id}-table`;

  toggle.addEventListener("click", () => {
    const open = tableWrap.hidden;
    tableWrap.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? opts.strings.hide : opts.strings.show;
  });

  replace(
    tableWrap,
    h(
      "table",
      { class: "data-table" },
      h("caption", { class: "sr-only", text: opts.title }),
      h("thead", null, h("tr", null, opts.table.headers.map((c) => h("th", { scope: "col", text: c })))),
      h(
        "tbody",
        null,
        opts.table.rows.map((r) =>
          h("tr", null, r.map((c, i) => (i === 0 ? h("th", { scope: "row", text: c }) : h("td", { text: c })))),
        ),
      ),
    ),
  );

  const legend = opts.legend?.length
    ? h(
        "ul",
        { class: "chart__legend", "aria-label": "Legend" },
        opts.legend.map((l) => h("li", null, h("span", { class: `swatch swatch--${l.swatch}`, "aria-hidden": "true" }), l.label)),
      )
    : null;

  const figure = h(
    "figure",
    { class: "chart", "aria-labelledby": `${id}-title`, "aria-describedby": `${id}-summary` },
    h(
      "div",
      { class: "chart__head" },
      h(
        "div",
        null,
        h("h3", { class: "chart__title", id: `${id}-title`, text: opts.title }),
        opts.subtitle ? h("p", { class: "chart__subtitle", text: opts.subtitle }) : null,
      ),
      toggle,
    ),
    legend,
    h("div", { class: "chart__stage" }, plot, tooltip),
    h("figcaption", { class: "chart__summary", id: `${id}-summary`, text: opts.summary }),
    tableWrap,
    live,
  );

  if (!opts.data.length) {
    replace(plot, h("p", { class: "empty-note", text: opts.strings.empty }));
    return figure;
  }

  let active = -1;
  let geometry = [];
  const height = opts.height || 220;

  function showTip(i, fromKeyboard) {
    const d = opts.data[i];
    const g = geometry[i];
    if (!d || !g) return;
    active = i;
    plot.querySelectorAll(".bar").forEach((b, j) => b.classList.toggle("is-active", j === i));
    replace(
      tooltip,
      h("strong", { class: "chart__tip-value", text: opts.format(d.value) }),
      h("span", { class: "chart__tip-label", text: d.detail || d.label }),
      d.tag ? h("span", { class: "chart__tip-tag", text: d.tag }) : null,
    );
    tooltip.classList.add("is-visible");
    const stageW = plot.clientWidth;
    const tipW = tooltip.offsetWidth;
    const left = Math.max(4, Math.min(stageW - tipW - 4, g.cx - tipW / 2));
    tooltip.style.transform = `translate(${left}px, ${Math.max(0, g.top - tooltip.offsetHeight - 10)}px)`;
    if (fromKeyboard) live.textContent = `${d.detail || d.label}: ${opts.format(d.value)}${d.tag ? ` (${d.tag})` : ""}`;
  }

  function hideTip() {
    active = -1;
    tooltip.classList.remove("is-visible");
    plot.querySelectorAll(".bar.is-active").forEach((b) => b.classList.remove("is-active"));
  }

  function render() {
    const width = Math.max(260, plot.clientWidth || 600);
    const innerW = width - M.left - M.right;
    const innerH = height - M.top - M.bottom;
    const values = opts.data.map((d) => d.value);
    const top = niceMax(Math.max(...values, opts.reference?.value || 0));
    const y = (v) => M.top + innerH - (v / top) * innerH;
    const band = innerW / opts.data.length;
    const barW = Math.max(4, Math.min(MAX_BAR, band - 6, band * 0.62));
    const labelEvery = Math.max(1, Math.ceil(44 / band));

    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
    const grid = ticks.map((t) =>
      svg(
        "g",
        { class: "chart__tick" },
        svg("line", { x1: M.left, x2: width - M.right, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 }),
        svg("text", { x: M.left - 8, y: y(t) + 4, "text-anchor": "end", text: opts.tickFormat(t) }),
      ),
    );

    geometry = [];
    const bars = opts.data.map((d, i) => {
      const cx = M.left + band * i + band / 2;
      const yTop = y(Math.max(0, d.value));
      const hgt = M.top + innerH - yTop;
      geometry.push({ cx, top: yTop });
      const cls = `bar${d.muted ? " bar--muted" : ""}`;
      const label =
        d.directLabel
          ? svg("text", { class: "bar__label", x: cx, y: yTop - 7, "text-anchor": "middle", text: opts.format(d.value) })
          : null;
      // In-plot tags need room; on narrow charts they live in the tooltip/legend/table.
      const tag = d.tag && hgt > 0 && band >= 56 ? svg("text", { class: "bar__tag", x: cx, y: yTop - (label ? 21 : 7), "text-anchor": "middle", text: d.tag }) : null;
      return svg(
        "g",
        { class: cls, "data-index": i },
        svg("path", { class: "bar__mark", d: columnPath(cx - barW / 2, yTop, barW, hgt) }),
        label,
        tag,
        svg("rect", {
          class: "bar__hit",
          x: M.left + band * i,
          y: M.top,
          width: band,
          height: innerH + 4,
          onpointerenter: () => showTip(i),
          onpointermove: () => active !== i && showTip(i),
        }),
        i % labelEvery === 0 || i === opts.data.length - 1
          ? svg("text", { class: "chart__xlabel", x: cx, y: height - M.bottom + 18, "text-anchor": "middle", text: d.label })
          : null,
      );
    });

    const ref = opts.reference
      ? svg(
          "g",
          { class: "chart__ref" },
          svg("line", {
            x1: M.left,
            x2: width - M.right,
            y1: Math.round(y(opts.reference.value)) + 0.5,
            y2: Math.round(y(opts.reference.value)) + 0.5,
          }),
          svg("text", { x: width - M.right, y: y(opts.reference.value) - 6, "text-anchor": "end", text: opts.reference.label }),
        )
      : null;

    const baseline = svg("line", {
      class: "chart__baseline",
      x1: M.left,
      x2: width - M.right,
      y1: M.top + innerH + 0.5,
      y2: M.top + innerH + 0.5,
    });

    const root = svg(
      "svg",
      {
        width,
        height,
        viewBox: `0 0 ${width} ${height}`,
        role: "img",
        "aria-label": `${opts.title}. ${opts.summary}`,
        onpointerleave: hideTip,
      },
      grid,
      baseline,
      bars,
      ref,
    );
    replace(plot, root);
    if (active >= 0) showTip(active);
  }

  // Keyboard: the plot is one tab stop; arrows move between columns.
  plot.tabIndex = 0;
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", `${opts.title} — use arrow keys to read values`);
  plot.addEventListener("keydown", (e) => {
    const n = opts.data.length;
    let next = active;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = active < 0 ? 0 : Math.min(n - 1, active + 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = active < 0 ? n - 1 : Math.max(0, active - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = n - 1;
    else if (e.key === "Escape") return hideTip();
    else return;
    e.preventDefault();
    showTip(next, true);
  });
  plot.addEventListener("focus", () => showTip(active < 0 ? opts.data.length - 1 : active, true));
  plot.addEventListener("blur", hideTip);

  let frame = 0;
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(render);
  });
  ro.observe(plot);
  figure._dispose = () => ro.disconnect();
  return figure;
}
