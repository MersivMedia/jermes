# Measuring and tuning Jermes

How to test Jermes on your own sessions before promoting anything, label an answer key, score skill selection, and improve it. The cost tools (`savings`, `costsim`, `ab`, `trimreport`) and guard benchmarks (`riskbench`, `loopbench`, `memorybench`) are listed in the [README](../README.md#commands); their results are in [RESULTS.md](RESULTS.md).

## Replay your past sessions (shadow test)

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

Example: `runpod-pods` was described as "Rent RunPod GPUs for models too big to run locally." Its script also lists, stops and terminates pods, so requests like "turn off the gpu instance now" were going to other GPU skills. Rewriting the description to cover those fixed 4 of the 40 labelled turns (see [docs/RESULTS.md](RESULTS.md)).

The skill list is read live, so a description change takes effect on the next request. No restart is needed.
