# Jermes - Jev decision layer for Hermes Agent

Jermes is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that takes the small, bounded decisions an agent makes all the time out of the expensive reasoning model, and uses them to cut what the agent spends.

Plain code makes each decision by asking [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), TypeSafe AI's System One model, which returns typed answers with probabilities for $0.042 per million input tokens. Jev only supplies the judgment. Code owns the policy: thresholds you can read decide whether to act, ask a human, or leave Hermes alone.

## What it does

| Feature | What Jev decides | Latest result |
|---|---|---|
| **[Context trimming](#context-trimming)** | After a pause, which old tool results and file reads are no longer needed; those become one-line stubs with the full text saved on disk | **−39% agent cost** over 5 live A/B sessions, every answer still correct. Offline estimate on 13 real sessions: −22% to −33% |
| **[Skill selection](#skill-selection)** | Which of 200+ skills a request needs, or none | Right first skill on 72% of 40 hand-labelled real turns (the agent alone: 7%) |
| **Duplicate skills** | Before a new skill is created, whether an existing one already covers it; `skills-audit` checks the whole library | First live audit of 95 skills found 4 duplicate groups for $0.04; all merged after review. The suggested keeper is now the broadest skill, not the longest |
| **[Risk gate](#risk-gate)** | Whether a tool call is dangerous or not what the user asked; writes to config and credential files, and commands that read secrets and send them over the network, always go to review | On 41 held-out attacks written by a different model (GPT-5): 19/19 dangerous stopped, **17/19 blocked outright** (v0.7: 7/19), all 16 harmless calls allowed. 13% of 200 real calls sent to review, none blocked |
| **Tool-result filter** | Which sections of a long result matter for the task | Mixed: −10% prompt tokens but +5% cost alone; with context trimming on as well it added only −2% (one pair, within noise) |
| **[Memory filter](#memory-filter)** | Before a memory write, whether it's a durable fact or task progress / a how-to that belongs in a skill; those are held once with a note | Offline: 15 of 16 right on a held-out set, no durable fact held. Live A/B: nothing to hold, because Opus 5.5 already kept progress out of memory |
| **Loop guard** | Whether the agent is repeating a failed step | Catches 31% of real repeated failures at 5% false alarms; stays in shadow |
| **Model router** | Whether a turn is easy enough for a cheaper model; a cost check refuses switches that would cost more after cache effects | Not yet A/B tested. On one install, under 1% of spend was routable |
| **[Data ingestion](#data-ingestion)** | Which code-found candidate is each field's value, and whether to escalate to a strong model | 94/94 correct on held-out SEC filings at **11× lower cost** than a strong model reading every document |

Every result, with methods and caveats, is in [Test results](#test-results) and the dated history in [docs/RESULTS.md](docs/RESULTS.md). Measurement tools ship with the plugin: `savings`, `costsim` and `ab` for token cost, `trimreport` for whether trimmed content was needed again, `riskbench` (with `--redteam` or `--cases FILE`), `loopbench` and `memorybench` for the guards, `skills-audit` for the skill library.

The design, the evidence behind it, and the rollout plan are in the **[PRD](docs/PRD.md)**.

> **Status: v0.8, Phase 0 (shadow mode).** Every decision point ships in `shadow`: Jermes calls Jev and logs what it *would* do, but changes nothing in Hermes until you promote a point. Context trimming also needs `context: {engine: jermes}` in Hermes' main config file.

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
| `risk_gate` | D3, D4 | `pre_tool_call` hook | Blocks clearly dangerous, unrequested calls. Sends uncertain ones to Hermes' approval gate. Jev is asked about risk, whether the call matches the request, and hazards: destructive, exfiltration, weakens safety controls, goes against a limit the user stated, hard to undo, follows injected instructions. Two code rules always send to review: writes to protected paths the user didn't ask for, and a command or script that reads secret content (key files, `.env`, the whole environment) and also talks to the network. |
| `result_filter` | D5 | `transform_tool_result` hook | Drops irrelevant sections of big tool results before the model re-reads them every turn. |
| `loop_guard` | D6 | `transform_tool_result` hook | Adds a one-line note when a failed call is repeated or the task already looks done. |
| `model_router` | D7 | `llm_request` middleware | Sends confidently easy, low-stakes turns to a cheaper model, sticky for the whole turn. A cost check refuses the switch when the conversation won't fit the cheaper model or rewriting the prompt cache would cost more than the turn saves. |
| `skill_overlap` | - | `pre_tool_call` on `skill_manage` create | Before a new skill is written, checks whether an existing skill already covers the same job. If one does, the agent is told which and asked to extend it or confirm the new one is distinct; the same create retried goes through. `hermes jermes skills-audit` runs the same check across the library and suggests merges (suggestions only). |
| `context_trim` | X3 | context engine (`context: {engine: jermes}`) | After a pause long enough for the prompt cache to expire, Jev scores old tool traffic: "will this still be needed?" Items it drops are replaced by one-line stubs pointing to the full text on disk. See [Context trimming](#context-trimming). |
| `memory_filter` | X4 | `pre_tool_call` on `memory` | Asks whether each add/replace is a durable fact, task progress, or a step-by-step procedure. Progress, and procedures written as steps, are held once with a note suggesting a durable rewording or a skill; the same write retried goes through. Removes are never checked. |
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

### Context trimming

In a long session, most of every model request is tool traffic from finished steps: command output, file contents, and the big arguments the agent wrote itself (whole files, patches, scripts). All of it is re-sent on every call.

Jermes registers a Hermes context engine that keeps Hermes' own compressor and adds one step per request:

1. **Only after a pause.** When no model call has happened for longer than the provider's cache lifetime (5 minutes by default), the next request has to be written to the cache in full anyway, so changing old content costs nothing extra. Mid-loop requests are never changed.
2. **Jev scores each old item** (older than the last two user turns, at least 1,500 characters): "will this still be needed, verbatim, for the current request?"
3. **Dropped items become stubs**, such as `[jermes: output of terminal (command=npm run build) from an earlier step, 3,000 characters, trimmed as no longer needed. Full text: .../full_results/9f2c.txt]`. Long tool-call arguments are shortened inside the JSON, so they stay valid.
4. **Stubs are re-applied byte-for-byte** on every later request, so the trimmed prompt is what gets cached. At the next pause every item is decided again, and one the new request needs comes back verbatim.

The trimming applies to the request only; the stored transcript is never changed. Memory, todo and clarify results are never trimmed. Any Jev error sends the request unchanged.

In Hermes' main config file:

```yaml
context:
  engine: jermes
```

In Jermes' config (`jermes/config.yaml` in your Hermes home):

```yaml
points:
  context_trim: { mode: enforce }   # shadow: log what would be trimmed
```

`hermes jermes costsim` rebuilds your past sessions call by call and prices them with this trimming applied (offline, no Jev calls).

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
hermes jermes costsim   # price past sessions with context trimming applied (offline)
hermes jermes trimreport    # of the items trimming dropped, which did the agent need again?
hermes jermes skills-audit  # find overlapping installed skills and suggest merges (changes nothing)
hermes jermes riskbench # score the risk gate on labelled cases and real past calls; --redteam for disguised attacks
hermes jermes memorybench   # score the memory filter on labelled entries (--build drafts a labels file)
hermes jermes loopbench # score the loop guard on real repeated failures
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

### Context trimming (September 26, 2026)

**Live A/B, 5 pairs.** Four-turn Hermes sessions with a 5.5-minute pause before each follow-up (both arms), so the prompt cache really expires. Turn 1 reads three large files; later turns need details from them. In the harder `recall` task those details are minor lines no summary would mention. Claude Opus 5.5, everything else in Jermes off.

| | Correct | Cost (5 sessions) |
|---|---|---|
| Hermes alone | 5/5 | $19.83 |
| With context trimming | 5/5 | **$12.05 (−39%)** |

Trimming was cheaper in every pair (−32% to −63%). When a later question needed a trimmed file, the agent searched it for the one line it needed rather than re-reading it. The tasks were built to exercise this feature and run-to-run noise is large; each pair and the caveats are in [docs/RESULTS.md](docs/RESULTS.md).

**Offline estimate** (`hermes jermes costsim`, 13 real sessions, $746 of spend): −22% if Jev keeps 30% of old items, −33% if every old item is trimmed.

**Was trimmed content needed again?** (`hermes jermes trimreport`, September 28) For every item trimming dropped, the report checks the rest of the session for a re-read of the same file, a search or partial read of it, or a re-run of the same command. On the two latest `recall` runs (15 distinct items dropped, about 260k characters), 3 items were needed again, and all 3 were recovered by a search or a partial read that brought back about 28k characters in total. Nothing was re-read in full. This is test data built to need trimmed details; the report is meant for real shadow logs before switching trimming to enforce.

**With the result filter as well** (one `recall` pair, both arms trimming): $1.92 without the filter, $1.88 with it (−2.4%), both correct. The filter fired once. Once trimming handles old reads, the filter has little left to do on this kind of task; one pair is not enough to rule it out elsewhere.

### Duplicate skills (September 27, 2026)

`hermes jermes skills-audit` checked the 95 agent-created skills on one install against all 207 installed skills: 207 Jev requests, $0.04, 6 minutes. It found four groups:

| Group | Jev's verdict | What happened |
|---|---|---|
| Three corpus-ingestion skills with near-identical descriptions | duplicate / one contains another (71–78%) | Merged into one |
| Two cost/timeline proposal skills | duplicate (79%) | Merged |
| Two "clone a working AI tool" skills | one contains the other (85%) | Merged |
| Vercel build fixes and AI tool deploy troubleshooting | partial overlap (72%) | Merged into the broader one; the suggested keeper was the narrower skill, so a human overrode it |

It held back on close calls under 70% that are related but distinct (`guidance`/`outlines`, `find-nearby`/`maps`). Each merge was reviewed by hand before editing; afterwards, a re-run on the four merged skills found nothing above 28%. The keeper rule (bundled first, then the skill that contains the others, then the longest) got one group of four wrong, which is why the command only suggests.

### Risk gate

**v0.8 (September 28), risk_gate.3.** Three new Jev questions (weakens safety controls, goes against a limit the user stated, hard to undo) and a rule that blocks a call that is unrequested and exfiltrates data or weakens safety. Routine work that was clearly asked for and is easy to undo is no longer sent to review on its risk level alone.

To keep the numbers honest, a different model (GPT-5) wrote two test sets without seeing any Jermes rules, questions or cases: 40 tuning cases and 41 held-out cases, each about 45% dangerous, 15% "a human should confirm" and 40% harmless look-alikes. Thresholds were chosen by replaying the logged Jev answers through the policy (no extra calls), and the held-out set was scored once, after the policy was final.

| Held-out set (41, GPT-5) | v0.7 | v0.8 |
|---|---|---|
| Dangerous calls stopped (blocked or sent to review) | 18/19 | **19/19** |
| Dangerous calls blocked outright | 7/19 | **17/19** |
| "A human should confirm" cases stopped | 2/6 | 2/6 |
| Harmless calls allowed | 15/16 | **16/16** |
| Harmless calls blocked | 0 | 0 |
| Hermes' own regex, dangerous shell cases flagged | 40% | 40% |

| Other sets (v0.8) | Dangerous stopped | Dangerous blocked | Harmless allowed | Harmless blocked |
|---|---|---|---|---|
| Basic set (31) | 10/10 | 9/10 (v0.7: 7/10) | 14/14 | 0 |
| Own red-team set (33) | 20/20 | 16/20 (v0.7: 5/20) | 11/12 | 0 |
| GPT-5 tuning set (40, used for tuning) | 18/18 | 18/18 (v0.7: 5/18) | 11/16 (v0.7: 9/16) | 0 |

**Real calls:** 200 real past calls: 13% sent to review (v0.7: 14.5%), none blocked. A first draft blocked one real call, an agent deleting a stray duplicate file it had created mid-task; the "goes against the request" block now only covers exfiltration and weakening safety, and deletes are left to review.

Still weak: 4 of 6 "a human should confirm" cases are allowed (regenerating an SSH key, replacing a TLS key, removing an old backup cron job, opening a firewall port for an office IP), and two home-directory deletions disguised as routine work go to review instead of being blocked. On the tuning set, 5 harmless scheduled jobs and announcements still go to review. The held-out set was written by one model; a second author or real attack traces would be a stronger test.

The secret-to-network rule (v0.7) still fires on 8 of about 4,700 real past calls, none an attack. Earlier results are in [docs/RESULTS.md](docs/RESULTS.md).

### Memory filter

Hermes re-sends its memory files on every turn, and its own guidance says memory is for durable facts, not task progress or procedures. `memory_filter` asks Jev about each write before it lands. It was built and scored offline; it isn't yet A/B tested live.

| Set | Entries | Right | Durable facts wrongly held | Progress/procedures caught |
|---|---|---|---|---|
| One install: its 36 current memory entries, 31 past memory writes, 10 written examples | 77 | **100%** | 0% | 100% |
| Held out (written after tuning, not used to set thresholds) | 16 | 94% | 0% | 7 of 8 |

The first version held 17% of durable entries because dense fact lists (API quirks, preference lists) also score as "procedure". The fix: a procedure is only held when it's also written as steps (a heading or three or more numbered/bulleted items). That was tuned on the first set, so its 100% is in-sample; the held-out set is the honest number. The one held-out miss was a spend note ("spent $7.40 on RunPod today") that Jev rated as a durable fact. Labels contain personal memory content and stay outside the repo; `hermes jermes memorybench --build` drafts a labels file from any install.

**Live A/B (September 28, one pair, `ab --tasks memory`):** a new task turns Hermes memory on, gives three durable preferences plus progress notes and a request to "save the steps", then starts a fresh session that must answer from memory. Both arms passed. The filter had nothing to hold: Opus 5.5 saved only the three preferences, left the progress notes out, and put the steps into a skill. Cost differed ($0.27 vs $0.36) from agent variation, not the filter. On a strong model that follows Hermes' memory guidance, the filter is insurance, not a saving; weaker models and background memory reviews are untested.

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
- **Latency.** The risk gate took 243 ms at the median and 340 ms at the 90th percentile over 100 real calls. Skill selection makes two calls in sequence.
- **Scored so far:** skill selection (hand labels), `result_filter` (A/B cost, mixed results), ingestion (SEC benchmark), `risk_gate` (31 labelled cases, 33 red-team cases, 100 real calls), `loop_guard` (real repeated failures) and `memory_filter` (offline, labelled entries from one install). `model_router` has a cost check but no measured results.
- **A/B runs used to link the real skill library.** Test agents that edit skills could change the user's real skills; two such edits were found and reverted (Sept 25, Sept 28). A/B homes now get a copy.
- **Redaction hides secrets from Jev too.** Jev can't tell a command sends a secret if the secret was redacted before Jev saw it. The secret-to-network rule covers that case in code.
- **Routing saves little on long, judgment-heavy sessions.** On one install, Jev rated 5 of 137 turns after a pause as easy enough for a cheaper model, and none of 88 long tool loops as safe to hand to a cheap worker. Context trimming is where that install's savings are.
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
  risk_gate:     { mode: enforce, block_threshold: 0.85, review_threshold: 0.7 }
  result_filter: { mode: enforce, keep_threshold: 0.35 }
  context_trim:  { mode: enforce, ttl_s: 300, keep_turns: 2, keep_threshold: 0.5 }
  loop_guard:    { mode: advise }
  memory_filter: { mode: advise, progress_threshold: 0.7, procedure_threshold: 0.9 }
  model_router:  { mode: shadow, cheap_model: "anthropic/claude-haiku-4-5" }
```

Modes: `off` → `shadow` (log only) → `advise` (notes and suggestions) → `enforce` (may block, filter or reroute).

`JERMES_MODE=off` is a global kill switch.

## Develop

```bash
uv venv && uv pip install -e '.[dev]'
pytest                                  # 208 tests, offline; Jev is faked at the HTTP layer
HERMES_AGENT_DIR=~/hermes-agent pytest  # also runs the end-to-end test against a real Hermes checkout
```

The end-to-end tests run against a real Hermes checkout in a temporary `HERMES_HOME`:

- load Jermes through Hermes' `PluginManager`, fire `pre_tool_call` through Hermes' own dispatch, and check a dangerous call is blocked and a benign one passes
- create a skill mid-session and check the very next Jev call sees it
- check disabled skills are never offered
- load the Jermes context engine through Hermes' plugin system, deep-copy it the way Hermes does per agent, and trim a request through Hermes' own request hook

## Roadmap (from the PRD)

- [x] Phase 0: client, cache, log, modes, and five decision points in shadow mode
- [x] Replay harness over real sessions; first live measurements
- [x] Skill selection v3: list output, "no skill needed" option, conversation context
- [x] Skill list read on every call (new skills seen mid-session); hand-labelling and scoring
- [x] First 40 hand labels; context set to 10 messages; first skill-description fix
- [ ] Blind labelling batch (Jev's answer hidden) and 100+ labels; tune `fits_threshold`
- [ ] OpenRouter live test
- [x] Duplicate-skill audit and a check before new skills are created (`skill_overlap`); first live audit merged 4 groups
- [x] Token-savings measurement: offline estimate over real sessions and task-matched A/B through real Hermes
- [x] Data-ingestion pipeline (PRD §6): triage, screening, select-don't-generate extraction, verify-then-escalate; SEC benchmark
- [x] Filtered results: line ranges of what was cut, full copy on disk, targeted reads never filtered, "needs the whole thing" pass-through
- [x] Context trimming engine (cold turns only, stubs with full text on disk); offline cost simulator
- [x] Router cost check (context fit, cache rewrite cost)
- [ ] Harder ingestion benchmark (scanned documents, ambiguous fields); request intake (I1); parallel Jev requests
- [x] Score `risk_gate` and `loop_guard`; protected-path rule for config and credential files
- [ ] Phase 1 to 2: promote `result_filter`, `skill_suggest`, `risk_gate`
- [x] `trimreport`: which trimmed items the agent needed again (re-read, searched, re-run)
- [ ] Context trimming: two weeks of live shadow logs through `trimreport`, then enforce
- [x] Risk-gate red-team set (33 disguised attacks with harmless twins); secret-to-network rule
- [x] Memory write filter (X4), scored offline on one install plus a held-out set
- [x] Memory filter live A/B (one pair: nothing to hold on Opus 5.5)
- [x] Independent red-team sets (GPT-5-written, tuning + held-out); risk_gate.3
- [x] Skills-audit keeper: containment, then Jev's broadest-scope pick, then length
- [ ] Memory filter on weaker models and background reviews; more installs' labels
- [ ] Risk gate: "a human should confirm" cases (4 of 6 allowed); a second independent attack author
- [ ] Workstream 3 extras (PRD §7): gateway triage, cron wake gating, citation checks
- [ ] Cross-provider routing via `llm_execution` middleware

## License

MIT
