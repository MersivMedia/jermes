"""D7: per-turn model routing (PRD section 5.7).

At the start of a turn, one Jev request scores difficulty and stakes. When a
turn is confidently easy and low-stakes, an ``llm_request`` middleware swaps
the ``model`` kwarg for ``cheap_model`` on every API call *in that turn*, so
the provider prompt cache is reused within the tool loop.

Current scope: same-provider routing only (just the model name changes).
Cross-provider routing needs ``llm_execution`` middleware and is deferred.
Nothing happens unless ``cheap_model`` is configured and mode is ``enforce``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from ..engine import Verdict
from ..questions import Noul, Score

SPEC_VERSION = "model_router.1"

DIFFICULTY_LEVELS = [
    "A direct lookup, a single command, or a short factual answer",
    "A localized change or a short synthesis of a few sources",
    "Multi-step reasoning, debugging, architecture, or long-form writing",
]


def build(user_message: str, recent_context: str = ""):
    state = {"request": user_message[:6000], "recent_context": recent_context[:2000]}
    qs = {
        "difficulty": Score(
            instructions="How hard is it to complete `request` well?",
            criteria=DIFFICULTY_LEVELS,
        ),
        "high_stakes": Noul(
            instructions=(
                "Would a mistake on `request` be costly or hard to undo, for example money, production "
                "systems, legal or medical matters, deleting data, or messages sent to other people?"
            )
        ),
    }
    return state, qs


# $ per million tokens: input, output, cache read, cache write (5-minute tier).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}
WINDOWS = {"claude-haiku-4-5": 200_000}


def _key(model: str) -> str:
    m = (model or "").lower().split("/")[-1].replace(".", "-")
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k):
            return k
    return m


def switch_pays(current: str, cheap: str, prompt_tokens: int, *, cache_warm: bool, calls: int = 6,
                window_margin: float = 0.85) -> tuple:
    """(ok, reason): is moving this turn to ``cheap`` actually cheaper?

    Compares one turn of ``calls`` requests over ``prompt_tokens`` of context.
    Staying on a warm cache reads the prefix; switching writes the whole
    prompt into the other model's cache first. Unknown prices: don't switch.
    """
    c, k = _key(current), _key(cheap)
    if prompt_tokens and WINDOWS.get(k) and prompt_tokens > WINDOWS[k] * window_margin:
        return False, f"context {prompt_tokens:,} tokens doesn't fit {cheap}"
    if c not in PRICES or k not in PRICES:
        return False, "no price for one of the models"
    if not prompt_tokens:
        return True, "context size unknown"
    _, _, cr, cw = PRICES[c]
    _, _, kr, kw = PRICES[k]
    first_stay = prompt_tokens * (cr if cache_warm else cw)
    stay = first_stay + (calls - 1) * prompt_tokens * cr
    switch = prompt_tokens * kw + (calls - 1) * prompt_tokens * kr
    if switch >= stay:
        return False, f"switching costs more ({switch / 1e6:.3f} vs {stay / 1e6:.3f} $ per turn)"
    return True, "cheaper"


def make_policy(cfg: Mapping[str, Any]):
    max_diff = float(cfg.get("max_difficulty", 0.6))
    min_conf = float(cfg.get("min_confidence", 0.7))
    max_stakes = float(cfg.get("max_stakes", 0.3))

    def policy(a: Dict[str, Any]) -> Verdict:
        d = a["difficulty"]
        s = a["high_stakes"].noul
        detail = {"difficulty": round(d.score, 3), "confidence": round(d.confidence, 3), "stakes": round(s, 3)}
        if d.score <= max_diff and d.confidence >= min_conf and s <= max_stakes:
            return Verdict("cheap", detail)
        return Verdict("default", detail)

    return policy
