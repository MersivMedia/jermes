"""``hermes jermes <cmd>`` (also runnable as ``python -m jermes``).

    status   backend, key presence, per-point modes, data paths
    check    one live Jev call with a harmless sample, to verify access
    rank     rank skills for one request:  jermes rank "make me a pitch deck"
    replay   shadow-replay real past turns from Hermes' state.db (offline)
    label    hand-label real turns with the skills that should load (answer key)
    score    score Jev (and the agent) against your labels
    savings  estimate reasoning-model tokens and $ Jermes would have saved (offline)
    ab       run the same tasks with Jermes off and on; compare Hermes' own token counts
    ingest   extract fields from documents (Jev picks, code copies); bench against one model
    riskbench  score the risk gate on labelled cases and on real past tool calls
    loopbench  score the loop guard's repeat-failure check on real past sessions
    costsim  price your past sessions under context trimming (offline, no Jev calls)
    stats    decisions per point and mode, cache hits, latency, tokens
    recent   last N logged decisions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        n = labels.export_sheet(turns, lambda t: _jev_list(h, t), Path(args.export), blind=args.blind)
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


def cmd_savings(args) -> int:
    from . import replay, savings

    h = _labelling_harness()
    fb = None
    if args.price:
        i, o, cr, cw = (float(x) for x in args.price.split(","))
        fb = savings.Prices(i, o, cr, cw, "assumed (--price)")
    rep = savings.run(Path(args.db) if args.db else replay.default_db(), h, fallback=fb)
    savings.print_report(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"\nfull report: {args.json}")
    return 0


def cmd_ab(args) -> int:
    from . import ab
    from .ab_tasks import TASKS

    names = args.tasks.split(",") if args.tasks else [t.name for t in TASKS]
    if args.list:
        for t in TASKS:
            print(f"  {t.name:<14} {t.exercises}")
        return 0
    out = Path(args.json) if args.json else None
    print(f"A/B: {len(names)} task(s) x 2 arms x {args.repeats} repeat(s) = {len(names) * 2 * args.repeats} agent runs")
    try:
        runs = ab.run_ab(names, args.repeats, points={"skill": args.skill_mode, "filt": args.filter_mode,
                                                      "trim": args.trim_mode, "guards": args.guards_mode},
                         timeout=args.timeout, out_path=out, parallel=args.parallel)
    except RuntimeError as exc:
        print(f"FAILED preflight: {exc}")
        return 1
    s = ab.summarize(runs)
    ab.print_summary(s)
    if out:
        out.with_suffix(".summary.json").write_text(json.dumps(s, indent=2))
    return 0


def cmd_ingest(args) -> int:
    from .ingest import bench

    eng = _batch_engine()
    if args.bench:
        docs, truth = bench.load_bench(Path(args.bench))
        if args.limit:
            keep = list(truth)[: args.limit]
            docs, truth = {k: docs[k] for k in keep}, {k: truth[k] for k in keep}
        rep = bench.benchmark(eng, docs, truth, strong_model=args.strong, cheap_model=args.cheap,
                              arms=args.arms.split(",") if args.arms else None)
        bench.print_report(rep)
        if args.json:
            Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
        return 0
    import yaml

    from .ingest import Pipeline, load_schema
    from .ingest.llm import ChatModel, extractor

    if not args.schema or not args.files:
        print("usage: jermes ingest --schema schema.yaml FILE [FILE ...]   (or --bench DIR)")
        return 2
    schema = load_schema(yaml.safe_load(Path(args.schema).read_text()))
    pipe = Pipeline(eng, schema, strong=extractor(ChatModel(args.strong)) if args.strong else None)
    rows = []
    for f in args.files:
        rec = pipe.run(Path(f).name, Path(f).read_text(errors="replace"))
        rows.append(rec.to_json())
        vals = ", ".join(f"{k}={v!r}" for k, v in rec.values().items())
        print(f"{Path(f).name}: {rec.disposition}  {vals}" + (f"  [{'; '.join(rec.reasons)}]" if rec.reasons else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


def cmd_riskbench(args) -> int:
    from . import riskbench
    from .replay import default_db

    eng = _batch_engine(interval_s=args.interval)
    eng.config["points"]["risk_gate"]["mode"] = "enforce"
    out: Dict[str, Any] = {}
    if not args.real_only:
        print("labelled cases:")
        s = riskbench.score_cases(eng)
        out["cases"] = s
        rx = s["regex"]
        print(f"\n  Jev: exact {_pct(s['exact'])}, dangerous blocked {_pct(s['dangerous_blocked'])}, "
              f"dangerous stopped (block or review) {_pct(s['dangerous_stopped'])}, "
              f"benign allowed {_pct(s['benign_allowed'])}, benign blocked {_pct(s['benign_blocked'])}"
              f"  ({s['errors']} errors)")
        print(f"  Hermes regex (shell cases only, n={rx['shell_cases']}): dangerous flagged "
              f"{_pct(rx['dangerous_flagged'])}, needs-human flagged {_pct(rx['needs_human_flagged'])}, "
              f"benign flagged {_pct(rx['benign_flagged'])}")
    if args.real:
        calls = riskbench.real_calls(Path(args.db) if args.db else default_db(), args.real)
        print(f"\n{len(calls)} real tool calls (all were run with the user's approval):")
        r = riskbench.score_real(eng, calls)
        out["real"] = r
        print(f"  actions {r['actions']}; block rate {_pct(r['block_rate'])}, review rate {_pct(r['review_rate'])}; "
              f"Hermes regex flagged {_pct(r['hermes_regex_flag_rate'])} of shell calls; "
              f"latency p50 {r['latency_ms_p50']} ms, p90 {r['latency_ms_p90']} ms")
        for f in r["flagged"][:15]:
            print(f"    {f['action']:<6} {f['tool']:<12} {str(f['reason'])[:40]:<42} {f['args'][:90]}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str))
    return 0


def cmd_loopbench(args) -> int:
    from . import loopbench
    from .replay import default_db

    eng = _batch_engine(interval_s=args.interval)
    eng.config["points"]["loop_guard"]["mode"] = "enforce"
    r = loopbench.score(eng, Path(args.db) if args.db else default_db(), n_positive=args.positives,
                        n_negative=args.negatives)
    for d in r["disagreements"][:20]:
        print(f"    {d['type']:<9} p={d['p']:<5} {d['tool']:<12} {d['latest'][:100]}")
    if args.json:
        Path(args.json).write_text(json.dumps(r, indent=1, default=str))
    return 0


def cmd_costsim(args) -> int:
    from . import costsim
    from .replay import default_db

    ss = costsim.sessions(Path(args.db) if args.db else default_db(), min_calls=args.min_calls)
    if not ss:
        print("no sessions with enough stored history to rebuild")
        return 1
    policies = [costsim.Policy("as recorded"),
                costsim.Policy(f"trim, Jev keeps {args.keep:.0%}", compact=True, keep_fraction=args.keep,
                               keep_turns=args.keep_turns, ttl=args.ttl),
                costsim.Policy("trim everything old", compact=True, keep_fraction=0.0, keep_turns=args.keep_turns,
                               ttl=args.ttl)]
    tot = {p.name: 0.0 for p in policies}
    print(f"{len(ss)} sessions rebuilt call by call (cache TTL {args.ttl:.0f} s, last {args.keep_turns} turns protected)")
    for s in ss:
        r = {p.name: costsim.simulate(s, p)["usd"] for p in policies}
        for k, v in r.items():
            tot[k] += v
        if args.verbose:
            b = r["as recorded"]
            print(f"  {s.sid[:24]:<24} {len(s.calls):>5} calls  ${b:8.2f}  " +
                  "  ".join(f"{(v - b) / b:+.0%}" for k, v in r.items() if k != "as recorded"))
    base = tot["as recorded"]
    for k, v in tot.items():
        print(f"  {k:<26} ${v:9.2f}" + ("" if k == "as recorded" else f"  ({(v - base) / base:+.1%})"))
    print("Estimates from rebuilt prompts and list prices; they don't include any rework if the agent")
    print("needs a trimmed item back. The A/B (`hermes jermes ab`) measures that.")
    return 0


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _batch_engine(interval_s: float = 2.1):
    """Engine for offline batch work: ingest point on, patient paced client
    (same settings as replay; the Vercel gateway allows ~30 requests/window)."""
    from .config import load_config
    from .engine import Engine

    cfg = load_config()
    if cfg["points"]["ingest"].get("mode") == "off":
        cfg["points"]["ingest"]["mode"] = "enforce"
    eng = Engine(cfg)
    c = eng.client.config
    c.deadline_s = max(c.deadline_s, 90.0)
    c.max_retries = max(c.max_retries, 6)
    c.min_interval_s = max(c.min_interval_s, interval_s)
    return eng


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
        p.add_argument("--blind", action="store_true", help="with --export: hide Jev's and the agent's answers")
        p.add_argument("--import", dest="import_", default=None, help="read labels back from a filled-in CSV, or gdrive:<sheet-id> for a Google Sheet")
        p = sub.add_parser("savings", help="estimate tokens and $ Jermes would have saved on past sessions")
        p.add_argument("--db", default=None, help="path to state.db (default: $HERMES_HOME/state.db)")
        p.add_argument("--price", default=None,
                       help="fallback $/M for models Hermes has no price for: input,output,cache_read,cache_write")
        p.add_argument("--json", default=None, help="write the full report as JSON")
        p = sub.add_parser("ab", help="task-matched A/B: same tasks with Jermes off and on (spends model tokens)")
        p.add_argument("--tasks", default=None, help="comma-separated task names (default: all; see --list)")
        p.add_argument("--list", action="store_true", help="list the built-in tasks")
        p.add_argument("--repeats", type=int, default=1)
        p.add_argument("--skill-mode", default="advise", choices=["off", "shadow", "advise", "enforce"])
        p.add_argument("--filter-mode", default="enforce", choices=["off", "shadow", "advise", "enforce"])
        p.add_argument("--trim-mode", default="off", choices=["off", "shadow", "enforce"],
                       help="context trimming (also switches Hermes to the jermes context engine)")
        p.add_argument("--parallel", type=int, default=0,
                       help="concurrent agent runs (default: 1, or 8 for tasks with pauses); each needs ~200 MB")
        p.add_argument("--guards-mode", default="shadow", choices=["off", "shadow"],
                       help="risk_gate and loop_guard in the on arm (off isolates other points, saves Jev calls)")
        p.add_argument("--timeout", type=int, default=900, help="seconds per agent run")
        p.add_argument("--json", default=None, help="write every run (and a .summary.json) here")
        p = sub.add_parser("ingest", help="extract fields from documents; --bench compares against one model")
        p.add_argument("files", nargs="*")
        p.add_argument("--schema", default=None, help="YAML schema: name, scope, fields[name, question, kind]")
        p.add_argument("--bench", default=None, help="benchmark dir with truth.json + <doc>.txt")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--arms", default=None, help="jev,strong,cheap (default: all configured)")
        p.add_argument("--strong", default="anthropic:claude-sonnet-4-5",
                       help="escalation / baseline model: anthropic:<id> (ANTHROPIC_API_KEY) or a gateway id")
        p.add_argument("--cheap", default=None, help="cheap baseline model, e.g. anthropic:claude-haiku-4-5")
        p.add_argument("--json", default=None)
        p = sub.add_parser("riskbench", help="score the risk gate on labelled cases and real past tool calls")
        p.add_argument("--real", type=int, default=0, help="also score N real tool calls from state.db")
        p.add_argument("--real-only", action="store_true")
        p.add_argument("--db", default=None)
        p.add_argument("--interval", type=float, default=2.1)
        p.add_argument("--json", default=None)
        p = sub.add_parser("loopbench", help="score the loop guard on real past sessions")
        p.add_argument("--positives", type=int, default=40)
        p.add_argument("--negatives", type=int, default=80)
        p.add_argument("--db", default=None)
        p.add_argument("--interval", type=float, default=2.1)
        p.add_argument("--json", default=None)
        p = sub.add_parser("costsim", help="price past sessions under context trimming (offline)")
        p.add_argument("--db", default=None)
        p.add_argument("--keep", type=float, default=0.3, help="fraction of old items assumed kept by Jev")
        p.add_argument("--keep-turns", type=int, default=2)
        p.add_argument("--ttl", type=float, default=300.0)
        p.add_argument("--min-calls", type=int, default=50)
        p.add_argument("-v", "--verbose", action="store_true")
        p = sub.add_parser("score", help="score Jev and the agent against your labels")
        p.add_argument("--json", default=None, help="write the full report as JSON")

    @staticmethod
    def handle(args: Any) -> int:
        cmd = getattr(args, "jermes_cmd", None) or "status"
        return {"status": cmd_status, "stats": cmd_stats, "recent": cmd_recent, "check": cmd_check,
                "rank": cmd_rank, "replay": cmd_replay, "label": cmd_label, "score": cmd_score, "savings": cmd_savings, "ab": cmd_ab, "ingest": cmd_ingest, "riskbench": cmd_riskbench, "loopbench": cmd_loopbench, "costsim": cmd_costsim}[cmd](args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="jermes")
    register_cli.setup(parser)
    return register_cli.handle(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
