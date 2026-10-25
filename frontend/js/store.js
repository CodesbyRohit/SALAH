// Minimal observable store. Views subscribe to the keys they render so a
// change in one area never rebuilds unrelated DOM (e.g. the chat composer).

export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();

  return {
    get: () => state,
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      const changed = Object.keys(next).filter((k) => state[k] !== next[k]);
      if (!changed.length) return;
      state = { ...state, ...next };
      for (const l of [...listeners]) {
        if (!l.keys || l.keys.some((k) => changed.includes(k))) l.fn(state, changed);
      }
    },
    /** subscribe(fn) or subscribe(["key"], fn). Returns an unsubscribe. */
    subscribe(keys, fn) {
      const entry = typeof keys === "function" ? { fn: keys, keys: null } : { fn, keys };
      listeners.add(entry);
      return () => listeners.delete(entry);
    },
  };
}

/** Remote resource helper state: { status, data, error }. */
export const resource = (status = "idle", data = null, error = null) => ({ status, data, error });
