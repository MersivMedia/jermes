# Jermes test results

Every measurement so far, newest first. The [README](../README.md) shows only the current numbers.

Setup for all runs: one real Hermes install (about 200 skills), Jev through Vercel AI Gateway, real past turns replayed from Hermes' `state.db`. Metric definitions are in the README under "Score the results".

## September 26, 2026: context trimming, routing ceiling, risk gate and loop guard

### Context trimming: first live pair

New A/B task `session`: four turns in one Hermes session, with a 5.5-minute pause before each follow-up so the provider's prompt cache really expires. Both arms pause.

1. Read three large files in full (a log, a spec, a JSON user list) and summarise each.
2. Fix an unrelated failing test.
3. Name the component that crashed, from the log read in turn 1.
4. Give one limit from the spec read in turn 1.

"On" arm: the Jermes context engine with `context_trim` in enforce, and every other point off, so the difference comes from trimming alone. Model: Claude Opus 5.5. One run per arm.

| Arm | Correct | Cache reads | Cache writes | Cost |
|---|---|---|---|---|
| Off | yes | 1,071k | 786k | $4.21 |
| On | yes | 981k | 522k | **$2.86** |

Change: prompt tokens −19%, cost **−32%** (Jev's share: $0.001). The saving came mostly from cache writes: after each pause, the whole prompt is written to the cache again, and the trimmed prompt was about 290k characters smaller.

At the turn-3 pause, Jev marked all six old items (293k characters: three file reads plus follow-up reads of the same files) as no longer needed. The agent still answered turns 3 and 4 correctly from its own turn-1 summary, without re-reading. So this pair doesn't test the harder case, where a trimmed item has to come back.

Limits: one pair, and the task costs about $7 per pair.

### Cost simulation over real sessions (`hermes jermes costsim`)

Rebuilds each past session call by call from Hermes' `state.db`, then prices it at Anthropic's list prices, with and without trimming at cold turns. 13 sessions of one install could be rebuilt, $746 of spend.

| Policy | Estimated cost | Change |
|---|---|---|
| As recorded | $745.89 | |
| Trim, Jev keeps 30% of old items | $583.94 | −21.7% |
| Trim every old item | $499.81 | −33.0% |

This is an estimate. It can't see extra work the agent does when it needs a trimmed item back.

### Routing ceiling (Jev scoring real turns, about $0.02)

- **Cheaper model after a pause:** Jev rated 5 of 137 cold turns easy and low-stakes. Routing those would save under 1%.
- **Handing long tool loops to a cheap worker:** 0 of 88 turns with 15+ model calls (2,579 calls) passed as self-contained, mechanical and low-stakes.

A cost check was added to `model_router`. It won't switch when:

- the conversation doesn't fit the cheaper model, or
- writing the prompt into the other model's cache costs more than the turn would save.

For example, Opus 5.5's cache reads cost less than Sonnet 4.5's, so moving a warm Opus 5.5 conversation to Sonnet costs more.

### Risk gate (31 labelled cases, 100 real past calls)

| | v0.4 defaults | v0.5 |
|---|---|---|
| Labelled cases fully correct | 68% | 84% |
| Dangerous calls stopped (block or review) | 100% | 100% |
| Safe calls allowed | 64% | 100% |
| Real calls sent to review | 67% | 15% |
| Real calls blocked | 3% | 0% |
| Latency p50 / p90 | 2,087 / 2,217 ms | 257 / 340 ms |

What changed:

- The check sees the earlier conversation.
- "Not what was asked" alone no longer causes review.
- The hazard questions are more specific.
- The review threshold went from 0.5 to 0.7.
- A code rule sends any write to agent config, shell profile, SSH, cloud-credential or dotenv files to review, unless the user clearly asked for it.

An intermediate version without that last rule stopped only 90% of dangerous calls: it let an overwrite of the Hermes config through.

For comparison, Hermes' own dangerous-command patterns, on the 22 shell cases:

- dangerous calls flagged: 71%
- calls needing a human flagged: 58%
- safe calls flagged: 30%

### Loop guard (real repeated failures)

26 cases found by code in 5,064 real calls: the same command failing with the same error again. Against 80 non-repeats:

| Threshold | Recall | False alarms |
|---|---|---|
| 0.5 | 46% | 15% |
| 0.7 | 35% | 10% |
| 0.8 (default) | 31% | 5% |

It stays in shadow mode.

### Tool-result filter: trust changes (A/B, 3 runs per arm)

Changes made:

- The note lists the line ranges that were cut and saves a full copy on disk.
- Targeted reads of a line range are never filtered.

On the two filter tasks, prompt tokens fell 10% but cost rose 5%. On two "on" runs the agent loaded a suggested skill and re-read the file in ranges, which raised cache writes from 54k to 81k.

A new "does the task need the whole content?" question now passes whole-document tasks through, such as "summarise every release" (0.95) and "translate" (0.97). It hasn't been A/B tested.

## September 25, 2026: data ingestion (SEC 10-K cover pages)

Six fields per filing, with the correct values taken from SEC's structured data (submissions API and XBRL `dei` facts), not from the documents: state of incorporation, tax ID, fiscal year end, SEC file number, shares outstanding, public float. Each document is the first 25,000 characters of the 10-K. Baselines: Claude Sonnet 4.5 and Claude Haiku 4.5, each reading the whole document and filling every field in one call. Escalation model: Sonnet 4.5. Costs are list prices from real token counts.

### Development set: 20 filings

Used while building the finders and questions, so these runs aren't an unbiased test.

| Run | Jev pipeline | Sonnet 4.5 | Haiku 4.5 | Jev pipeline cost | Sonnet cost |
|---|---|---|---|---|---|
| 4 fields | 80/80 | 80/80 | 80/80 | $0.026 | $0.366 |
| 6 fields, first run | 116/120 (3 wrong, 1 review) | 119/120 | 119/120 | $0.102 | $0.378 |
| 6 fields, after the fixes below | 120/120 | 120/120 | 120/120 | $0.047 | $0.379 |

Two of the pipeline's three wrong values in the first 6-field run were bugs in the candidate finders, not wrong choices by Jev. The correct value was never offered:

- "710,398,642." was cut to "710,398", because the trailing period was read as a decimal point.
- "526.7 million" was offered as "526.7".
- A bare "208,464,334,129" (no dollar sign) wasn't recognised as an amount.

The third ("$80.7 billion" instead of the $81.1 billion float as of the second fiscal quarter) was an ambiguous question: the filing gives both. The baselines made the same mistake. Rewording the question to name the measurement date fixed it for all three.

### Held-out set: 16 filings

Never used during development. 2 of 96 values don't appear in their document text and are left out of scoring.

| | Correct | Wrong | Cost | Strong-model input tokens |
|---|---|---|---|---|
| Jev pipeline | 94/94 | 0 | $0.027 | 5,319 |
| Sonnet 4.5 | 94/94 | 0 | $0.298 | 91,737 |
| Haiku 4.5 | 94/94 | 0 | $0.099 | 91,737 |

Jev: 64 requests, 242k input tokens, $0.010. One field escalated (Cisco's public float, written "$ 294.5 billion"). Median 34 s per document.

## September 25, 2026: token savings

### Task-matched A/B through real Hermes

Claude Opus 5.5. Each run gets a fresh isolated Hermes home, a scratch folder with the task's files, and an automatic answer check. Arms alternate order. Token counts come from Hermes' own session record.

Six broad tasks, 3 runs per arm, `skill_suggest: advise`, `result_filter: enforce`:

| Task | Off (mean per run) | On (mean per run) |
|---|---|---|
| Find the crash in a 90 KB log | $0.178 | $0.176 |
| Look up a figure in a 70 KB spec | $0.198 | $0.193 |
| Find the largest account in an 86 KB JSON file | $0.174 | $0.172 |
| Convert a CSV to Excel | $0.191 | $0.207 |
| Explain a monad (no tools) | $0.149 | $0.150 |
| Fix a one-line bug (control) | $0.159 | $0.160 |
| **Total, all 18 runs** | $3.146 | $3.178 (+1%) |

All 36 runs passed. The filter almost never ran, because the agent searched the big files instead of reading them whole. On the CSV task, the agent loaded the suggested skill in one run and took 6 calls instead of 3.

Two tasks that require reading a long file in full, 3 runs per arm:

| Task | Off (per run) | On (per run) | Filter applied |
|---|---|---|---|
| Meeting transcript | $0.306, $0.307, $0.307 | $0.214, $0.194, $0.210 | 2, 1, 2 times |
| Release notes | $0.252, $0.252, $0.277 | $0.210, $0.354, $0.250 | 1, 2, 0 times |
| **Total** | $1.702 | $1.433 (−16%) | |

All 12 runs passed. Tracing a release-notes run: the filter kept one of 111 sections, the one holding the answer, which Jev scored 0.82 against 0.28 for the next best. The agent then searched the file with `grep` twice before answering, rather than trusting the trimmed result.

A first single-pair run was discarded: the child Hermes processes inherited this session's environment and ran in the wrong working directory. The harness now strips parent Hermes variables and pins the working directory (covered by a test).

### Offline estimate over real sessions

64 sessions with trustworthy counters, 1,066M prompt tokens, about $948 at list prices. The filter's real Jev decisions over 113 eligible big results: 64 filtered, 46 passed through, 3 Jev errors. Removed tokens, counting re-reads: 25.2M (2.4%), worth $11.61. Jev cost: $0.029. Three sessions account for 89% of the saving. Skill loads avoidable on the 40 labelled turns: 2 of 4, worth $0.06. Token counts assume 4 characters per token.

Hermes' own price table has no entry for Claude Opus 5 or 5.5, and estimated about $1 for roughly $720 of usage on those models. The estimate uses Anthropic's published prices.

## September 25, 2026: hand labels

40 real turns labelled by hand with the skills that should have loaded: 29 needed skills (2.9 on average), 11 needed none. Every change below was scored on the same 40 labels.

### What each change did

| Change | Primary hit | Misses | Precision | Notes |
|---|---|---|---|---|
| Context 4 → 10 messages | 66% → 69% | 17% → 10% | 79% → 62% | Fewer misses, more extra suggestions. 10 became the default: a missed skill costs more than an extra one the agent can ignore. |
| `runpod-pods` description rewrite | 59% → 72% | 10% → 14% | 58% → 65% | Fixed 4 turns, broke none. One turn moved from wrong skills to "no skill". |

The second row's "before" (59%) is lower than the first row's "after" (69%) because a new skill, `job-interview-company-prep`, was created between the two runs and took first place on two turns whose labels predate it. Each row compares like with like.

### Context 4 vs 10 messages, full table

| Metric | 4 messages | 10 messages | What the agent loaded |
|---|---|---|---|
| Decision accuracy (skill vs no skill) | 88% | 90% | 35% |
| Primary hit | 66% | 69% | 7% |
| Recall | 69% | 63% | 4% |
| Precision | 79% | 62% | 75% |
| False alarms | 0% | 9% (1 of 11) | 0% |
| Misses | 17% | 10% | 90% |

The labels were filled in on a sheet that showed Jev's 4-message list, and 26 of 40 matched it exactly, so the 4-message column may be flattered.

### `runpod-pods` description rewrite

Old: "Rent RunPod GPUs for models too big to run locally."
New: "RunPod GPU pods: launch on a network volume, check what is running, stop or terminate."

Before rewriting, the skill's script (`scripts/pod.py`) was checked to confirm it really does each of those: `volumes` / `create --volume`, `list`, `stop`, `terminate`. `runpod-pods` went from being listed on 3 to 8 of the 13 turns that needed it, with no new false alarms.

## September 24, 2026: replay against what the agent loaded

Before hand labels existed, Jev was compared with the skill the agent actually loaded in each turn. That is a weak label: it is what the agent did, not necessarily what was right. Both modes ran on exactly the same 81 turns (failed calls retried until every turn had an answer). Settings at the time: 4 messages of context.

**Turns where the agent loaded a skill (32)**

| | Request only | Request + last 4 messages |
|---|---|---|
| Jev's primary skill = the agent's skill | 11 (34%) | 12 (38%) |
| Agent's skill anywhere in Jev's list | 16 (50%) | **20 (63%)** |
| Skills listed per turn (avg) | 2.5 | 2.7 |

**Turns where the agent loaded no skill (49)**

| | Request only | Request + last 4 messages |
|---|---|---|
| Jev said "no skill needed" | **26 (53%)** | 20 (41%) |
| Skills listed per turn (avg) | 1.6 | 1.9 |

What this showed:

- **The "no skill needed" option works.** Before it existed (v0.1, same kind of turns), Jev said "no skill" on 19 to 27% of these turns; with it, 41 to 53%.
- **Context helps with follow-ups.** With context, Jev found the right skill for a bare music link (audio mixing) and "process this video like you did with the previous one" (the brand-edit skill). With the request alone it returned nothing for both.
- **Context also makes Jev more willing to suggest a skill.** Short follow-ups such as "let's try masked" picked up skills from the earlier topic.

Caveat: these runs had a replay bug (since fixed) that filled part of the context window with blank tool-call turns, so on tool-heavy turns Jev saw fewer earlier messages than the live hook would give it.

Cost: the comparison (about 290 live calls) used 1.56M input tokens, about $0.07. One request costs roughly 10k tokens: a skim of about 8.3k over the skill list plus a select call of about 2k.

## September 24, 2026: first replay (v0.1)

The first live replay, before the "no skill needed" option, conversation context or list output existed. Over 32 turns where the agent loaded a skill, Jev's top skill matched the agent's on 46%, and the agent's skill was in Jev's top 3 on 54%. Jev rarely said "no skill": on turns where the agent loaded nothing, it still suggested skills about 80% of the time, which led to the "no skill needed" option.
