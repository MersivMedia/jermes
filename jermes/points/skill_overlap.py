"""Skill overlap: duplicate audit and a check before new skills are created.

Two uses, one set of questions:

* **Audit** (``hermes jermes skills-audit``): finds groups of installed skills
  that overlap and suggests how to merge each group. Suggestions only; the
  command never edits, moves or archives a skill.
* **Create check** (``skill_overlap`` point, ``pre_tool_call`` on
  ``skill_manage`` with ``action: create``): before a new skill is written,
  asks whether an existing skill already covers it. If one does, the agent is
  told which, and why, and asked to either extend that skill or confirm the
  new one is distinct. In ``advise`` mode that note is shown once per
  proposed skill name; retrying the same create goes through.

How candidates are found (Jev never reads the whole library at once):

1. **Shortlist.** One Choice over every installed skill (name + short
   description, 254 per chunk, the same skim skill selection uses), asking
   which covers the same job as the target, plus a "none" option. The top few
   by probability go on.
2. **Judge.** For each shortlisted skill, with full descriptions and SKILL.md
   excerpts side by side: "does this cover the same job?", "would one skill
   serve both without losing anything?", and a Choice of how they relate
   (duplicate / one contains the other / partial overlap / related but
   distinct / unrelated).

Bundled and hub-installed skills are never proposed as merge *targets to
rewrite*: they can absorb nothing (they're overwritten on update). A
duplicate of one is reported as "retire the local copy".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..engine import Verdict
from ..questions import MAX_CHOICE_OPTIONS, Choice, Noul
from .skill_suggest import Skill

SPEC_VERSION = "skill_overlap.1"
POINT = "skill_overlap"
NONE_OPTION = "(no overlapping skill)"
CHUNK = MAX_CHOICE_OPTIONS - 1
SKIM_DESC = 200
EXCERPT = 1500

RELATIONS = {
    "duplicate": "They do the same job. Either one could replace the other.",
    "target_contains": "The target's job is fully covered by the existing skill, which also does more.",
    "existing_contains": "The existing skill's job is fully covered by the target, which also does more.",
    "partial": "They share a significant part of their job, but each also does something the other doesn't.",
    "related": "Same area or often used together, but different jobs.",
    "unrelated": "Different jobs.",
}
MERGE_RELATIONS = {"duplicate", "target_contains", "existing_contains"}


@dataclass
class Match:
    name: str
    relation: str
    same_job: float
    one_serves_both: float
    shortlist_p: float
    source: str = "local"      # local | bundled | hub | external

    @property
    def overlaps(self) -> bool:
        return self.relation in MERGE_RELATIONS or (self.relation == "partial" and self.one_serves_both >= 0.5)


def card(s: Skill, excerpt: int = EXCERPT) -> Dict[str, Any]:
    body = re.sub(r"\n{3,}", "\n\n", s.body or "").strip()
    return {"name": s.name, "category": s.category, "description": s.description,
            "excerpt": body[:excerpt]}


# ---------------------------------------------------------------- questions

def shortlist_questions(target: Skill, roster: Sequence[Skill]) -> List[Dict[str, Any]]:
    out = []
    pool = [s for s in roster if s.name != target.name]
    for start in range(0, len(pool), CHUNK):
        chunk = pool[start:start + CHUNK]
        crit: Dict[str, Optional[str]] = {s.name: (s.description or "")[:SKIM_DESC] or None for s in chunk}
        crit[NONE_OPTION] = "No listed skill covers the same job as `target`; at most they're in the same area."
        out.append({"which": Choice(
            instructions=("Which listed skill covers the same job as `target` (the same kind of request, "
                          "producing the same kind of result)? Being in the same broad area, or using the "
                          f"same tools, is not enough. If none does, choose '{NONE_OPTION}'."),
            criteria=crit)})
    return out


def judge_questions(n: int) -> Dict[str, Any]:
    if n < 1:
        raise ValueError("nothing to judge")
    qs: Dict[str, Any] = {}
    for i in range(n):
        qs[f"same_{i}"] = Noul(
            f"Do `target` and `existing.e{i}` cover the same job: a user asking for one would be equally "
            "well served by the other? Shared tools, shared domain, or one being a step inside the other "
            "does not make them the same job.")
        qs[f"one_{i}"] = Noul(
            f"Could `target` and `existing.e{i}` be combined into one skill with a clear, single-sentence "
            "description, without losing anything either one does, and without the combined skill "
            "becoming a grab-bag of unrelated procedures?")
        qs[f"rel_{i}"] = Choice(
            instructions=f"How does `target` relate to `existing.e{i}`?",
            criteria={k: v.replace("The target", "`target`").replace("the target", "`target`")
                      .replace("the existing skill", f"`existing.e{i}`")
                      .replace("The existing skill", f"`existing.e{i}`")
                      for k, v in RELATIONS.items()})
    return qs


# ---------------------------------------------------------------- engine calls

def find_matches(engine: Any, target: Skill, roster: Sequence[Skill], *, top_k: int = 4,
                 min_shortlist_p: float = 0.08, sources: Optional[Mapping[str, str]] = None,
                 deadline_s: Optional[float] = None, session_id: str = "",
                 exclude: Iterable[str] = ()) -> Tuple[List[Match], Optional[str]]:
    """Shortlist then judge. Returns (matches, error)."""
    excluded = set(exclude) | {target.name}
    pool = [s for s in roster if s.name not in excluded]
    if not pool:
        return [], None
    by_name = {s.name: s for s in pool}
    tcard = card(target)
    tstate = {"target": {k: v for k, v in tcard.items() if k != "excerpt"} | {"excerpt": tcard["excerpt"][:600]}}
    probs: Dict[str, float] = {}
    for qs in shortlist_questions(target, pool):
        d = engine.decide(f"{POINT}.shortlist", tstate, qs, lambda a: Verdict("scored"),
                          spec_version=SPEC_VERSION, session_id=session_id, deadline_s=deadline_s)
        if d.error:
            return [], d.error
        for name, p in (d.answers["which"].probabilities or {}).items():
            if name != NONE_OPTION:
                probs[name] = max(probs.get(name, 0.0), float(p))
    short = [n for n, p in sorted(probs.items(), key=lambda kv: -kv[1]) if p >= min_shortlist_p][:top_k]
    if not short:
        return [], None
    state = {"target": tcard, "existing": {f"e{i}": card(by_name[n]) for i, n in enumerate(short)}}
    d = engine.decide(f"{POINT}.judge", state, judge_questions(len(short)), lambda a: Verdict("scored"),
                      spec_version=SPEC_VERSION, session_id=session_id, deadline_s=deadline_s)
    if d.error:
        return [], d.error
    out = []
    for i, n in enumerate(short):
        out.append(Match(
            name=n,
            relation=d.answers[f"rel_{i}"].choice,
            same_job=float(getattr(d.answers[f"same_{i}"], "probability", 0.0)),
            one_serves_both=float(getattr(d.answers[f"one_{i}"], "probability", 0.0)),
            shortlist_p=probs[n],
            source=(sources or {}).get(n, "local"),
        ))
    out.sort(key=lambda m: (-(m.relation in MERGE_RELATIONS), -m.same_job))
    return out, None


# ---------------------------------------------------------------- create check

def target_from_create(args: Mapping[str, Any]) -> Optional[Skill]:
    if not isinstance(args, Mapping) or args.get("action") != "create":
        return None
    name = str(args.get("name") or "").strip()
    content = str(args.get("content") or "")
    if not name or not content:
        return None
    desc, body = "", content
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.S)
    if m:
        front, body = m.group(1), m.group(2)
        dm = re.search(r"^description:\s*(.+)$", front, re.M)
        if dm:
            desc = dm.group(1).strip().strip("'\"")
    return Skill(name=name, description=desc, body=body.strip(), category=str(args.get("category") or ""))


def create_note(target: Skill, matches: Sequence[Match], threshold: float) -> Optional[str]:
    hits = [m for m in matches if m.overlaps and m.same_job >= threshold]
    if not hits:
        return None
    lines = [f"[jermes: before creating skill '{target.name}', check these existing skills. They look like "
             "they cover the same job:"]
    for m in hits[:3]:
        how = {"duplicate": "does the same job",
               "target_contains": "already covers everything the new skill does, and more",
               "existing_contains": "is a narrower version of the new skill",
               "partial": "overlaps substantially"}.get(m.relation, m.relation)
        extra = "" if m.source == "local" else f" ({m.source} skill: it can't be edited in place, so a local extension is fine)"
        lines.append(f"  - {m.name}: {how} (same job {m.same_job:.0%}){extra}")
    first = hits[0]
    lines.append(
        f"Prefer extending '{first.name}' (skill_manage patch, or write_file for a references/ file) if the new "
        "procedure fits under its description. Create the new skill only if it serves a genuinely different "
        "request; if so, say in one line why and retry the same create. If the user asked for a new skill, "
        "tell them about the overlap and let them choose.]")
    return "\n".join(lines)


# ---------------------------------------------------------------- audit

@dataclass
class Group:
    members: List[str]
    pairs: List[Tuple[str, str, str, float]] = field(default_factory=list)  # a, b, relation, same_job
    suggestion: str = ""
    keep: str = ""


def group_pairs(pairs: Sequence[Tuple[str, str, str, float]]) -> List[Group]:
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _, _ in pairs:
        parent[find(a)] = find(b)
    groups: Dict[str, Group] = {}
    for a, b, rel, p in pairs:
        g = groups.setdefault(find(a), Group(members=[]))
        for n in (a, b):
            if n not in g.members:
                g.members.append(n)
        g.pairs.append((a, b, rel, p))
    return sorted(groups.values(), key=lambda g: (-len(g.members), -max(p for *_, p in g.pairs)))


def suggest(group: Group, sources: Mapping[str, str], sizes: Mapping[str, int]) -> Group:
    """Pick which skill to keep and phrase the suggestion. Pure code, no model call."""
    local = [n for n in group.members if sources.get(n, "local") == "local"]
    fixed = [n for n in group.members if sources.get(n, "local") != "local"]
    contains: Dict[str, int] = {n: 0 for n in group.members}
    for a, b, rel, _ in group.pairs:
        if rel == "target_contains":
            contains[b] += 1
        elif rel == "existing_contains":
            contains[a] += 1
    if fixed:
        keep = max(fixed, key=lambda n: (contains[n], sizes.get(n, 0)))
        retire = [n for n in local]
        group.keep = keep
        group.suggestion = (f"Keep {keep} ({sources.get(keep)} skill). Move anything unique from "
                            f"{', '.join(retire) or '-'} into a local skill or references file, then archive "
                            f"{'them' if len(retire) > 1 else 'it'}." if retire else
                            f"All are {sources.get(keep)} skills; disable the ones you don't use.")
        return group
    keep = max(local, key=lambda n: (contains[n], sizes.get(n, 0)))
    others = [n for n in group.members if n != keep]
    group.keep = keep
    group.suggestion = (f"Merge into {keep}: fold the unique parts of {', '.join(others)} into it "
                        "(as sections or references/ files), widen its description to cover both, "
                        f"then archive {'them' if len(others) > 1 else 'it'}.")
    return group


def skill_sources(roster: Sequence[Skill]) -> Dict[str, str]:
    """local | bundled | hub | external, from Hermes' public provenance helpers.

    Unknown provenance (helpers missing) counts as local, which only affects
    wording: nothing here edits skills.
    """
    try:
        from tools.skill_usage import is_agent_created, is_bundled, is_hub_installed  # type: ignore
    except Exception:
        return {s.name: "local" for s in roster}
    out: Dict[str, str] = {}
    for s in roster:
        try:
            if is_hub_installed(s.name):
                out[s.name] = "hub"
            elif is_bundled(s.name):
                out[s.name] = "bundled"
            elif not is_agent_created(s.name):
                out[s.name] = "external"
            else:
                out[s.name] = "local"
        except Exception:
            out[s.name] = "local"
    return out
