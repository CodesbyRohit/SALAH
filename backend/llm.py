"""
SALAH — LLM safety layer (Phase 3).

AUTHORITY SPLIT
---------------
* ``backend/analytics.py`` is the deterministic engine and the SINGLE SOURCE OF
  TRUTH. It computes every financial number, the evidence trace
  (``build_trace``) and the one recommended experiment
  (``get_recommended_action``).
* This module lets Gemini *explain* that output in merchant-facing Hinglish.
  The model may not compute, correct, extend, contradict or act on anything.
  It cannot touch the database, execute transactions, or call other services.

PHASE 3 PIPELINE (``explain_recommendation``)
---------------------------------------------
1. SANITIZE  — whitelist-copy context/trace/action before anything is sent:
               unknown keys dropped, unsupported types dropped, long strings and
               long lists clipped, confidence forced into the four allowed
               levels.
2. CONSTRAIN — one REST call with an evidence-only system prompt and a fixed
               JSON output contract; the deterministic trace/action are passed
               as data, never as instructions.
3. VALIDATE  — schema, unsupported numbers (validated by CATEGORY: counts,
               amounts, percentages, dates and clock times each have their own
               allowlist), unsupported claims, unsupported sources, causal
               upgrades of UNKNOWN/HYPOTHESIS items, invented customers, and
               any attempt to change the recommendation.
4. FALL BACK — any failure (no API key, timeout, HTTP error, malformed JSON,
               rejected output) returns a deterministic explanation built from
               the same context. Salah never depends on the model.

The model output is never trusted: it is only ever shown if it passes step 3,
and even then the deterministic trace and action are returned alongside it.

Baseline compatibility: ``llm_available()`` and ``explain()`` keep their Phase
0/1 signatures so ``backend/main.py`` keeps working untouched.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Callable

import requests

import analytics

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
LLM_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT", "15"))

# ---------------------------------------------------------------------------
# Contracts (the only shapes the model is allowed to see or produce)
# ---------------------------------------------------------------------------

CONFIDENCE_LEVELS = ("FACT", "PATTERN", "HYPOTHESIS", "UNKNOWN")

CONTEXT_KEYS = (
    "generated_at",
    "weekly_revenue",
    "revenue_by_hour",
    "weakest_weekday",
    "lapsed_regulars",
    "ticket_trend",
    "evening_share",
    "weekday_profile",
)
TRACE_FIELDS = ("signal", "value", "implication", "source", "confidence")
ACTION_FIELDS = ("priority", "observed", "unknown", "experiment", "measure", "basis", "weeks")

REQUIRED_OUTPUT_KEYS = ("answer", "facts_used", "priority")
OPTIONAL_OUTPUT_KEYS = ("caveats",)

# Sanitization limits (also the prompt-size guard).
MAX_STRING_CHARS = 300
MAX_LIST_ITEMS = 40
MAX_TRACE_ITEMS = 40
MAX_DEPTH = 4
MAX_HISTORY_TURNS = 6

# Output limits.
MAX_ANSWER_CHARS = 400
MAX_ANSWER_SENTENCES = 4

# Rejection reasons (stable identifiers, asserted by tests).
R_INVALID_JSON = "invalid_json"
R_SCHEMA = "schema_invalid"
R_TOO_LONG = "answer_too_long"
R_RECOMMENDATION_CHANGED = "recommendation_changed"
R_UNSUPPORTED_SOURCE = "unsupported_source"
R_UNSUPPORTED_NUMBER = "unsupported_number"
R_UNSUPPORTED_CLAIM = "unsupported_claim"
R_CAUSAL_UPGRADE = "causal_upgrade"
R_INVENTED_CUSTOMER = "invented_customer"

SYSTEM_PROMPT = (
    "You are the EXPLANATION LAYER of 'Salah', a payments assistant for a small "
    "Indian merchant. You do NOT analyse data and you do NOT decide anything. "
    "You only explain what the DETERMINISTIC_DATA below already says, in simple "
    "Hindi/Hinglish that a shopkeeper can understand.\n"
    "HARD RULES — breaking any rule makes your answer unusable and it will be "
    "discarded and replaced by a mechanical message:\n"
    "1. Use ONLY numbers that already appear in DETERMINISTIC_DATA. Never "
    "calculate, round into new numbers, or invent customers, trends or events.\n"
    "2. Items with confidence UNKNOWN have no explanation in the data. Never "
    "state a cause for them; say the payment data does not show why.\n"
    "3. Items with confidence HYPOTHESIS are possible only — word them as "
    "'possible'/'ho sakta hai', never as established.\n"
    "4. Never change the recommendation in ACTION. Copy its priority exactly.\n"
    "5. Never mention staff, inventory, stock, opening hours, pricing, weather "
    "or competition: those are not in the data.\n"
    "6. State the facts first, then the recommendation under 'Salah ka sujhav'.\n"
    "7. Reply with ONE JSON object and nothing else — no markdown, no prose "
    "outside the JSON:\n"
    '{"answer": "<max 3 short sentences>", '
    '"facts_used": ["<signal names you actually used>"], '
    '"priority": "<ACTION priority copied exactly>", '
    '"caveats": ["<what the data cannot show>"]}'
)

_last_error: str | None = None


# ---------------------------------------------------------------------------
# Configuration / availability
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY


def llm_available() -> bool:
    """True only when an API key is configured. Everything still works when
    this is False — the deterministic fallback answers instead."""
    return bool(_api_key())


def status() -> dict:
    return {
        "llm_available": llm_available(),
        "model": GEMINI_MODEL,
        "timeout_seconds": LLM_TIMEOUT_SECONDS,
        "last_error": _last_error,
        "confidence_levels": list(CONFIDENCE_LEVELS),
    }


# ---------------------------------------------------------------------------
# 1. SANITIZE — nothing leaves the process until it has been whitelisted
# ---------------------------------------------------------------------------

def _clean_value(value: Any, depth: int = 0) -> Any:
    """Coerce one value into something safe to serialize. Unknown types and
    non-finite floats are dropped rather than forwarded."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if len(value) <= MAX_STRING_CHARS:
            return value
        return value[:MAX_STRING_CHARS] + "…"
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:MAX_LIST_ITEMS]:
            if isinstance(k, str) and 0 < len(k) <= 64:
                out[k] = _clean_value(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean_value(v, depth + 1) for v in list(value)[:MAX_LIST_ITEMS]]
    return None


def sanitize_context(context: Any) -> tuple[dict, list[str]]:
    """Whitelist-copy the deterministic context. Returns (safe_context, warnings).

    Unknown top-level keys are DROPPED: a caller (or a future analytics change)
    cannot smuggle extra text into the model prompt by adding a key.
    """
    warnings: list[str] = []
    if not isinstance(context, dict):
        return {}, ["context_not_a_dict"]
    safe: dict[str, Any] = {}
    for key in CONTEXT_KEYS:
        if key in context:
            safe[key] = _clean_value(context[key])
    for key in context:
        if key not in CONTEXT_KEYS:
            warnings.append(f"dropped_context_key:{key}")
    return safe, warnings[:10]


def sanitize_trace(trace: Any) -> tuple[list[dict], list[str]]:
    """Whitelist-copy the deterministic trace. Items missing a signal are
    dropped; an unrecognised confidence is downgraded to UNKNOWN (understating
    certainty is the safe direction) and reported."""
    warnings: list[str] = []
    if not isinstance(trace, (list, tuple)):
        return [], ["trace_not_a_list"]
    safe: list[dict] = []
    for item in list(trace)[:MAX_TRACE_ITEMS]:
        if not isinstance(item, dict):
            warnings.append("dropped_trace_item:not_a_dict")
            continue
        signal = item.get("signal")
        if not isinstance(signal, str) or not signal.strip():
            warnings.append("dropped_trace_item:no_signal")
            continue
        confidence = item.get("confidence")
        if confidence not in CONFIDENCE_LEVELS:
            warnings.append(f"confidence_downgraded:{signal}")
            confidence = "UNKNOWN"
        safe.append({
            "signal": signal[:120],
            "value": _clean_value(item.get("value")),
            "implication": _clean_value(item.get("implication")),
            "source": _clean_value(item.get("source")),
            "confidence": confidence,
        })
    if len(trace) > MAX_TRACE_ITEMS:
        warnings.append("trace_truncated")
    return safe, warnings[:10]


def sanitize_action(action: Any) -> tuple[dict, list[str]]:
    """Whitelist-copy the recommended action to its seven fields."""
    warnings: list[str] = []
    if not isinstance(action, dict):
        return {"priority": "none"}, ["action_not_a_dict"]
    safe = {k: _clean_value(action.get(k)) for k in ACTION_FIELDS if k in action}
    priority = safe.get("priority")
    if not isinstance(priority, str) or not priority.strip():
        warnings.append("action_has_no_priority")
        safe["priority"] = "none"
    return safe, warnings[:10]


# ---------------------------------------------------------------------------
# 2. CONSTRAIN — build the one payload that goes to the model
# ---------------------------------------------------------------------------

def build_llm_payload(
    context: dict,
    trace: list[dict],
    action: dict,
    question: str | None = None,
    history: list | None = None,
) -> dict:
    """Deterministic Gemini REST payload. Pure function — no I/O."""
    contents: list[dict] = []
    if history:
        for h in list(history)[-MAX_HISTORY_TURNS:]:
            if not isinstance(h, dict):
                continue
            role = "user" if h.get("role") == "user" else "model"
            text = str(h.get("text", ""))[:MAX_STRING_CHARS]
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": (
        "DETERMINISTIC_DATA (the only numbers you may use):\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        "EVIDENCE_TRACE (confidence labels are final — do not upgrade them):\n"
        f"{json.dumps(trace, ensure_ascii=False)}\n\n"
        "ACTION (the only recommendation you may communicate):\n"
        f"{json.dumps(action, ensure_ascii=False)}\n\n"
        f"MERCHANT_QUESTION: {question or 'Aaj ke data ka short update do'}"
    )}]})
    return {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }


# ---------------------------------------------------------------------------
# 3. VALIDATE — the model's output is treated as untrusted input
# ---------------------------------------------------------------------------

# A digit run glued to letters/digits/underscores is an identifier (C001,
# CUST_013, W51), not an amount. Requires the match to start at a real
# boundary.
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_])\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CUST_ID_RE = re.compile(r"\bC(?:UST_)?\d{2,}\b", re.IGNORECASE)
# Sentence boundary = terminator followed by whitespace/end, so the '.' in
# "46.62" and "1000.0" is not treated as a sentence break.
_SENTENCE_RE = re.compile(r"[.!?।]+(?=\s|$)")

# Causal wording. Assertive markers commit to a cause; reason markers
# ("wajah", "reason") are allowed when the sentence negates them.
_ASSERTIVE_CAUSE_RE = re.compile(
    r"\b(because|due to|caused by|caused|the reason is|reason is|as a result of|"
    r"isliye|kyunki|kyon ki|kyun ki|wajah se|ki wajah se)\b",
    re.IGNORECASE,
)
_REASON_MARKER_RE = re.compile(r"\b(wajah|reason|karan|driver)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(
    r"\b(nahi|nahin|not|cannot|can'?t|no|unknown|unclear|pata nahi|maloom nahi)\b",
    re.IGNORECASE,
)

# Topics with no evidence anywhere in the deterministic material.
_UNSUPPORTED_TOPICS = (
    "inventory", "stock", "staff", "staffing", "employee", "employees",
    "opening hours", "closing hours", "closed", "weather", "competition",
    "competitor", "pricing", "price", "prices", "rent", "salary", "daam",
)

# Topics whose cause is UNKNOWN -> asserted causes about them are rejected.
_TOPIC_KEYWORDS = {
    "weekday": [
        "weekday", "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "somwar", "mangalwar", "budhwar", "guruwar",
        "shukrawar", "shanivar", "ravivar", "din",
    ],
    "lapse": [
        "lapse", "lapsed", "regular", "regulars", "customer", "customers",
        "stopped", "band", "gaye", "grahak",
    ],
    "ticket": ["ticket", "spend", "kharch", "basket"],
}

# Numbers are validated by CATEGORY, never by one blanket small-number
# exemption: a count ("12 customers") is only allowed if 12 occurs as a count in
# the deterministic material itself. 1 is the only structurally permitted
# number (singular/indefinite use: "ek test").
_STRUCTURAL_COUNTS = {1.0}

# Structured fields whose integer values are counts (weeks, days, customers).
# Deliberately explicit: a loose hint like "n" would also match "revenue".
COUNT_FIELD_NAMES = (
    "count", "weeks", "days_in_window", "below_avg_days", "visits", "occurrences",
)

_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_PCT_RE = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)\s*%")


def _walk_material(
    node: Any,
    key: str | None,
    strings: list[str],
    counts: list[float],
    depth: int = 0,
) -> None:
    """Collect the material's string values and count-shaped numeric fields.

    Only strings are scanned for numbers, so JSON keys and unrelated numeric
    fields (hour-of-day, timestamps) cannot pollute the count pool.
    """
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_material(v, k if isinstance(k, str) else None, strings, counts, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk_material(v, None, strings, counts, depth + 1)
    elif isinstance(node, str):
        strings.append(node)
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and key:
        if any(name in key.lower() for name in COUNT_FIELD_NAMES):
            try:
                if float(node).is_integer():
                    counts.append(float(node))
            except (TypeError, ValueError):
                pass


def _material_parts(context: dict, trace: list[dict], action: dict) -> tuple[list[str], list[float]]:
    strings: list[str] = []
    counts: list[float] = []
    for part in (context, trace, action):
        _walk_material(part, None, strings, counts)
    return strings, counts


def allowed_sources(trace: list[dict]) -> set[str]:
    """Signal/source names the explanation is allowed to cite."""
    out: set[str] = set()
    for item in trace:
        for field in ("signal", "source"):
            v = item.get(field)
            if isinstance(v, str) and v:
                out.add(v)
    return out


def allowed_numbers(context: dict, trace: list[dict], action: dict) -> set[float]:
    """Flat allowlist for AMOUNTS, DATES and CLOCK TIMES: the numbers that
    already exist in the deterministic material, plus whole/one-decimal
    roundings of them (honest summarising)."""
    blob = json.dumps([context, trace, action], ensure_ascii=False)
    allowed: set[float] = set()
    for token in _NUM_RE.findall(blob):
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        allowed.update({value, round(value), round(value, 1), round(value, 2)})
    return allowed


def count_pool(context: dict, trace: list[dict], action: dict) -> set[float]:
    """The integers the model may use as COUNTS.

    Built from the material's own text with ISO timestamps, clock times and
    percentages masked out (so digits inside a date such as ``2024-10-06`` can
    never be laundered into a count), plus the integer values of count-shaped
    fields such as ``weeks`` / ``days_in_window`` / ``count``.
    """
    strings, counts = _material_parts(context, trace, action)
    pool: set[float] = set(_STRUCTURAL_COUNTS)
    for text in strings:
        # Mask timestamps, clock times, percentages and identifiers first, so
        # digits inside a date (2024-10-06) or a customer id (CUST_013) can
        # never be laundered into a count.
        masked = _WORD_RE.sub(" ", _PCT_RE.sub(" ", _CLOCK_RE.sub(" ", _ISO_TS_RE.sub(" ", text))))
        for token in _NUM_RE.findall(masked):
            try:
                value = float(token.replace(",", ""))
            except ValueError:
                continue
            if value.is_integer() and 0 <= value < 100:
                pool.add(value)
    for value in counts:
        if 0 <= value < 100:
            pool.add(value)
    return pool


def percentage_values(context: dict, trace: list[dict], action: dict) -> set[float]:
    """Percentages the material actually states, with honest roundings."""
    strings, _ = _material_parts(context, trace, action)
    out: set[float] = set()
    for text in strings:
        for token in _PCT_RE.findall(text):
            try:
                value = float(token.replace(",", ""))
            except ValueError:
                continue
            out.update({value, round(value), round(value, 1), round(value, 2)})
    return out


def _classify_number_token(token: str, match: re.Match, text: str) -> str:
    """Which category the model is using this number in."""
    start = match.start()
    if any(m.start() <= start < m.end() for m in _ISO_TS_RE.finditer(text)):
        return "date"
    if any(m.start() <= start < m.end() for m in _CLOCK_RE.finditer(text)):
        return "time"
    if text[match.end():match.end() + 4].lstrip().startswith("%"):
        return "percentage"
    if "." in token or "," in token:
        return "amount"
    try:
        value = float(token.replace(",", ""))
    except ValueError:
        return "date"
    return "amount" if value >= 100 else "count"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text or "") if s.strip()]


def _parse_json_object(raw: Any) -> tuple[dict | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        # A list/number/None is not a JSON object, whatever it claims to be.
        return None, R_INVALID_JSON
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except Exception:
        return None, R_INVALID_JSON
    if not isinstance(obj, dict):
        # A bare string/array/number is not our output contract.
        return None, R_INVALID_JSON
    return obj, None


def _reject(reason: str, detail: str = "") -> dict:
    return {"ok": False, "reason": reason, "detail": detail[:160], "payload": None}


def _unknown_topics(trace: list[dict]) -> set[str]:
    """Which topics the trace explicitly marks as unexplained."""
    topics: set[str] = set()
    for item in trace:
        if item.get("confidence") != "UNKNOWN":
            continue
        signal = str(item.get("signal", "")).lower()
        if "weekday" in signal:
            topics.add("weekday")
        if "lapse" in signal or "churn" in signal:
            topics.add("lapse")
        if "ticket" in signal:
            topics.add("ticket")
    return topics


def validate_explanation(
    raw: Any,
    context: dict,
    trace: list[dict],
    action: dict,
) -> dict:
    """Validate untrusted model output.

    Returns {"ok": bool, "reason": str|None, "detail": str, "payload": dict|None}.
    ``payload`` is the cleaned ``{answer, facts_used, priority, caveats}`` and is
    only ever populated when ok is True.
    """
    obj, err = _parse_json_object(raw)
    if obj is None:
        return _reject(err or R_INVALID_JSON)

    # --- schema ---------------------------------------------------------
    for key in REQUIRED_OUTPUT_KEYS:
        if key not in obj:
            return _reject(R_SCHEMA, f"missing:{key}")
    answer = obj.get("answer")
    facts_used = obj.get("facts_used")
    priority = obj.get("priority")
    caveats = obj.get("caveats", [])
    if not isinstance(answer, str) or not answer.strip():
        return _reject(R_SCHEMA, "answer_not_a_non_empty_string")
    if not isinstance(facts_used, list) or not all(isinstance(f, str) for f in facts_used):
        return _reject(R_SCHEMA, "facts_used_not_a_list_of_strings")
    if not isinstance(priority, str):
        return _reject(R_SCHEMA, "priority_not_a_string")
    if not isinstance(caveats, list) or not all(isinstance(c, str) for c in caveats):
        return _reject(R_SCHEMA, "caveats_not_a_list_of_strings")

    answer = answer.strip()
    facts_used = [f.strip() for f in facts_used]
    projected_text = " ".join([answer] + facts_used + caveats)

    # --- length ---------------------------------------------------------
    if len(answer) > MAX_ANSWER_CHARS:
        return _reject(R_TOO_LONG, f"{len(answer)} chars")
    if len(_sentences(answer)) > MAX_ANSWER_SENTENCES:
        return _reject(R_TOO_LONG, f"{len(_sentences(answer))} sentences")

    # --- recommendation must not change ---------------------------------
    expected_priority = action.get("priority") or "none"
    if priority.strip() != expected_priority:
        return _reject(R_RECOMMENDATION_CHANGED, f"{priority!r} != {expected_priority!r}")

    # --- only real trace signals may be cited ---------------------------
    sources = allowed_sources(trace)
    for fact in facts_used:
        if fact not in sources:
            return _reject(R_UNSUPPORTED_SOURCE, fact)

    # --- no new numbers, validated by category --------------------------
    # Amounts / dates / clock times share the flat material allowlist; counts
    # and percentages are checked against their own strict sets, so a figure
    # cannot be invented as a "count" just because it is small.
    permitted = allowed_numbers(context, trace, action)
    counts = count_pool(context, trace, action)
    percentages = percentage_values(context, trace, action)
    for match in _NUM_RE.finditer(projected_text):
        token = match.group()
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        category = _classify_number_token(token, match, projected_text)
        if category == "count":
            if value not in counts:
                return _reject(R_UNSUPPORTED_NUMBER, f"{token} (count)")
        elif category == "percentage":
            if value not in percentages:
                return _reject(R_UNSUPPORTED_NUMBER, f"{token} (percentage)")
        elif value not in permitted:
            return _reject(R_UNSUPPORTED_NUMBER, f"{token} ({category})")

    # --- no invented customers -----------------------------------------
    known_ids = {
        c.get("customer_id")
        for c in ((context.get("lapsed_regulars") or {}).get("customers") or [])
        if isinstance(c, dict)
    }
    for token in _CUST_ID_RE.findall(projected_text):
        if token not in known_ids:
            return _reject(R_INVENTED_CUSTOMER, token)

    # --- no causal upgrade of UNKNOWN items ----------------------------
    topics = _unknown_topics(trace)
    if topics:
        keywords = {k for t in topics for k in _TOPIC_KEYWORDS[t]}
        for sentence in _sentences(projected_text):
            low = sentence.lower()
            if not any(k in low for k in keywords):
                continue
            if _ASSERTIVE_CAUSE_RE.search(sentence):
                return _reject(R_CAUSAL_UPGRADE, sentence)
            marker = _REASON_MARKER_RE.search(sentence)
            if marker and not _NEGATION_RE.search(sentence):
                return _reject(R_CAUSAL_UPGRADE, sentence)

    # --- no claims about topics the data does not contain ---------------
    material = json.dumps([context, trace, action], ensure_ascii=False).lower()
    for sentence in _sentences(projected_text):
        low = sentence.lower()
        for topic in _UNSUPPORTED_TOPICS:
            if topic in low and topic not in material and not _NEGATION_RE.search(sentence):
                return _reject(R_UNSUPPORTED_CLAIM, topic)

    payload = {
        "answer": answer,
        "facts_used": facts_used,
        "priority": priority.strip(),
        "caveats": [c.strip() for c in caveats if c.strip()],
    }
    return {"ok": True, "reason": None, "detail": "", "payload": payload}


# ---------------------------------------------------------------------------
# 4. FALL BACK — deterministic explanation, no model involved
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> dict:
    """Dict-shaped material, or {} — the safety net must never raise on a
    caller's malformed shape."""
    return value if isinstance(value, dict) else {}


def _as_rows(value: Any) -> list[dict]:
    """List of dict rows, or [] — non-dict entries are skipped."""
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, dict)]


def _as_number(value: Any) -> int | float | None:
    """A real number, or None. Booleans do not count. The value is returned
    unchanged so formatting matches the raw deterministic value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def deterministic_explanation(context: Any, trace: Any, action: Any) -> str:
    """Hindi/Hinglish explanation built only from deterministic values.

    Always returns a non-empty string, never depends on the network, and —
    because it is the safety net — never raises: a malformed piece of the
    material is skipped rather than propagated, and a value is only quoted when
    it is present and numeric.
    """
    context = _as_dict(context)
    action = _as_dict(action)
    trace = trace if isinstance(trace, (list, tuple)) else []
    parts: list[str] = []

    revenues = [v for v in (
        _as_number(row.get("revenue")) for row in _as_rows(context.get("weekly_revenue"))
    ) if v is not None]
    if revenues:
        total = round(sum(revenues), 2)
        avg = round(total / len(revenues), 2)
        parts.append(
            f"Pichhle {len(revenues)} hafte ka total revenue {total} hai "
            f"(average {avg} per hafta)."
        )

    weekday = _as_dict(context.get("weakest_weekday"))
    day = weekday.get("weekday")
    day_avg = _as_number(weekday.get("avg_daily_revenue"))
    day_overall = _as_number(weekday.get("overall_avg_daily_revenue"))
    if day and day_avg is not None and day_overall is not None:
        parts.append(
            f"{day} ka average {day_avg} hai, jabki overall daily average {day_overall} hai."
        )

    lapsed_count = _as_number(_as_dict(context.get("lapsed_regulars")).get("count"))
    if lapsed_count:
        parts.append(
            f"{lapsed_count} purane regular customers 4 hafte se koi "
            "transaction nahi kar rahe."
        )

    ticket = _as_dict(context.get("ticket_trend"))
    early = _as_number(ticket.get("early_avg_ticket"))
    recent = _as_number(ticket.get("recent_avg_ticket"))
    change = _as_number(ticket.get("change_pct"))
    if early is not None and recent is not None and change is not None:
        parts.append(f"Average ticket {early} se {recent} hua ({change}%).")

    share = _as_number(_as_dict(context.get("evening_share")).get("evening_share_pct"))
    if share is not None:
        parts.append(f"Revenue ka {share}% 18:00-22:00 ke beech aata hai.")

    if not parts:
        parts.append("Abhi data itna kam hai ki koi clear trend nahi dikh raha.")

    if any(isinstance(item, dict) and item.get("confidence") == "UNKNOWN"
           for item in trace):
        parts.append("Payment data se yeh pata nahi chalta ki aisa kyun hua.")

    priority = action.get("priority")
    if priority and priority != "none":
        parts.append(
            f"Salah ka sujhav: {action.get('experiment')} "
            f"Kaise naapein: {action.get('measure')}"
        )
    else:
        parts.append("Filhaal koi bada experiment suggest nahi karte — measure karte rahiye.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _default_transport(url: str, payload: dict, timeout: float) -> dict:
    """The only place Salah touches the network for LLM work."""
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_text(data: Any) -> str | None:
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


def _sanitize_history(history: list | None) -> list[dict]:
    if not isinstance(history, (list, tuple)):
        return []
    out = []
    for h in list(history)[-MAX_HISTORY_TURNS:]:
        if isinstance(h, dict):
            out.append({
                "role": "user" if h.get("role") == "user" else "model",
                "text": str(h.get("text", ""))[:MAX_STRING_CHARS],
            })
    return out


def explain_recommendation(
    question: str,
    context: dict,
    trace: list[dict] | None = None,
    action: dict | None = None,
    history: list | None = None,
    transport: Callable[[str, dict, float], dict] | None = None,
) -> dict:
    """Run the full Phase 3 pipeline and always return a usable explanation.

    Returns::

        {
          "answer": str,                 # merchant-facing text (never empty)
          "source": "llm" | "deterministic",
          "fallback_reason": str | None, # why the deterministic path was used
          "rejections": [ {"reason": str, "detail": str} ],
          "trace": list[dict],           # sanitized deterministic evidence
          "action": dict,                # sanitized deterministic recommendation
          "sanitize_warnings": list[str],
          "model_output": str | None,    # raw model text, for debugging only
        }
    """
    global _last_error
    sanitized_ctx, w_ctx = sanitize_context(context)
    build_warnings: list[str] = []
    if trace is None or action is None:
        # The deterministic engine remains the source of truth, but a malformed
        # context must not be able to break the safety net that protects it.
        try:
            built_trace = analytics.build_trace(sanitized_ctx)
            built_action = analytics.get_recommended_action(sanitized_ctx)
        except Exception as exc:
            built_trace, built_action = [], {"priority": "none"}
            build_warnings.append(f"trace_build_failed:{type(exc).__name__}")
        if trace is None:
            trace = built_trace
        if action is None:
            action = built_action
    sanitized_trace, w_trace = sanitize_trace(trace)
    sanitized_action, w_action = sanitize_action(action)
    warnings = w_ctx + build_warnings + w_trace + w_action

    result = {
        "answer": "",
        "source": "deterministic",
        "fallback_reason": None,
        "rejections": [],
        "trace": sanitized_trace,
        "action": sanitized_action,
        "sanitize_warnings": warnings,
        "model_output": None,
    }

    def fallback(reason: str) -> dict:
        result["fallback_reason"] = reason
        result["answer"] = deterministic_explanation(
            sanitized_ctx, sanitized_trace, sanitized_action
        )
        return result

    if not llm_available():
        return fallback("llm_unavailable")

    payload = build_llm_payload(
        sanitized_ctx, sanitized_trace, sanitized_action,
        question=question, history=_sanitize_history(history),
    )
    url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL)
    post = transport or _default_transport
    try:
        data = post(url, payload, LLM_TIMEOUT_SECONDS)
        _last_error = None
    except Exception as exc:  # timeout, HTTP error, connection reset, bad JSON
        _last_error = f"{type(exc).__name__}: {exc}"
        return fallback("llm_error")

    raw = _extract_text(data)
    if not raw:
        _last_error = "no candidate text in response"
        return fallback("empty_model_response")
    result["model_output"] = raw[:500]

    verdict = validate_explanation(raw, sanitized_ctx, sanitized_trace, sanitized_action)
    if not verdict["ok"]:
        result["rejections"].append({"reason": verdict["reason"], "detail": verdict["detail"]})
        _last_error = f"rejected: {verdict['reason']}"
        return fallback(f"rejected:{verdict['reason']}")

    result["source"] = "llm"
    result["answer"] = verdict["payload"]["answer"]
    result["explanation"] = verdict["payload"]
    _last_error = None
    return result


def explain(
    question: str,
    merchant_context: dict,
    history: list | None = None,
    transport: Callable[[str, dict, float], dict] | None = None,
) -> str:
    """Baseline-compatible entrypoint (used by ``backend/main.py``): runs the
    safety pipeline and returns only the text. Never returns None — when the
    model is unavailable or its output is rejected, the deterministic
    explanation is returned instead."""
    result = explain_recommendation(
        question, merchant_context, trace=None, action=None,
        history=history, transport=transport,
    )
    return result["answer"]


if __name__ == "__main__":
    ctx = analytics.build_merchant_context()
    print(json.dumps(status(), indent=2))
    out = explain_recommendation("Mangalwar itna khaali kyun tha?", ctx)
    print(json.dumps({k: v for k, v in out.items() if k != "trace"}, indent=2, ensure_ascii=False))
