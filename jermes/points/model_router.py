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
