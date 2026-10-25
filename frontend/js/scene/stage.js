// Stage controller for the Intelligence Core.
//
// One fixed canvas sits behind the interface (pointer-events: none). Views
// mark where the core should appear with a [data-core-anchor] element; the
// stage frames the scene inside the visible anchor and pauses rendering when
// no anchor is on screen. The 3D module is loaded lazily, after first paint,
// and every failure path (no WebGL, import error, context loss) degrades to a
// static SVG diagram so the app never depends on it.

import { svg } from "../dom.js";

export const RENDER_MODES = ["auto", "full", "low", "static", "off"];

export function detectWebGL() {
  try {
    const c = document.createElement("canvas");
    const gl = c.getContext("webgl2") || c.getContext("webgl");
    const ok = Boolean(gl);
    gl?.getExtension("WEBGL_lose_context")?.loseContext();
    return ok;
  } catch {
    return false;
  }
}

export function prefersReducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function looksLowPower() {
  const nav = navigator;
  const narrow = window.matchMedia?.("(max-width: 760px)").matches;
  const fewCores = (nav.hardwareConcurrency || 8) <= 4;
  const lowMem = typeof nav.deviceMemory === "number" && nav.deviceMemory <= 4;
  const saveData = nav.connection?.saveData === true;
  return narrow || fewCores || lowMem || saveData;
}

/** Resolve the user's preference into what will actually run. */
export function resolveMode(pref, { webgl, reducedMotion, lowPower }) {
  if (pref === "off" || !webgl) return "off";
  if (pref === "static") return "static";
  if (pref === "full") return reducedMotion ? "static" : "full";
  if (pref === "low") return reducedMotion ? "static" : "low";
  if (reducedMotion) return "static";
  return lowPower ? "low" : "full";
}

/** Static diagram used when WebGL is off or unavailable. */
export function coreFallbackSvg() {
  const rings = [
    { r: 58, c: "var(--tier-fact)", w: 3, dash: "" },
    { r: 72, c: "var(--tier-pattern)", w: 2.5, dash: "" },
    { r: 86, c: "var(--tier-hypothesis)", w: 2, dash: "5 6" },
    { r: 100, c: "var(--tier-unknown)", w: 1.5, dash: "3 7" },
  ];
  return svg(
    "svg",
    { class: "core-fallback", viewBox: "0 0 240 240", "aria-hidden": "true", focusable: "false" },
    svg(
      "defs",
      null,
      svg(
        "radialGradient",
        { id: "coreGlow" },
        svg("stop", { offset: "0", "stop-color": "#5ef0b0", "stop-opacity": "0.55" }),
        svg("stop", { offset: "1", "stop-color": "#1fa874", "stop-opacity": "0" }),
      ),
    ),
    svg("circle", { cx: 120, cy: 120, r: 110, fill: "url(#coreGlow)" }),
    rings.map((ring) =>
      svg("circle", {
        cx: 120,
        cy: 120,
        r: ring.r,
        fill: "none",
        stroke: ring.c,
        "stroke-width": ring.w,
        "stroke-dasharray": ring.dash || null,
        opacity: 0.85,
      }),
    ),
    svg("circle", { cx: 120, cy: 120, r: 38, fill: "#0b2a1f", stroke: "#5ef0b0", "stroke-width": 1.5 }),
    svg("line", { x1: 120, y1: 82, x2: 120, y2: 18, stroke: "#9cf5cf", "stroke-width": 1.5, opacity: 0.7 }),
  );
}

export function createStage({ canvas, root = document.documentElement, onInfo }) {
  const webgl = detectWebGL();
  let reducedMotion = prefersReducedMotion();
  let pref = "auto";
  let mode = "off";
  let core = null;
  let createCore = null;
  let loading = null;
  let failed = false;
  let avgMs = null;
  let slowSeconds = 0;
  let autoDowngraded = false;
  let anchor = null;
  let visible = false;
  let data = {};
  let lastState = "idle";
  let frameReq = 0;
  let generation = 0;

  const info = () => ({ webgl, reducedMotion, pref, mode: failed ? "off" : mode, avgMs, failed, autoDowngraded });
  const emit = () => onInfo?.(info());

  function setRootMode(m) {
    root.dataset.render = m;
  }

  function onStats({ avgMs: ms }) {
    avgMs = ms;
    // Measured, not assumed: sustained slow frames in full mode → low power.
    if (mode === "full" && pref === "auto") {
      slowSeconds = ms > 34 ? slowSeconds + 1 : 0;
      if (slowSeconds >= 3) {
        autoDowngraded = true;
        slowSeconds = 0;
        rebuild("low");
      }
    }
    emit();
  }

  function fail() {
    failed = true;
    teardown();
    mode = "off";
    setRootMode("off");
    emit();
  }

  function onContextLost(e) {
    e.preventDefault();
    fail();
  }
  canvas.addEventListener("webglcontextlost", onContextLost);

  /** Tear down the current renderer. A disposed context cannot be reused, so
   *  the canvas is swapped for a fresh one before the next renderer starts. */
  function teardown() {
    if (!core) return;
    canvas.removeEventListener("webglcontextlost", onContextLost);
    core.dispose();
    core = null;
    const fresh = canvas.cloneNode(false);
    canvas.replaceWith(fresh);
    canvas = fresh;
    canvas.addEventListener("webglcontextlost", onContextLost);
  }

  async function load() {
    if (createCore) return createCore;
    if (!loading) loading = import("./core.js").then((m) => (createCore = m.createCore));
    return loading;
  }

  async function rebuild(nextMode) {
    const gen = ++generation;
    teardown();
    mode = nextMode;
    if (nextMode === "off" || failed) {
      setRootMode("off");
      emit();
      return;
    }
    setRootMode("loading");
    try {
      const make = await load();
      if (gen !== generation) return;
      core = make(canvas, { quality: nextMode === "full" ? "full" : "low", animate: nextMode !== "static", onStats });
      core.setData(data);
      core.setState(lastState);
      setRootMode(nextMode);
      refreshAnchor();
    } catch (err) {
      console.warn("SALAH: 3D core unavailable, using static diagram.", err);
      fail();
    }
    emit();
  }

  function refreshAnchor() {
    frameReq = 0;
    if (!core) return;
    const rect = anchor?.getBoundingClientRect();
    const vh = window.innerHeight;
    const onScreen = rect && rect.width > 0 && rect.bottom > 0 && rect.top < vh;
    visible = Boolean(onScreen) && !document.hidden;
    root.dataset.coreVisible = visible ? "true" : "false";
    if (!visible) {
      core.stop();
      return;
    }
    const cw = canvas.clientWidth || window.innerWidth;
    const ch = canvas.clientHeight || vh;
    core.setFraming({
      fx: (rect.left + rect.width / 2) / cw,
      fy: (rect.top + rect.height / 2) / ch,
      size: Math.min(rect.width, rect.height),
      // Small anchors (phones) frame the engine and rings; outer pillars may bleed.
      radius: Number(anchor.dataset.coreFit) || (Math.min(rect.width, rect.height) < 420 ? 3.6 : undefined),
    });
    core.start();
  }

  const schedule = () => {
    if (!frameReq) frameReq = requestAnimationFrame(refreshAnchor);
  };
  window.addEventListener("scroll", schedule, { passive: true, capture: true });
  window.addEventListener("resize", schedule, { passive: true });
  document.addEventListener("visibilitychange", schedule);
  const anchorObserver = new ResizeObserver(schedule);

  const motionQuery = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  motionQuery?.addEventListener?.("change", (e) => {
    reducedMotion = e.matches;
    setPreference(pref);
  });

  function setPreference(next) {
    pref = RENDER_MODES.includes(next) ? next : "auto";
    autoDowngraded = false;
    const resolved = resolveMode(pref, { webgl, reducedMotion, lowPower: looksLowPower() });
    if (resolved !== mode || !core) rebuild(resolved);
    else emit();
  }

  return {
    info,
    /** Start after first paint so the 3D never delays useful content. */
    init(preference) {
      pref = preference;
      setRootMode(webgl ? "loading" : "off");
      const go = () => setPreference(preference);
      if ("requestIdleCallback" in window) requestIdleCallback(go, { timeout: 1200 });
      else setTimeout(go, 200);
    },
    setPreference,
    setAnchor(el) {
      anchorObserver.disconnect();
      anchor = el || null;
      if (anchor) anchorObserver.observe(anchor);
      schedule();
    },
    setData(patch) {
      data = { ...data, ...patch };
      core?.setData(patch);
    },
    setState(s) {
      lastState = s;
      core?.setState(s);
    },
    setLevel(v) {
      core?.setLevel(v);
    },
  };
}
