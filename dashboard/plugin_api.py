"""Jermes dashboard API: mounted by Hermes at /api/plugins/jermes/ behind the dashboard login.

Read routes summarise the local decision log and config. Write routes are
limited to: setting a point's mode (enforce requires an explicit confirm flag,
which the page only sends after a confirmation dialog), and starting one
skills-audit run.

Nothing here returns secrets: key presence only, and previews come from the
decision log, which Jermes writes after its own secret redaction.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from jermes.config import DEFAULTS, config_path, load_config  # noqa: E402
from jermes.store import data_dir  # noqa: E402

router = APIRouter()

UI_MODES = ("off", "shadow", "advise", "enforce")
# "advise" only changes behaviour where the code acts on it (notes, suggestions,
# holds). Elsewhere it behaves like shadow but runs in the foreground, adding
# delay for nothing, so the dashboard doesn't offer it there.
ADVISE_POINTS = {"skill_suggest", "skill_overlap", "memory_filter", "loop_guard"}
# What switching a feature to enforce does, shown in the confirmation dialog.
ENFORCE_EFFECT = {
    "context_trim": "After a pause of 5+ minutes, old tool output Jev judges unneeded is replaced by one-line stubs "
                    "(full text kept on disk). Needs context.engine: jermes in Hermes' config; applies to new sessions.",
    "risk_gate": "Tool calls Jev judges clearly dangerous and unrequested are refused; uncertain ones go to "
                 "Hermes' approval prompt. Hermes' own safety checks still run.",
    "result_filter": "Long tool results are cut down to the parts Jev judges relevant before the model reads them.",
    "model_router": "Easy, low-stakes turns are sent to the cheaper model when the cost check says it pays.",
    "skill_suggest": "Same as advise: skill suggestions are added to each turn.",
    "skill_overlap": "Same as advise: a skill create that overlaps an existing skill is paused once with a note.",
    "memory_filter": "Same as advise: memory writes that look like task progress are held once with a note.",
    "loop_guard": "Same as advise: a note is added when a failed step is repeated.",
}


def ui_modes(point: str) -> tuple:
    return UI_MODES if point in ADVISE_POINTS else ("off", "shadow", "enforce")
JEV_USD_PER_M = 0.042
POINTS_INFO = {
    "context_trim": ("Context trimming", "After a pause, drops old tool output the conversation no longer needs"),
    "risk_gate": ("Risk gate", "Checks tool calls for danger or mismatch with the request"),
    "skill_overlap": ("Duplicate skills", "Before a new skill is created, checks for an existing one"),
    "memory_filter": ("Memory filter", "Keeps task progress and how-tos out of persistent memory"),
    "skill_suggest": ("Skill selection", "Suggests which skills a request needs"),
    "result_filter": ("Tool-result filter", "Drops irrelevant parts of long tool results"),
    "loop_guard": ("Loop guard", "Notices repeated failed steps"),
    "model_router": ("Model router", "Sends easy turns to a cheaper model"),
}
ACTION_WORDS = {"block": "would block", "review": "would ask approval", "would_trim": "would trim",
                "hold": "would hold", "advise": "would advise", "note": "would note", "route": "would route"}


def _db() -> Optional[sqlite3.Connection]:
    p = data_dir() / "decisions.sqlite"
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
    c.row_factory = sqlite3.Row
    return c


def _json(s: Optional[str]) -> Dict[str, Any]:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- status

@router.get("/status")
async def status() -> Dict[str, Any]:
    from jermes.client import BACKENDS
    import os

    cfg = load_config()
    name = cfg["backend"].get("name", "vercel")
    key_env = BACKENDS.get(name, {}).get("api_key_env", "")
    points = []
    for p, (label, desc) in POINTS_INFO.items():
        if p in cfg["points"]:
            mode = cfg["points"][p].get("mode")
            points.append({"point": p, "label": label, "description": desc, "mode": mode,
                           "modes": list(ui_modes(p)), "settable": True,
                           "enforce_effect": ENFORCE_EFFECT.get(p, "")})
    last = None
    c = _db()
    if c:
        try:
            r = c.execute("SELECT max(ts) FROM decisions WHERE error IS NULL AND cached=0").fetchone()
            last = r[0]
        finally:
            c.close()
    return {"backend": name, "model": cfg["backend"].get("model") or BACKENDS.get(name, {}).get("model"),
            "key_env": key_env, "key_present": bool(os.environ.get(key_env, "").strip()) or _key_in_env_files(key_env),
            "config_path": str(config_path()), "last_jev_call": last, "points": points,
            "forced_mode": os.environ.get("JERMES_MODE") or None}


def _key_in_env_files(name: str) -> bool:
    """Whether a Hermes .env the agent loads defines the key (value never read into the response)."""
    if not name:
        return False
    from jermes.store import hermes_home

    for f in (hermes_home() / ".env", Path.home() / "hermes-agent" / ".env"):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                k, _, v = line.partition("=")
                if k.strip() == name and v.strip():
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- decisions

@router.get("/decisions")
async def decisions(point: str = "", action: str = "", limit: int = 100, flagged: bool = False) -> Dict[str, Any]:
    c = _db()
    if not c:
        return {"rows": []}
    limit = max(1, min(int(limit), 500))
    q = "SELECT id, ts, session_id, point, mode, action, applied, cached, latency_ms, error, detail_json " \
        "FROM decisions WHERE point NOT LIKE '%.%'"
    args: List[Any] = []
    if point:
        q += " AND point = ?"
        args.append(point)
    if action:
        q += " AND action = ?"
        args.append(action)
    if flagged:
        q += " AND action IN ('block','review','would_trim','hold','advise','note','route')"
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        rows = []
        for r in c.execute(q, args):
            d = _json(r["detail_json"])
            rows.append({
                "id": r["id"], "ts": r["ts"], "session": (r["session_id"] or "")[-12:], "point": r["point"],
                "mode": r["mode"], "action": r["action"], "label": ACTION_WORDS.get(r["action"] or "", r["action"]),
                "applied": bool(r["applied"]), "latency_ms": r["latency_ms"], "error": (r["error"] or "")[:160],
                "reason": d.get("reason") or d.get("why") or "",
                "tool": d.get("tool") or "",
                "preview": str(d.get("preview") or d.get("name") or "")[:200],
                "summary": _summary(r["point"], d),
            })
        return {"rows": rows}
    finally:
        c.close()


def _summary(point: str, d: Dict[str, Any]) -> str:
    if point == "context_trim":
        n, k, ch = d.get("candidates"), d.get("trimmed"), d.get("chars_trimmed") or d.get("chars")
        if n is not None:
            return f"{k if k is not None else '?'} of {n} old items" + (f", {int(ch):,} chars" if ch else "")
    if point == "risk_gate" and d.get("hazards"):
        top = max(d["hazards"].items(), key=lambda kv: kv[1])
        return f"highest hazard {top[0].replace('_', ' ')} {top[1]:.2f}"
    if point == "memory_filter":
        return f"durable {d.get('durable')}, progress {d.get('progress')}"
    return ""


@router.get("/counts")
async def counts(hours: int = 24) -> Dict[str, Any]:
    c = _db()
    if not c:
        return {"hours": hours, "points": {}}
    since = time.time() - max(1, min(int(hours), 24 * 90)) * 3600
    try:
        out: Dict[str, Dict[str, int]] = {}
        for r in c.execute("SELECT point, action, count(*) n, sum(error IS NOT NULL) e FROM decisions "
                           "WHERE ts >= ? AND point NOT LIKE '%.%' GROUP BY point, action", (since,)):
            p = out.setdefault(r["point"], {"checked": 0, "errors": 0})
            p["checked"] += r["n"]
            p["errors"] += r["e"] or 0
            p[r["action"] or "none"] = p.get(r["action"] or "none", 0) + r["n"]
        return {"hours": hours, "points": out}
    finally:
        c.close()


# ---------------------------------------------------------------- cost

@router.get("/cost")
async def cost(days: int = 30) -> Dict[str, Any]:
    c = _db()
    days = max(1, min(int(days), 365))
    series = []
    today = _dt.datetime.now(_dt.timezone.utc).date()
    by_day: Dict[str, Dict[str, float]] = {}
    if c:
        since = time.time() - days * 86400
        try:
            for r in c.execute("SELECT date(ts,'unixepoch') d, count(*) n, coalesce(sum(input_tokens),0) t "
                               "FROM decisions WHERE ts >= ? AND cached=0 AND error IS NULL GROUP BY d", (since,)):
                by_day[r["d"]] = {"requests": r["n"], "tokens": r["t"]}
            tot = c.execute("SELECT count(*), coalesce(sum(input_tokens),0) FROM decisions "
                            "WHERE cached=0 AND error IS NULL").fetchone()
        finally:
            c.close()
    else:
        tot = (0, 0)
    for i in range(days - 1, -1, -1):
        d = (today - _dt.timedelta(days=i)).isoformat()
        v = by_day.get(d, {"requests": 0, "tokens": 0})
        series.append({"day": d, "requests": v["requests"], "tokens": v["tokens"],
                       "usd": round(v["tokens"] * JEV_USD_PER_M / 1e6, 5)})
    week = series[-7:]
    return {"days": series, "usd_per_m_input": JEV_USD_PER_M,
            "today_usd": series[-1]["usd"], "week_usd": round(sum(x["usd"] for x in week), 5),
            "all_time_usd": round(tot[1] * JEV_USD_PER_M / 1e6, 5), "all_time_requests": tot[0],
            "note": "Jev bills input tokens only; cached answers cost nothing."}


# ---------------------------------------------------------------- reports

@router.get("/trimreport")
async def trimreport(days: int = 14) -> Dict[str, Any]:
    from jermes import trimreport as tr
    from jermes.store import hermes_home

    dec = data_dir() / "decisions.sqlite"
    state = hermes_home() / "state.db"
    if not dec.exists() or not state.exists():
        return {"items": 0, "note": "No context-trimming decisions logged yet."}
    try:
        r = tr.report(dec, state, since=time.time() - max(1, min(int(days), 365)) * 86400)
    except Exception as exc:  # the report is advisory; never break the page
        return {"items": 0, "error": str(exc)[:200]}
    detail = r.pop("detail", []) or []
    keep = ("tool", "call", "chars", "outcome", "evidence", "shadow", "returned_chars")
    r["needed"] = [{k: d.get(k) for k in keep} for d in detail if d.get("outcome") != "not needed"][:30]
    return r


# ---------------------------------------------------------------- mode changes

@router.post("/mode")
async def set_mode(body: Dict[str, Any]) -> Dict[str, Any]:
    point = str(body.get("point") or "")
    mode = str(body.get("mode") or "")
    if point not in POINTS_INFO or point not in DEFAULTS["points"]:
        raise HTTPException(400, f"unknown feature: {point!r}")
    if mode not in ui_modes(point):
        raise HTTPException(400, f"{point} has no {mode!r} mode here (it would act like shadow); "
                                 f"choose one of {', '.join(ui_modes(point))}")
    if mode == "enforce" and body.get("confirm") is not True:
        raise HTTPException(400, "switching to enforce needs confirm: true")
    return _write_mode(point, mode)


def _write_mode(point: str, mode: str) -> Dict[str, Any]:
    import yaml

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    current = load_config()["points"].get(point, {}).get("mode")
    backup = None
    if text:
        from jermes.store import hermes_home

        bdir = hermes_home() / "data" / "jermes" / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        backup = bdir / f"jermes-config.yaml.{_dt.datetime.now(_dt.timezone.utc):%Y%m%dT%H%M%S%fZ}"
        shutil.copy2(path, backup)
    new = _set_point_line(text, point, mode)
    loaded = yaml.safe_load(new) or {}
    got = ((loaded.get("points") or {}).get(point) or {}).get("mode")
    if got is False:                          # YAML reads a bare `off` as false; load_config does the same mapping
        got = "off"
    if got != mode:
        raise HTTPException(500, "could not update the config safely; nothing was changed")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    return {"point": point, "mode": mode, "previous": current, "backup": str(backup) if backup else None,
            "applies": _applies(point, mode)}


def _applies(point: str, mode: str) -> str:
    if point == "context_trim":
        return ("Takes effect in new sessions that use the jermes context engine "
                "(context.engine: jermes in Hermes' config).")
    return "Running agents pick this up within a few seconds."


def _set_point_line(text: str, point: str, mode: str) -> str:
    """Change one point's mode, keeping comments and every other line as written."""
    import re

    lines = text.splitlines()
    inline = re.compile(rf"^(\s+){re.escape(point)}:\s*\{{.*\}}\s*(#.*)?$")
    for i, line in enumerate(lines):
        m = inline.match(line)
        if m:
            import yaml

            # Only the mode value changes; other keys keep their original text
            # (re-dumping would turn `off` into `False` and reformat numbers).
            inner = line.split("{", 1)[1].rsplit("}", 1)[0]
            parts = [p.strip() for p in inner.split(",") if p.strip()]
            parts = [p for p in parts if not re.match(r"^mode\s*:", p)]
            body = ", ".join([f"mode: {mode}"] + parts)
            lines[i] = f"{m.group(1)}{point}: {{{body}}}" + (f"  {m.group(2)}" if m.group(2) else "")
            return "\n".join(lines) + "\n"
    # block style:  point:\n    mode: x
    for i, line in enumerate(lines):
        if re.match(rf"^(\s+){re.escape(point)}:\s*(#.*)?$", line):
            ind = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > ind):
                if re.match(r"^\s+mode:\s*", lines[j]):
                    lines[j] = re.sub(r"(mode:\s*)[^#\s]+", rf"\g<1>{mode}", lines[j], count=1)
                    return "\n".join(lines) + "\n"
                j += 1
            lines.insert(i + 1, " " * (ind + 2) + f"mode: {mode}")
            return "\n".join(lines) + "\n"
    # not present: add under points:, or add the section
    for i, line in enumerate(lines):
        if re.match(r"^points:\s*(#.*)?$", line):
            lines.insert(i + 1, f"  {point}: {{mode: {mode}}}")
            return "\n".join(lines) + "\n"
    return (text.rstrip("\n") + "\n" if text.strip() else "") + f"points:\n  {point}: {{mode: {mode}}}\n"


# ---------------------------------------------------------------- skills audit

_audit_lock = threading.Lock()
_audit: Dict[str, Any] = {"state": "idle"}


def _audit_file() -> Path:
    return data_dir() / "skills_audit_latest.json"


@router.get("/skills-audit")
async def skills_audit_status() -> Dict[str, Any]:
    with _audit_lock:
        cur = dict(_audit)
    f = _audit_file()
    if cur.get("state") != "running" and f.exists():
        try:
            rep = json.loads(f.read_text())
            cur["report"] = {k: rep.get(k) for k in ("skills_checked", "errors", "seconds", "scope", "threshold")}
            cur["report"]["groups"] = [{k: g.get(k) for k in ("members", "keep", "suggestion", "pairs")}
                                       for g in rep.get("groups", [])]
            cur["finished_at"] = rep.get("finished_at")
        except (OSError, ValueError):
            pass
    return cur


@router.post("/skills-audit")
async def skills_audit_start() -> Dict[str, Any]:
    with _audit_lock:
        if _audit.get("state") == "running":
            raise HTTPException(409, "an audit is already running")
        _audit.clear()
        _audit.update({"state": "running", "started_at": time.time(), "progress": "starting"})
    threading.Thread(target=_run_audit, name="jermes-skills-audit", daemon=True).start()
    return {"state": "running", "estimate": "about 6 minutes and $0.04 for ~100 skills; suggestions only"}


def _run_audit() -> None:
    try:
        from jermes import skill_audit
        from jermes.cli import _batch_engine
        from jermes.points import skill_suggest

        roster = skill_suggest.load_roster()
        eng = _batch_engine(interval_s=2.1)
        if eng.mode("skill_overlap") == "off":
            eng.config["points"]["skill_overlap"]["mode"] = "shadow"
        if not eng.client.available():
            raise RuntimeError("no Jev key configured")

        def prog(line: str) -> None:
            with _audit_lock:
                _audit["progress"] = line.strip()[:160]

        rep = skill_audit.audit(eng, roster, progress=prog)
        rep["finished_at"] = time.time()
        rep.pop("checked", None)
        _audit_file().write_text(json.dumps(rep, indent=1))
        with _audit_lock:
            _audit.update({"state": "done", "progress": "", "finished_at": rep["finished_at"]})
    except Exception as exc:
        with _audit_lock:
            _audit.update({"state": "error", "error": str(exc)[:200]})
