# Salah — voice-first business partner for small Indian merchants

> "Paytm already tells merchants what happened. Salah tells them what changed,
> what to try next, and whether it worked."

## Run

```bash
cd salah
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env      # add GEMINI_API_KEY / SARVAM_API_KEY (optional)
python data/seed.py       # generate data/merchant.db
python backend/main.py    # http://localhost:8000
```

## Architecture

```
Payment data (SQLite)
  → Deterministic analytics (backend/analytics.py — NO LLM)
  → Structured merchant context
  → Evidence-aware LLM explanation (Gemini, REST)
  → Merchant action / experiment
  → Future measurement
  → Memory (Cognee + local JSONL fallback)
```

- `/context` — deterministic merchant metrics
- `/ask` — question → answer (+trace)
- `/nudge` — proactive message
- `/voice/stt`, `/voice/tts` — Sarvam voice
- `/reset` — clear conversation

## Honest data statement

The dataset is **representative synthetic merchant data with planted business
patterns** (Tuesday weakness, evening concentration, lapsed regulars, ticket
decline) so the analytics and decision loop can be demonstrated reproducibly.
It is NOT from a real merchant.

## LLM safety layer (Phase 3)

`backend/llm.py` lets Gemini *explain* the deterministic output and nothing
else. The model never computes a number, never chooses the recommendation, and
never touches the database.

| Authority | Owner |
|---|---|
| Every financial metric, the evidence trace, the recommended experiment | `backend/analytics.py` (deterministic) |
| Wording only — a merchant-facing Hinglish explanation of that output | Gemini, inside `backend/llm.py` |

Pipeline (`llm.explain_recommendation`):

1. **Sanitize** — context, trace and action are whitelist-copied before
   anything is sent: unknown keys dropped, unsupported types dropped, long
   strings/lists clipped, confidence forced into
   `FACT | PATTERN | HYPOTHESIS | UNKNOWN`.
2. **Constrain** — one REST call with an evidence-only system prompt. The only
   numbers the model may use are the numbers already present in the
   deterministic data, and it must copy the action's priority exactly.
3. **Validate** — untrusted output is rejected when it is not a JSON object,
   breaks the schema, exceeds 4 sentences / 400 characters, changes the
   recommendation, cites a signal that is not in the trace, introduces a number
   that is not in the deterministic data, invents a customer id, asserts a cause
   for an `UNKNOWN` item, or claims staff / inventory / opening hours / pricing /
   weather / competition.
4. **Fall back** — on a missing key, timeout, HTTP error, malformed or rejected
   output, `deterministic_explanation()` answers from the same context. Salah
   stays fully functional with no API key at all.

Rejections are reported, never repaired: `explain_recommendation()` returns
`{answer, source, fallback_reason, rejections, trace, action, sanitize_warnings,
model_output}`, so the deterministic evidence and the reason for any fallback
stay visible.

### Configure

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(unset)* | Unlocks the LLM layer. Unset = deterministic-only mode. |
| `GEMINI_MODEL` | `gemini-1.5-flash` | Model used for the explanation. |
| `GEMINI_TIMEOUT` | `15` | Seconds before the call is abandoned and the fallback answers. |

## Tests

```bash
python -m unittest discover -s tests -v
```

- `tests/test_llm_safety.py` — sanitization, a valid trace → valid explanation,
  and every rejection/fallback path (no network, no database).
- `tests/test_deterministic_pipeline.py` — Phase 1/2 regression: trace and
  action determinism, trace numbers traceable to the context, no cause presented
  as `FACT`, no hardcoded customer ids or planted weekdays. Skipped if
  `data/merchant.db` is missing.

## Status

- **Phase 1** — data/time correctness: `get_data_end()` anchoring, behavioural
  regular classification, `first_visit` fix.
- **Phase 2** — deterministic glass-box trace (`build_trace`) and one-experiment
  action engine (`get_recommended_action`).
- **Phase 3** — LLM safety layer: sanitized input, constrained prompt, validated
  output, deterministic fallback. The model explains; it never decides.

Not yet built (later phases, tracked separately): wiring the trace/action into
`/ask` and `/nudge`, scripted-demo cache, persistent memory, frontend, voice/n8n,
E2E, hardening.
