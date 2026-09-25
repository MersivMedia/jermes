"""Offline token-savings estimate over real Hermes sessions.

Answers "if Jermes had been on, how many reasoning-model tokens would it have
removed from these sessions, and what is that worth?" without running an
agent. Two sources of savings are estimated:

1. result_filter: every big tool result that the live hook would consider is
   run through the same eligibility rules and the same Jev question. Tokens it
   would drop are counted once when first sent and again on every later API
   call in the session (they stay in the context the model re-reads), until
   the session ended or Hermes compressed it.

2. skill loads (optional, needs labels): a load is "avoidable" when the
   labelled answer for that turn says the skill was not needed. The loaded
   SKILL.md is counted the same way as a tool result.

Costs use Hermes' own price table (agent.usage_pricing) where the model is
listed; unlisted models fall back to an explicit price given on the command
line and are marked as assumed. A token re-read from Anthropic's prompt cache
is billed at the cache-read rate; the first time it is sent, at the cache-write
rate. Jev's own cost is included and subtracted.

What this cannot see: behaviour changes (the agent re-running a tool because
content was filtered out, or taking a different path). That is what the
task-matched A/B (``jermes ab``) measures.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import _redact
from .labels import _norm, load_labels, turn_key

CHARS_PER_TOKEN = 4.0  # stated approximation; Hermes does not store per-message token counts
JEV_INPUT_PER_M = 0.042


def tok(chars: int) -> float:
    return chars / CHARS_PER_TOKEN


# Anthropic list prices for models missing from Hermes' own table (checked
# 2026-09-25 at https://platform.claude.com/docs/en/about-claude/pricing).
# 5-minute cache-write rate. Used only when Hermes has no entry.
KNOWN_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}
KNOWN_PRICES_SOURCE = "anthropic pricing page 2026-09-25"


@dataclass
class Prices:
    """USD per million tokens."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    source: str

    @classmethod
    def for_model(cls, model: str, fallback: Optional["Prices"] = None) -> Optional["Prices"]:
        try:
            from agent.usage_pricing import get_pricing_entry  # type: ignore

            e = get_pricing_entry(model, provider="anthropic" if "claude" in model else None)
        except Exception:
            e = None
        if e is not None and e.input_cost_per_million is not None:
            f = lambda d: float(d) if isinstance(d, Decimal) else float(d or 0)
            return cls(f(e.input_cost_per_million), f(e.output_cost_per_million),
                       f(e.cache_read_cost_per_million), f(e.cache_write_cost_per_million),
                       f"hermes:{e.pricing_version}")
        leaf = model.split("/")[-1]
        if leaf in KNOWN_PRICES:
            return cls(*KNOWN_PRICES[leaf], KNOWN_PRICES_SOURCE)
        return fallback


@dataclass
class Removal:
    session_id: str
    message_id: int
    kind: str            # "result_filter" | "skill_load"
    tool: str
    removed_tokens: float
    later_calls: int     # API calls in the session after this point (re-reads)
    detail: str = ""

    @property
    def total_tokens(self) -> float:
        return self.removed_tokens * (1 + self.later_calls)


@dataclass
class Session:
    id: str
    model: str
    api_calls: int
    message_count: int
    prompt_tokens: int   # input + cache read + cache write
    output_tokens: int
    end_reason: str


def open_db(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)


def real_sessions(conn: sqlite3.Connection) -> Dict[str, Session]:
    """Sessions with trustworthy counters (api_call_count > 0). Old Hermes
    versions recorded cumulative input totals with no API-call count, which
    inflates totals by orders of magnitude; those are excluded."""
    out = {}
    for r in conn.execute(
        "SELECT id, model, api_call_count, message_count, coalesce(input_tokens,0)+coalesce(cache_read_tokens,0)"
        "+coalesce(cache_write_tokens,0), coalesce(output_tokens,0), coalesce(end_reason,'') "
        "FROM sessions WHERE api_call_count > 0"
    ):
        out[r[0]] = Session(*r)
    return out


def later_calls(conn: sqlite3.Connection, s: Session, message_id: int) -> int:
    """API calls after ``message_id`` in the same session.

    Hermes does not log which message each API call followed, so this uses the
    share of assistant messages after the point, scaled to the session's real
    API-call count. Compression ends a session (the summary continues in a
    child session), so re-reads never cross a compression.
    """
    total, after = conn.execute(
        "SELECT count(*), sum(id > ?) FROM messages WHERE session_id=? AND role='assistant'", (message_id, s.id)
    ).fetchone()
    if not total:
        return 0
    return int(round(s.api_calls * (after or 0) / total))


def big_tool_results(conn: sqlite3.Connection, sessions: Dict[str, Session], min_chars: int):
    """(session, message_id, tool_name, content, request) for large tool results."""
    ids = ",".join("?" * len(sessions))
    rows = conn.execute(
        f"SELECT m.session_id, m.id, m.tool_call_id, m.tool_name, m.content FROM messages m "
        f"WHERE m.role='tool' AND length(m.content) >= ? AND m.session_id IN ({ids}) ORDER BY m.id",
        [min_chars, *sessions],
    ).fetchall()
    for sid, mid, call_id, tool_name, content in rows:
        if not tool_name and call_id:
            tool_name = _tool_name_for_call(conn, sid, mid, call_id)
        req = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' AND id<? AND content IS NOT NULL "
            "AND trim(content) != '' ORDER BY id DESC LIMIT 1", (sid, mid)
        ).fetchone()
        yield sessions[sid], mid, tool_name or "?", content, (req[0] if req else "")


def _tool_name_for_call(conn, sid, mid, call_id) -> Optional[str]:
    for (raw,) in conn.execute(
        "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' AND id<? AND tool_calls IS NOT NULL "
        "ORDER BY id DESC LIMIT 5", (sid, mid)
    ):
        try:
            for it in json.loads(raw) or []:
                if it.get("id") == call_id or it.get("call_id") == call_id:
                    return (it.get("function") or {}).get("name")
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------- estimates


def estimate_result_filter(harness, conn, sessions, *, progress=print) -> Tuple[List[Removal], Dict[str, Any]]:
    """Run the live filter's own rules and Jev question over real big results."""
    cfg = harness.engine.point_config("result_filter")
    min_chars = int(cfg.get("min_chars", 6000))
    removals: List[Removal] = []
    stats = {"big_results": 0, "eligible": 0, "filtered": 0, "passed": 0, "errors": 0, "jev_tokens": 0,
             "ineligible_by_tool": {}}
    rows = list(big_tool_results(conn, sessions, min_chars))
    stats["big_results"] = len(rows)
    for i, (s, mid, tool, content, request) in enumerate(rows, 1):
        prepared = harness.prepare_filter(tool, content, "", request)
        if prepared is None:
            stats["ineligible_by_tool"][tool] = stats["ineligible_by_tool"].get(tool, 0) + 1
            continue
        stats["eligible"] += 1
        chunks, wrapper, key, text, state, qs, policy = prepared
        d = harness.engine.decide("result_filter", state, qs, policy, session_id=f"savings:{s.id}",
                                  spec_version="result_filter.1", log_detail={"tool": tool, "estimate": True})
        stats["jev_tokens"] += d.input_tokens
        if not d.ok:
            stats["errors"] += 1
            continue
        if d.action != "filter":
            stats["passed"] += 1
            continue
        stats["filtered"] += 1
        from .points import result_filter as rf

        new = rf.render(chunks, d.detail["kept_idx"], wrapper, key, len(text))
        removed = max(0, len(content) - len(new))
        removals.append(Removal(s.id, mid, "result_filter", tool, tok(removed), later_calls(conn, s, mid),
                                f"kept {d.detail['kept']}/{d.detail['chunks']} sections"))
        if progress:
            progress(f"  [{i}/{len(rows)}] {tool:<12} {len(content):>7} chars -> removed {removed:>7} "
                     f"({removed / len(content):.0%}), re-read ~{removals[-1].later_calls}x")
    return removals, stats


def estimate_skill_loads(conn, sessions, labels_file: Optional[Path] = None) -> Tuple[List[Removal], Dict[str, Any]]:
    """Skill loads on labelled turns that the label says were not needed."""
    labels = load_labels(labels_file)
    removals: List[Removal] = []
    stats = {"labelled_turns": len(labels), "turns_found": 0, "loads": 0, "avoidable": 0, "needed": 0}
    for key, lab in labels.items():
        sid, _, mid = key.rpartition("#")
        if sid not in sessions:
            continue
        mid = int(mid)
        stats["turns_found"] += 1
        nxt = conn.execute("SELECT min(id) FROM messages WHERE session_id=? AND role='user' AND id>?",
                           (sid, mid)).fetchone()[0] or 10 ** 12
        gold = {_norm(x) for x in lab.skills}
        for tmid, content, call_id in conn.execute(
            "SELECT id, content, tool_call_id FROM messages WHERE session_id=? AND role='tool' AND id>? AND id<? "
            "AND (tool_name='skill_view')", (sid, mid, nxt)
        ):
            try:
                name = json.loads(content).get("name", "")
            except (TypeError, ValueError, AttributeError):
                name = ""
            stats["loads"] += 1
            if _norm(name) in gold:
                stats["needed"] += 1
                continue
            stats["avoidable"] += 1
            removals.append(Removal(sid, tmid, "skill_load", "skill_view", tok(len(content or "")),
                                    later_calls(conn, sessions[sid], tmid), f"loaded {name}, not in label"))
    return removals, stats


# ---------------------------------------------------------------- report


def cost(removals: List[Removal], sessions: Dict[str, Session], prices: Dict[str, Prices]) -> float:
    usd = 0.0
    for r in removals:
        p = prices.get(sessions[r.session_id].model)
        if p is None:
            continue
        usd += r.removed_tokens * p.cache_write / 1e6 + r.removed_tokens * r.later_calls * p.cache_read / 1e6
    return usd


def baseline(sessions: Dict[str, Session], prices: Dict[str, Prices], conn) -> Dict[str, Any]:
    q = ("SELECT model, sum(input_tokens), sum(cache_read_tokens), sum(cache_write_tokens), sum(output_tokens), "
         "sum(api_call_count) FROM sessions WHERE api_call_count > 0 GROUP BY model")
    total_tokens = 0
    usd = 0.0
    by_model = []
    for model, inp, cr, cw, out, calls in conn.execute(q):
        inp, cr, cw, out = (inp or 0), (cr or 0), (cw or 0), (out or 0)
        total_tokens += inp + cr + cw
        p = prices.get(model)
        m_usd = None
        if p:
            m_usd = (inp * p.input + cr * p.cache_read + cw * p.cache_write + out * p.output) / 1e6
            usd += m_usd
        by_model.append({"model": model, "api_calls": calls, "prompt_tokens": inp + cr + cw, "output_tokens": out,
                         "usd": m_usd, "price_source": p.source if p else None})
    return {"prompt_tokens": total_tokens, "usd": usd, "by_model": by_model}


def run(db: Path, harness, *, labels_file: Optional[Path] = None, fallback: Optional[Prices] = None,
        progress=print) -> Dict[str, Any]:
    conn = open_db(db)
    sessions = real_sessions(conn)
    prices = {}
    for m in {s.model for s in sessions.values()}:
        p = Prices.for_model(m, fallback)
        if p:
            prices[m] = p
    base = baseline(sessions, prices, conn)
    progress(f"{len(sessions)} sessions with trustworthy counters; "
             f"{base['prompt_tokens'] / 1e6:.0f}M prompt tokens")
    rf_rem, rf_stats = estimate_result_filter(harness, conn, sessions, progress=progress)
    sk_rem, sk_stats = estimate_skill_loads(conn, sessions, labels_file)
    jev_usd = rf_stats["jev_tokens"] * JEV_INPUT_PER_M / 1e6
    out = {
        "chars_per_token": CHARS_PER_TOKEN,
        "baseline": base,
        "prices": {m: vars(p) for m, p in prices.items()},
        "result_filter": {
            **rf_stats,
            "removed_tokens_once": sum(r.removed_tokens for r in rf_rem),
            "removed_tokens_total": sum(r.total_tokens for r in rf_rem),
            "usd": cost(rf_rem, sessions, prices),
            "jev_usd": jev_usd,
            "by_tool": _by_tool(rf_rem),
        },
        "skill_loads": {
            **sk_stats,
            "removed_tokens_total": sum(r.total_tokens for r in sk_rem),
            "usd": cost(sk_rem, sessions, prices),
        },
        "removals": [vars(r) | {"total_tokens": r.total_tokens} for r in rf_rem + sk_rem],
    }
    conn.close()
    return out


def _by_tool(rem: List[Removal]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for r in rem:
        b = out.setdefault(r.tool, {"results": 0, "removed_tokens_total": 0.0})
        b["results"] += 1
        b["removed_tokens_total"] += r.total_tokens
    return out


def print_report(rep: Dict[str, Any], out=print) -> None:
    b, rf, sk = rep["baseline"], rep["result_filter"], rep["skill_loads"]
    pct = lambda x: f"{100 * x / b['prompt_tokens']:.1f}%" if b["prompt_tokens"] else "-"
    out("")
    out("Baseline (sessions with trustworthy counters)")
    for m in b["by_model"]:
        usd = f"${m['usd']:.2f}" if m["usd"] is not None else "not priced"
        out(f"  {m['model']:<34} {m['api_calls']:>6} API calls  {m['prompt_tokens'] / 1e6:>7.0f}M prompt tokens  "
            f"{usd}  ({m['price_source'] or 'no price'})")
    out(f"  total prompt tokens {b['prompt_tokens'] / 1e6:.0f}M, cost ${b['usd']:.2f}")
    out("")
    out("result_filter (live hook rules + real Jev answers)")
    out(f"  big results {rf['big_results']}, eligible {rf['eligible']}, filtered {rf['filtered']}, "
        f"passed through {rf['passed']}, Jev errors {rf['errors']}")
    out(f"  tokens removed: {rf['removed_tokens_once'] / 1e3:.0f}k first time, "
        f"{rf['removed_tokens_total'] / 1e6:.1f}M counting re-reads ({pct(rf['removed_tokens_total'])} of prompt tokens)")
    out(f"  worth ${rf['usd']:.2f}; Jev cost ${rf['jev_usd']:.4f}")
    for t, v in sorted(rf["by_tool"].items(), key=lambda kv: -kv[1]["removed_tokens_total"]):
        out(f"    {t:<14} {v['results']:>4} results  {v['removed_tokens_total'] / 1e6:>6.2f}M tokens")
    inel = rf.get("ineligible_by_tool") or {}
    if inel:
        out("  not eligible under current settings: " + ", ".join(f"{k} {v}" for k, v in
                                                                sorted(inel.items(), key=lambda kv: -kv[1])))
    out("")
    out("skill loads (labelled turns only)")
    out(f"  labelled turns found {sk['turns_found']}/{sk['labelled_turns']}; skill loads {sk['loads']}, "
        f"needed {sk['needed']}, avoidable {sk['avoidable']}")
    out(f"  tokens {sk['removed_tokens_total'] / 1e6:.2f}M counting re-reads, worth ${sk['usd']:.2f}")
    out("")
    out(f"Token counts use {rep['chars_per_token']:.0f} characters per token. Behaviour changes (re-running a tool")
    out("after filtering) are not visible here; the task-matched A/B measures those.")
