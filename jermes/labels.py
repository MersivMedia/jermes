"""Hand-labelled evaluation for skill selection.

Workflow (``hermes jermes label`` / ``hermes jermes score``):

1. ``label`` walks through real past turns one at a time. For each it shows
   the request, the conversation just before it, what the agent loaded, and
   Jev's list. You answer with the skills that *should* have been loaded, or
   ``none``. Answers go to ``$JERMES_HOME/labels.jsonl`` (append-only; the
   latest label for a turn wins), so you can stop and resume any time.

2. ``score`` re-runs Jev on every labelled turn (cached decisions are free)
   and compares its list with your labels.

Labels are sets, because a turn can need several skills (a research document
needs the document skill and the citation skill). Scoring therefore works on
sets too:

* **decision accuracy**: did Jev get "skill vs no skill" right?
* **primary hit**: is Jev's first skill one of the labelled skills?
* **recall**: share of labelled skills that appear in Jev's list
* **precision**: share of Jev's listed skills that are labelled as needed
* **false alarms**: Jev listed skills on a turn labelled "none"
* **misses**: Jev said "no skill" on a turn that needed one

Each metric is also reported for "what the agent actually did", so you can see
whether Jev beats the agent's own choices, not just whether it copies them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .engine import _redact
from .store import data_dir


def _norm(name: str) -> str:
    return name.split(":")[-1].split("/")[-1].strip().lower()


def labels_path() -> Path:
    return data_dir() / "labels.jsonl"


def turn_key(session_id: str, message_id: int) -> str:
    return f"{session_id}#{message_id}"


@dataclass
class Label:
    key: str
    request: str
    skills: List[str] = field(default_factory=list)  # empty = no skill needed
    agent_loaded: List[str] = field(default_factory=list)
    note: str = ""
    ts: float = 0.0

    @property
    def none(self) -> bool:
        return not self.skills


def load_labels(path: Optional[Path] = None) -> Dict[str, Label]:
    """Latest label per turn wins; ``skip`` entries remove a turn from the set."""
    path = Path(path) if path else labels_path()
    out: Dict[str, Label] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("skip"):
            out.pop(d.get("key", ""), None)
            continue
        out[d["key"]] = Label(key=d["key"], request=d.get("request", ""), skills=list(d.get("skills") or []),
                              agent_loaded=list(d.get("agent_loaded") or []), note=d.get("note", ""),
                              ts=float(d.get("ts") or 0))
    return out


def append_label(label: Label, path: Optional[Path] = None, *, skip: bool = False) -> None:
    path = Path(path) if path else labels_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"key": label.key, "request": _redact(label.request)[:500], "skills": label.skills,
           "agent_loaded": label.agent_loaded, "note": label.note, "ts": label.ts or time.time()}
    if skip:
        row = {"key": label.key, "skip": True, "ts": time.time()}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- scoring


@dataclass
class Counts:
    turns: int = 0
    decision_ok: int = 0
    needs_skill: int = 0
    primary_hit: int = 0
    labelled_total: int = 0   # sum of |label| over turns that need skills
    recalled: int = 0         # labelled skills found in the list
    listed_total: int = 0     # sum of |list|
    listed_correct: int = 0   # listed skills that are labelled
    none_turns: int = 0
    false_alarms: int = 0     # listed something on a "none" turn
    misses: int = 0           # listed nothing on a turn needing a skill

    def add(self, predicted: Sequence[str], gold: Sequence[str]) -> None:
        pred = [_norm(p) for p in predicted]
        g = {_norm(x) for x in gold}
        self.turns += 1
        self.decision_ok += int(bool(pred) == bool(g))
        self.listed_total += len(pred)
        self.listed_correct += sum(p in g for p in pred)
        if g:
            self.needs_skill += 1
            self.labelled_total += len(g)
            self.recalled += len(g & set(pred))
            self.primary_hit += int(bool(pred) and pred[0] in g)
            self.misses += int(not pred)
        else:
            self.none_turns += 1
            self.false_alarms += int(bool(pred))

    def report(self) -> Dict[str, Any]:
        def pct(n: int, d: int) -> Optional[float]:
            return round(100.0 * n / d, 1) if d else None
        return {
            "turns": self.turns,
            "decision_accuracy_pct": pct(self.decision_ok, self.turns),
            "primary_hit_pct": pct(self.primary_hit, self.needs_skill),
            "recall_pct": pct(self.recalled, self.labelled_total),
            "precision_pct": pct(self.listed_correct, self.listed_total),
            "false_alarm_pct": pct(self.false_alarms, self.none_turns),
            "miss_pct": pct(self.misses, self.needs_skill),
            "avg_listed": round(self.listed_total / self.turns, 2) if self.turns else None,
            "needs_skill_turns": self.needs_skill,
            "none_turns": self.none_turns,
        }


def score(labels: Iterable[Label], predict: Callable[[Label], Optional[List[str]]]) -> Dict[str, Any]:
    """Compare ``predict(label)`` (Jev's list, or None on error) and the agent's
    own loads against each label."""
    jev, agent = Counts(), Counts()
    rows: List[Dict[str, Any]] = []
    errors = 0
    for lab in labels:
        pred = predict(lab)
        if pred is None:
            errors += 1
            continue
        jev.add(pred, lab.skills)
        agent.add(list(dict.fromkeys(lab.agent_loaded)), lab.skills)
        rows.append({"key": lab.key, "request": lab.request[:200], "label": lab.skills, "jev": pred,
                     "agent": lab.agent_loaded, "jev_ok": _turn_ok(pred, lab.skills)})
    return {"jev": jev.report(), "agent": agent.report(), "errors": errors, "rows": rows}


def _turn_ok(pred: Sequence[str], gold: Sequence[str]) -> bool:
    """A turn is right when "none" matches "none", or the first listed skill is
    one of the labelled skills. Always a real bool (it is saved to JSON)."""
    g = {_norm(x) for x in gold}
    if not g:
        return not pred
    return bool(pred) and _norm(pred[0]) in g


def print_score(rep: Dict[str, Any], out=print) -> None:
    j, a = rep["jev"], rep["agent"]
    out(f"scored {j['turns']} labelled turns ({j['needs_skill_turns']} need a skill, {j['none_turns']} need none)"
        + (f"; {rep['errors']} could not be scored (Jev unavailable)" if rep["errors"] else ""))
    out("")
    out(f"  {'metric':<42}{'Jev':>8}{'agent':>8}")
    lines = [
        ("decision accuracy (skill vs no skill)", "decision_accuracy_pct"),
        ("primary hit (first skill is correct)", "primary_hit_pct"),
        ("recall (needed skills that were listed)", "recall_pct"),
        ("precision (listed skills that were needed)", "precision_pct"),
        ("false alarms (skills on a 'none' turn)", "false_alarm_pct"),
        ("misses ('none' on a turn needing a skill)", "miss_pct"),
    ]
    for label, k in lines:
        fmt = lambda v: "-" if v is None else f"{v:.0f}%"
        out(f"  {label:<42}{fmt(j[k]):>8}{fmt(a[k]):>8}")
    out(f"  {'skills listed per turn (avg)':<42}{j['avg_listed'] or 0:>8}{a['avg_listed'] or 0:>8}")
    out("")
    out("  Higher is better except false alarms and misses. 'agent' is what the agent actually loaded.")
    wrong = [r for r in rep["rows"] if not r["jev_ok"]]
    if wrong:
        out("")
        out(f"  Jev got {len(wrong)} wrong:")
        for r in wrong[:15]:
            out(f"    label={r['label'] or ['none']}  jev={r['jev'][:3] or ['none']}  | {r['request'][:70]!r}")


# ---------------------------------------------------------------- labelling UI

HELP = (
    "  enter = accept Jev's list | a = accept what the agent loaded | n = no skill needed\n"
    "  or type skill names separated by commas | s = skip this turn | q = quit"
)


def _parse_answer(ans: str, jev: Sequence[str], agent: Sequence[str], known: Iterable[str]):
    """Returns ("label", [skills]) | ("skip", None) | ("quit", None) | ("error", message)."""
    a = ans.strip()
    low = a.lower()
    if low in ("q", "quit", "exit"):
        return "quit", None
    if low in ("s", "skip"):
        return "skip", None
    if low == "":
        return "label", list(jev)
    if low == "a":
        return "label", list(dict.fromkeys(agent))
    if low in ("n", "none", "no"):
        return "label", []
    names = [x.strip() for x in a.split(",") if x.strip()]
    by_norm = {_norm(k): k for k in known}
    out, bad = [], []
    for n in names:
        hit = by_norm.get(_norm(n))
        (out.append(hit) if hit else bad.append(n))
    if bad:
        near = []
        for b in bad:
            cands = [k for k in by_norm.values() if _norm(b) in _norm(k) or _norm(k) in _norm(b)][:4]
            near.append(f"{b!r} (did you mean: {', '.join(cands)})" if cands else repr(b))
        return "error", "unknown skill: " + "; ".join(near)
    return "label", list(dict.fromkeys(out))


def interactive(turns, rank: Callable[[Any], Optional[List[str]]], known: Sequence[str], *,
                path: Optional[Path] = None, ask: Callable[[str], str] = input, out=print,
                context_lines: int = 4) -> int:
    """Label turns one by one. Returns the number labelled this session."""
    done = load_labels(path)
    todo = [t for t in turns if turn_key(t.session_id, t.message_id) not in done]
    out(f"{len(done)} turns already labelled; {len(todo)} to go. Labels: {path or labels_path()}")
    out(HELP)
    n = 0
    for i, t in enumerate(todo, 1):
        jev = rank(t)
        out("")
        out(f"[{i}/{len(todo)}] " + "-" * 60)
        prior = [m for m in t.history if isinstance(m.get("content"), str) and m["content"].strip()][-context_lines:]
        for m in prior:
            out(f"   {m['role']}: {_redact(' '.join(m['content'].split()))[:160]}")
        out(f" > REQUEST: {_redact(t.request)[:600]}")
        out(f"   agent loaded: {', '.join(dict.fromkeys(t.loaded_skills)) or '(nothing)'}")
        out(f"   Jev lists:    {', '.join(jev) if jev else ('(no skill needed)' if jev == [] else '(Jev unavailable)')}")
        while True:
            kind, val = _parse_answer(ask("   which skills should be loaded? "), jev or [], t.loaded_skills, known)
            if kind == "error":
                out(f"   {val}")
                continue
            break
        if kind == "quit":
            break
        lab = Label(key=turn_key(t.session_id, t.message_id), request=t.request, skills=val or [],
                    agent_loaded=list(dict.fromkeys(t.loaded_skills)))
        if kind == "skip":
            append_label(lab, path, skip=True)
            continue
        append_label(lab, path)
        n += 1
    out(f"\nlabelled {n} this session; {len(load_labels(path))} total")
    return n


# ---------------------------------------------------------------- spreadsheet round trip

SHEET_COLS = ["key", "request", "earlier_conversation", "agent_loaded", "jev_lists", "correct_skills"]
SHEET_HELP = ("Fill in correct_skills: skill names separated by commas, 'none' if no skill is needed, "
              "or leave blank to skip. Leave the key column unchanged.")


BLIND_COLS = ["key", "request", "earlier_conversation", "correct_skills"]


def export_sheet(turns, rank: Callable[[Any], Optional[List[str]]], dest: Path, *, path: Optional[Path] = None,
                 include_labelled: bool = False, blind: bool = False) -> int:
    """Write a labelling sheet.

    ``blind=True`` hides both Jev's list and what the agent loaded, so labels
    can't anchor on either (batch 1 showed Jev's list, and 26 of 40 labels
    matched it exactly). Jev isn't called at all for a blind sheet.
    """
    import csv

    done = load_labels(path)
    rows = 0
    with Path(dest).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(BLIND_COLS if blind else SHEET_COLS)
        for t in turns:
            key = turn_key(t.session_id, t.message_id)
            if key in done and not include_labelled:
                continue
            prior = [m for m in t.history if isinstance(m.get("content"), str) and m["content"].strip()][-4:]
            conv = " || ".join(f"{m['role']}: {' '.join(m['content'].split())[:200]}" for m in prior)
            existing = done.get(key)
            if blind:
                w.writerow([key, _redact(t.request)[:1500], _redact(conv), ""])
                rows += 1
                continue
            jev = rank(t)
            w.writerow([key, _redact(t.request)[:1500], _redact(conv), ", ".join(dict.fromkeys(t.loaded_skills)),
                        "none" if jev == [] else ", ".join(jev or []),
                        ("none" if existing.none else ", ".join(existing.skills)) if existing else ""])
            rows += 1
    return rows


def import_sheet(src: Path, known: Sequence[str], *, path: Optional[Path] = None) -> Dict[str, Any]:
    import csv

    added, blank, problems = 0, 0, []
    with Path(src).open(newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), 2):
            ans = (row.get("correct_skills") or "").strip()
            if not ans:
                blank += 1
                continue
            kind, val = _parse_answer("n" if ans.lower() == "none" else ans, [], [], known)
            if kind != "label":
                problems.append(f"row {i}: {val}")
                continue
            agent = [x.strip() for x in (row.get("agent_loaded") or "").split(",") if x.strip()]
            append_label(Label(key=row["key"], request=row.get("request", ""), skills=val, agent_loaded=agent), path)
            added += 1
    return {"added": added, "blank": blank, "problems": problems}
