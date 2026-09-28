# Jermes - Jev decision layer for Hermes Agent

**Cut your Hermes Agent model bill by about 39%, with every answer still correct.**

- **−39% agent cost in live A/B tests.** Context trimming took five real Hermes sessions from $19.83 to $12.05, 32% to 63% cheaper per session, with 5/5 correct answers in both arms.
- **About $11–13 saved in the first 3.5 hours of live use**, for $0.15 of Jev calls. 98% of the 1,215 old tool results it trimmed (or, in shadow, would have) were never needed again.
- **Every dangerous call stopped** on a held-out attack set written by a different model (19/19), with all 16 harmless look-alikes allowed.

Jermes is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin. It takes the small, bounded decisions an agent makes all the time (what old context to keep, which skill to load, whether a tool call is safe) away from the expensive reasoning model. Plain code makes each one by asking [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), TypeSafe AI's System One model, which returns typed answers with probabilities for $0.042 per million input tokens. Jev supplies the judgment; thresholds in code decide whether to act, ask you, or leave Hermes alone.

Numbers come from one install and from test tasks built for each feature. Methods and caveats: [Results](docs/RESULTS.md).

## What it does

| Feature | What Jev decides | Result |
|---|---|---|
| **Context trimming** | After a pause, which old tool output and file reads are no longer needed. Those become one-line stubs, with the full text saved on disk | **−39% cost** in live A/B tests; 98% of trimmed items never needed again |
| **Risk gate** | Whether a tool call is dangerous or not what you asked. Clear dangers are blocked; uncertain ones go to Hermes' approval prompt | 19/19 dangerous calls stopped (13 blocked outright), 16/16 harmless allowed |
| **Skill selection** | Which of 200+ skills a request needs, or none | Right first skill on 72% of hand-labelled turns (agent alone: 7%) |
| **Duplicate skills** | Whether a new skill repeats an existing one; `skills-audit` checks the whole library | Found 4 duplicate groups in 95 skills for $0.04 |
| **Memory filter** | Whether a memory write is a durable fact or task progress that doesn't belong there | 15 of 16 right on a held-out set, no real fact held |
| **Loop guard** | Whether the agent is repeating a failed step | Catches 31% of repeated failures at 5% false alarms |
| **Tool-result filter** | Which sections of a long result matter | Mixed; little extra once trimming is on |
| **Model router** | Whether a turn is easy enough for a cheaper model, only when that still saves after cache costs | No live saving yet on long sessions |
| **Data ingestion** | Which candidate value in a document answers each field | 94/94 correct at 11× lower cost than a strong model |

> **Status: v1.1.1.** Every feature ships in `shadow`: Jermes logs what it *would* do and changes nothing until you promote it. The first install running everything in `enforce` has done so since September 28, 2026.

## Install

1. **Install [Hermes Agent](https://github.com/NousResearch/hermes-agent#quick-install)** if you don't have it.
2. **Install the plugin.** Hermes scans every plugin before installing it and shows the result.

   ```bash
   hermes plugins install MersivMedia/jermes --enable
   ```

3. **Add one Jev key** to `~/.hermes/.env` (see [Jev provider](#jev-provider)).
4. **Check it works:**

   ```bash
   hermes jermes status    # provider, key, and each feature's mode
   hermes jermes check     # one live Jev call
   ```

5. **Restart Hermes** (CLI, TUI, gateway and dashboard). Plugins load when Hermes starts.

To update later: `hermes plugins update jermes`, then restart.

## Configure

### Jev provider

Add one key to `~/.hermes/.env`. Jermes detects which is set.

| Provider | Key | Notes |
|---|---|---|
| **Vercel AI Gateway** | `AI_GATEWAY_API_KEY=vck_...` | Needs paid AI Gateway credit on the team; free-tier accounts get HTTP 403 |
| **OpenRouter** | `OPENROUTER_API_KEY=sk-or-...` | Uses OpenRouter's [System One API](https://openrouter.ai/docs/guides/community/typesafe-sdk); not yet tested live |
| **TypeSafe direct** | `TYPESAFE_API_KEY=...` | TypeSafe's own API |

With several keys set, the order is Vercel, TypeSafe, OpenRouter. To force one, set `backend: {name: openrouter}` in the config file below.

### Turn on context trimming

Trimming is a Hermes context engine, so it also needs one line in Hermes' main config file (`config.yaml` in your Hermes home):

```yaml
context:
  engine: jermes
```

### Choose modes

Each feature has a mode:

| Mode | What it does |
|---|---|
| `off` | Nothing |
| `shadow` | Logs what it would do; no effect on Hermes (the default) |
| `advise` | Adds notes and suggestions for the agent (skill selection, duplicate skills, memory filter, loop guard) |
| `enforce` | Acts: trims context, blocks or asks about tool calls, filters, reroutes |

Change modes from the **Jermes tab** in the Hermes web dashboard (`hermes dashboard`), or in `jermes/config.yaml` in your Hermes home:

```yaml
points:
  context_trim:  { mode: enforce }
  risk_gate:     { mode: enforce }
  skill_suggest: { mode: advise }
  memory_filter: { mode: advise }
  loop_guard:    { mode: advise }
  skill_overlap: { mode: advise }
  result_filter: { mode: shadow }
  model_router:  { mode: shadow }
```

Running agents pick up mode changes within a few seconds; no restart is needed. Anything you leave out uses the defaults in [`jermes/config.py`](jermes/config.py). `JERMES_MODE=off` in the environment turns everything off.

A sensible rollout: leave everything in shadow for a few days, run `hermes jermes trimreport` and look at the dashboard's would-block list, then promote context trimming first.

### Dashboard tab

The **Jermes** tab (after Skills) shows each feature's mode with switches (enforce asks for confirmation first), recent decisions, the trim report, would-block lists, Jev spend per day, and a button to run the duplicate-skill audit. If the dashboard was running when you installed Jermes, restart it once.

## Commands

```bash
hermes jermes status        # provider, key, per-feature modes
hermes jermes check         # one live Jev call
hermes jermes recent        # latest decisions
hermes jermes stats         # decisions, latency, tokens per feature
hermes jermes trimreport    # was anything trimming dropped needed again?
hermes jermes costsim       # price your past sessions with trimming applied (offline)
hermes jermes replay        # shadow-test over your real past sessions
hermes jermes skills-audit  # find overlapping skills and suggest merges
hermes jermes ingest        # extract fields from documents
```

The full list, plus the benchmark and labelling commands, is in [Measuring](docs/MEASURING.md).

## More

- **[Results](docs/RESULTS.md):** every measurement, with methods and caveats
- **[Known issues](docs/KNOWN_ISSUES.md):** current limits and gotchas
- **[How it works](docs/HOW_IT_WORKS.md):** each decision point, the guarantees in the code, the dashboard, development and tests
- **[Measuring and tuning](docs/MEASURING.md):** replay, labelling, scoring
- **[Roadmap](docs/ROADMAP.md)** and the **[PRD](docs/PRD.md)**

Jermes uses only public Hermes plugin surfaces. It needs no core patches, never touches the system prompt, and fails open: any Jev error, timeout or missing key leaves Hermes unchanged.

## License

MIT
