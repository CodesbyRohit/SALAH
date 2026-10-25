// Presentation-only formatting. Nothing here computes a financial metric:
// values arrive from the deterministic backend and are only rendered.

const LOCALES = { en: "en-IN", hi: "hi-IN", es: "es-ES", fr: "fr-FR" };

export const localeFor = (lang) => LOCALES[lang] || LOCALES.en;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export function money(value, lang = "en") {
  if (!isNum(value)) return "—";
  const whole = Number.isInteger(value);
  return new Intl.NumberFormat(localeFor(lang), {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(value);
}

/** Axis ticks: compact from 1,000 up (₹1K). */
export function moneyCompact(value, lang = "en") {
  if (!isNum(value)) return "—";
  return new Intl.NumberFormat(localeFor(lang), {
    style: "currency",
    currency: "INR",
    notation: value >= 1000 ? "compact" : "standard",
    maximumFractionDigits: value >= 1000 ? 1 : 0,
  }).format(value);
}

export function number(value, lang = "en", digits = 1) {
  if (!isNum(value)) return "—";
  return new Intl.NumberFormat(localeFor(lang), { maximumFractionDigits: digits }).format(value);
}

/** Backend percentages are already in 0–100 units (e.g. 38.3). */
export function percent(value, lang = "en", { signed = false } = {}) {
  if (!isNum(value)) return "—";
  const text = new Intl.NumberFormat(localeFor(lang), {
    maximumFractionDigits: 1,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(value);
  return `${text}%`;
}

function parseLocalIso(iso) {
  if (typeof iso !== "string") return null;
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!m) return null;
  const [, y, mo, d, hh = "0", mm = "0"] = m;
  return new Date(Number(y), Number(mo) - 1, Number(d), Number(hh), Number(mm));
}

export function date(iso, lang = "en", opts = { day: "numeric", month: "short", year: "numeric" }) {
  const dt = parseLocalIso(iso);
  return dt ? new Intl.DateTimeFormat(localeFor(lang), opts).format(dt) : "—";
}

export function dateTime(iso, lang = "en") {
  return date(iso, lang, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function clock(dt, lang = "en") {
  return new Intl.DateTimeFormat(localeFor(lang), { hour: "2-digit", minute: "2-digit" }).format(dt);
}

export function hourLabel(hour, lang = "en") {
  if (!Number.isInteger(hour)) return "—";
  return `${String(hour).padStart(2, "0")}:00`;
}

const WEEKDAY_INDEX = { Monday: 0, Tuesday: 1, Wednesday: 2, Thursday: 3, Friday: 4, Saturday: 5, Sunday: 6 };

/** Localize an English weekday name coming from the backend (display only). */
export function weekday(name, lang = "en", width = "long") {
  if (!(name in WEEKDAY_INDEX)) return typeof name === "string" ? name : "—";
  const monday = new Date(2024, 0, 1); // 1 Jan 2024 was a Monday
  const dt = new Date(monday.getFullYear(), 0, 1 + WEEKDAY_INDEX[name]);
  return new Intl.DateTimeFormat(localeFor(lang), { weekday: width }).format(dt);
}

/** Calendar dates covered by an ISO week key like "2024-W51". */
export function isoWeekRange(key) {
  const m = typeof key === "string" && key.match(/^(\d{4})-W(\d{2})$/);
  if (!m) return null;
  const year = Number(m[1]);
  const week = Number(m[2]);
  const jan4 = new Date(year, 0, 4);
  const jan4Dow = (jan4.getDay() + 6) % 7; // Monday = 0
  const start = new Date(year, 0, 4 - jan4Dow + (week - 1) * 7);
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6);
  return { start, end, week };
}

export function weekLabel(key, lang = "en") {
  const range = isoWeekRange(key);
  if (!range) return typeof key === "string" ? key : "—";
  const f = new Intl.DateTimeFormat(localeFor(lang), { day: "numeric", month: "short" });
  return `${f.format(range.start)} – ${f.format(range.end)}`;
}

/**
 * The backend's weekly window starts 8 weeks before the last transaction, so
 * its first ISO-week bucket can be a partial week. This only labels the bar;
 * it never adjusts the backend's revenue value.
 */
export function windowStartsMidWeek(generatedAt, weeks = 8) {
  const end = parseLocalIso(generatedAt);
  if (!end) return false;
  const start = new Date(end.getTime() - weeks * 7 * 86400000);
  const isMondayMidnight = start.getDay() === 1 && start.getHours() === 0 && start.getMinutes() === 0;
  return !isMondayMidnight;
}

export function humanizeSignal(signal) {
  if (typeof signal !== "string" || !signal) return "—";
  const text = signal.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function duration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
