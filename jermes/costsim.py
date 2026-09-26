"""Offline cost simulation over real Hermes sessions (``jermes costsim``).

Rebuilds, call by call, what each model request in a session contained, then
prices the same sessions under different policies with the provider's cache
rules:

* a call whose previous call was more than ``ttl`` seconds ago starts cold:
  its whole prompt is written to the cache;
* otherwise it reads the cached prefix and writes only what was added.

Policies:

``baseline``   what happened.
``compact``    at the start of every cold turn, tool results and tool-call
               arguments older than ``keep_turns`` user turns are replaced by
               one-line stubs (the rewrite is free: the cache is cold anyway).
               ``keep_fraction`` models Jev keeping some old items it ranks as
               still relevant.
``route``      ``compact`` plus: a cold turn that Jev rated easy and low-stakes
               runs on ``cheap_model`` if its prompt fits that model's window.

Token counts come from characters (4 per token) plus a fixed per-call overhead
for the system prompt and tool schemas, fitted so the baseline matches the
token totals Hermes recorded for each session. Quality effects (a cheap model
failing, a trimmed item being needed) are not simulated; the A/B measures them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STUB_CHARS = 160

# $ per million tokens: input, output, cache read, cache write (5-minute tier)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}
WINDOW = {"claude-haiku-4-5": 200_000, "claude-sonnet-4-5": 1_000_000}


@dataclass
class Item:
    kind: str       # system | user | asst_text | tool_args | tool_result | summary
    chars: int
    turn: int
    tool: str = ""


@dataclass
class Call:
    turn: int
    ts: float
    items_before: int          # context = items[ctx_start:items_before]
    output_chars: int
    ctx_start: int = 0


@dataclass
class Turn:
    index: int
    request: str
    ts: float
    first_call: int            # index into calls
    n_calls: int = 0
    gap: float = 0.0           # seconds since the previous model call


@dataclass
class Session:
    sid: str
    model: str
    items: List[Item] = field(default_factory=list)
    calls: List[Call] = field(default_factory=list)
    turns: List[Turn] = field(default_factory=list)
    recorded_prompt_tokens: int = 0
    recorded_calls: int = 0
    overhead_tokens: int = 0


def load_session(conn: sqlite3.Connection, sid: str) -> Optional[Session]:
    row = conn.execute("SELECT model, api_call_count, coalesce(input_tokens,0)+coalesce(cache_read_tokens,0)"
                       "+coalesce(cache_write_tokens,0) FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row or not row[0]:
        return None
    s = Session(sid, row[0], recorded_calls=row[1] or 0, recorded_prompt_tokens=row[2] or 0)
    turn = 0
    last_ts = 0.0          # timestamp of the last model call (assistant message)
    ctx_start = 0
    tools_by_id: Dict[str, str] = {}
    for role, content, tcs, ts, tcid, tname in conn.execute(
            "SELECT role, content, tool_calls, timestamp, tool_call_id, tool_name FROM messages "
            "WHERE session_id=? ORDER BY id", (sid,)):
        t = float(ts) if ts else last_ts
        content = content or ""
        if role == "user" and "[CONTEXT COMPACTION" in content:
            # Hermes compacted: the live context restarts from its summary.
            ctx_start = len(s.items)
            s.items.append(Item("summary", len(content), turn))
            continue
        if role == "user":
            real = content and not content.lstrip().startswith("[")
            if real:
                turn += 1
                s.turns.append(Turn(turn, content, t, len(s.calls), gap=t - last_ts if last_ts else 1e9))
            s.items.append(Item("user", len(content), turn))
        elif role == "assistant":
            s.calls.append(Call(turn, t, len(s.items), len(content) + len(tcs or ""), ctx_start))
            if s.turns:
                s.turns[-1].n_calls += 1
            s.items.append(Item("asst_text", len(content), turn))
            if tcs:
                try:
                    for x in json.loads(tcs):
                        f = x.get("function") or {}
                        tools_by_id[x.get("id") or ""] = f.get("name", "")
                        s.items.append(Item("tool_args", len(f.get("arguments") or ""), turn, f.get("name", "")))
                except ValueError:
                    s.items.append(Item("tool_args", len(tcs), turn))
            last_ts = t
        elif role == "tool":
            s.items.append(Item("tool_result", len(content), turn, tname or tools_by_id.get(tcid or "", "")))
    if not s.calls:
        return None
    # Fit the fixed per-call overhead (system prompt + tool schemas) so the
    # rebuilt baseline matches Hermes' recorded prompt tokens for the session.
    rebuilt = sum(sum(i.chars for i in s.items[c.ctx_start:c.items_before]) for c in s.calls) / 4
    per_call_recorded = s.recorded_prompt_tokens / max(1, s.recorded_calls)
    s.overhead_tokens = max(15_000, int(per_call_recorded - rebuilt / len(s.calls)))
    return s


@dataclass
class Policy:
    name: str
    compact: bool = False
    keep_turns: int = 2
    keep_fraction: float = 0.0
    route: bool = False
    cheap_model: str = "claude-haiku-4-5"
    cheap_turns: Optional[set] = None          # turn indices Jev rated cheap-eligible
    ttl: float = 300.0


def simulate(s: Session, p: Policy) -> Dict[str, Any]:
    """Price every call of ``s`` under ``p``. Returns $ and token totals."""
    stubbed: Dict[int, int] = {}           # item index -> chars after stubbing
    cost = 0.0
    tokens = 0
    cached_model = None
    cached_prefix = 0.0                     # tokens in the cache for cached_model
    last_ts = None
    turn_model: Dict[int, str] = {}
    cheap_turns = 0
    turn_of_call = {i: c.turn for i, c in enumerate(s.calls)}
    first_call_of_turn = {t.first_call: t for t in s.turns}
    for ci, c in enumerate(s.calls):
        cold = last_ts is None or (c.ts - last_ts) > p.ttl
        t = first_call_of_turn.get(ci)
        if t is not None and cold and p.compact:
            # Stub old tool traffic; keep a fraction (Jev keeps what it ranks relevant).
            olds = [k for k in range(c.ctx_start, c.items_before) if s.items[k].kind in ("tool_result", "tool_args")
                    and s.items[k].turn <= c.turn - p.keep_turns and k not in stubbed]
            olds.sort(key=lambda k: -s.items[k].chars)
            n_keep = int(len(olds) * p.keep_fraction)
            for k in olds[n_keep:]:
                stubbed[k] = min(s.items[k].chars, STUB_CHARS)
        prompt = s.overhead_tokens + sum(stubbed.get(k, s.items[k].chars) for k in range(c.ctx_start, c.items_before)) / 4
        model = s.model
        if t is not None:
            use_cheap = (p.route and cold and p.cheap_turns is not None and c.turn in p.cheap_turns
                         and prompt < WINDOW.get(p.cheap_model, 0) * 0.9)
            turn_model[c.turn] = p.cheap_model if use_cheap else s.model
            cheap_turns += int(use_cheap)
        model = turn_model.get(c.turn, s.model)
        pin, pout, pread, pwrite = PRICES.get(model, PRICES["claude-opus-5"])
        if cold or model != cached_model:
            cost += prompt * pwrite / 1e6
        else:
            cost += cached_prefix * pread / 1e6 + max(0.0, prompt - cached_prefix) * pwrite / 1e6
        cost += (c.output_chars / 4) * pout / 1e6
        tokens += prompt
        cached_model, cached_prefix, last_ts = model, prompt, c.ts
    colds = sum(1 for i, c in enumerate(s.calls) if i == 0 or c.ts - s.calls[i - 1].ts > p.ttl)
    return {"usd": cost, "prompt_tokens": tokens, "cheap_turns": cheap_turns, "cold_calls": colds}


def sessions(db: Path, min_calls: int = 50) -> List[Session]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE api_call_count >= ? ORDER BY api_call_count DESC",
                                      (min_calls,))]
    out = []
    for x in (load_session(conn, i) for i in ids):
        # Skip sessions whose stored transcript doesn't cover the recorded calls
        # (older rows pruned): they can't be rebuilt faithfully.
        if x and x.model in PRICES and 0.7 <= len(x.calls) / max(1, x.recorded_calls) <= 1.5:
            out.append(x)
    conn.close()
    return out


def cold_turn_requests(ss: List[Session], ttl: float = 300.0) -> List[Tuple[str, int, str, str]]:
    """(session id, turn index, request, recent context) for every cold turn."""
    out = []
    for s in ss:
        prev: List[str] = []
        for t in s.turns:
            if t.gap > ttl and t.n_calls:
                out.append((s.sid, t.index, t.request, "\n".join(prev[-4:])))
            prev.append("user: " + " ".join(t.request.split())[:300])
    return out
