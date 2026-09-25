"""``hermes jermes <cmd>`` (also runnable as ``python -m jermes``).

    status   backend, key presence, per-point modes, data paths
    check    one live Jev call with a harmless sample, to verify access
    rank     rank skills for one request:  jermes rank "make me a pitch deck"
    replay   shadow-replay real past turns from Hermes' state.db (offline)
    label    hand-label real turns with the skills that should load (answer key)
    score    score Jev (and the agent) against your labels
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


def _auto_backend() -> bool:
    import os

    import yaml

    if os.environ.get("JERMES_BACKEND"):
        return False
    try:
        raw = yaml.safe_load(config_path().read_text(encoding="utf-8")) if config_path().exists() else {}
        name = ((raw or {}).get("backend") or {}).get("name")
    except Exception:
        name = None
    return not name or str(name).lower() == "auto"


def cmd_status(_args) -> int:
    cfg = load_config()
    client = _client(cfg)
    r = client.config.resolved()
    from .config import AUTO_ORDER

    how = "auto-detected from " + next((env for n, env in AUTO_ORDER if n == cfg["backend"]["name"]), "?") \
        if _auto_backend() else "set in config"
    print(f"backend       {cfg['backend']['name']}  ({r['base_url']})  [{how}]")
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
                            only_with_skill=args.with_skill, export=args.export, interval_s=args.interval,
                            use_context=not args.no_context)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"FAILED: {exc}")
        return 1
    replay.print_summary(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nfull report: {args.json}")
    return 0


def _labelling_harness():
    from . import replay
    from .harness import Harness

    h = Harness()
    h.background_shadow = False
    replay._batch_mode(h, interval_s=2.1)
    return h


def _jev_list(h, t):
    r = h.rank_skills(f"replay:{t.session_id}", t.request, t.history)
    return None if r is None else [x["skill"] for x in r]


def cmd_label(args) -> int:
    from . import labels, replay

    h = _labelling_harness()
    known = [s.name for s in h.roster()]
    turns = list(replay.iter_turns(replay.default_db(), limit=args.limit, only_with_skill=args.with_skill))
    if args.export:
        n = labels.export_sheet(turns, lambda t: _jev_list(h, t), Path(args.export))
        print(f"wrote {n} turns to {args.export}")
        print(labels.SHEET_HELP)
        print(f"then: hermes jermes label --import {args.export}")
        return 0
    if args.import_:
        src = args.import_
        if src.startswith("gdrive:"):
            src = _download_sheet_csv(src.split(":", 1)[1])
        rep = labels.import_sheet(Path(src), known)
        print(f"imported {rep['added']} labels ({rep['blank']} blank rows skipped)")
        for p in rep["problems"]:
            print("  " + p)
        return 1 if rep["problems"] else 0
    labels.interactive(turns, lambda t: _jev_list(h, t), known)
    return 0


def _download_sheet_csv(file_id: str) -> str:
    """Export a Google Sheet as CSV via the google-workspace skill's Drive auth."""
    import tempfile

    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    from .store import hermes_home

    creds = Credentials.from_authorized_user_file(str(hermes_home() / "google_token.json"))
    data = build("drive", "v3", credentials=creds).files().export(fileId=file_id, mimeType="text/csv").execute()
    fh = tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False)
    fh.write(data)
    fh.close()
    return fh.name


def cmd_score(args) -> int:
    from . import labels, replay

    labs = labels.load_labels()
    if not labs:
        print("no labels yet: run `hermes jermes label` first")
        return 1
    h = _labelling_harness()
    turns = {labels.turn_key(t.session_id, t.message_id): t
             for t in replay.iter_turns(replay.default_db(), limit=100000)}

    def predict(lab):
        t = turns.get(lab.key)
        return None if t is None else _jev_list(h, t)

    rep = labels.score(labs.values(), predict)
    labels.print_score(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
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
        p.add_argument("--no-context", action="store_true", help="send only the request, no earlier conversation")
        p = sub.add_parser("label", help="hand-label real turns with the skills that should load")
        p.add_argument("-n", "--limit", type=int, default=40, help="recent real turns to offer")
        p.add_argument("--with-skill", action="store_true", help="only turns where the agent loaded a skill")
        p.add_argument("--export", default=None, help="write a CSV to fill in (e.g. in Google Sheets) instead")
        p.add_argument("--import", dest="import_", default=None, help="read labels back from a filled-in CSV, or gdrive:<sheet-id> for a Google Sheet")
        p = sub.add_parser("score", help="score Jev and the agent against your labels")
        p.add_argument("--json", default=None, help="write the full report as JSON")

    @staticmethod
    def handle(args: Any) -> int:
        cmd = getattr(args, "jermes_cmd", None) or "status"
        return {"status": cmd_status, "stats": cmd_stats, "recent": cmd_recent, "check": cmd_check,
                "rank": cmd_rank, "replay": cmd_replay, "label": cmd_label, "score": cmd_score}[cmd](args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="jermes")
    register_cli.setup(parser)
    return register_cli.handle(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
