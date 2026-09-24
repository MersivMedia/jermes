"""Offline shadow replay over real Hermes sessions.

The fastest way to learn what Jermes *would* have done: read past user turns
from Hermes' ``state.db`` (read-only), run every enabled decision point over
them, and report. Nothing touches a live agent.

For skill ranking there is a ground truth of sorts: the skill the agent
actually loaded with ``skill_view`` in that turn. The report scores Jev's
ranking against it (top-1 / top-3 agreement, and how often Jev says "no skill"
on turns where the agent loaded none).

Caveat baked into the report: "what the agent loaded" is a weak label. The
agent itself loads the wrong skill part of the time (TypeSafe measured 16.8%
on the Hermes roster), so disagreements need a human look, not an automatic
verdict. ``--export`` writes every disagreement to JSONL for labelling.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .harness import Harness
from .points import risk_gate
from .store import hermes_home

# Harness-generated "user" messages that are not real requests.
_SYNTHETIC = re.compile(
    r"^\s*\[(IMPORTANT|ASYNC DELEGAT|System note|CONTEXT COMPA|The user sent|SYSTEM|Background process)",
    re.IGNORECASE,
)
_SKILL_VIEW_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')


@dataclass
class Turn:
    session_id: str
    message_id: int
    request: str
    loaded_skills: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


def default_db() -> Path:
    return hermes_home() / "state.db"


def iter_turns(db: Path, *, limit: int = 50, min_chars: int = 15, since_days: Optional[float] = None,
               only_with_skill: bool = False) -> Iterator[Turn]:
    """Yield real user turns with the skills and tool calls that followed them.

    Opened read-only (``mode=ro``) so a running Hermes is never disturbed.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    try:
        where = "role='user' AND content IS NOT NULL AND length(content) >= ?"
        args: List[Any] = [min_chars]
        if since_days:
            where += " AND timestamp >= ?"
            args.append(time.time() - since_days * 86400)
        users = conn.execute(
            f"SELECT id, session_id, content FROM messages WHERE {where} ORDER BY id DESC", args
        ).fetchall()
        yielded = 0
        for mid, sid, content in users:
            if _SYNTHETIC.match(content or ""):
                continue
            # Everything up to the next user message in the same session is this turn.
            nxt = conn.execute(
                "SELECT MIN(id) FROM messages WHERE session_id=? AND role='user' AND id>?", (sid, mid)
            ).fetchone()[0]
            q = "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' AND id>? AND tool_calls IS NOT NULL"
            qa: List[Any] = [sid, mid]
            if nxt:
                q += " AND id<?"
                qa.append(nxt)
            loaded: List[str] = []
            calls: List[Dict[str, Any]] = []
            for (raw,) in conn.execute(q + " ORDER BY id", qa):
                try:
                    items = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for it in items or []:
                    fn = (it or {}).get("function") or {}
                    name, argstr = fn.get("name"), fn.get("arguments") or ""
                    try:
                        cargs = json.loads(argstr) if isinstance(argstr, str) else dict(argstr)
                    except (TypeError, ValueError):
                        cargs = {}
                    calls.append({"tool": name, "args": cargs})
                    if name == "skill_view":
                        skill = cargs.get("name") if isinstance(cargs, dict) else None
                        if not skill:
                            m = _SKILL_VIEW_NAME.search(argstr)
                            skill = m.group(1) if m else None
                        if skill and not (isinstance(cargs, dict) and cargs.get("file_path")):
                            loaded.append(str(skill))
            if only_with_skill and not loaded:
                continue
            yield Turn(sid, mid, content, loaded, calls)
            yielded += 1
            if yielded >= limit:
                return
    finally:
        conn.close()


def _norm(name: str) -> str:
    # skill_view accepts "category/name" and "plugin:name"; compare on the leaf.
    return name.split(":")[-1].split("/")[-1].strip().lower()


def replay_skills(harness: Harness, turns: List[Turn], *, progress=print) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for i, t in enumerate(turns, 1):
        ranking = harness.rank_skills(f"replay:{t.session_id}", t.request)
        ranked = [r["skill"] for r in (ranking or [])]
        loaded = [s for s in dict.fromkeys(t.loaded_skills)]
        first = _norm(loaded[0]) if loaded else None
        rows.append({
            "session_id": t.session_id,
            "message_id": t.message_id,
            "request": t.request[:500],
            "agent_loaded": loaded,
            "jev_ranking": ranking,
            "error": ranking is None,
            "top1": bool(first and ranked and _norm(ranked[0]) == first),
            "top3": bool(first and any(_norm(r) == first for r in ranked[:3])),
        })
        if progress:
            mark = "ERR" if ranking is None else ("=" if rows[-1]["top1"] else ("~" if rows[-1]["top3"] else "x" if first else "."))
            progress(f"  [{i:>3}/{len(turns)}] {mark}  jev={ranked[:3]}  agent={loaded[:2]}  | {t.request[:70]!r}")
    return summarize_skills(rows)


def summarize_skills(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in rows if not r["error"]]
    with_skill = [r for r in ok if r["agent_loaded"]]
    without = [r for r in ok if not r["agent_loaded"]]
    def pct(n: int, d: int) -> Optional[float]:
        return round(100.0 * n / d, 1) if d else None
    return {
        "turns": len(rows),
        "errors": len(rows) - len(ok),
        "agent_loaded_a_skill": len(with_skill),
        "top1_agreement_pct": pct(sum(r["top1"] for r in with_skill), len(with_skill)),
        "top3_agreement_pct": pct(sum(r["top3"] for r in with_skill), len(with_skill)),
        "jev_said_none_when_agent_loaded_nothing_pct": pct(sum(not r["jev_ranking"] for r in without), len(without)),
        "jev_ranked_when_agent_loaded_nothing": sum(bool(r["jev_ranking"]) for r in without),
        "rows": rows,
    }


def replay_risk(harness: Harness, turns: List[Turn], *, max_calls: int = 200, progress=print) -> Dict[str, Any]:
    """Run the risk gate over the gated tool calls that actually happened."""
    cfg = harness.engine.point_config("risk_gate")
    gated = set(cfg.get("gated_tools") or [])
    counts: Dict[str, int] = {}
    flagged: List[Dict[str, Any]] = []
    n = 0
    for t in turns:
        for c in t.tool_calls:
            if c["tool"] not in gated or n >= max_calls:
                continue
            n += 1
            state = risk_gate.build_state(c["tool"], c["args"] if isinstance(c["args"], dict) else {}, t.request,
                                          max_arg_chars=int(cfg.get("max_arg_chars", 4000)))
            d = harness.engine.decide("risk_gate", state, risk_gate.questions(False), risk_gate.make_policy(cfg),
                                      session_id=f"replay:{t.session_id}", spec_version=risk_gate.SPEC_VERSION,
                                      log_detail={"tool": c["tool"], "replay": True})
            action = d.action or "error"
            counts[action] = counts.get(action, 0) + 1
            if action in ("block", "review"):
                flagged.append({"action": action, "tool": c["tool"], "reason": d.detail.get("reason"),
                                "args": state["arguments"][:300], "request": t.request[:200]})
    if progress:
        progress(f"  risk_gate over {n} real tool calls: {counts}")
    return {"calls": n, "actions": counts, "flagged": flagged}


def run(db: Optional[Path] = None, *, limit: int = 50, points: str = "skills", since_days: Optional[float] = None,
        only_with_skill: bool = False, export: Optional[Path] = None, harness: Optional[Harness] = None,
        progress=print) -> Dict[str, Any]:
    db = Path(db) if db else default_db()
    if not db.exists():
        raise FileNotFoundError(f"no Hermes session database at {db}")
    h = harness or Harness()
    h.background_shadow = False
    if not h.engine.client.available():
        raise RuntimeError(f"{h.engine.client.config.resolved()['api_key_env']} is not set")
    turns = list(iter_turns(db, limit=limit, since_days=since_days, only_with_skill=only_with_skill))
    progress(f"replaying {len(turns)} real turns from {db}")
    report: Dict[str, Any] = {"db": str(db), "turns": len(turns)}
    if points in ("skills", "all"):
        progress(f"skill ranking over {len(h.roster())} skills:")
        report["skills"] = replay_skills(h, turns, progress=progress)
    if points in ("risk", "all"):
        report["risk"] = replay_risk(h, turns, progress=progress)
    if export:
        export = Path(export)
        with export.open("w", encoding="utf-8") as fh:
            for r in report.get("skills", {}).get("rows", []):
                if r["error"] or not r["top1"]:
                    fh.write(json.dumps({**r, "label": None}, ensure_ascii=False) + "\n")
            for f in report.get("risk", {}).get("flagged", []):
                fh.write(json.dumps({"kind": "risk", **f, "label": None}, ensure_ascii=False) + "\n")
        progress(f"disagreements written to {export} (fill in 'label' to build an eval set)")
    return report


def print_summary(report: Dict[str, Any], out=print) -> None:
    s = report.get("skills")
    if s:
        out("")
        out("skill ranking vs. what the agent actually loaded")
        out(f"  turns replayed                         {s['turns']}  (errors: {s['errors']})")
        out(f"  turns where the agent loaded a skill   {s['agent_loaded_a_skill']}")
        out(f"  Jev #1 == agent's skill                {s['top1_agreement_pct']}%")
        out(f"  agent's skill in Jev top 3             {s['top3_agreement_pct']}%")
        out(f"  agent loaded nothing, Jev said none    {s['jev_said_none_when_agent_loaded_nothing_pct']}%")
        out("  note: the agent's own choice is a weak label; review disagreements before tuning.")
    r = report.get("risk")
    if r:
        out("")
        out(f"risk gate over {r['calls']} real tool calls: {r['actions']}")
        for f in r["flagged"][:10]:
            out(f"  {f['action']:<6} {f['tool']:<12} {f['reason']}  | {f['args'][:90]}")
