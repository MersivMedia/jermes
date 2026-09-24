# Jermes

**Jev Deterministic Decision Layer for Hermes Agent.**

Jermes is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that takes the small, bounded decisions an agent makes all the time out of the expensive reasoning model:

- Is this tool call dangerous?
- Which parts of this web page matter?
- Which skill fits this request?
- Is the agent going in circles?
- Does this turn even need the big model?

Plain code makes each of those decisions by consulting [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), TypeSafe AI's System One model. Jev returns typed answers with probabilities in roughly 100 ms for $0.042 per million input tokens.

Jev only supplies the judgment. Code owns the policy: thresholds you can read decide whether to act, ask a human, or leave Hermes alone.

The design, the evidence behind it, and the rollout plan are in the **[PRD](docs/PRD.md)**.

> **Status: v0.1, Phase 0 (shadow mode).** Every decision point ships in `shadow`. Jermes calls Jev and logs what it *would* do, but changes nothing in Hermes until you promote a point. This is how the PRD says to start: measure against real sessions first.

## How it fits into Hermes

Jermes uses only public Hermes plugin surfaces. It needs no core patches and never touches the system prompt, so prompt caching stays intact.

| Decision point | PRD | Hermes surface | What `enforce`/`advise` does |
|---|---|---|---|
| `risk_gate` | D3, D4 | `pre_tool_call` hook | Blocks clearly dangerous, unrequested calls. Sends uncertain ones to Hermes' approval gate. |
| `result_filter` | D5 | `transform_tool_result` hook | Drops irrelevant sections of big tool results before the model re-reads them every turn. |
| `loop_guard` | D6 | `transform_tool_result` hook | Adds a one-line note when a failed call is repeated or the task already looks done. |
| `skill_suggest` | D1 | `pre_llm_call` hook | Two-stage skill ranking. Adds one suggestion line to the user message. |
| `model_router` | D7 | `llm_request` middleware | Sends confidently easy, low-stakes turns to a cheaper model, sticky for the whole turn. |

Guarantees built into the code:

- **Deterministic.** Decisions are cached by `(model, canonical state, question spec)`. The same input always produces the same action, even if Jev's sampling drifts.
- **Fails open.** Any error, timeout, 429/529 or missing key leaves Hermes' behaviour unchanged. The client's deadline (2.5 s by default) is shorter than Hermes' hook timeout, so a slow Jev can't cause Hermes to fail a `pre_tool_call` *closed*.
- **Never lowers protection.** `risk_gate` sits in front of Hermes' own dangerous-command detector and approval flow. It doesn't replace them.
- **Zero latency in shadow mode.** Shadow decisions run in a background thread.
- **Everything is logged.** SQLite at `$HERMES_HOME/jermes/decisions.sqlite` records answers, probabilities, action, mode, policy version, latency and tokens for every decision.
- **Redacted.** State goes through Hermes' secret redactor before it leaves the machine.

## Install

```bash
hermes plugins install MersivMedia/jermes --enable
```

Jev access goes through **Vercel AI Gateway** by default:

```bash
# ~/.hermes/.env
AI_GATEWAY_API_KEY=vck_...
```

Vercel AI Gateway won't serve requests until the Vercel team has a credit card on file. Until then, calls return HTTP 403 `customer_verification_required`. Jermes treats that like any other failure and leaves Hermes unchanged.

To call TypeSafe directly instead, set `backend.name: typesafe` and `TYPESAFE_API_KEY`. Both backends use the same TypeSafe wire format (`POST /v1/systemone`).

Verify access:

```bash
hermes jermes check     # one live Jev call with a harmless sample
hermes jermes status    # backend, key, per-point modes
hermes jermes stats     # decisions, cache hits, latency, tokens per point
hermes jermes recent    # last decisions
```

## Configure

Optional file at `$HERMES_HOME/jermes/config.yaml`. Anything you leave out uses the defaults in [`jermes/config.py`](jermes/config.py).

```yaml
backend:
  name: vercel            # or: typesafe
  deadline_s: 2.5
  zero_data_retention: true

points:
  risk_gate:     { mode: enforce, block_threshold: 0.85, review_threshold: 0.5 }
  result_filter: { mode: enforce, keep_threshold: 0.35 }
  loop_guard:    { mode: advise }
  skill_suggest: { mode: advise }
  model_router:  { mode: shadow, cheap_model: "anthropic/claude-haiku-4-5" }
```

Modes: `off` → `shadow` (log only) → `advise` (notes and suggestions) → `enforce` (may block, filter or reroute).

`JERMES_MODE=off` is a global kill switch.

## Develop

```bash
uv venv && uv pip install -e '.[dev]'
pytest                                  # 46 offline tests; Jev is faked at the HTTP layer
HERMES_AGENT_DIR=~/hermes-agent pytest  # also runs the end-to-end test against a real Hermes checkout
```

The end-to-end test loads Jermes through Hermes' real `PluginManager` in a temporary `HERMES_HOME`, enables it, and fires `pre_tool_call` through Hermes' own dispatch. It checks that a dangerous call is blocked and a benign one passes. It passes against Hermes `origin/main` at `9514d35`.

## Roadmap (from the PRD)

- [x] Phase 0: client, cache, log, modes, and five decision points in shadow mode
- [ ] Label shadow logs from real sessions; tune thresholds per point
- [ ] Phase 1 to 2: promote `result_filter`, `skill_suggest`, `risk_gate`
- [ ] Data-ingestion pipeline (PRD §6): intake, triage, select-don't-generate extraction, verify-then-escalate cascade
- [ ] Workstream 3 extras (PRD §7): gateway triage, cron wake gating, memory filter, citation checks
- [ ] Cross-provider routing via `llm_execution` middleware
- [ ] Upstream proposal: a generic turn-resolution hook for the D2 fast path

## License

MIT
