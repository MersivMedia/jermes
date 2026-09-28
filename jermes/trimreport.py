"""Trim report (``hermes jermes trimreport``): did the agent need what was trimmed?

For every item context trimming dropped (enforce) or would have dropped
(shadow), look at the rest of that session in Hermes' state.db and check
whether the agent went back for it:

* **re-read**: ``read_file`` on the same path
* **searched**: ``search_files`` scoped to that path, or ``grep``/``rg``/``cat``
  /``head``/``tail``/``sed -n`` on it in a terminal call
* **re-ran**: the same terminal command (whitespace-normalised)
* **reloaded**: ``skill_view`` of the same skill
* **full copy opened**: a read of the saved full-text file

A "needed again" item is not automatically a mistake: in enforce mode the
agent recovering a line with a search is the design working (a few hundred
characters instead of re-sending the whole file on every call). What the
report separates is *how much* came back: a targeted search is cheap, a full
re-read of the same file means trimming it bought little.

Only items older than the protected window are ever trimmed, so the check
starts after the decision's timestamp.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SEARCH_CMDS = {"grep", "rg", "cat", "head", "tail", "sed", "awk", "less", "wc", "jq"}


@dataclass
class Item:
    session_id: str
    ts: float
    tool: str
    call: str          # "path=...", "command=...", "name=..." (from the trim log)
    chars: int
    shadow: bool
    outcome: str = "not needed"
    evidence: str = ""
    returned_chars: int = 0


def _parse_call(call: str) -> Tuple[str, str]:
    if "=" not in call:
        return "", call
    k, v = call.split("=", 1)
    return k.strip(), v.strip()


def _norm_cmd(c: str) -> str:
    return " ".join((c or "").split())


def _paths_in_command(cmd: str) -> List[str]:
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    out, prog = [], None
    for t in toks:
        if prog is None:
            prog = Path(t).name
            continue
        if t in ("|", "&&", ";", "||"):
            prog = None
            continue
        if prog in _SEARCH_CMDS and not t.startswith("-"):
            out.append(t)
    return out


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a, b = a.rstrip("/").removeprefix("./"), b.rstrip("/").removeprefix("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def later_calls(db: sqlite3.Connection, session_id: str, after_ts: float) -> List[Dict[str, Any]]:
    """Tool calls in this session after ``after_ts``, with their results' sizes."""
    rows = db.execute(
        "select role, tool_calls, tool_call_id, content, timestamp from messages "
        "where session_id=? and timestamp>? order by id", (session_id, after_ts)).fetchall()
    calls: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for role, tc, tcid, content, ts in rows:
        if role == "assistant" and tc:
            try:
                for x in json.loads(tc):
                    f = x.get("function") or {}
                    try:
                        args = json.loads(f.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    cid = x.get("id") or ""
                    calls[cid] = {"tool": f.get("name", ""), "args": args if isinstance(args, dict) else {},
                                  "ts": ts, "result_chars": 0}
                    order.append(cid)
            except ValueError:
                continue
        elif role == "tool" and tcid in calls:
            calls[tcid]["result_chars"] = len(content or "")
    return [calls[c] for c in order]


def classify(item: Item, later: Iterable[Dict[str, Any]], full_copy_dir: str = "full_results") -> Item:
    kind, value = _parse_call(item.call)
    for c in later:
        tool, a = c["tool"], c["args"]
        if tool == "read_file":
            p = str(a.get("path") or "")
            if full_copy_dir in p:
                item.outcome, item.evidence = "full copy opened", p
                item.returned_chars += c["result_chars"]
                return item
            if kind == "path" and _same_path(p, value):
                targeted = a.get("offset") is not None or a.get("limit") is not None
                item.outcome = "searched" if targeted else "re-read"
                item.evidence = f"read_file {p}" + (" (range)" if targeted else "")
                item.returned_chars += c["result_chars"]
                if not targeted:
                    return item
        elif tool == "search_files" and kind == "path":
            if _same_path(str(a.get("path") or ""), value):
                item.outcome, item.evidence = "searched", f"search_files {a.get('pattern', '')!r}"
                item.returned_chars += c["result_chars"]
        elif tool == "terminal":
            cmd = str(a.get("command") or "")
            if kind == "command" and _norm_cmd(cmd) == _norm_cmd(value):
                item.outcome, item.evidence = "re-ran", cmd[:80]
                item.returned_chars += c["result_chars"]
                return item
            if kind == "path" and any(_same_path(p, value) for p in _paths_in_command(cmd)):
                if item.outcome == "not needed":
                    item.outcome, item.evidence = "searched", cmd[:80]
                item.returned_chars += c["result_chars"]
        elif tool == "skill_view" and kind == "name" and str(a.get("name") or "") == value:
            item.outcome, item.evidence = "reloaded", f"skill_view {value}"
            item.returned_chars += c["result_chars"]
            return item
    return item


def load_items(decisions_db: Path, since: float = 0.0) -> List[Item]:
    if not decisions_db.exists():
        return []
    c = sqlite3.connect(f"file:{decisions_db}?mode=ro", uri=True)
    out = []
    for sid, ts, action, detail in c.execute(
            "select session_id, ts, action, detail_json from decisions where point='context_trim' "
            "and action in ('trim','would_trim') and ts>=? order by id", (since,)):
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            continue
        for it in d.get("items") or []:
            out.append(Item(session_id=sid or "", ts=ts, tool=it.get("tool", ""), call=it.get("call", ""),
                            chars=int(it.get("chars", 0)), shadow=(action == "would_trim")))
    return out


def dedupe(items: List[Item]) -> List[Item]:
    """Each cold turn re-decides every old item, so one item can be logged at
    several decisions. Keep its first appearance; later calls are checked from
    there, which covers everything after the later decisions too."""
    seen, out = set(), []
    for it in sorted(items, key=lambda i: i.ts):
        key = (it.session_id, it.tool, it.call)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def report(decisions_db: Path, state_db: Path, since: float = 0.0) -> Dict[str, Any]:
    items = dedupe(load_items(decisions_db, since))
    db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    cache: Dict[Tuple[str, float], List[Dict[str, Any]]] = {}
    for it in items:
        key = (it.session_id, it.ts)
        if key not in cache:
            cache[key] = later_calls(db, it.session_id, it.ts) if it.session_id else []
        classify(it, cache[key])
    by = Counter(i.outcome for i in items)
    dropped = sum(i.chars for i in items)
    back = sum(i.returned_chars for i in items)
    full_back = sum(i.chars for i in items if i.outcome in ("re-read", "re-ran", "reloaded", "full copy opened"))
    return {
        "items": len(items),
        "sessions": len({i.session_id for i in items}),
        "shadow_items": sum(i.shadow for i in items),
        "outcomes": dict(by),
        "chars_dropped": dropped,
        "chars_brought_back": back,
        "chars_of_items_fully_reloaded": full_back,
        "needed_rate": round(sum(1 for i in items if i.outcome != "not needed") / len(items), 3) if items else None,
        "detail": [i.__dict__ for i in items],
    }


def render(r: Dict[str, Any], top: int = 12) -> str:
    if not r["items"]:
        return ("No trimmed items logged yet. Trimming decisions appear after a pause of at least the cache "
                "lifetime in a session using the jermes context engine (`context: {engine: jermes}`).")
    lines = [f"{r['items']} trimmed item(s) in {r['sessions']} session(s) "
             f"({r['shadow_items']} shadow, {r['items'] - r['shadow_items']} enforced)"]
    for k in ("not needed", "searched", "re-read", "re-ran", "reloaded", "full copy opened"):
        if r["outcomes"].get(k):
            lines.append(f"  {k:<17} {r['outcomes'][k]}")
    lines.append(f"Needed again (any way): {r['needed_rate']:.0%}")
    lines.append(f"Characters dropped: {r['chars_dropped']:,}; brought back by later calls: "
                 f"{r['chars_brought_back']:,}; items fully re-loaded: {r['chars_of_items_fully_reloaded']:,}")
    needed = [d for d in r["detail"] if d["outcome"] != "not needed"]
    if needed:
        lines.append("\nItems the agent went back for:")
        for d in sorted(needed, key=lambda d: -d["chars"])[:top]:
            lines.append(f"  {d['outcome']:<12} {d['tool']} {d['call'][:60]}  ({d['chars']:,} chars) <- {d['evidence'][:60]}")
    lines.append("\nA search that recovers a few lines is trimming working as designed. Frequent full re-reads "
                 "or re-runs mean the threshold is too aggressive for how you work.")
    return "\n".join(lines)
