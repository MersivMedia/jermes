"""``hermes jermes <cmd>`` (also runnable as ``python -m jermes``).

    status   backend, key presence, per-point modes, data paths
    check    one live Jev call with a harmless sample, to verify access
    rank     rank skills for one request:  jermes rank "make me a pitch deck"
    replay   shadow-replay real past turns from Hermes' state.db (offline)
    stats    decisions per point and mode, cache hits, latency, tokens
    recent   last N logged decisions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from .client import ClientConfig, JevClient, JevError
from .config import config_path, load_config
from .questions import Choice, Noul, Score, answer_to_dict
from .store import Store, data_dir


def _client(cfg) -> JevClient:
    b = cfg["backend"]
    return JevClient(ClientConfig(backend=b["name"], base_url=b.get("base_url"), model=b.get("model"),
                                  deadline_s=max(5.0, float(b.get("deadline_s", 2.5)))))


def cmd_status(_args) -> int:
    cfg = load_config()
    client = _client(cfg)
    r = client.config.resolved()
    print(f"backend       {cfg['backend']['name']}  ({r['base_url']})")
    print(f"model         {r['model']}")
    print(f"api key       {r['api_key_env']} {'set' if r['api_key'] else 'MISSING'}")
    print(f"config        {config_path()}{'' if config_path().exists() else '  (defaults)'}")
    print(f"data          {data_dir()}")
    print("points")
    for name, p in cfg["points"].items():
        print(f"  {name:<14} {p['mode']}")
    return 0


def cmd_stats(_args) -> int:
    rows = Store().stats()
    if not rows:
        print("no decisions logged yet")
        return 0
    head = ["point", "mode", "n", "errors", "cache_hits", "applied", "avg_latency_ms", "input_tokens"]
    print("  ".join(f"{h:>14}" for h in head))
    for r in rows:
        print("  ".join(f"{str(r[h]):>14}" for h in head))
    return 0


def cmd_recent(args) -> int:
    for r in Store().recent(limit=args.limit, point=args.point):
        print(json.dumps({k: r[k] for k in ("id", "point", "mode", "action", "cached", "latency_ms", "error")}))
    return 0


def cmd_check(_args) -> int:
    cfg = load_config()
    client = _client(cfg)
    state = {"message": "Hi, my payouts have been failing for 3 days and I'm losing sales. Please help ASAP."}
    qs = {
        "urgent": Noul(instructions="Does `message` convey urgency?"),
        "team": Choice(instructions="Which team should handle `message`?",
                       criteria={"billing": "Payments, payouts, refunds", "technical": "Bugs and outages",
                                 "sales": "Pricing and upgrades"}),
        "frustration": Score(instructions="How frustrated is the sender of `message`?",
                             criteria=["Calm", "Frustrated", "Very angry"]),
    }
    try:
        resp = client.ask(state, qs)
    except JevError as exc:
        print(f"FAILED ({exc.kind}, HTTP {exc.status}): {exc}")
        return 1
    print(f"ok  model={resp.model}  latency={resp.latency_ms:.0f}ms  input_tokens={resp.input_tokens}")
    for k, v in resp.answers.items():
        print(f"  {k}: {json.dumps(answer_to_dict(v))}")
    return 0


def cmd_rank(args) -> int:
    from .harness import Harness
    from .points.skill_suggest import ranking_block

    h = Harness()
    h.background_shadow = False
    request = " ".join(args.request)
    print(f"roster: {len(h.roster())} skills")
    ranking = h.rank_skills("cli", request)
    if ranking is None:
        row = (h.engine.store.recent(1) or [{}])[0]
        print(f"FAILED: {row.get('error') or 'Jev unavailable (check `jermes check`)'}")
        return 1
    for i, r in enumerate(ranking, 1):
        print(f"  {i}. {r['skill']:<40} p={r['p']:.2f}  fits={r['fits']:.2f}")
    detail = json.loads((h.engine.store.recent(1, point='skill_suggest') or [{}])[0].get("detail_json") or "{}")
    rejected = [c for c in detail.get("candidates", []) if c["skill"] not in {r["skill"] for r in ranking}]
    for c in rejected:
        print(f"     (rejected) {c['skill']:<29} p={c['p']:.2f}  fits={c['fits']:.2f}")
    print("\nwhat the agent would see:\n" + ranking_block(ranking))
    return 0


def cmd_replay(args) -> int:
    from . import replay

    try:
        report = replay.run(args.db, limit=args.limit, points=args.points, since_days=args.days,
                            only_with_skill=args.with_skill, export=args.export, interval_s=args.interval)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"FAILED: {exc}")
        return 1
    replay.print_summary(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nfull report: {args.json}")
    return 0


class register_cli:  # namespace used by the plugin entry point
    @staticmethod
    def setup(parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(dest="jermes_cmd")
        sub.add_parser("status", help="show backend, key, and per-point modes")
        sub.add_parser("stats", help="decision statistics")
        p = sub.add_parser("recent", help="recent decisions")
        p.add_argument("-n", "--limit", type=int, default=20)
        p.add_argument("--point", default=None)
        sub.add_parser("check", help="make one live Jev call to verify access")
        p = sub.add_parser("rank", help="rank skills for one request")
        p.add_argument("request", nargs="+")
        p = sub.add_parser("replay", help="shadow-replay real past turns from Hermes' state.db")
        p.add_argument("--db", default=None, help="path to state.db (default: $HERMES_HOME/state.db)")
        p.add_argument("-n", "--limit", type=int, default=50, help="number of recent real user turns")
        p.add_argument("--points", choices=["skills", "risk", "all"], default="skills")
        p.add_argument("--days", type=float, default=None, help="only turns from the last N days")
        p.add_argument("--with-skill", action="store_true", help="only turns where the agent loaded a skill")
        p.add_argument("--export", default=None, help="write disagreements to JSONL for labelling")
        p.add_argument("--json", default=None, help="write the full report as JSON")
        p.add_argument("--interval", type=float, default=2.1, help="seconds between Jev requests (gateway allows ~30/min)")

    @staticmethod
    def handle(args: Any) -> int:
        cmd = getattr(args, "jermes_cmd", None) or "status"
        return {"status": cmd_status, "stats": cmd_stats, "recent": cmd_recent, "check": cmd_check,
                "rank": cmd_rank, "replay": cmd_replay}[cmd](args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="jermes")
    register_cli.setup(parser)
    return register_cli.handle(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
