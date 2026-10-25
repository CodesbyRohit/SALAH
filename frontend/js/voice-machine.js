// Voice interaction state machine (pure — no browser APIs, unit-tested).
//
//   ready ──start──▶ requesting ──granted──▶ recording ──stop──▶ processing
//     ▲                  │denied                │cancel            │transcript / empty / fail
//     │                  ▼                      ▼                  ▼
//     └──────reset──── error ◀───────────── ready          transcribed ──submit──▶ answering
//                                                                                    │answered
//   ready/transcribed/answering ──play──▶ playing ──ended──▶ ready                   ▼
//                                                                                  ready
// `unavailable` is entered from any state when the server or browser cannot do
// voice, and left only via `available`.

export const VOICE_STATES = [
  "ready",
  "requesting",
  "recording",
  "processing",
  "transcribed",
  "answering",
  "playing",
  "error",
  "unavailable",
];

const TABLE = {
  ready: { start: "requesting", play: "playing", submit: "answering", fail: "error" },
  requesting: { granted: "recording", denied: "error", cancel: "ready" },
  recording: { stop: "processing", cancel: "ready", fail: "error" },
  processing: { transcript: "transcribed", empty: "error", fail: "error", cancel: "ready" },
  transcribed: { submit: "answering", start: "requesting", discard: "ready", play: "playing" },
  answering: { answered: "ready", fail: "error", play: "playing" },
  playing: { ended: "ready", stopPlayback: "ready", fail: "error", start: "requesting" },
  error: { reset: "ready", start: "requesting", play: "playing" },
  unavailable: { available: "ready" },
};

/** Returns the next state, or the same state when the event is not allowed. */
export function transition(state, event) {
  if (event === "unavailable") return "unavailable";
  return TABLE[state]?.[event] ?? state;
}

export const canRecord = (state) => ["ready", "transcribed", "error", "playing"].includes(state);
export const isBusy = (state) => ["requesting", "recording", "processing"].includes(state);

/** Pick a recording type the backend (which labels uploads audio/webm) can accept. */
export function pickRecordingMime(isSupported) {
  const preferred = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  for (const type of preferred) {
    try {
      if (isSupported(type)) return type;
    } catch {
      /* ignore */
    }
  }
  return "";
}

export function extensionFor(mime) {
  if (mime.startsWith("audio/ogg")) return "ogg";
  if (mime.startsWith("audio/mp4")) return "m4a";
  return "webm";
}
