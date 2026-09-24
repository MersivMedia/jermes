"""D1: skill ranking (PRD section 5.1).

Built on TypeSafe's two-call skill-suggestion cookbook, which measured wrong
skill loads falling 16.8% -> 7.3% on the Hermes roster:

  call 1 (skim):   Choice over every skill (name -> index description) plus
                   three gate Nouls asking whether the turn wants action taken.
  call 2 (rerank): Choice over the top N with full description + SKILL.md
                   excerpt, plus one "does this skill fit" Noul per candidate.

Unlike the cookbook (one suggestion), Jermes returns a *ranking*: candidates
that pass the "fits" gate, ordered by the rerank Choice probability. The block
is injected into the *user message* via ``pre_llm_call`` (Hermes' cache-safe
injection point), never the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..engine import Verdict
from ..questions import MAX_CHOICE_OPTIONS, Choice, Noul

SPEC_VERSION = "skill_suggest.2"
SKIM_DESC_CHARS = 200  # keep call 1 well inside Jev's 32k state+question budget

GATE_QUESTIONS = {
    "acts_on_user_system": (
        "Is the assistant being asked to act on the user's files, accounts, devices, "
        "or online services, rather than only to explain or advise?"
    ),
    "would_follow_documented_procedure": (
        "Would a careful expert answering this consult a specific documented procedure "
        "or set of commands, rather than answering from general understanding?"
    ),
    "prose_suffices": (
        "Could a knowledgeable generalist fully satisfy this request in prose, with "
        "no tools, no documentation, and no access to the user's files or accounts?"
    ),
}
INVERTED = {"prose_suffices"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str = ""
    category: str = ""


def load_roster() -> List[Skill]:
    """Read the active profile's skills via Hermes' own helpers."""
    try:
        from agent.skill_utils import (  # type: ignore
            get_all_skills_dirs,
            iter_skill_index_files,
            parse_frontmatter,
        )
    except Exception:
        return []
    seen: Dict[str, Skill] = {}
    for root in get_all_skills_dirs():
        root = Path(root)
        if not root.exists():
            continue
        for path in iter_skill_index_files(root, "SKILL.md"):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                meta, body = parse_frontmatter(text)
            except Exception:
                continue
            name = str(meta.get("name") or Path(path).parent.name).strip()
            if not name or name in seen:
                continue
            seen[name] = Skill(
                name=name,
                description=str(meta.get("description") or "").strip(),
                body=body.strip(),
                category=Path(path).parent.parent.name,
            )
    return sorted(seen.values(), key=lambda s: s.name)


def state_for(user_message: str, recent_context: str = "") -> Dict[str, Any]:
    return {"request": user_message[:6000], "recent_context": recent_context[:2000]}


def skim_questions(roster: Sequence[Skill]) -> List[Dict[str, Any]]:
    """One question set per chunk of <=255 skills (plus gate Nouls on the first)."""
    out: List[Dict[str, Any]] = []
    for start in range(0, len(roster), MAX_CHOICE_OPTIONS):
        chunk = roster[start : start + MAX_CHOICE_OPTIONS]
        if len(chunk) < 2:
            chunk = list(roster[max(0, start - 1) : start + 1])
        qs: Dict[str, Any] = {
            "which": Choice(
                instructions="Which of these skills, if any, is the right one to load to help with the user's latest `request`?",
                criteria={s.name: ((s.description or "")[:SKIM_DESC_CHARS] or None) for s in chunk},
            )
        }
        if start == 0:
            for key, text in GATE_QUESTIONS.items():
                qs[f"gate::{key}"] = Noul(instructions=text)
        out.append(qs)
    return out


def gate_value(answers: Mapping[str, Any]) -> float:
    vals = []
    for key in GATE_QUESTIONS:
        v = answers[f"gate::{key}"].noul
        vals.append(1.0 - v if key in INVERTED else v)
    return sum(vals) / len(vals)


def rerank_questions(names: Sequence[str], by_name: Mapping[str, Skill], excerpt_chars: int) -> Dict[str, Any]:
    qs: Dict[str, Any] = {
        "which": Choice(
            instructions=(
                "Exactly one of these skills is the right one to load for the user's latest `request`. "
                "Which one? Read what each actually does, not just its name."
            ),
            criteria={
                n: f"{by_name[n].description} — {by_name[n].body[:excerpt_chars]}" for n in names
            },
        )
    }
    for n in names:
        qs[f"fits::{n}"] = Noul(
            instructions=(
                f"Does the skill '{n}' do the specific thing the user's `request` asks for? "
                f"It is described as: {by_name[n].description}"
            )
        )
    return qs


def make_rerank_policy(fits_threshold: float, names: Sequence[str], max_ranked: int = 3):
    """Rank = rerank Choice probability, filtered by each skill's own "fits" Noul.

    The Choice is relative (which one), the Nouls are absolute (does it fit at
    all), so all candidates can fail and the ranking can come back empty.
    """

    def policy(a: Dict[str, Any]) -> Verdict:
        probs = a["which"].probabilities
        fits = {n: a[f"fits::{n}"].noul for n in names}
        ordered = sorted(names, key=lambda n: -probs.get(n, 0.0))
        ranking = [
            {"skill": n, "p": round(probs.get(n, 0.0), 3), "fits": round(fits[n], 3)}
            for n in ordered
            if fits[n] >= fits_threshold
        ][:max_ranked]
        detail = {
            "candidates": [{"skill": n, "p": round(probs.get(n, 0.0), 3), "fits": round(fits[n], 3)} for n in ordered],
            "ranking": ranking,
        }
        if not ranking:
            return Verdict("none", {**detail, "reason": "no candidate fits"})
        return Verdict("ranked", {**detail, "skill": ranking[0]["skill"]})

    return policy


def ranking_block(ranking: Sequence[Mapping[str, Any]]) -> str:
    """What the agent sees. Wording follows the cookbook's findings: always say
    something (an explicit "nothing applies" counters the index's own push to
    load a skill), and say it can be ignored (a forceful wrong suggestion does
    more damage than none)."""
    if not ranking:
        body = "No skill in the roster appears relevant to this request."
    else:
        lines = [f"{i}. {r['skill']}" for i, r in enumerate(ranking, 1)]
        body = (
            "Skills ranked by relevance to the current request (most relevant first):\n"
            + "\n".join(lines)
            + "\nLoad the first one that fits. Ignore this list if none match what the user actually asked for."
        )
    return f"<skill_relevance>\n{body}\n</skill_relevance>"


def suggestion_block(skill: Optional[str]) -> str:
    """Back-compat: a one-item ranking."""
    return ranking_block([{"skill": skill}] if skill else [])


def merge_rankings(chunk_answers: Sequence[Mapping[str, Any]], k: int) -> List[Tuple[str, float]]:
    """Merge per-chunk Choice distributions into one ranking by probability.

    Probabilities from different chunks are not strictly comparable; a chunk
    winner is still a reasonable shortlist candidate, which is all the rerank
    step needs (the cookbook's own suggestion for rosters over 255).
    """
    pool: List[Tuple[str, float]] = []
    for ans in chunk_answers:
        pool.extend(ans["which"].top(k))
    pool.sort(key=lambda kv: -kv[1])
    seen, out = set(), []
    for name, p in pool:
        if name not in seen:
            seen.add(name)
            out.append((name, p))
        if len(out) >= k:
            break
    return out
