# Jermes test results

Every measurement so far, newest first. The [README](../README.md) shows only the current numbers.

Setup for all runs: one real Hermes install (about 200 skills), Jev through Vercel AI Gateway, real past turns replayed from Hermes' `state.db`. Metric definitions are in the README under "Score the results".

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
