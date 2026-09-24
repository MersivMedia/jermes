"""Typed Jev questions and answers.

Three primitives, mirroring the TypeSafe System One API:

* ``Noul``   - is this statement true?        -> probability in [0, 1]
* ``Choice`` - which of these options?        -> option, per-option probs, confidence
* ``Score``  - where on this ordered scale?   -> weighted score, per-level probs, confidence

Limits are validated client-side so a malformed spec fails at build time rather
than as a 422 on the hot path: Choice <= 255 options, Score 2..10 levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

Instructions = Union[str, Dict[str, Any], List[Any]]


class SpecError(ValueError):
    """A question spec violates the Jev API contract."""


@dataclass(frozen=True)
class Noul:
    instructions: Instructions
    criteria: Optional[Mapping[str, Any]] = None  # {"true": ..., "false": ...}

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            bad = set(self.criteria) - {"true", "false"}
            if bad:
                raise SpecError(f"Noul criteria keys must be 'true'/'false', got {sorted(bad)}")
            out["criteria"] = dict(self.criteria)
        return out


@dataclass(frozen=True)
class Choice:
    instructions: Instructions
    criteria: Mapping[str, Any]  # option -> description (or None)

    def to_wire(self) -> Dict[str, Any]:
        n = len(self.criteria)
        if n < 2:
            raise SpecError("Choice needs at least 2 options")
        if n > MAX_CHOICE_OPTIONS:
            raise SpecError(f"Choice supports at most {MAX_CHOICE_OPTIONS} options, got {n}")
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Score:
    instructions: Instructions
    criteria: List[Any]  # ordered low -> high

    def to_wire(self) -> Dict[str, Any]:
        n = len(self.criteria)
        if not MIN_SCORE_LEVELS <= n <= MAX_SCORE_LEVELS:
            raise SpecError(
                f"Score needs {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} levels, got {n}"
            )
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.criteria)}


Question = Union[Noul, Choice, Score]


def questions_to_wire(questions: Mapping[str, Question]) -> Dict[str, Dict[str, Any]]:
    if not questions:
        raise SpecError("at least one question is required")
    return {qid: q.to_wire() for qid, q in questions.items()}


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


def _distribution_confidence(probabilities: Mapping[str, float]) -> float:
    """Fallback confidence when a backend omits it.

    Uses ``(n * p_max - 1) / (n - 1)``, the approximation TypeSafe's confidence
    docs show for its interactive demo: 1.0 when all mass sits on one option,
    0.0 for a uniform split. The real statistic is undisclosed, so this is only
    used when the response carries no ``confidence`` field.
    """
    values = [float(v) for v in probabilities.values()]
    n = len(values)
    if n < 2:
        return 1.0
    return max(0.0, min(1.0, (n * max(values) - 1.0) / (n - 1.0)))


@dataclass(frozen=True)
class NoulAnswer:
    noul: float
    type: str = "noul"

    @property
    def probability(self) -> float:
        return self.noul


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Dict[str, float]
    confidence: float
    type: str = "choice"

    def top(self, k: int) -> List[tuple]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])[:k]


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: Dict[str, float]
    confidence: float
    legend: Dict[str, Any] = field(default_factory=dict)
    type: str = "score"


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer]


def parse_answer(raw: Mapping[str, Any]) -> Answer:
    """Parse one answer from either wire dialect.

    TypeSafe (and Vercel's TypeSafe-compatible endpoint) use ``noul``; Vercel's
    native ``/v1/evaluate`` uses ``boolean`` with ``probability``. Both land in
    ``NoulAnswer``.
    """
    kind = raw.get("type")
    if kind in ("noul", "boolean"):
        value = raw.get("noul", raw.get("probability"))
        if value is None:
            raise ValueError(f"noul answer missing probability: {raw!r}")
        return NoulAnswer(noul=float(value))
    if kind == "choice":
        probs = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
        choice = raw.get("choice")
        if choice is None and probs:
            choice = max(probs, key=probs.get)
        conf = raw.get("confidence")
        return ChoiceAnswer(
            choice=str(choice),
            probabilities=probs,
            confidence=float(conf) if conf is not None else _distribution_confidence(probs),
        )
    if kind == "score":
        probs = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
        score = raw.get("score")
        if score is None:
            score = sum(int(k) * v for k, v in probs.items())
        conf = raw.get("confidence")
        return ScoreAnswer(
            score=float(score),
            probabilities=probs,
            confidence=float(conf) if conf is not None else _distribution_confidence(probs),
            legend=dict(raw.get("legend") or {}),
        )
    raise ValueError(f"unknown answer type {kind!r}")


def answer_to_dict(answer: Answer) -> Dict[str, Any]:
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "noul": answer.noul}
    if isinstance(answer, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": answer.choice,
            "probabilities": answer.probabilities,
            "confidence": answer.confidence,
        }
    return {
        "type": "score",
        "score": answer.score,
        "probabilities": answer.probabilities,
        "confidence": answer.confidence,
        "legend": answer.legend,
    }
