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

> **Status: v0.4, Phase 0 (shadow mode).** Every decision point ships in `shadow`. Jermes calls Jev and logs what it *would* do, but changes nothing in Hermes until you promote a point.
>
> Latest measurements (details in [Test results](#test-results)):
> - **Ingestion:** on 16 SEC 10-K filings not used during development, the Jev pipeline matched a strong model on every value (94/94) at **11x lower cost**, sending 94% fewer tokens to the strong model.
> - **Skill selection:** on 40 hand-labelled real turns, Jev names a correct first skill 72% of the time; the agent on its own did so 7% of the time.
> - **Tool-result filtering:** on tasks that read long files, it cut agent cost by 33% on one task and cost 5% more on the other.

## Quick start

**1. Install Hermes Agent** if you don't have it: see the [Hermes Agent install guide](https://github.com/NousResearch/hermes-agent#quick-install).

**2. Install the plugin:**

```bash
hermes plugins install MersivMedia/jermes --enable
```

`MersivMedia/jermes` is shorthand for `https://github.com/MersivMedia/jermes`; either form works. Hermes scans every plugin before installing it and shows the result.

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
2. Add a credit card to the same team: [AI Gateway card prompt](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card). Upgrading the team plan alone did not clear the 403 in our testing; this prompt did. Vercel launched Jev as free until Sept 25, 2026; the card is required either way.
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
| `ingest` | I2 to I9 | `hermes jermes ingest` (explicit, not a hook) | Extracts fields from documents: code finds candidates, Jev picks, code copies. Only flagged fields reach a strong model. See [Data ingestion](#data-ingestion). |

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

The skill list is read on every call through Hermes' own skill discovery, so Jev sees exactly what the agent can load: a skill created mid-session is picked up on the next request, and disabled skills are never offered. Hermes caches that scan until a skill directory changes, so re-reading it takes under a millisecond.

Jev sees the latest request plus the last ten user and assistant messages (400 characters each, about 1,000 tokens; tool output and harness notes are stripped), so follow-ups like "yes, do the invert test next" can be resolved. The request stays primary: the questions tell Jev to use the earlier turns only to work out what the request refers to.

### Data ingestion

`hermes jermes ingest` pulls structured fields out of documents without letting a model write any value:

1. **Triage** (one Jev request): in scope, readable, language, document type. Out-of-scope documents stop here.
2. **Screen**: four questions per chunk (relevant; contains an instruction aimed at the processor; contradicts a stated premise). Chunks with planted instructions are blanked, so they can't supply a value.
3. **Find candidates** in code: regular expressions tuned to over-find dates, amounts, numbers, IDs, jurisdictions, emails and so on, from the relevant chunks first.
4. **Select** (one Jev request): per field, a Choice over the candidate IDs plus "not stated".
5. **Verify** (one Jev request): per field, "is this the wrong value?" and "does it come from an unrelated passage?"
6. **Escalate**: only fields where a check fires (the highest flag, not the average) go to a strong model, in one call per document. Its answer must also appear verbatim in the document, or the field goes to review.
7. **Classify** (optional two-level taxonomy): low confidence reports the parent category instead of guessing.

Every accepted value is an exact span of the document, copied and normalised by code, so it can't be invented or have a digit transposed. Each record keeps Jev's answers, probabilities, the route each chunk took, and why it ended up accepted, in review, or rejected.

```bash
hermes jermes ingest --schema invoice.yaml docs/*.txt --json out.json
hermes jermes ingest --bench bench_dir --cheap anthropic:claude-haiku-4-5   # compare against models reading every document
```

A schema is a small YAML file:

```yaml
name: invoice
scope: supplier invoices
fields:
  - {name: invoice_date, question: "What is the invoice date?", kind: date, required: true}
  - {name: total, question: "What is the total amount due?", kind: money, required: true}
  - {name: vendor_tax_id, question: "What is the vendor's tax ID?", kind: code}
```

Candidate kinds: `date`, `money`, `number`, `percent`, `size`, `code`, `email`, `phone`, `url`, `jurisdiction`, `name`. The strong model for escalation defaults to `anthropic:claude-sonnet-4-5` (needs `ANTHROPIC_API_KEY`); any OpenAI-compatible gateway model ID also works via `--strong`.

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
hermes jermes label     # hand-label real turns: which skills should have loaded
hermes jermes score     # score Jev and the agent against your labels
hermes jermes ingest    # extract fields from documents; --bench compares against models
hermes jermes savings   # estimate tokens and dollars Jermes would have saved on your sessions
hermes jermes ab        # run fixed tasks with Jermes off and on; compare Hermes' own costs
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

Replay is offline, so it paces requests to stay under the gateway's rate limit (2.1 s apart by default, `--interval` to change) and waits out 429s and 503s instead of failing. A 50-turn run takes 5 to 15 minutes and costs about $0.02 (roughly 10k input tokens per turn with about 200 skills). Repeat runs are free because decisions are cached.

For live shadow mode, use Hermes normally. Every point logs what it would have done, and `hermes jermes stats` / `recent` show the results.

## Score the results

Comparing Jev with what the agent loaded only shows agreement. To measure *correctness* you need an answer key: a set of real turns where you have written down which skills should have loaded. About 40 labelled turns is enough to compare settings. 100 or more gives numbers you can tune thresholds against.

**Label in the terminal**, one turn at a time. Each turn shows the request, the conversation just before it, what the agent loaded and what Jev lists:

```bash
hermes jermes label -n 40
#   enter = accept Jev's list    a = accept what the agent loaded    n = no skill needed
#   or type skill names, comma-separated    s = skip    q = quit (resume any time)
```

**Or label in a spreadsheet** (easier on a phone):

```bash
hermes jermes label -n 40 --export labels.csv     # fill in the correct_skills column
hermes jermes label --import labels.csv           # or --import gdrive:<sheet-id> for a Google Sheet
```

Skill names are checked on import, and typos come back with suggestions. Labels are stored in `$HERMES_HOME/jermes/labels.jsonl`; relabelling a turn replaces the old answer.

**Score:**

```bash
hermes jermes score
```

The score reruns Jev on every labelled turn (cached decisions are free) and reports each metric for Jev and for what the agent actually loaded:

| Metric | Meaning |
|---|---|
| Decision accuracy | Got "skill vs no skill" right |
| Primary hit | Jev's first skill is one of the correct ones |
| Recall | Share of the correct skills that appear in the list |
| Precision | Share of the listed skills that were correct |
| False alarms | Listed skills on a turn that needed none |
| Misses | Said "no skill" on a turn that needed one |

After changing a setting (context size, threshold), run `score` again on the same labels to see what moved.

### Improving accuracy

The biggest lever is the skills' own descriptions, not Jermes' settings. Jev's first call only sees each skill's name and one-line description. If a description covers half of what the skill does, Jev will miss the other half.

1. Run `hermes jermes score` and look at the "Jev got N wrong" list.
2. When one skill keeps being missed, open its `SKILL.md` and its scripts and check what it *actually* does.
3. Rewrite the `description` (and the "When to Use" triggers) to cover those things. Don't describe what the skill can't do.
4. Run `score` again on the same labels.

Example: `runpod-pods` was described as "Rent RunPod GPUs for models too big to run locally." Its script also lists, stops and terminates pods, so requests like "turn off the gpu instance now" were going to other GPU skills. Rewriting the description to cover those fixed 4 of the 40 labelled turns (see [docs/RESULTS.md](docs/RESULTS.md)).

The skill list is read live, so a description change takes effect on the next request. No restart is needed.

## Test results

Latest results for each area. Earlier measurements and what each change did are in [docs/RESULTS.md](docs/RESULTS.md).

### Data ingestion: SEC 10-K cover pages (September 25, 2026)

16 real 10-K filings, fetched from EDGAR, that were never used while building the candidate finders or writing the field questions. Six fields per filing: state of incorporation, tax ID, fiscal year end, SEC file number, shares outstanding, public float. The correct values come from SEC's own structured data, not from reading the documents. Values that don't appear in the document text are left out of scoring (2 of 96), so 94 values are scored. Each document is the first 25,000 characters of the filing, so the cover values sit among real business text and dozens of other numbers.

| | Correct | Wrong | Cost | Strong-model input tokens |
|---|---|---|---|---|
| **Jev pipeline** (Sonnet 4.5 only for flagged fields) | 94/94 | 0 | **$0.027** | 5,319 |
| Claude Sonnet 4.5 reads every document | 94/94 | 0 | $0.298 | 91,737 |
| Claude Haiku 4.5 reads every document | 94/94 | 0 | $0.099 | 91,737 |

Jev's own cost is included in the pipeline's figure ($0.010 for 64 requests). One field in 96 was escalated to the strong model.

What this does and doesn't show:

- **Cost:** the pipeline reaches the same answers for about a tenth of the strong model's cost, and about a quarter of the cheap model's.
- **Accuracy:** these fields were too easy to separate the three approaches, since all of them scored perfectly. A harder benchmark (scanned documents, ambiguous fields) is needed before claiming the pipeline is as accurate in general.
- **Speed:** about 34 seconds per document, because the pipeline makes four Jev requests in sequence under the Vercel rate limit. Fine for batch ingestion, too slow for interactive use.

Rebuild the benchmark with `python scripts/build_sec_bench.py DIR 20` (development set) or `... DIR 16 --heldout`.

### Tool-result filtering: task-matched A/B (September 25, 2026)

`hermes jermes ab` runs a fixed task set through real Hermes twice, with Jermes off and on, in isolated Hermes homes, and reads token counts from each run's own session record. Tasks write their own files and check the answer automatically. Model: Claude Opus 5.5.

Two tasks where the agent must read a long file in full, 3 runs per arm:

| Task | Off | On | Change | Correct (off/on) |
|---|---|---|---|---|
| Meeting transcript (74k chars; decisions change during the meeting) | $0.307 | $0.206 | **−33%** | 3/3, 3/3 |
| Release notes (48k chars; one breaking change, no keyword to search for) | $0.260 | $0.272 | +5% | 3/3, 3/3 |

On the release notes, Jev kept only the section with the answer (scored 0.82; the next highest 0.28). But the agent read the note saying the rest had been cut, didn't trust it, and searched the file itself, which took extra calls. The filter only saves tokens when the agent accepts what it's given.

On six broader tasks (log search, JSON lookup, a spreadsheet conversion, a short code fix, a chat answer), 3 runs each, Jermes-on cost 1% more, within run-to-run noise. The agent searched big files instead of reading them whole, so the filter rarely had anything to trim. On the spreadsheet task, a suggested skill made the agent do more work.

### Offline savings estimate over real sessions

`hermes jermes savings` replays the filter's real decisions over past sessions and counts how often each trimmed result would have been re-read. On one install (64 sessions, about $950 of model spend), trimming big tool results would have removed about 25M prompt tokens (2.4%), worth about $11.60, for $0.03 of Jev calls. Three sessions account for 89% of that, so the saving depends heavily on how an install is used. It can't see changes in agent behaviour; the A/B above does.

### Skill selection, scored against hand labels (September 25, 2026)

40 real turns from one Hermes install (about 200 skills), labelled by hand with the skills that should have loaded: 29 turns needed skills (2.9 on average), 11 needed none. Current defaults: 10 messages of context, rewritten `runpod-pods` description.

| Metric | Jev | What the agent loaded |
|---|---|---|
| Decision accuracy (skill vs no skill) | **88%** | 35% |
| Primary hit (first skill correct) | **72%** | 7% |
| Recall (needed skills listed) | **61%** | 4% |
| Precision (listed skills needed) | 65% | 75% |
| False alarms (skills on a "none" turn) | 9% (1 of 11) | 0% |
| Misses ("none" on a turn needing a skill) | **14%** | 90% |
| Skills listed per turn | 2.0 | 0.1 |

The agent column is low mostly because this install rarely called `skill_view` in these sessions. Its precision is high because on the few turns it did load a skill, it was usually right.

Caveats:

- **40 turns is small.** A difference of one turn moves some rows by 3 to 9 points.
- **The labels may lean toward Jev's old answers.** They were filled in on a sheet that showed Jev's 4-message list, and 26 of 40 matched it exactly. A blind batch would settle how much this matters.
- **The skill list changed during testing.** A new skill (`job-interview-company-prep`) was created mid-session and now ranks first on two job-interview turns whose labels predate it. Those two count as wrong here.
- **Suggestions aren't free.** In the A/B above, a suggested skill made the agent do more work on a simple task. Skill selection saves tokens only when it prevents unneeded loads.

## Known issues and limits

- **Vercel rate limits.** A new Vercel account allowed 30 requests per window. Skill selection makes two calls per request. Normal use, with pauses while you read replies, should stay under that; back-to-back automation will not.
- **Vercel 503s.** During testing, up to about 20% of calls got a temporary 503 from the gateway. Live hooks fail open (Hermes carries on unchanged); replay retries.
- **OpenRouter is untested live.** The request format matches OpenRouter's documentation and is covered by tests, but no live call has been made with an OpenRouter key yet.
- **Live latency hasn't been measured.** Single calls took 0.8 to 2 s. Timings recorded during replay include deliberate waits, so they don't reflect live use.
- **Scored so far:** skill selection (hand labels), `result_filter` (A/B cost, mixed results), and ingestion (SEC benchmark). `risk_gate`, `loop_guard` and `model_router` are built and tested offline but have no measured results yet.
- **Agents may not trust filtered results.** When `result_filter` trims a file, the agent sometimes re-reads or searches it anyway, which costs more than not filtering.
- **Ingestion is sequential and slow** (about 34 s per document on Vercel), and its accuracy has only been measured on clean, typed text.
- **Vercel blocks paid models on free-tier accounts.** The ingestion baselines and escalation therefore use `ANTHROPIC_API_KEY` directly.
- **Hermes' price table is missing Claude Opus 5 and 5.5**, so its own cost estimates for those models are far too low. `hermes jermes savings` uses Anthropic's published prices instead.

## Configure

Optional file at `$HERMES_HOME/jermes/config.yaml`. Anything you leave out uses the defaults in [`jermes/config.py`](jermes/config.py).

```yaml
backend:
  name: auto              # auto | vercel | openrouter | typesafe
  deadline_s: 2.5
  zero_data_retention: true   # Vercel only

points:
  skill_suggest: { mode: advise, fits_threshold: 0.5, max_listed: 4, context_messages: 10 }
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
pytest                                  # 127 tests, offline; Jev is faked at the HTTP layer
HERMES_AGENT_DIR=~/hermes-agent pytest  # also runs the end-to-end test against a real Hermes checkout
```

The end-to-end tests run against a real Hermes checkout in a temporary `HERMES_HOME`:

- load Jermes through Hermes' `PluginManager`, fire `pre_tool_call` through Hermes' own dispatch, and check a dangerous call is blocked and a benign one passes
- create a skill mid-session and check the very next Jev call sees it
- check disabled skills are never offered

## Roadmap (from the PRD)

- [x] Phase 0: client, cache, log, modes, and five decision points in shadow mode
- [x] Replay harness over real sessions; first live measurements
- [x] Skill selection v3: list output, "no skill needed" option, conversation context
- [x] Skill list read on every call (new skills seen mid-session); hand-labelling and scoring
- [x] First 40 hand labels; context set to 10 messages; first skill-description fix
- [ ] Blind labelling batch (Jev's answer hidden) and 100+ labels; tune `fits_threshold`
- [ ] Live latency test; OpenRouter live test
- [x] Token-savings measurement: offline estimate over real sessions and task-matched A/B through real Hermes
- [x] Data-ingestion pipeline (PRD §6): triage, screening, select-don't-generate extraction, verify-then-escalate; SEC benchmark
- [ ] Make filtered results trustworthy to the agent (wording, or a way to fetch omitted sections)
- [ ] Harder ingestion benchmark (scanned documents, ambiguous fields); request intake (I1); parallel Jev requests
- [ ] Label and score `risk_gate`, `loop_guard`
- [ ] Phase 1 to 2: promote `result_filter`, `skill_suggest`, `risk_gate`
- [ ] Workstream 3 extras (PRD §7): gateway triage, cron wake gating, memory filter, citation checks
- [ ] Cross-provider routing via `llm_execution` middleware

## License

MIT
