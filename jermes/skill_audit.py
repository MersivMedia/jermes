"""Duplicate-skill audit over the installed library (``hermes jermes skills-audit``).

Runs the skill_overlap shortlist + judge for each skill in scope, collects
pairs Jev rates as the same job, groups them, and suggests what to keep.
Suggestions only: nothing is edited, moved or archived.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .points import skill_overlap as so
from .points.skill_suggest import Skill


def audit(engine: Any, roster: Sequence[Skill], *, scope: str = "local", threshold: float = 0.7,
          top_k: int = 4, limit: int = 0, progress: Callable[[str], None] = print) -> Dict[str, Any]:
    sources = so.skill_sources(roster)
    sizes = {s.name: len(s.body or "") for s in roster}
    targets = [s for s in roster if scope == "all" or sources.get(s.name, "local") == "local"]
    if limit:
        targets = targets[:limit]
    pairs: Dict[frozenset, tuple] = {}
    checked: List[Dict[str, Any]] = []
    errors = 0
    t0 = time.monotonic()
    for i, target in enumerate(targets):
        matches, err = so.find_matches(engine, target, roster, top_k=top_k, sources=sources)
        if err:
            errors += 1
            progress(f"  [{i + 1}/{len(targets)}] {target.name}: error {err}")
            continue
        hits = [m for m in matches if m.overlaps and m.same_job >= threshold]
        checked.append({"skill": target.name, "matches": [asdict(m) for m in matches]})
        for m in hits:
            key = frozenset((target.name, m.name))
            prev = pairs.get(key)
            if prev is None or m.same_job > prev[3]:
                pairs[key] = (target.name, m.name, m.relation, m.same_job)
        if hits:
            progress(f"  [{i + 1}/{len(targets)}] {target.name}: " +
                     ", ".join(f"{m.name} ({m.relation}, {m.same_job:.0%})" for m in hits))
    by_name = {s.name: s for s in roster}
    groups = []
    for g in so.group_pairs(list(pairs.values())):
        contains = {n: 0 for n in g.members}
        for a, b, rel, _ in g.pairs:
            if rel == "target_contains":
                contains[b] += 1
            elif rel == "existing_contains":
                contains[a] += 1
        pool = [n for n in g.members if sources.get(n, "local") != "local"] or list(g.members)
        top = max(contains[n] for n in pool)
        tied = [n for n in pool if contains[n] == top]
        breadth = so.rank_breadth(engine, g, by_name) if len(tied) > 1 else {}
        g = so.suggest(g, sources, sizes, breadth)
        g.breadth = {k: round(v, 3) for k, v in breadth.items()}
        groups.append(g)
    return {
        "scope": scope, "threshold": threshold, "skills_checked": len(checked), "errors": errors,
        "seconds": round(time.monotonic() - t0, 1),
        "groups": [{"members": g.members, "keep": g.keep, "suggestion": g.suggestion,
                    "breadth": g.breadth,
                    "pairs": [{"a": a, "b": b, "relation": r, "same_job": round(p, 3)} for a, b, r, p in g.pairs],
                    "sources": {n: sources.get(n, "local") for n in g.members}} for g in groups],
        "checked": checked,
    }


def render(report: Dict[str, Any]) -> str:
    lines = [f"{report['skills_checked']} skills checked ({report['scope']}), "
             f"{len(report['groups'])} overlap group(s), {report['errors']} error(s), {report['seconds']}s"]
    for n, g in enumerate(report["groups"], 1):
        lines.append(f"\n{n}. {', '.join(g['members'])}")
        for p in g["pairs"]:
            lines.append(f"     {p['a']} / {p['b']}: {p['relation']}, same job {p['same_job']:.0%}")
        lines.append(f"   -> {g['suggestion']}")
    if not report["groups"]:
        lines.append("No overlapping skills above the threshold.")
    lines.append("\nSuggestions only. Nothing was changed. Review a group before merging: "
                 "`hermes skills` / skill_manage patch, then `hermes curator archive <name>`.")
    return "\n".join(lines)
