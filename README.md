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

## Status

Baseline scaffold implementing the specified architecture. Phase plan
(P0 data/time correctness → deterministic glass-box trace → action engine →
LLM safety → integration → cache → memory → frontend → voice/n8n → E2E →
hardening) tracked separately.
