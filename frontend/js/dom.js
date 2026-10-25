// Safe DOM construction. Strings always become text nodes — model output and
// API data are never parsed as HTML anywhere in the app.

const SVG_NS = "http://www.w3.org/2000/svg";

function applyProps(node, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text") node.textContent = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** h("div", {class: "x"}, "text", childNode, [more]) */
export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  applyProps(node, props);
  append(node, children);
  return node;
}

export function svg(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "text") node.textContent = value;
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

export function replace(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

export const $ = (selector, root = document) => root.querySelector(selector);

const ICONS = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  evidence: "M9 3h6M10 3v5.5L4.6 18a2 2 0 0 0 1.7 3h11.4a2 2 0 0 0 1.7-3L14 8.5V3M7.5 14h9",
  copilot: "M4 5h16v11H8l-4 4V5Zm4 5h.01M12 10h.01M16 10h.01",
  updates: "M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8Zm4 12a2 2 0 0 0 4 0",
  system: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2-1.2L14.5 3h-5l-.4 2.6a7.6 7.6 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.6 7.6 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-7 9a7 7 0 0 0 14 0M12 19v3",
  stop: "M7 7h10v10H7z",
  send: "M4 12 20 4l-6 16-2.5-6.5L4 12Zm7.5 1.5L20 4",
  play: "M8 5v14l11-7L8 5Z",
  speaker: "M4 9v6h4l5 4V5L8 9H4Zm12.5-1.5a5 5 0 0 1 0 9M19 5a8.5 8.5 0 0 1 0 14",
  reset: "M4 4v6h6M4.9 15a8 8 0 1 0 1.6-8.6L4 10",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  chevron: "m9 6 6 6-6 6",
  close: "M6 6l12 12M18 6 6 18",
  retry: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  info: "M12 8h.01M11 12h1v5h1M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
  table: "M4 5h16v14H4V5Zm0 5h16M4 14.5h16M10 5v14",
  check: "m5 12.5 4.5 4.5L19 7.5",
  alert: "M12 9v4m0 4h.01M10.3 3.9 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  bolt: "M13 3 5 14h6l-1 7 8-11h-6l1-7Z",
  spark: "M12 3v4m0 10v4M3 12h4m10 0h4M6 6l2.5 2.5m7 7L18 18M6 18l2.5-2.5m7-7L18 6",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-9-9h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3Z",
};

export function icon(name, size = 20) {
  return svg(
    "svg",
    {
      class: "icon",
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 1.7,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
      focusable: "false",
    },
    svg("path", { d: ICONS[name] || ICONS.info }),
  );
}

let uid = 0;
export const nextId = (prefix = "id") => `${prefix}-${++uid}`;
