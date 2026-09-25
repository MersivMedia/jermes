"""D1: skill selection (PRD section 5.1).

Filters a roster of 100+ skills down to the few worth loading for one request.
Two Jev calls, adapted from TypeSafe's skill-suggestion cookbook (which
measured wrong skill loads falling 16.8% -> 7.3% on the Hermes roster):

  call 1 (skim):   Choice over every skill (name -> short description) plus an
                   explicit "no skill needed" option. If that option wins, stop.
  call 2 (select): Choice over the shortlist + "no skill needed", with full
                   descriptions and SKILL.md excerpts, plus one "would this
                   skill help" Noul per candidate.

Output is a *list*: the Choice winner first (the primary skill), then every
other candidate whose own Noul clears the threshold (supporting skills, e.g.
``research-design-documents`` + ``grounded-citations``). The Choice is relative
("which one most"), the Nouls are absolute ("does this one help at all"), so a
request can get several skills, one, or none.

State carries the latest request plus a short window of recent conversation,
so follow-ups like "yes do the invert test next" can be resolved. The request
is always primary; the context is labelled as reference only.

The block is injected into the *user message* via ``pre_llm_call`` (Hermes'
cache-safe injection point), never the system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..engine import Verdict
from ..questions import MAX_CHOICE_OPTIONS, Choice, Noul

SPEC_VERSION = "skill_suggest.3"
SKIM_DESC_CHARS = 200  # keep call 1 well inside Jev's 32k state+question budget

NONE_OPTION = "(no skill needed)"
NONE_CRITERIA = (
    "None of the listed skills is needed. Choose this for conversation, opinions, "
    "status or progress questions, short acknowledgements, follow-ups the assistant "
    "can handle with what it already has, or requests no listed skill covers."
)
CHUNK_SIZE = MAX_CHOICE_OPTIONS - 1  # one slot per chunk is the none option

_CONTEXT_NOTE = (
    "Judge the user's latest `request`. `recent_context` is the earlier conversation; "
    "use it only to understand what the request refers to, not as a request of its own."
)
_SYNTHETIC = re.compile(
    r"^\s*\[(IMPORTANT|ASYNC DELEGAT|System note|CONTEXT COMPA|The user sent|SYSTEM|Background process)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str = ""
    category: str = ""


def current_roster() -> List[Skill]:
    """The skills the agent can load *right now*, read on every call.

    Uses Hermes' own discovery (``tools.skills_tool._find_all_skills``), the
    same function behind the agent's ``skills_list``, so Jermes sees exactly
    what the agent sees: disabled skills, platform/environment gating and
    project-skill trust are all applied. Hermes caches that scan keyed by a
    signature of the skill directories' mtimes, so a skill created mid-session
    shows up on the next call, and repeated calls cost almost nothing.

    Bodies are not loaded here: only the shortlist needs them (``skill_body``).
    Falls back to a direct scan when Hermes' helper is unavailable.
    """
    try:
        from tools.skills_tool import _find_all_skills  # type: ignore
    except Exception:
        return load_roster()
    try:
        found = _find_all_skills()
    except Exception:
        return load_roster()
    out = {}
    for s in found:
        name = str(s.get("name") or "").strip()
        if name and name != NONE_OPTION and name not in out:
            out[name] = Skill(name=name, description=str(s.get("description") or "").strip(),
                              category=str(s.get("category") or ""))
    return sorted(out.values(), key=lambda s: s.name)


_BODY_PATHS: Dict[str, Path] = {}


def _index_skill_paths() -> None:
    try:
        from agent.skill_utils import get_all_skills_dirs, iter_skill_index_files, parse_frontmatter  # type: ignore
    except Exception:
        return
    found: Dict[str, Path] = {}
    for root in get_all_skills_dirs():
        root = Path(root)
        if not root.exists():
            continue
        for path in iter_skill_index_files(root, "SKILL.md"):
            try:
                meta, _ = parse_frontmatter(Path(path).read_text(encoding="utf-8", errors="replace")[:4000])
            except Exception:
                continue
            name = str(meta.get("name") or Path(path).parent.name).strip()
            found.setdefault(name, Path(path))
    _BODY_PATHS.clear()
    _BODY_PATHS.update(found)


def skill_body(skill: Skill) -> str:
    """SKILL.md body for one skill, read fresh (so edits are seen). The
    name -> path index is rebuilt only when a name is unknown or its file moved."""
    if skill.body:
        return skill.body
    path = _BODY_PATHS.get(skill.name)
    if path is None or not path.exists():
        _index_skill_paths()
        path = _BODY_PATHS.get(skill.name)
    if path is None:
        return ""
    try:
        from agent.skill_utils import parse_frontmatter  # type: ignore

        _, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        return body.strip()
    except Exception:
        return ""


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
            if not name or name in seen or name == NONE_OPTION:
                continue
            seen[name] = Skill(
                name=name,
                description=str(meta.get("description") or "").strip(),
                body=body.strip(),
                category=Path(path).parent.parent.name,
            )
    return sorted(seen.values(), key=lambda s: s.name)


# ---------------------------------------------------------------- context


def format_context(messages: Sequence[Mapping[str, Any]], *, max_messages: int = 4, chars_each: int = 400) -> str:
    """Last few user/assistant text turns, oldest first, each trimmed.

    Tool calls, tool results and harness-injected notes are skipped: they are
    bulky, and the request's meaning lives in what the people said.
    """
    if max_messages <= 0:
        return ""
    picked: List[str] = []
    for m in reversed(list(messages)):
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if not text or _SYNTHETIC.match(text):
            continue
        if len(text) > chars_each:
            text = text[: chars_each - 3] + "..."
        picked.append(f"{role}: {text}")
        if len(picked) >= max_messages:
            break
    return "\n".join(reversed(picked))


def history_before_request(history: Sequence[Mapping[str, Any]], request: str) -> List[Mapping[str, Any]]:
    """Hermes' ``conversation_history`` may already end with the current user
    message; drop it so the request is not repeated as its own context."""
    msgs = list(history or [])
    if msgs and msgs[-1].get("role") == "user":
        last = msgs[-1].get("content")
        if isinstance(last, str) and last.strip() == (request or "").strip():
            msgs = msgs[:-1]
    return msgs


def state_for(user_message: str, recent_context: str = "", *, max_context_chars: int = 20000) -> Dict[str, Any]:
    # The context is already sized by format_context (messages x chars each);
    # this cap is only a backstop well inside Jev's 32k-token state budget.
    state: Dict[str, Any] = {"request": user_message[:6000]}
    if recent_context:
        state["recent_context"] = recent_context[:max_context_chars]
    return state


# ---------------------------------------------------------------- questions


def skim_questions(roster: Sequence[Skill], *, with_context: bool = False) -> List[Dict[str, Any]]:
    """One Choice per chunk of <=254 skills, each with the none option."""
    out: List[Dict[str, Any]] = []
    note = f" {_CONTEXT_NOTE}" if with_context else ""
    for start in range(0, len(roster), CHUNK_SIZE):
        chunk = roster[start : start + CHUNK_SIZE]
        criteria: Dict[str, Optional[str]] = {
            s.name: ((s.description or "")[:SKIM_DESC_CHARS] or None) for s in chunk
        }
        criteria[NONE_OPTION] = NONE_CRITERIA
        out.append({
            "which": Choice(
                instructions=(
                    "Which of these skills is most needed to carry out the user's latest `request`? "
                    f"If no skill is needed, choose '{NONE_OPTION}'.{note}"
                ),
                criteria=criteria,
            )
        })
    return out


def skim_verdict(chunk_answers: Sequence[Mapping[str, Any]], k: int) -> Tuple[bool, List[Tuple[str, float]], float]:
    """(none_wins, shortlist, max P(none)).

    "No skill" wins only if it is the top option in every chunk, so a skill
    that clearly wins its own chunk is never hidden by another chunk's "none".
    """
    none_wins = bool(chunk_answers) and all(a["which"].choice == NONE_OPTION for a in chunk_answers)
    p_none = max((a["which"].probabilities.get(NONE_OPTION, 0.0) for a in chunk_answers), default=0.0)
    return none_wins, merge_rankings(chunk_answers, k), p_none


def select_questions(names: Sequence[str], by_name: Mapping[str, Skill], excerpt_chars: int,
                     *, with_context: bool = False) -> Dict[str, Any]:
    note = f" {_CONTEXT_NOTE}" if with_context else ""
    criteria: Dict[str, Optional[str]] = {
        n: f"{by_name[n].description} — {skill_body(by_name[n])[:excerpt_chars]}" for n in names
    }
    criteria[NONE_OPTION] = NONE_CRITERIA
    qs: Dict[str, Any] = {
        "which": Choice(
            instructions=(
                "Which one of these skills is most needed for the user's latest `request`? Read what each "
                f"actually does, not just its name. If none is needed, choose '{NONE_OPTION}'.{note}"
            ),
            criteria=criteria,
        )
    }
    for n in names:
        qs[f"fits::{n}"] = Noul(
            instructions=(
                f"Would loading the skill '{n}' help carry out the user's latest `request`, "
                f"either all of it or a distinct part of it? It is described as: {by_name[n].description}"
            )
        )
    return qs


# Back-compat name used by earlier callers.
rerank_questions = select_questions


def make_select_policy(fits_threshold: float, names: Sequence[str], max_listed: int = 4):
    """Primary = the Choice winner (if it clears its own fit check);
    supporting = other candidates whose Noul clears the threshold, by fit."""

    def policy(a: Dict[str, Any]) -> Verdict:
        which = a["which"]
        probs = which.probabilities
        fits = {n: a[f"fits::{n}"].noul for n in names}

        def row(n: str) -> Dict[str, Any]:
            return {"skill": n, "p": round(probs.get(n, 0.0), 3), "fits": round(fits[n], 3)}

        detail: Dict[str, Any] = {
            "candidates": [row(n) for n in sorted(names, key=lambda n: -fits[n])],
            "p_none": round(probs.get(NONE_OPTION, 0.0), 3),
            "choice": which.choice,
        }
        if which.choice == NONE_OPTION:
            return Verdict("none", {**detail, "ranking": [], "reason": "jev chose no skill"})
        listed = [n for n in sorted(names, key=lambda n: (-fits[n], -probs.get(n, 0.0))) if fits[n] >= fits_threshold]
        if which.choice in listed:  # primary first, supporting skills after
            listed.remove(which.choice)
            listed.insert(0, which.choice)
        ranking = [row(n) for n in listed[:max_listed]]
        detail["ranking"] = ranking
        if not ranking:
            return Verdict("none", {**detail, "reason": "no candidate passed its fit check"})
        return Verdict("ranked", {**detail, "skill": ranking[0]["skill"]})

    return policy


def make_rerank_policy(fits_threshold: float, names: Sequence[str], max_ranked: int = 3):
    """Back-compat alias."""
    return make_select_policy(fits_threshold, names, max_ranked)


# ---------------------------------------------------------------- output


def ranking_block(ranking: Sequence[Mapping[str, Any]]) -> str:
    """What the agent sees. Wording follows the cookbook's findings: always say
    something (an explicit "nothing applies" counters the skill index's own push
    to load a skill), and say it can be ignored (a forceful wrong suggestion
    does more damage than none)."""
    if not ranking:
        body = "No skill is needed for this request. Answer directly unless it clearly calls for one."
    elif len(ranking) == 1:
        body = (
            f"Relevant skill for this request: {ranking[0]['skill']}. "
            "Ignore it if it does not fit what the user actually asked for."
        )
    else:
        lines = [f"{i}. {r['skill']}" for i, r in enumerate(ranking, 1)]
        body = (
            "Relevant skills for this request (primary first, then supporting):\n"
            + "\n".join(lines)
            + "\nLoad the ones that apply. Ignore any that do not match what the user actually asked for."
        )
    return f"<skill_relevance>\n{body}\n</skill_relevance>"


def suggestion_block(skill: Optional[str]) -> str:
    """Back-compat: a one-item list."""
    return ranking_block([{"skill": skill}] if skill else [])


def merge_rankings(chunk_answers: Sequence[Mapping[str, Any]], k: int) -> List[Tuple[str, float]]:
    """Merge per-chunk Choice distributions into one shortlist (none excluded).

    Probabilities from different chunks are not strictly comparable; a chunk
    winner is still a reasonable shortlist candidate, which is all the select
    step needs (the cookbook's own suggestion for rosters over 255).
    """
    pool: List[Tuple[str, float]] = []
    for ans in chunk_answers:
        pool.extend((n, p) for n, p in ans["which"].top(k + 1) if n != NONE_OPTION)
    pool.sort(key=lambda kv: -kv[1])
    seen, out = set(), []
    for name, p in pool:
        if name not in seen:
            seen.add(name)
            out.append((name, p))
        if len(out) >= k:
            break
    return out
