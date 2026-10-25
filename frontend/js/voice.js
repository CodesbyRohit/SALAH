// Browser voice I/O: MediaRecorder capture → POST /voice/stt, and
// POST /voice/tts → audio playback. State lives in the store (`voice`);
// the high-frequency input level goes straight to a callback, not the store.

import { transition, pickRecordingMime, extensionFor, isBusy } from "./voice-machine.js";
import { ApiError } from "./api.js";

const MAX_RECORDING_MS = 60000;
const MIN_RECORDING_BYTES = 1200;

export function voiceSupport() {
  if (!window.isSecureContext) return "insecure";
  if (!navigator.mediaDevices?.getUserMedia || typeof window.MediaRecorder === "undefined") return "browser";
  return "ok";
}

export function createVoice({ store, api, onLevel, onTranscript }) {
  let stream = null;
  let recorder = null;
  let chunks = [];
  let audioCtx = null;
  let levelRaf = 0;
  let maxTimer = 0;
  let cancelled = false;
  let sttAbort = null;
  let audio = null;
  let audioUrl = null;
  let playToken = 0;

  const get = () => store.get().voice;
  const send = (event, patch = {}) => {
    const v = get();
    store.set({ voice: { ...v, ...patch, state: transition(v.state, event) } });
  };

  function stopLevel() {
    cancelAnimationFrame(levelRaf);
    levelRaf = 0;
    onLevel?.(0);
    audioCtx?.close().catch(() => {});
    audioCtx = null;
  }

  function releaseStream() {
    stream?.getTracks().forEach((tr) => tr.stop());
    stream = null;
  }

  function meter(src) {
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      audioCtx.createMediaStreamSource(src).connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        onLevel?.(Math.min(1, Math.sqrt(sum / buf.length) * 4));
        levelRaf = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      /* metering is optional */
    }
  }

  async function start() {
    const v = get();
    if (!["ready", "transcribed", "error", "playing"].includes(v.state)) return;
    stopPlayback();
    send("start", { message: "", startedAt: 0 });
    cancelled = false;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (err) {
      const denied = err?.name === "NotAllowedError" || err?.name === "SecurityError";
      send("denied", { message: denied ? "denied" : "browser" });
      return;
    }
    if (cancelled || get().state !== "requesting") {
      releaseStream();
      return;
    }
    const mime = pickRecordingMime((type) => MediaRecorder.isTypeSupported(type));
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch {
      releaseStream();
      send("denied", { message: "browser" });
      return;
    }
    chunks = [];
    recorder.ondataavailable = (e) => e.data?.size && chunks.push(e.data);
    recorder.onstop = () => finish(recorder.mimeType || mime || "audio/webm");
    recorder.start(250);
    meter(stream);
    send("granted", { startedAt: Date.now() });
    maxTimer = setTimeout(stop, MAX_RECORDING_MS);
  }

  function stop() {
    clearTimeout(maxTimer);
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function cancel() {
    cancelled = true;
    clearTimeout(maxTimer);
    sttAbort?.abort();
    if (recorder && recorder.state !== "inactive") recorder.stop();
    stopLevel();
    releaseStream();
    const s = get().state;
    if (["requesting", "recording", "processing"].includes(s)) send("cancel", { message: "" });
  }

  async function finish(mime) {
    stopLevel();
    releaseStream();
    recorder = null;
    if (cancelled) return;
    send("stop");
    const blob = new Blob(chunks, { type: mime.split(";")[0] });
    chunks = [];
    if (blob.size < MIN_RECORDING_BYTES) {
      send("empty", { message: "empty" });
      return;
    }
    sttAbort = new AbortController();
    try {
      const text = await api.stt(blob, `question.${extensionFor(mime)}`, { signal: sttAbort.signal });
      if (cancelled) return;
      if (!text) {
        send("empty", { message: "empty" });
        return;
      }
      send("transcript", { message: "" });
      onTranscript?.(text);
    } catch (err) {
      if (err instanceof ApiError && err.kind === "aborted") return;
      send("fail", { message: "stt", error: err });
    } finally {
      sttAbort = null;
    }
  }

  function stopPlayback() {
    playToken++;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = null;
    if (store.get().playingId) store.set({ playingId: null });
    if (get().state === "playing") send("stopPlayback");
  }

  async function play(id, text, language) {
    if (isBusy(get().state)) return;
    stopPlayback();
    const token = ++playToken;
    store.set({ playingId: id, playLoading: id });
    try {
      const blob = await api.tts(text, language);
      if (token !== playToken) return;
      audio = audio || new Audio();
      audioUrl = URL.createObjectURL(blob);
      audio.src = audioUrl;
      audio.onended = () => token === playToken && stopPlayback();
      audio.onerror = () => {
        if (token !== playToken) return;
        stopPlayback();
        send("fail", { message: "tts", error: new ApiError("shape", "audio could not be decoded") });
      };
      await audio.play();
      store.set({ playLoading: null });
      send("play", { message: "" });
    } catch (err) {
      if (token !== playToken) return;
      store.set({ playingId: null, playLoading: null });
      send("fail", { message: "tts", error: err });
    }
  }

  return {
    start,
    stop,
    cancel,
    play,
    stopPlayback,
    dismiss: () => send(get().state === "transcribed" ? "discard" : "reset", { message: "", error: null }),
    dispose() {
      cancel();
      stopPlayback();
    },
  };
}
