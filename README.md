# Jermes - Jev decision layer for Hermes Agent

Jermes is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that takes the small, bounded decisions an agent makes all the time out of the expensive reasoning model:

- Which of my 200 skills does this request need?
- Is this tool call dangerous?
- Which parts of this web page matter?
- Is the agent going in circles?
- Does this turn even need the big model?

Plain code makes each of those decisions by consulting [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), TypeSafe AI's System One model. Jev returns typed answers with probabilities for $0.042 per million input tokens.

Jev only supplies the judgment. Code owns the policy: thresholds you can read decide whether to act, ask a human, or leave Hermes alone.

The design, the evidence behind it, and the rollout plan are in the **[PRD](docs/PRD.md)**.

> **Status: v0.2, Phase 0 (shadow mode).** Every decision point ships in `shadow`. Jermes calls Jev and logs what it *would* do, but changes nothing in Hermes until you promote a point. Measured results so far are in [Test results](#test-results).

## Quick start

**1. Install Hermes Agent** (skip if you have it):

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

**2. Install the plugin:**

```bash
hermes plugins install MersivMedia/jermes --enable
```

**3. Add one Jev key** to `~/.hermes/.env` (pick a provider below). Jermes detects which key is set and uses that provider.

**4. Check it works:**

```bash
hermes jermes status    # shows the provider it picked and whether the key is set
hermes jermes check     # one live Jev call
```

**5. Start Hermes normally.** Every decision point runs in shadow mode: nothing changes in Hermes, and every decision is logged.

```bash
hermes            # classic CLI
hermes --tui      # or the TUI
hermes gateway    # or the messaging gateway
```

Plugins load when Hermes starts, so restart any Hermes session or gateway that was already running.

## Pick a Jev provider

All three speak TypeSafe's System One wire format (`POST /v1/systemone`) and return the same answers. The model is the same; only billing and account setup differ.

| Provider | Key in `~/.hermes/.env` | Model ID | Notes |
|---|---|---|---|
| **Vercel AI Gateway** | `AI_GATEWAY_API_KEY=vck_...` | `typesafe-ai/jev` | Needs a credit card on the Vercel team before it serves any request (HTTP 403 `customer_verification_required` until then). Rate limit seen on a new account: 30 requests / 250k tokens per window. |
| **OpenRouter** | `OPENROUTER_API_KEY=sk-or-...` | `typesafe/jev-1.13` | Uses OpenRouter's [System One API](https://openrouter.ai/docs/guides/community/typesafe-sdk). Jev is not listed in OpenRouter's chat model catalog; that is expected. |
| **TypeSafe direct** | `TYPESAFE_API_KEY=...` | `jev-1.13.0` | TypeSafe's own API. |

**Vercel AI Gateway**

1. In the Vercel dashboard, open your team's AI Gateway page and create an API key.
2. Add a credit card to the same team: [AI Gateway card prompt](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card). Upgrading the team plan alone did not clear the 403 in our testing; this prompt did. Jev is free on the gateway until Sept 25, 2026, but the card is still required.
3. Add the key to `~/.hermes/.env`:

   ```bash
   AI_GATEWAY_API_KEY=vck_...
   ```

**OpenRouter**

1. Create a key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) and add credit to the account.
2. Add it to `~/.hermes/.env`:

   ```bash
   OPENROUTER_API_KEY=sk-or-...
   ```

If you already use OpenRouter for Hermes' main model, the same key works, and Jermes will pick it up with no further setup. If you have more than one key set, Jermes picks in this order: Vercel, TypeSafe, OpenRouter. To force one, set it in the config:

```yaml
# ~/.hermes/jermes/config.yaml
backend:
  name: openrouter   # auto | vercel | openrouter | typesafe
```

`JERMES_BACKEND=openrouter` does the same for a single run.

## How it fits into Hermes

Jermes uses only public Hermes plugin surfaces. It needs no core patches and never touches the system prompt, so prompt caching stays intact.

| Decision point | PRD | Hermes surface | What `enforce`/`advise` does |
|---|---|---|---|
| `skill_suggest` | D1 | `pre_llm_call` hook | Filters 100+ skills down to the few worth loading: a primary skill plus supporting ones, or "no skill needed". Sees the request plus the last few turns of conversation. |
| `risk_gate` | D3, D4 | `pre_tool_call` hook | Blocks clearly dangerous, unrequested calls. Sends uncertain ones to Hermes' approval gate. |
| `result_filter` | D5 | `transform_tool_result` hook | Drops irrelevant sections of big tool results before the model re-reads them every turn. |
| `loop_guard` | D6 | `transform_tool_result` hook | Adds a one-line note when a failed call is repeated or the task already looks done. |
| `model_router` | D7 | `llm_request` middleware | Sends confidently easy, low-stakes turns to a cheaper model, sticky for the whole turn. |

### Skill selection

With 200 skills installed, Hermes' system prompt carries a truncated index of all of them and tells the model to load one whenever in doubt. Jermes asks Jev instead, in two calls:

1. **Skim.** One Choice over every skill (name and short description), plus a **"no skill needed"** option. If that option wins, the agent is told no skill is needed and Jermes stops there.
2. **Select.** The top five from the skim, now with full descriptions and SKILL.md excerpts, plus "no skill needed" again. Jev picks the one most needed and separately answers, for each candidate, "would this skill help with all or part of the request?"

The agent gets a short list: the primary skill first, then any supporting skills that passed their own check.

```
<skill_relevance>
Relevant skills for this request (primary first, then supporting):
1. research-design-documents
2. grounded-citations
Load the ones that apply. Ignore any that do not match what the user actually asked for.
</skill_relevance>
```

Jev sees the latest request plus the last four user and assistant messages (400 characters each; tool output and harness notes are stripped), so follow-ups like "yes, do the invert test next" can be resolved. The request stays primary: the questions tell Jev to use the earlier turns only to work out what the request refers to.

### Guarantees built into the code

- **Deterministic.** Decisions are cached by `(model, canonical state, question spec)`. The same input always produces the same action, even if Jev's sampling drifts.
- **Fails open.** Any error, timeout, 429/503 or missing key leaves Hermes' behaviour unchanged. The live client's deadline (2.5 s) is shorter than Hermes' hook timeout, so a slow Jev can't cause Hermes to fail a `pre_tool_call` *closed*.
- **Never lowers protection.** `risk_gate` sits in front of Hermes' own dangerous-command detector and approval flow. It doesn't replace them.
- **Zero latency in shadow mode.** Shadow decisions run in a background thread.
- **Everything is logged.** SQLite at `$HERMES_HOME/jermes/decisions.sqlite` records answers, probabilities, action, mode, policy version, latency and tokens for every decision.
- **Redacted.** State goes through Hermes' secret redactor, plus a Jermes layer that catches long high-entropy tokens whatever their prefix, before anything leaves the machine.

## Commands

```bash
hermes jermes status    # provider, key, per-point modes
hermes jermes check     # one live Jev call with a harmless sample
hermes jermes rank "make me a pitch deck as a pptx"   # Jev's skill list for one request
hermes jermes replay    # shadow-test over your real past sessions (below)
hermes jermes stats     # decisions, cache hits, latency, tokens per point
hermes jermes recent    # last decisions
```

## Test in shadow mode quickly

You don't need to wait for new sessions. `replay` reads your real past user turns from Hermes' `state.db` (opened read-only), rebuilds the same conversation context the live hook would see, and runs the decision points over them. Nothing touches a live agent.

```bash
hermes jermes replay -n 50                      # skill selection over your last 50 real requests
hermes jermes replay --with-skill -n 40         # only turns where the agent actually loaded a skill
hermes jermes replay --with-skill --no-context  # same, request only (to measure what context adds)
hermes jermes replay --points all -n 30         # skills + risk gate over tool calls that really ran
hermes jermes replay -n 50 --export review.jsonl   # disagreements, ready for labelling
```

The report compares Jev's list with the skill the agent actually loaded in that turn:

- Jev's primary skill is the agent's skill
- the agent's skill appears anywhere in Jev's list
- how often Jev says "no skill needed" when the agent loaded none
- skills listed per turn

What the agent loaded is a weak label: the agent itself picks the wrong skill part of the time. Treat disagreements as things to review, not as Jev errors.

Replay is offline, so it paces requests to stay under the gateway's rate limit (2.1 s apart by default, `--interval` to change) and waits out 429s and 503s instead of failing. A 50-turn run takes 5 to 15 minutes and costs about $0.02 (roughly 10k input tokens per turn with a 206-skill roster). Repeat runs are free because decisions are cached.

For live shadow mode, use Hermes normally. Every point logs what it would have done, and `hermes jermes stats` / `recent` show the results.

## Test results

Live replay results against real sessions are being collected and will be posted here.

## Configure

Optional file at `$HERMES_HOME/jermes/config.yaml`. Anything you leave out uses the defaults in [`jermes/config.py`](jermes/config.py).

```yaml
backend:
  name: auto              # auto | vercel | openrouter | typesafe
  deadline_s: 2.5
  zero_data_retention: true   # Vercel only

points:
  skill_suggest: { mode: advise, fits_threshold: 0.5, max_listed: 4, context_messages: 4 }
  risk_gate:     { mode: enforce, block_threshold: 0.85, review_threshold: 0.5 }
  result_filter: { mode: enforce, keep_threshold: 0.35 }
  loop_guard:    { mode: advise }
  model_router:  { mode: shadow, cheap_model: "anthropic/claude-haiku-4-5" }
```

Modes: `off` → `shadow` (log only) → `advise` (notes and suggestions) → `enforce` (may block, filter or reroute).

`JERMES_MODE=off` is a global kill switch.

## Develop

```bash
uv venv && uv pip install -e '.[dev]'
pytest                                  # offline tests; Jev is faked at the HTTP layer
HERMES_AGENT_DIR=~/hermes-agent pytest  # also runs the end-to-end test against a real Hermes checkout
```

The end-to-end test loads Jermes through Hermes' real `PluginManager` in a temporary `HERMES_HOME`, enables it, and fires `pre_tool_call` through Hermes' own dispatch. It checks that a dangerous call is blocked and a benign one passes.

## Roadmap (from the PRD)

- [x] Phase 0: client, cache, log, modes, and five decision points in shadow mode
- [x] Replay harness over real sessions; first live measurements
- [x] Skill selection v3: list output, "no skill needed" option, conversation context
- [ ] Label shadow logs from real sessions; tune thresholds per point
- [ ] Phase 1 to 2: promote `result_filter`, `skill_suggest`, `risk_gate`
- [ ] Data-ingestion pipeline (PRD §6): intake, triage, select-don't-generate extraction, verify-then-escalate cascade
- [ ] Workstream 3 extras (PRD §7): gateway triage, cron wake gating, memory filter, citation checks
- [ ] Cross-provider routing via `llm_execution` middleware

## License

MIT
