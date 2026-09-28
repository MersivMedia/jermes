"""Score the memory write filter offline (``hermes jermes memorybench``).

Reads a labelled JSON file of memory entries ({"entries": [{"text", "label",
"source"}]}, label keep|hold) and asks Jev the same questions the live filter
does. Labels file lives outside the repo (memory content is personal);
``--build`` drafts one from the current memory files and past memory writes
in state.db, for a human to review before scoring.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .points import memory_filter


def draft_labels(hermes_home: Path, state_db: Path) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for name, target in (("MEMORY.md", "memory"), ("USER.md", "user")):
        f = hermes_home / "memories" / name
        if f.exists():
            for i, e in enumerate(x for x in f.read_text().split("\n§\n") if x.strip()):
                entries.append({"id": f"{target[0].upper()}{i:02d}", "text": e, "target": target,
                                "source": "current", "label": "keep"})
    c = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    n = 0
    for (tc,) in c.execute("select tool_calls from messages where role='assistant' and tool_calls like '%\"memory\"%' "
                           "order by id"):
        for x in json.loads(tc):
            f = x.get("function") or {}
            if f.get("name") != "memory":
                continue
            try:
                args = json.loads(f.get("arguments") or "{}")
            except ValueError:
                continue
            for target, action, content in memory_filter.writes_in(args):
                entries.append({"id": f"W{n:02d}", "text": content, "target": target, "source": "past_write",
                                "label": "?"})
                n += 1
    return {"entries": entries}


def score(engine: Any, labels: Dict[str, Any], progress=print) -> Dict[str, Any]:
    cfg = engine.point_config(memory_filter.POINT)
    rows = []
    for e in labels["entries"]:
        if e.get("label") not in ("keep", "hold"):
            continue
        d = engine.decide(memory_filter.POINT, {"entry": e["text"][:4000], "target": e.get("target", "memory"),
                                                 "action": "add"},
                          memory_filter.questions(), memory_filter.make_policy(cfg, e["text"]),
                          spec_version=memory_filter.SPEC_VERSION)
        got = "error" if d.error else d.action
        rows.append({"id": e["id"], "source": e.get("source", ""), "label": e["label"], "got": got,
                     **{k: d.detail.get(k) for k in ("durable", "progress", "procedure", "reason")}})
        if progress:
            mark = "ok " if got == ("hold" if e["label"] == "hold" else "allow") else "   "
            progress(f"  {mark}{e['id']:<5} {e['label']:<5} -> {got:<5} {d.detail.get('reason') or '':<9} "
                     f"{' '.join(e['text'].split())[:70]}")
    ok = [r for r in rows if r["got"] != "error"]
    hold = [r for r in ok if r["label"] == "hold"]
    keep = [r for r in ok if r["label"] == "keep"]
    held_chars = sum(len(e["text"]) for e in labels["entries"]
                     for r in ok if r["id"] == e["id"] and r["got"] == "hold" and r["label"] == "hold")

    def rate(xs, pred):
        return round(sum(1 for x in xs if pred(x)) / len(xs), 3) if xs else None

    by_source = {}
    for s in sorted({r["source"] for r in ok}):
        sub = [r for r in ok if r["source"] == s]
        by_source[s] = {"n": len(sub), "correct": rate(sub, lambda r: (r["got"] == "hold") == (r["label"] == "hold"))}
    return {"n": len(rows), "errors": len(rows) - len(ok),
            "hold_caught": rate(hold, lambda r: r["got"] == "hold"),
            "keep_wrongly_held": rate(keep, lambda r: r["got"] == "hold"),
            "accuracy": rate(ok, lambda r: (r["got"] == "hold") == (r["label"] == "hold")),
            "chars_kept_out": held_chars, "by_source": by_source, "rows": rows}
