"""Score the loop guard's repeat-failure check on real sessions (``jermes loopbench``).

Ground truth comes from code, not labels: a call is a *repeat failure* when an
earlier call in the same turn used the same tool with near-identical
arguments (after stripping whitespace and quoting), that earlier call failed
(an error, a non-zero exit code, or "not found"/"no such file"), and nothing
succeeded in between.

That rule is narrow on purpose: it's precise but misses repeats that change
a flag or two. So disagreements are shown for inspection rather than all
counted as Jev mistakes.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import Engine
from .points import loop_guard

_FAIL = re.compile(r'"error"\s*:\s*"[^"]|"exit_code"\s*:\s*[1-9]|no such file|not found|traceback|permission denied',
                   re.IGNORECASE)


def failed(result: str) -> bool:
    head = (result or "")[:3000]
    if '"exit_code": 0' in head and '"error": null' in head:
        return False
    return bool(_FAIL.search(head))


def _norm_args(args: str) -> str:
    return re.sub(r"[\s'\"`]+", " ", args or "").strip().lower()


def is_repeat_failure(calls: List[Dict[str, str]]) -> bool:
    """Latest call repeats an earlier failed call with nothing changed in between.

    Same tool, arguments >= 90% similar, the earlier call failed, and every
    call between them also failed (a successful different call in between,
    such as fixing a config before retrying, counts as a change of approach).
    """
    last = calls[-1]
    for i in range(len(calls) - 2, -1, -1):
        c = calls[i]
        if not failed(c["result_excerpt"]):
            return False
        if c["tool"] == last["tool"] and \
                SequenceMatcher(None, _norm_args(c["args"]), _norm_args(last["args"])).ratio() >= 0.9:
            return True
    return False


def windows(db: Path, window: int = 8) -> List[Dict[str, Any]]:
    """Every tool call with the calls before it in the same turn, as the live hook sees them."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT session_id, role, content, tool_calls, tool_call_id, tool_name FROM messages "
                        "ORDER BY session_id, id").fetchall()
    conn.close()
    out = []
    req: Dict[str, str] = {}
    pending: Dict[str, Dict[str, str]] = {}
    turn: Dict[str, List[Dict[str, str]]] = {}
    for sid, role, content, tcs, tcid, tname in rows:
        if role == "user" and content and not content.lstrip().startswith("["):
            req[sid] = content
            turn[sid] = []
        elif role == "assistant" and tcs:
            try:
                for x in json.loads(tcs):
                    f = x.get("function") or {}
                    pending[x.get("id") or x.get("call_id") or ""] = {"tool": f.get("name", ""),
                                                                      "args": (f.get("arguments") or "")[:600]}
            except ValueError:
                pass
        elif role == "tool" and sid in req:
            p = pending.pop(tcid or "", None) or {"tool": tname or "", "args": ""}
            entry = {"tool": p["tool"], "args": p["args"], "status": "",
                     "result_excerpt": (content or "")[:600]}
            calls = (turn.setdefault(sid, []) + [entry])[-window:]
            turn[sid].append(entry)
            if len(calls) >= 2:
                out.append({"session": sid, "request": req[sid], "calls": calls})
    return out


def score(engine: Engine, db: Path, *, n_positive: int = 40, n_negative: int = 80, seed: int = 3,
          progress=print) -> Dict[str, Any]:
    ws = windows(db)
    pos = [w for w in ws if is_repeat_failure(w["calls"])]
    # Hard negatives first: the latest call follows a failure but changes approach.
    near = [w for w in ws if not is_repeat_failure(w["calls"]) and failed(w["calls"][-2]["result_excerpt"])]
    other = [w for w in ws if not is_repeat_failure(w["calls"]) and w not in near]
    rng = random.Random(seed)
    rng.shuffle(pos)
    rng.shuffle(near)
    rng.shuffle(other)
    neg = near[: n_negative // 2] + other[: n_negative - min(len(near), n_negative // 2)]
    sample = [(w, True) for w in pos[:n_positive]] + [(w, False) for w in neg]
    cfg = engine.point_config("loop_guard")
    tp = fp = fn = tn = err = 0
    disagree = []
    scored = []
    for w, truth in sample:
        state, qs = loop_guard.build(w["request"], w["calls"], ask_completion=False)
        d = engine.decide("loop_guard", state, qs, loop_guard.make_policy(cfg), session_id=f"loopbench:{w['session']}",
                          spec_version=loop_guard.SPEC_VERSION)
        if d.error:
            err += 1
            continue
        said = d.action == "note_loop"
        p = round(d.detail.get("repeat_failure", 0.0), 2)
        scored.append((p, truth))
        if said and truth:
            tp += 1
        elif said and not truth:
            fp += 1
            disagree.append({"type": "jev_only", "p": p, "latest": w["calls"][-1]["args"][:160],
                             "tool": w["calls"][-1]["tool"]})
        elif truth:
            fn += 1
            disagree.append({"type": "rule_only", "p": p, "latest": w["calls"][-1]["args"][:160],
                             "tool": w["calls"][-1]["tool"]})
        else:
            tn += 1
    res = {
        "windows": len(ws), "rule_repeats": len(pos), "near_misses": len(near), "sampled": len(sample),
        "errors": err, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "false_alarm_rate": round(fp / (fp + tn), 3) if fp + tn else None,
        "disagreements": disagree,
        "sweep": {str(th): {"recall": round(sum(1 for p, tr in scored if tr and p >= th) / max(1, sum(tr for _, tr in scored)), 3),
                            "false_alarm_rate": round(sum(1 for p, tr in scored if not tr and p >= th)
                                                      / max(1, sum(not tr for _, tr in scored)), 3)}
                  for th in (0.5, 0.6, 0.7, 0.8)},
    }
    if progress:
        progress(f"  {len(ws)} tool-call windows, {len(pos)} repeat failures by rule, sampled {len(sample)}: "
                 f"recall {res['recall']}, false alarms {res['false_alarm_rate']} (tp {tp} fn {fn} fp {fp} tn {tn}, "
                 f"{err} errors)")
    return res
