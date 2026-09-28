"""Memory write filter (PRD X4): keep task progress out of persistent memory.

Hermes re-injects its memory files into every turn, so each entry costs
tokens for the life of the install. Its own guidance says memory is for
durable facts (preferences, corrections, environment details, stable
conventions), and that task progress, completed-work logs, commit ids and
"phase N done" notes don't belong there; procedures belong in skills.

This point asks Jev those questions about each ``memory`` add/replace before
it's written (``pre_tool_call``):

* ``durable``: still true and useful a month from now?
* ``progress``: mainly task progress or a record of finished work, likely
  stale within a week?
* ``procedure``: mainly a multi-step how-to that belongs in a skill?

advise/enforce: a write judged progress (or a long procedure) is held once
with a note saying why; the agent can rephrase it as a durable fact, move it
to a skill, or retry the same write, which then goes through. Removes are
never checked. Shadow (default) only logs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..engine import Verdict
from ..questions import Noul

SPEC_VERSION = "memory_filter.1"
POINT = "memory_filter"

Q_DURABLE = Noul(
    "Will `entry` still be true and useful to an assistant a month from now? Durable means a user "
    "preference or correction, a personal detail, an environment or account fact, or a stable convention.")
Q_PROGRESS = Noul(
    "Is `entry` mainly task progress or a record of finished work, the kind of note that is stale within a "
    "week? Examples: something was fixed, created, pushed, deployed or completed; commit or PR numbers; "
    "counts of items done; a current phase or status.")
Q_PROCEDURE = Noul(
    "Is `entry` mainly a multi-step procedure, how-to or code pattern (the kind of content that belongs in "
    "a reusable skill document), rather than a short fact or preference?")


def writes_in(args: Mapping[str, Any]) -> List[Tuple[str, str, str]]:
    """(target, action, content) for every add/replace in a memory tool call."""
    if not isinstance(args, Mapping):
        return []
    target = str(args.get("target") or "memory")
    ops = args.get("operations") or [args]
    out = []
    for o in ops if isinstance(ops, list) else []:
        if not isinstance(o, Mapping):
            continue
        action = str(o.get("action") or "")
        content = o.get("content") or o.get("new_text")
        if action in ("add", "replace") and isinstance(content, str) and content.strip():
            out.append((str(o.get("target") or target), action, content))
    return out


def questions() -> Dict[str, Noul]:
    return {"durable": Q_DURABLE, "progress": Q_PROGRESS, "procedure": Q_PROCEDURE}


def looks_structured(content: str) -> bool:
    """A markdown heading, or three or more numbered/bulleted steps: how a how-to is written.

    Dense fact entries (tool quirks, preference lists) are prose and score
    high on "procedure" too, so Jev's answer alone over-fires; the shape
    check keeps the hold for documents written as steps.
    """
    import re

    if re.search(r"(^|\n)\s*#{1,4}\s+\S", content):
        return True
    steps = re.findall(r"(?:^|\n|\s)(?:\d{1,2}[.)]|[-*•])\s+\*{0,2}\S", content)
    return len(steps) >= 3


def make_policy(cfg: Mapping[str, Any], content: str):
    prog_t = float(cfg.get("progress_threshold", 0.7))
    proc_t = float(cfg.get("procedure_threshold", 0.9))

    def policy(a: Dict[str, Any]) -> Verdict:
        d, p, s = (getattr(a[k], "noul", 0.0) for k in ("durable", "progress", "procedure"))
        shaped = looks_structured(content)
        detail = {"durable": round(d, 3), "progress": round(p, 3), "procedure": round(s, 3), "chars": len(content),
                  "structured": shaped}
        if p >= prog_t and d < 0.5:
            return Verdict("hold", {**detail, "reason": "progress"})
        if s >= proc_t and shaped:
            return Verdict("hold", {**detail, "reason": "procedure"})
        return Verdict("allow", detail)

    return policy


def note(held: List[Tuple[str, str, Dict[str, Any]]]) -> str:
    lines = ["[jermes: memory not written yet. Memory is re-sent on every turn, so it should hold durable facts:"]
    for target, content, det in held:
        why = ("reads as task progress or a record of finished work, likely stale within a week"
               if det.get("reason") == "progress" else
               "reads as a multi-step procedure, which fits a skill better than memory")
        lines.append(f"  - ({target}) \"{' '.join(content.split())[:90]}\": {why}")
    lines.append("Options: rewrite it as the durable fact or preference it implies, put a procedure in a skill "
                 "(skill_manage), or leave it out. If it really should be remembered as written, retry the "
                 "same memory call and it will be saved.]")
    return "\n".join(lines)
