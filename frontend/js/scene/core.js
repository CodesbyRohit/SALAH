// SALAH Intelligence Core — Three.js scene.
//
// Every element encodes real backend data; nothing is decorative filler:
//   * pillars        — one per week in /context.weekly_revenue (height ∝ revenue)
//   * signal paths   — curves from each week into the engine at the centre;
//                      particles travelling them are allocated by revenue share
//   * the core       — the deterministic analytics engine
//   * evidence rings — one ring per confidence level (FACT → UNKNOWN, inner →
//                      outer), one arc per /insights trace item on that ring
//   * the beacon     — the single recommended experiment (dim when "none")
//
// Performance: one Points draw call with all motion on the GPU (no per-frame
// allocation), capped DPR, 30 fps cap + fewer particles in low-power mode, and
// no loop at all in static mode.

import * as THREE from "../../vendor/three/three.module.min.js";

const MAX_PATHS = 12;
const TIERS = ["FACT", "PATTERN", "HYPOTHESIS", "UNKNOWN"];
const TIER_COLOR = { FACT: 0x3ddc97, PATTERN: 0x7cc7e8, HYPOTHESIS: 0xe7c46a, UNKNOWN: 0x7f8f89 };
const TIER_RADIUS = { FACT: 1.62, PATTERN: 1.92, HYPOTHESIS: 2.22, UNKNOWN: 2.52 };
const PILLAR_RADIUS = 4.1;
const FLOOR_Y = -1.45;
export const SCENE_RADIUS = 5.0;
const FOV = 32;

const STATE_ENERGY = { idle: 0.12, thinking: 1, listening: 0.45, answered: 0.65, error: 0.18 };

const easeOutCubic = (t) => 1 - (1 - t) ** 3;

function haloTexture() {
  const size = 128;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, "rgba(94,240,176,0.55)");
  grad.addColorStop(0.35, "rgba(31,168,116,0.18)");
  grad.addColorStop(1, "rgba(31,168,116,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const coreVertex = /* glsl */ `
  uniform float uTime;
  uniform float uEnergy;
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vPos;
  void main() {
    vec3 p = position;
    float wave = sin(p.y * 7.0 + uTime * 1.3) * sin(p.x * 5.0 - uTime * 0.9);
    p += normal * wave * (0.012 + uEnergy * 0.035);
    vPos = p;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const coreFragment = /* glsl */ `
  uniform float uTime;
  uniform float uEnergy;
  uniform float uTint;
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vPos;
  void main() {
    float fres = pow(1.0 - max(dot(normalize(vNormal), normalize(vView)), 0.0), 2.4);
    vec3 deep = vec3(0.018, 0.075, 0.052);
    vec3 mint = vec3(0.37, 0.94, 0.69);
    vec3 amber = vec3(0.91, 0.7, 0.35);
    vec3 rim = mix(mint, amber, uTint);
    float bands = smoothstep(0.965, 1.0, abs(sin(vPos.y * 13.0 + uTime * 0.35))) * (0.10 + uEnergy * 0.25);
    float core = 0.10 + 0.25 * uEnergy;
    vec3 col = deep + rim * (fres * (0.9 + uEnergy * 0.9) + bands + core * (1.0 - fres) * 0.35);
    gl_FragColor = vec4(col, 1.0);
    #include <colorspace_fragment>
  }
`;

const particleVertex = /* glsl */ `
  #define MAX_PATHS ${MAX_PATHS}
  uniform float uTime;
  uniform float uEnergy;
  uniform float uPixelRatio;
  uniform vec3 uP0[MAX_PATHS];
  uniform vec3 uP1[MAX_PATHS];
  uniform vec3 uP2[MAX_PATHS];
  attribute float aPath;
  attribute float aOffset;
  attribute float aSpeed;
  attribute float aSize;
  varying float vT;
  void main() {
    int i = int(aPath + 0.5);
    float t = fract(aOffset + uTime * aSpeed);
    float it = 1.0 - t;
    vec3 p = it * it * uP0[i] + 2.0 * it * t * uP1[i] + t * t * uP2[i];
    vT = t;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = aSize * uPixelRatio * (1.0 + uEnergy * 0.6) * (9.0 / -mv.z);
  }
`;

const particleFragment = /* glsl */ `
  uniform float uEnergy;
  uniform float uTint;
  varying float vT;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c);
    if (d > 0.5) discard;
    float soft = smoothstep(0.5, 0.0, d);
    float life = sin(vT * 3.14159);
    vec3 mint = mix(vec3(0.24, 0.86, 0.59), vec3(0.75, 1.0, 0.88), vT);
    vec3 col = mix(mint, vec3(0.91, 0.7, 0.35), uTint * 0.8);
    gl_FragColor = vec4(col, soft * life * (0.55 + uEnergy * 0.45));
    #include <colorspace_fragment>
  }
`;

const beamVertex = /* glsl */ `
  uniform float uHeight;
  varying float vY;
  void main() {
    vY = position.y / uHeight + 0.5;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const beamFragment = /* glsl */ `
  uniform float uIntensity;
  uniform float uTime;
  varying float vY;
  void main() {
    float pulse = 0.75 + 0.25 * sin(uTime * 1.6 - vY * 6.0);
    float a = (1.0 - vY) * uIntensity * pulse;
    gl_FragColor = vec4(vec3(0.55, 1.0, 0.8), a);
    #include <colorspace_fragment>
  }
`;

/**
 * @param {HTMLCanvasElement} canvas
 * @param {{quality:"full"|"low", animate:boolean, onStats?:(s:{avgMs:number})=>void}} opts
 */
export function createCore(canvas, opts) {
  const low = opts.quality === "low";
  const animate = opts.animate;

  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: !low,
    powerPreference: low ? "low-power" : "high-performance",
    preserveDrawingBuffer: false,
  });
  renderer.setClearColor(0x000000, 0);
  // Low power trades antialiasing and particles for speed, but keeps enough
  // resolution that thin rings stay legible on high-density phone screens.
  const dprCap = low ? 1.5 : 1.75;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, dprCap));
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x050807, 11, 26);
  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 60);
  const lookAt = new THREE.Vector3(0, 0.35, 0);

  const world = new THREE.Group();
  scene.add(world);

  // --- lighting: the core lights the data around it ---------------------
  scene.add(new THREE.AmbientLight(0x6f9a8a, 0.55));
  const key = new THREE.DirectionalLight(0xdff7ec, 1.1);
  key.position.set(4, 7, 6);
  scene.add(key);
  const coreLight = new THREE.PointLight(0x3ddc97, 26, 9, 1.7);
  world.add(coreLight);

  // --- floor: polar measurement grid --------------------------------------
  const floorPts = [];
  for (const r of [1.4, 2.8, 4.1, 5.2]) {
    const seg = 96;
    for (let i = 0; i < seg; i++) {
      const a0 = (i / seg) * Math.PI * 2;
      const a1 = ((i + 1) / seg) * Math.PI * 2;
      floorPts.push(Math.cos(a0) * r, FLOOR_Y, Math.sin(a0) * r, Math.cos(a1) * r, FLOOR_Y, Math.sin(a1) * r);
    }
  }
  for (let i = 0; i < 24; i++) {
    const a = (i / 24) * Math.PI * 2;
    floorPts.push(Math.cos(a) * 1.4, FLOOR_Y, Math.sin(a) * 1.4, Math.cos(a) * 5.2, FLOOR_Y, Math.sin(a) * 5.2);
  }
  const floorGeo = new THREE.BufferGeometry();
  floorGeo.setAttribute("position", new THREE.Float32BufferAttribute(floorPts, 3));
  const floor = new THREE.LineSegments(
    floorGeo,
    new THREE.LineBasicMaterial({ color: 0x2c4a3f, transparent: true, opacity: 0.32, depthWrite: false }),
  );
  world.add(floor);

  // --- the core -----------------------------------------------------------
  const uniforms = {
    uTime: { value: 0 },
    uEnergy: { value: STATE_ENERGY.idle },
    uTint: { value: 0 },
    uPixelRatio: { value: renderer.getPixelRatio() },
  };
  const coreMat = new THREE.ShaderMaterial({ uniforms, vertexShader: coreVertex, fragmentShader: coreFragment });
  const coreMesh = new THREE.Mesh(new THREE.IcosahedronGeometry(1, low ? 12 : 28), coreMat);
  world.add(coreMesh);

  const lattice = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1.28, 1)),
    new THREE.LineBasicMaterial({ color: 0x3ddc97, transparent: true, opacity: 0.22, depthWrite: false }),
  );
  world.add(lattice);

  const halo = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: haloTexture(),
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      opacity: 0.55,
    }),
  );
  halo.scale.setScalar(5.6);
  world.add(halo);

  // --- dynamic groups (rebuilt by setData) --------------------------------
  const pillarsGroup = new THREE.Group();
  const pathsGroup = new THREE.Group();
  const ringsGroup = new THREE.Group();
  ringsGroup.rotation.x = Math.PI / 2 - 0.38;
  world.add(pillarsGroup, pathsGroup, ringsGroup);
  const tierGroups = {};
  for (const tier of TIERS) {
    const g = new THREE.Group();
    tierGroups[tier] = g;
    ringsGroup.add(g);
  }

  // beacon: the single recommended experiment
  const beamUniforms = { uIntensity: { value: 0 }, uTime: uniforms.uTime, uHeight: { value: 2.6 } };
  const beam = new THREE.Mesh(
    new THREE.CylinderGeometry(0.016, 0.016, 2.6, 8, 1, true),
    new THREE.ShaderMaterial({
      uniforms: beamUniforms,
      vertexShader: beamVertex,
      fragmentShader: beamFragment,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      side: THREE.DoubleSide,
    }),
  );
  beam.position.y = 1.05 + 1.3;
  const beaconRing = new THREE.Mesh(
    new THREE.TorusGeometry(0.2, 0.01, 6, 48),
    new THREE.MeshBasicMaterial({ color: 0x9cf5cf, transparent: true, opacity: 0, depthWrite: false }),
  );
  beaconRing.rotation.x = Math.PI / 2;
  beaconRing.position.y = 1.45;
  world.add(beam, beaconRing);

  // particles
  const particleUniforms = {
    uTime: uniforms.uTime,
    uEnergy: uniforms.uEnergy,
    uTint: uniforms.uTint,
    uPixelRatio: uniforms.uPixelRatio,
    uP0: { value: Array.from({ length: MAX_PATHS }, () => new THREE.Vector3()) },
    uP1: { value: Array.from({ length: MAX_PATHS }, () => new THREE.Vector3()) },
    uP2: { value: Array.from({ length: MAX_PATHS }, () => new THREE.Vector3()) },
  };
  const particleMat = new THREE.ShaderMaterial({
    uniforms: particleUniforms,
    vertexShader: particleVertex,
    fragmentShader: particleFragment,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  let particles = null;

  function disposeChildren(group) {
    for (const child of [...group.children]) {
      group.remove(child);
      child.traverse?.((o) => {
        o.geometry?.dispose();
        if (o.material) [].concat(o.material).forEach((m) => m.dispose());
      });
    }
  }

  function buildWeeks(weekly) {
    disposeChildren(pillarsGroup);
    disposeChildren(pathsGroup);
    if (particles) {
      world.remove(particles);
      particles.geometry.dispose();
      particles = null;
    }
    const weeks = (weekly || []).slice(-MAX_PATHS);
    if (!weeks.length) return;
    const max = Math.max(...weeks.map((w) => w.revenue), 1);
    const total = weeks.reduce((s, w) => s + Math.max(0, w.revenue), 0) || 1;
    const n = weeks.length;
    const pillarGeo = new THREE.BoxGeometry(0.17, 1, 0.17);
    pillarGeo.translate(0, 0.5, 0);
    const capGeo = new THREE.BoxGeometry(0.19, 0.018, 0.19);
    const pathMat = new THREE.LineBasicMaterial({ color: 0x1fa874, transparent: true, opacity: 0.16, depthWrite: false });

    weeks.forEach((w, i) => {
      // Chronological arc behind the core, oldest on the left.
      const a = THREE.MathUtils.degToRad(205 + (130 * i) / Math.max(1, n - 1));
      const x = Math.cos(a) * PILLAR_RADIUS;
      const z = Math.sin(a) * PILLAR_RADIUS;
      const norm = Math.max(0, w.revenue) / max;
      const height = 0.12 + 2.3 * norm;
      const partial = Boolean(w.partial);
      const mat = new THREE.MeshStandardMaterial({
        color: 0x15211d,
        metalness: 0.25,
        roughness: 0.42,
        emissive: 0x1fa874,
        emissiveIntensity: partial ? 0.04 : 0.1 + 0.42 * norm,
        transparent: partial,
        opacity: partial ? 0.55 : 1,
      });
      const pillar = new THREE.Mesh(pillarGeo, mat);
      pillar.position.set(x, FLOOR_Y, z);
      pillar.scale.y = height;
      const cap = new THREE.Mesh(
        capGeo,
        new THREE.MeshBasicMaterial({ color: partial ? 0x56706a : 0x7ff0bf, transparent: true, opacity: partial ? 0.5 : 0.95 }),
      );
      cap.position.set(x, FLOOR_Y + height, z);
      pillarsGroup.add(pillar, cap);

      const p0 = new THREE.Vector3(x, FLOOR_Y + height, z);
      const dir = new THREE.Vector3(x, 0, z).normalize();
      const p2 = dir.clone().multiplyScalar(1.02).setY(0.15);
      const p1 = new THREE.Vector3(x * 0.42, FLOOR_Y + height + 1.5, z * 0.42);
      particleUniforms.uP0.value[i].copy(p0);
      particleUniforms.uP1.value[i].copy(p1);
      particleUniforms.uP2.value[i].copy(p2);
      const curve = new THREE.QuadraticBezierCurve3(p0, p1, p2);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(40)), pathMat);
      pathsGroup.add(line);
    });

    // Particles: allocated by each week's share of revenue.
    const budget = low ? 160 : 520;
    const counts = weeks.map((w) => Math.max(4, Math.round((budget * Math.max(0, w.revenue)) / total)));
    const count = counts.reduce((a, b) => a + b, 0);
    const aPath = new Float32Array(count);
    const aOffset = new Float32Array(count);
    const aSpeed = new Float32Array(count);
    const aSize = new Float32Array(count);
    let k = 0;
    let seed = 7;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    counts.forEach((c, i) => {
      for (let j = 0; j < c; j++, k++) {
        aPath[k] = i;
        aOffset[k] = j / c + rand() * 0.02;
        aSpeed[k] = 0.05 + rand() * 0.035;
        aSize[k] = 1.6 + rand() * 2.2;
      }
    });
    const geo = new THREE.BufferGeometry();
    // Positions are computed in the vertex shader; a zeroed attribute keeps
    // three.js happy and a fixed bounding sphere avoids per-frame culling math.
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(count * 3), 3));
    geo.setAttribute("aPath", new THREE.BufferAttribute(aPath, 1));
    geo.setAttribute("aOffset", new THREE.BufferAttribute(aOffset, 1));
    geo.setAttribute("aSpeed", new THREE.BufferAttribute(aSpeed, 1));
    geo.setAttribute("aSize", new THREE.BufferAttribute(aSize, 1));
    geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), SCENE_RADIUS + 2);
    particles = new THREE.Points(geo, particleMat);
    particles.frustumCulled = false;
    world.add(particles);
  }

  function buildRings(trace) {
    for (const tier of TIERS) disposeChildren(tierGroups[tier]);
    const byTier = Object.fromEntries(TIERS.map((t) => [t, []]));
    for (const item of trace || []) (byTier[item.confidence] || byTier.UNKNOWN).push(item);
    for (const tier of TIERS) {
      const r = TIER_RADIUS[tier];
      const color = TIER_COLOR[tier];
      const group = tierGroups[tier];
      // faint full track
      group.add(
        new THREE.Mesh(
          new THREE.TorusGeometry(r, 0.004, 4, low ? 96 : 160),
          new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.12, depthWrite: false }),
        ),
      );
      const items = byTier[tier];
      const n = items.length;
      if (!n) continue;
      const slot = (Math.PI * 2) / n;
      const gap = Math.min(0.22, slot * 0.18);
      const dashed = tier === "HYPOTHESIS" || tier === "UNKNOWN";
      const tube = (tier === "FACT" ? 0.022 : tier === "PATTERN" ? 0.018 : 0.014) * (low ? 1.7 : 1);
      const opacity = tier === "UNKNOWN" ? 0.55 : 0.9;
      const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
      for (let i = 0; i < n; i++) {
        const start = i * slot + gap / 2;
        const length = slot - gap;
        // Dashed tiers are drawn as short segments: visibly "not solid".
        const pieces = dashed ? Math.max(3, Math.round(length / 0.16)) : 1;
        const pieceLen = length / pieces;
        for (let p = 0; p < pieces; p++) {
          const arcLen = dashed ? pieceLen * 0.55 : pieceLen;
          const arc = new THREE.Mesh(new THREE.TorusGeometry(r, tube, 5, Math.max(6, Math.round(arcLen * 22)), arcLen), mat);
          arc.rotation.z = start + p * pieceLen;
          group.add(arc);
        }
      }
    }
  }

  // --- state ---------------------------------------------------------------
  let state = "idle";
  let level = 0;
  let energyTarget = STATE_ENERGY.idle;
  let tintTarget = 0;
  let beaconTarget = 0;
  let stateTimer = 0;
  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  let framing = { fx: 0.5, fy: 0.5, size: 400 };
  let width = 1;
  let height = 1;
  let entry = animate ? 0 : 1;
  let running = false;
  let rafId = 0;
  let last = 0;
  let elapsed = 0;
  const frameTimes = [];
  let statsTimer = 0;

  function resize() {
    width = canvas.clientWidth || window.innerWidth;
    height = canvas.clientHeight || window.innerHeight;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    applyFraming();
  }

  function applyFraming() {
    // Place the core at the anchor's centre and fit the scene inside it.
    const dx = (framing.fx - 0.5) * width;
    const dy = (framing.fy - 0.5) * height;
    camera.setViewOffset(width, height, -dx, -dy, width, height);
    const tanHalf = Math.tan(THREE.MathUtils.degToRad(FOV / 2));
    const fit = ((framing.radius || SCENE_RADIUS) * height) / (Math.max(120, framing.size) * tanHalf);
    const d = THREE.MathUtils.clamp(fit, 9, 60);
    camera.userData.distance = d;
    scene.fog.near = d - 3;
    scene.fog.far = d + 14;
    camera.far = d + 30;
    camera.updateProjectionMatrix();
  }

  function placeCamera() {
    const d = camera.userData.distance || 14;
    const k = animate ? 1 + 0.32 * (1 - easeOutCubic(entry)) : 1;
    const elev = 0.24;
    const sway = d * 0.035;
    camera.position.set(pointer.x * sway, Math.sin(elev) * d * k + pointer.y * sway * 0.6 + 0.6, Math.cos(elev) * d * k);
    camera.lookAt(lookAt);
  }

  function update(dt) {
    elapsed += dt;
    if (entry < 1) entry = Math.min(1, entry + dt / 1.8);
    const ease = Math.min(1, dt * 3.2);
    if (state === "answered" || state === "error") {
      stateTimer -= dt;
      if (stateTimer <= 0) setState("idle");
    }
    const target = state === "listening" ? STATE_ENERGY.listening + level * 0.7 : energyTarget;
    uniforms.uEnergy.value += (target - uniforms.uEnergy.value) * ease;
    uniforms.uTint.value += (tintTarget - uniforms.uTint.value) * ease;
    beamUniforms.uIntensity.value += (beaconTarget - beamUniforms.uIntensity.value) * ease;
    beaconRing.material.opacity = beamUniforms.uIntensity.value * 0.8;
    // Integrate time so a change in energy speeds motion up without a jump.
    uniforms.uTime.value += dt * (0.7 + uniforms.uEnergy.value * 1.2);

    const e = uniforms.uEnergy.value;
    const s = (0.86 + 0.14 * easeOutCubic(entry)) * (1 + (state === "listening" ? level * 0.07 : 0));
    world.scale.setScalar(s);
    coreMesh.rotation.y += dt * (0.12 + e * 0.5);
    lattice.rotation.y -= dt * (0.05 + e * 0.25);
    lattice.rotation.x += dt * 0.02;
    tierGroups.FACT.rotation.z += dt * 0.05 * (1 + e);
    tierGroups.PATTERN.rotation.z -= dt * 0.04 * (1 + e);
    tierGroups.HYPOTHESIS.rotation.z += dt * 0.03 * (1 + e);
    tierGroups.UNKNOWN.rotation.z -= dt * 0.02 * (1 + e);
    halo.material.opacity = 0.42 + e * 0.35;
    coreLight.intensity = 20 + e * 22;
    beaconRing.scale.setScalar(1 + 0.12 * Math.sin(elapsed * 1.6));

    pointer.x += (pointer.tx - pointer.x) * Math.min(1, dt * 2);
    pointer.y += (pointer.ty - pointer.y) * Math.min(1, dt * 2);
    placeCamera();
  }

  function renderOnce() {
    placeCamera();
    renderer.render(scene, camera);
  }

  const minFrame = low ? 1 / 31 : 0;
  function loop(now) {
    if (!running) return;
    rafId = requestAnimationFrame(loop);
    const raw = last ? (now - last) / 1000 : 1 / 60;
    if (raw < minFrame) return;
    last = now;
    const dt = Math.min(raw, 0.1);
    update(dt);
    renderer.render(scene, camera);
    frameTimes.push(raw * 1000);
    if (frameTimes.length > 90) frameTimes.shift();
    statsTimer += raw;
    if (statsTimer > 1 && opts.onStats) {
      statsTimer = 0;
      const avg = frameTimes.reduce((a, b) => a + b, 0) / frameTimes.length;
      opts.onStats({ avgMs: avg });
    }
  }

  function onPointer(e) {
    if (!animate) return;
    pointer.tx = (e.clientX / window.innerWidth - 0.5) * 2;
    pointer.ty = -(e.clientY / window.innerHeight - 0.5) * 2;
  }
  window.addEventListener("pointermove", onPointer, { passive: true });

  const ro = new ResizeObserver(() => {
    resize();
    if (!running) renderOnce();
  });
  ro.observe(canvas);
  resize();

  // --- public API -------------------------------------------------------------
  function setState(next, opts2 = {}) {
    state = next in STATE_ENERGY ? next : "idle";
    energyTarget = STATE_ENERGY[state];
    tintTarget = state === "error" ? 1 : 0;
    if (state === "answered") stateTimer = 1.6;
    if (state === "error") stateTimer = opts2.hold || 2.4;
    if (!animate) {
      uniforms.uEnergy.value = energyTarget;
      uniforms.uTint.value = tintTarget;
      renderOnce();
    }
  }

  return {
    setData({ weekly, trace, action }) {
      if (weekly !== undefined) buildWeeks(weekly);
      if (trace !== undefined) buildRings(trace);
      if (action !== undefined) beaconTarget = action && action.priority && action.priority !== "none" ? 1 : 0.15;
      if (!animate) {
        beamUniforms.uIntensity.value = beaconTarget;
        beaconRing.material.opacity = beaconTarget * 0.8;
        renderOnce();
      }
    },
    setState,
    setLevel(v) {
      level = Math.max(0, Math.min(1, v || 0));
    },
    setFraming(f) {
      framing = f;
      applyFraming();
      if (!running) renderOnce();
    },
    start() {
      if (running || !animate) {
        if (!animate) renderOnce();
        return;
      }
      running = true;
      last = 0;
      rafId = requestAnimationFrame(loop);
    },
    stop() {
      running = false;
      cancelAnimationFrame(rafId);
    },
    get running() {
      return running;
    },
    renderOnce,
    dispose() {
      running = false;
      cancelAnimationFrame(rafId);
      ro.disconnect();
      window.removeEventListener("pointermove", onPointer);
      scene.traverse((o) => {
        o.geometry?.dispose();
        if (o.material) {
          [].concat(o.material).forEach((m) => {
            m.map?.dispose();
            m.dispose();
          });
        }
      });
      renderer.dispose();
      renderer.forceContextLoss();
    },
  };
}
