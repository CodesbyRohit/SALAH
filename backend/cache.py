"""
SALAH — scripted-demo cache.

Cache-first reliability layer for the three scripted demo questions. The cache
is generated FROM the live analytics context (no hardcoded fake numbers) and is
ONLY a demo reliability layer — live questions still use the live system.

BASELINE (pre-Phase-6) BEHAVIOUR — kept intentionally so Phase 6 has real work:
  * entries store {answer} only; {answer, trace} contract arrives in Phase 6
  * no precompute wiring in main.py yet
"""
import os

_cache: dict[str, dict] = {}

DEMO_QUESTIONS = [
    "is hafte kitna kamaya",
    "mangalwar itna khaali kyun tha",
    "kya karun?",
]


def _normalize(q: str) -> str:
    return " ".join(q.lower().strip().split())


def get_cached(question: str) -> dict | None:
    return _cache.get(_normalize(question))


def put_cached(question: str, answer: str) -> None:
    _cache[_normalize(question)] = {"answer": answer}


def precompute(context: dict, answers: dict[str, str]) -> int:
    """Populate cache from live answers (answers dict: question -> answer text)."""
    n = 0
    for q in DEMO_QUESTIONS:
        a = answers.get(q)
        if a:
            put_cached(q, a)
            n += 1
    return n


def warm(context: dict, llm_explain) -> int:
    """Baseline warm helper: generate answers via the provided explain callable."""
    answers = {}
    for q in DEMO_QUESTIONS:
        a = llm_explain(q, context)
        if a:
            answers[q] = a
    return precompute(context, answers)


def cache_size() -> int:
    return len(_cache)


if __name__ == "__main__":
    print("demo questions:", DEMO_QUESTIONS)
