# How Jermes works

Detail behind the [README](../README.md): which Hermes surface each decision point uses, how the main features work, what the code guarantees, the dashboard tab, and how to run the tests. Results are in [RESULTS.md](RESULTS.md); limits in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Decision points and Hermes surfaces

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

## Skill selection

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

## Data ingestion

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

## Context trimming

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

## Guarantees built into the code

- **Deterministic.** Decisions are cached by `(model, canonical state, question spec)`. The same input always produces the same action, even if Jev's sampling drifts.
- **Fails open.** Any error, timeout, 429/503 or missing key leaves Hermes' behaviour unchanged. The live client's deadline (2.5 s) is shorter than Hermes' hook timeout, so a slow Jev can't cause Hermes to fail a `pre_tool_call` *closed*.
- **Never lowers protection.** `risk_gate` sits in front of Hermes' own dangerous-command detector and approval flow. It doesn't replace them.
- **Zero latency in shadow mode.** Shadow decisions run in a background thread.
- **Everything is logged.** SQLite at `$HERMES_HOME/jermes/decisions.sqlite` records answers, probabilities, action, mode, policy version, latency and tokens for every decision.
- **Redacted.** State goes through Hermes' secret redactor, plus a Jermes layer that catches long high-entropy tokens whatever their prefix, before anything leaves the machine.

## Dashboard tab

With the plugin installed, the Hermes web dashboard shows a **Jermes** tab (after Skills). If the dashboard was already running when you installed Jermes, restart it once (stop and start `hermes dashboard`, or restart its systemd service if you run it as one) so it picks up the new tab and its API.

| Card | What it shows |
|---|---|
| Status | Jev provider and model, whether the API key is set (never its value), last Jev call, config path |
| Jev cost | Spend today, last 7 days and all time, with a per-day bar chart (7, 30 or 90 days) |
| Features | Each decision point with a mode switch. `advise` is only offered where the code acts on it (skill suggestions, duplicate skills, memory filter, loop guard); elsewhere it would behave like shadow. Switching to `enforce` shows what enforce does for that feature and asks for confirmation |
| Context trimming: ready for enforce? | The trim report for the last 14 days: items dropped, how many were needed again, and how they came back |
| Guards: ready for advise or enforce? | Risk-gate counts for the last 24 hours, plus every would-block, would-hold and skill-overlap flag |
| Shadow decisions | Recent decisions with filters by feature and by would-act, with redacted previews of each call |
| Duplicate-skill audit | Runs `skills-audit` in the background (about 6 minutes and $0.04 for ~100 skills) and shows the groups and suggested keepers. Suggestions only |

Mode changes write `$HERMES_HOME/jermes/config.yaml`, keeping comments and other settings, after backing it up to `$HERMES_HOME/data/jermes/backups/`. Running agents pick up the new modes within a few seconds (Jermes now re-reads the file when it changes); context trimming applies to new sessions. The dashboard's routes live under `/api/plugins/jermes/` behind the dashboard's own login.

## Development and tests

```bash
uv venv && uv pip install -e '.[dev]'
pytest                                  # offline; Jev is faked at the HTTP layer
HERMES_AGENT_DIR=~/hermes-agent pytest  # also runs the end-to-end test against a real Hermes checkout
```

The end-to-end tests run against a real Hermes checkout in a temporary `HERMES_HOME`:

- load Jermes through Hermes' `PluginManager`, fire `pre_tool_call` through Hermes' own dispatch, and check a dangerous call is blocked and a benign one passes
- create a skill mid-session and check the very next Jev call sees it
- check disabled skills are never offered
- load the Jermes context engine through Hermes' plugin system, deep-copy it the way Hermes does per agent, and trim a request through Hermes' own request hook
