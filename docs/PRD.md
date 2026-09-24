# Jev as a Deterministic Decision Layer for Hermes Agent

Research report and product requirements document. Prepared 24 September 2026.

Scope: how TypeSafe AI's Jev model can serve as the decision layer of an agentic harness built on the open-source Hermes Agent, with full requirements for three workstreams: (1) tool-call decisions, (2) data ingestion requests, and (3) additional harness use cases. The governing objective is to cut reasoning-model token spend by sending a reasoning model only the work that genuinely needs one.

Evidence standard: every external claim carries a numbered citation to the source list at the end. Performance figures published by TypeSafe are labelled as vendor-reported. Claims that could not be confirmed from a primary source are marked [UNVERIFIED]. Hermes Agent file references point to upstream commit 9514d35 (1 September 2026).

## 1. Plain-language summary

Jev is a new kind of AI model released on 15 September 2026. It does not write text. A program hands it a block of information and a list of multiple-choice, yes/no, or rating-scale questions, and Jev returns an answer to each question with a probability attached, typically in well under a second and at a small fraction of the cost of a language model.[1][23]

That shape fits the parts of an AI agent that are really just decisions: which tool to use, whether a command looks dangerous, whether a web page is relevant, which model a request needs, whether the agent is going in circles. Today Hermes Agent, like most agent frameworks, makes all of those decisions inside an expensive reasoning model, either implicitly in its chain of thought or through extra side calls to a helper model.

The proposal is a Hermes plugin, working name `jev-harness`, that moves those decisions into ordinary code that consults Jev at each decision point. The code, not Jev, decides what happens: it reads Jev's answer and confidence, compares them with thresholds written in a policy file, and either acts, asks a human, or falls back to the reasoning model. The reasoning model keeps the work that actually requires reasoning (planning, writing, coding, synthesis) and stops paying for work that does not.

Three findings shape the plan:

- **The fit is unusually direct.** TypeSafe has already published a cookbook that runs Jev against the Hermes skill catalog and cut wrong skill loads from 16.8% to 7.3% and needless loads from 9.8% to 4.0%.[7] Hermes already exposes the plugin seams needed for almost every decision point in this PRD without modifying core files.[27][28][29]
- **The savings come mostly from what the reasoning model no longer reads, not from Jev's own price.** An illustrative cost model in Section 8 shows Jev's own fees at roughly 0.2% of baseline spend, while routing easy turns to a cheaper model and filtering bulky tool output before the reasoning model sees it produce a 30% to 50% reduction under stated assumptions. These are modelled figures, not measurements.
- **The claims need independent checking.** TypeSafe's headline speed and cost multiples come from its own evaluations, which it says sit at the high end of real-world results.[1][24] In one third-party summary of TypeSafe's own workflow data, Jev matched mid-tier frontier models on agreement but trailed the strongest ones by about six points.[25] The plan therefore starts in shadow mode, measuring Jev against current Hermes behaviour before any decision is handed over.

## 2. What Jev is

### 2.1 Facts from primary sources

Jev is the first model in what TypeSafe AI calls the "System One" class, released in early access on 15 September 2026.[1][22] TypeSafe describes it as "a frontier-intelligence function call: unstructured state in, typed probabilistic decisions out."[1] It is trained with a method TypeSafe calls Reinforcement Learning for Calibrated Decisions (RLCD), which optimises for probabilities that match outcomes rather than for answers human raters prefer.[1][23]

A request contains a `state` (a string, JSON object, or array of text) and a map of named questions. Every question is evaluated independently and in parallel against the same state, so adding questions barely changes response time and one answer never becomes context for another.[3][23] There are three question types:

| Primitive | Asks | Returns | Limits |
|---|---|---|---|
| Choice | Which of these options? | Chosen option, a probability per option, confidence | Up to 255 options[4] |
| Score | Where on this ordered scale? | Probability-weighted score, a probability per level, confidence | 2 to 10 levels[4][23] |
| Noul | Is this statement true? | A single probability from 0 to 1 | No separate confidence field[5] |

All three are served from one endpoint, `POST https://api.typesafe.ai/v1/systemone`, with SDKs for Python (`typesafe_sdk`) and JavaScript, and a LangChain integration (`langchain-typesafe`).[4][21]

Current operating envelope for `jev-1.13.0`:[2]

| Parameter | Value |
|---|---|
| Price | $0.042 per million input tokens; output tokens not charged |
| Rate limits | 250,000 tokens per second; 1,200 requests per minute (adjusting dynamically during early access) |
| Context | 64k tokens per request; 32k for state plus the longest single question |
| Input | Text only; images, audio, and video must be converted to text first |
| Language | English strongest; other languages handled but less accurately |
| Customisation | No fine-tuning or adapters; all customisation happens through state and question wording |
| Data handling | Not trained on customer requests; zero data retention for enterprise plans |
| Versioning | Aliases `jev-latest` and `jev-preview` move between releases; versioned IDs can be pinned |

TypeSafe reports end-to-end latency of 70 to 500 milliseconds against 3 to 329 seconds for frontier language models, and says the answer can never fall outside the developer-defined schema.[1]

### 2.2 What the evidence does and does not support

**Schema conformance is guaranteed; correctness is not.** TypeSafe's "can't hallucinate" language refers to a narrower property: Jev cannot return a value outside the options supplied. It can still pick the wrong option.[24][25] The practical consequence for this design is that every Choice question needs a real "none of these" or "insufficient evidence" option, because probability has to land somewhere.[23][25]

**The headline multiples are vendor-run.** The 193.6x speed and 444.6x cost figures come from four workflows written by TypeSafe's own team and scored against the averaged answers of two large models rather than independent ground truth. TypeSafe itself flags possible bias and describes the gains as likely at the high end of real use.[1][24]

**Independent and semi-independent data points are mixed but encouraging.**

| Source | Finding |
|---|---|
| Anthony Maio's reading of TypeSafe's workflow evals | Jev 67.8% agreement at $0.0004 and 0.4 s per case; GPT "Terra" 67.9% at $0.0304 and 10.1 s; GPT "Sol" 74.1% and Claude Opus 5 73.1%; widest gap on invoice processing, 61.8% versus 79.1%[25] |
| Every (reported by The Frame News) | About 25x faster and an estimated 580x cheaper than Fable 5.1 on planted writing problems; Jev found six of seven, Fable seven of seven[24] |
| Vercel engineer (TechCrunch) | Replacing a GPT Luna 5.6 command-safety classifier with Jev gave results 5 to 18 times faster with greater accuracy[26] |
| Bryo AI CTO (TechCrunch) | Gemini slightly more accurate on business-email classification but 10 to 20 times more expensive[26] |

**Calibration is claimed, not yet independently shown.** No published calibration curves or technical paper exist, and the confidence statistic's exact formula is undisclosed.[25] TypeSafe's documentation does state that confidence is computed from the shape of the probability distribution and that the raw distribution is always returned, so a deployment can compute its own statistic.[5]

**Answers are highly consistent but not strictly deterministic.** In TypeSafe's parallel-questions cookbook most answers were identical across five repeats (standard deviation exactly 0.0), while two of thirteen questions showed small run-to-run noise, identical in size whether questions were batched or sent singly.[13] Section 4.3 explains how the harness makes its own behaviour deterministic despite this.

### 2.3 Known failure modes

TypeSafe publishes a list of jagged edges for `jev-1.13`.[6] Each maps to a design rule in this PRD:

| Failure mode | Design rule adopted here |
|---|---|
| Literal reading of instructions | State the exact condition; put boundary cases in criteria |
| Arithmetic and counting | All maths in code; count by asking one Noul per item and summing |
| Date comparison | Extract date components as Choices; compare in code |
| Multi-hop indirection | Point questions at named state fields with backticks |
| Large, noisy state | Filter in code first; send only fields the question needs |
| Adversarial content in state | Treat Jev as one layer of defence, never the only one |
| Contradictory instructions and criteria | Keep criteria as an extension of the instruction; no inverted Nouls |
| Structural invariants between questions | Never carry a threshold tuned on a Noul over to a Choice |
| Text generation | Never ask Jev to generate; use code or an LLM to propose candidates, Jev to select |

### 2.4 What Jev is not

Jev is not a replacement for the model that drives an agent. TypeSafe states plainly that there is no setting that turns a coding agent into a Jev-powered agent, because Jev does not stream text, call tools, or edit files.[17] Its role is a decision primitive inside software that the agent harness calls.[17][23] The reasoning model remains the author of plans, prose, and code.

## 3. Why Hermes Agent is the right base

Hermes Agent already separates the deterministic harness from the model in the ways this design needs. The relevant surfaces, verified at commit 9514d35:

| Hermes surface | What it allows | Decision points it hosts |
|---|---|---|
| `pre_tool_call` hook | Return `block`, `approve` (escalate to the human-approval gate), or `modify` (merge new arguments) before any tool runs; fails closed on timeout[27] | Tool risk gating, argument checks |
| `tool_request` middleware | Rewrite tool arguments before hooks, guardrails, and approvals see them[29] | Argument normalisation |
| `transform_tool_result` hook | Replace any tool's result string before the model sees it[27] | Result filtering, loop and completion notes |
| `pre_llm_call` hook | Append context to the current turn's user message, never the system prompt, preserving prompt caching[27] | Skill and tool suggestions |
| `llm_request` middleware | Replace the effective provider request kwargs before each API call[29][32] | Model routing |
| `llm_execution` middleware | Wrap the provider call itself[29] | Cross-provider routing, fallback |
| `pre_gateway_dispatch` hook | Skip, rewrite, or allow each incoming platform message before agent dispatch[28] | Message triage |
| `pre_verify` hook | Keep the agent working instead of stopping, with a follow-up instruction[28] | Completion checks |
| `register_hook`, `register_middleware`, `register_tool`, `register_command` | Plugin registration API[28] | All of the above |

Two project policies constrain the design. Plugins must not modify core files; missing capabilities are added by expanding the generic plugin surface upstream.[31] And nothing may break prompt caching mid-conversation: no changes to past context, toolsets, or system prompt within a session.[31] Every mechanism below respects both.

Hermes also contains a direct precedent for the approach. Its `smart` approval mode already asks an auxiliary LLM to classify each flagged shell command as APPROVE, DENY, or ESCALATE, with explicit defences against prompt injection inside the command text.[30] That is a three-option Choice question with a confidence gate, currently implemented as a full LLM call on the hot path of every flagged command.

## 4. Design principles

### 4.1 Code owns control flow

Jev supplies judgments; code owns policy. No Jev answer ever triggers an action directly. Each decision point is a function that assembles a minimal state, asks a fixed set of atomic questions, and passes the answers through a policy that maps (answer, confidence) to an action. This mirrors TypeSafe's own guidance to decompose broad judgments into atomic questions and combine them with weights in code.[3]

### 4.2 Three confidence bands, thresholds scaled to stakes

Every decision point defines three bands, following TypeSafe's recommended pattern:[5][16]

| Band | Behaviour |
|---|---|
| High confidence | Act automatically |
| Medium confidence | Confirm with the user, route to review, or gather more context |
| Low confidence | Do not act on Jev's answer; fall back to the reasoning model or a human |

Thresholds differ by consequence. A read-only lookup may act at 0.6; an irreversible action requires 0.9 or more and still asks for confirmation below that.[5][16] Initial values are conservative and are tuned only against logged data from shadow mode (Section 10).

### 4.3 Deterministic harness over a near-deterministic model

"Deterministic" in this PRD describes the harness, not the model. The harness guarantees that identical inputs produce identical actions through four mechanisms:

1. **Pinned model version.** The plugin calls `jev-1.13.0`, never an alias, because an alias can move to a new release and change answers without any change on the caller's side. TypeSafe recommends pinning once thresholds are tuned.[2]
2. **Decision cache.** Each call is keyed by a hash of the pinned model ID, the canonicalised state, and the canonicalised question spec. A cache hit replays the stored answer, so small sampling noise[13] can never flip a decision for the same input.
3. **Versioned policy.** Thresholds live in a versioned policy file; each logged decision records the policy version that acted on it.
4. **Canonical state.** State is serialised with sorted keys and with volatile fields (timestamps, request IDs) removed, which maximises cache hits and makes replay exact.

### 4.4 Fail safe, in the right direction

Every decision point declares its failure direction when Jev is slow, rate-limited, or unavailable:

- **Advisory decisions** (suggestions, routing, filtering) fail open to current Hermes behaviour. The reasoning model simply does what it does today.
- **Safety decisions** (risk gating) fail to Hermes' existing controls: the built-in dangerous-command detector and the human approval gate.[30]

Hermes' `pre_tool_call` hook fails closed if a callback exceeds `plugins.hook_callback_timeout`.[27] The plugin therefore enforces its own shorter client-side timeout and returns no directive on timeout, so a Jev outage degrades to normal Hermes behaviour rather than blocking every tool.

### 4.5 Cache-safe by construction

Anything the harness adds to the conversation goes into the user message (`pre_llm_call`) or into new tool results (`transform_tool_result`), both of which are new content at the tail of the conversation. Nothing edits the system prompt, the toolset, or prior messages. Model routing is sticky per turn, for the reason given in Section 5.7.

### 4.6 Minimal state, redacted

State sent to Jev is the smallest slice each question needs, both because accuracy falls as irrelevant content grows[6] and because every byte leaves the machine. Secrets are redacted before transmission using Hermes' registered redaction patterns.[28] Whether a public redaction helper is exposed to plugins at this commit was not confirmed. [UNVERIFIED]

## 5. Workstream 1: tool-call decisions

The Hermes tool loop contains seven decision points where a reasoning model currently spends tokens on work that is a bounded judgment. Each is specified below with its hook, questions, policy, fallback, and expected effect.

### 5.1 D1: Skill and tool suggestion

**Problem.** Hermes places a skill index in the system prompt with descriptions truncated to about 60 characters, and instructs the model to err on the side of loading skills.[7] On TypeSafe's measurement against `claude-haiku-4-5`, the agent loaded the wrong skill on 16.8% of covered requests and loaded one needlessly on 9.8% of uncovered requests; of 36 wrong picks, 10 came from the correct skill's own category.[7] Every wrong or needless load pulls a full SKILL.md into context for the rest of the session.

**Mechanism.** Adapt TypeSafe's two-call pattern to a `pre_llm_call` hook:[7]

| Call | Questions | Decision in code |
|---|---|---|
| 1: skim | One Choice over every skill name, with the index description as each option's criteria, plus an explicit "no skill needed" option | If "no skill needed" is the top option, tell the agent no skill is needed and stop; otherwise carry the top five skills into call 2 |
| 2: select | One Choice over the shortlist plus "no skill needed", now with the full description and the first 700 characters of each SKILL.md; one "would this skill help with all or a distinct part of the request" Noul per candidate | If "no skill needed" wins, no skill. Otherwise the Choice winner is the primary skill, followed by every other candidate whose own Noul is at least 0.50, up to four skills |

The output is a list rather than a single pick: requests often need a primary skill plus a supporting one (a research document needs both the document skill and the citation skill). The Choice answers "which one most"; the per-skill Nouls answer "does this one help at all", so one request can get several skills, one, or none. "No skill needed" is a Choice option instead of the cookbook's three gate Nouls, which removes a hand-tuned threshold.

The state carries the latest request plus the last four user and assistant text turns (400 characters each, with tool output and harness notes stripped). Without it, short follow-ups ("yes do the invert test next") cannot be resolved. The request stays primary, and the questions tell Jev to use the earlier turns only to understand what the request refers to. Hermes already passes `conversation_history` to `pre_llm_call`, so this needs no core change.

The result is injected as one short block listing the skills (primary first, then supporting), or stating that no skill is needed. TypeSafe found both halves matter: the "ignore this if it does not fit" wording limits damage from wrong suggestions, and an explicit "nothing applies" counteracts the index's own push to load something.[7] The list output, the none option and the conversation context are all departures from the cookbook, which measured only a single suggestion from the latest message; each is measured against real sessions with `jermes replay` (with and without `--no-context`) before Phase 1.

**Hermes adaptation.** The cookbook appends the suggestion to the system prompt after the cache breakpoint.[7] Hermes' `pre_llm_call` hook injects into the user message instead, which is the project's sanctioned cache-safe location.[27] Rosters larger than 254 skills are split into chunks (one Choice slot per chunk is the "no skill needed" option), each ranked separately, before the select step; "no skill needed" wins only if it tops every chunk.[7]

**Expected effect.** Measured on TypeSafe's harness: wrong loads 16.8% to 7.3%, needless loads 9.8% to 4.0%; 37 requests fixed and 7 broken out of 315.[7] Measured latency was 0.16 to 0.31 s for call 1 and about 0.1 s for call 2.[7] Token effect: fewer unnecessary SKILL.md loads per session. A second-stage experiment (Section 10, Phase 3) tests whether, with suggestions in place, the index can be collapsed to names only using Hermes' existing `compact_categories` mechanism in `build_skills_system_prompt`, which already demotes whole categories to a names-only line while keeping every skill loadable.[36] Because that changes the system prompt, it would be applied at session start only, never mid-session.[31] The experiment is untested and must show no rise in wrong loads before adoption.

The same pattern applies to large MCP tool rosters: rank the roster, rerank the top few, and suggest.

### 5.2 D2: Deterministic fast path for closed-set requests

**Problem.** Many requests map cleanly to one function with arguments drawn from fixed lists: list cron jobs, pause a job, show today's calendar, check a price. Routing these through a full reasoning loop costs a planning call, a tool call, and a summarising call.

**Mechanism.** TypeSafe's function-calling cookbook turns a sentence into a typed call: one Choice picks the function; each closed-set argument (Python `Literal`, list of `Literal`, or `bool`) gets its own Choice or Noul; an optional "was this argument stated at all" Noul lets unstated arguments fall back to the function's defaults. The call's confidence is the weakest single judgment, not the product.[8] Free-text, numeric, and date arguments get no question and keep their defaults.[8]

Policy: execute directly only when (a) the function is read-only, (b) the tool-choice probability and the weakest-argument confidence both exceed 0.9, and (c) no free-text argument is required. Mutating functions ask for confirmation. Everything else continues to the reasoning model as today. The reply is rendered from a template or by the cheapest configured model.

**Gap.** Hermes has no generic hook that lets a plugin resolve an entire turn and return a final response. `pre_gateway_dispatch` can skip a message on messaging platforms, but a skip suppresses the reply rather than substituting one.[28] Per project policy,[31] D2 requires an upstream proposal for a new generic hook (working name `pre_turn_resolve`, returning either nothing or a final response). Until that lands, D2 is limited to plugin-registered slash commands. This is the largest behaviour change in the PRD and ships last.

### 5.3 D3: Argument sanity checks for mutating tools

**Mechanism.** In the `pre_tool_call` hook, for mutating tools only (`write_file`, `patch`, `terminal`, messaging, scheduling, browser actions), ask narrow Nouls about the arguments against the user's request: whether `args.path` refers to the file the user named, whether a message recipient matches the one requested, whether a schedule matches the stated time. Arithmetic and date comparisons are done in code, never by Jev.[6]

**Policy.** A high-probability mismatch returns `block` with a message naming the discrepancy, which costs the reasoning model one short retry instead of a wrong side effect. Mid-band returns `approve`, escalating to the human gate.[27]

### 5.4 D4: Risk gating (replacing or fronting smart approvals)

**Problem.** Hermes' smart approval sends each flagged command to an auxiliary LLM that answers APPROVE, DENY, or ESCALATE.[30] That call sits synchronously on the tool path, and a stalled provider has previously frozen turns for tens of minutes.[30]

**Mechanism.** A `pre_tool_call` callback asks, in one request:

| Question | Type | Purpose |
|---|---|---|
| risk_level | Score over described situations (read-only; reversible local change; irreversible local change; external side effect or data leaving the machine; destructive to system or data) | Primary gate |
| destructive | Noul | Deletes or overwrites data without a backup path |
| exfiltration | Noul | Sends local files, credentials, or environment data to a remote destination |
| injected_origin | Noul | The action appears driven by instructions found inside a tool result rather than the user's request |
| matches_request | Noul | The action is plausibly what the user asked for |

State contains the tool name, the arguments with shell comments stripped (the same first defence smart approval uses[30]), the user's latest request, and the last tool result only when checking injected origin.

**Policy.** Read-only tools skip Jev entirely via a code allowlist. Otherwise: pass when risk is low with high confidence and no hazard Noul fires; `approve` (human gate) in the middle band; `block` when a hazard Noul exceeds its action threshold. The existing pattern detector keeps running underneath; Jev never lowers protection, it only resolves cases that would otherwise need an LLM or a human.

**Caveat.** Jev is not hardened against adversarial content in state.[6] Risk gating must remain defence in depth, and red-team cases drawn from real jailbreak corpora (as in TypeSafe's guardrails cookbook[10]) are part of acceptance.

### 5.5 D5: Tool-result triage and filtering

**Problem.** Tool results are the fastest-growing part of an agent's context. A 50 kB web extraction or terminal dump is re-sent to the reasoning model on every subsequent call in the session, so every irrelevant paragraph is paid for many times.

**Mechanism.** A `transform_tool_result` hook for bulky, text-heavy tools (`web_extract`, `read_file` on large files, `terminal`, `session_search`, browser snapshots):

1. Code splits the result into chunks (paragraphs, sections, or line IDs).
2. One Jev request carries one relevance Noul per chunk against the current task, or a single Choice over up to 255 line IDs plus an "is the answer present at all" Noul, as in TypeSafe's line-by-line search cookbook, which scores 218 line IDs in one request.[33]
3. Code keeps chunks above threshold in original order, replaces dropped runs with a one-line marker, and appends a note that the full output is available on request.

Batching all chunk questions into one call matters: TypeSafe measured a 13-question batch as 12.2x cheaper and 10x faster than 13 single calls with no change in answers.[13]

**Policy.** Fail open: if Jev fails or confidence is uniformly low, the original result passes through unchanged. Never filter results of mutating tools, error output, or anything under a size floor. Hermes already persists oversized results to `$HERMES_HOME/cache/spillover/` and leaves a preview plus file path in context;[35] the filter keeps that path in its note so the agent can always read the full output.

**Expected effect.** This is the main lever for the token-reduction term s in Section 8.

### 5.6 D6: Loop and completion control

**Problem.** Agents burn iterations repeating a failing action or continuing after the task is done.

**Mechanism.** A `post_tool_call` observer keeps a rolling window of recent tool calls and results. On each new result, a `transform_tool_result` callback asks two Nouls: whether the latest action repeats an earlier one that already failed, and whether the results so far fully satisfy the user's request. When either fires above threshold, a short bracketed harness note is appended to the tool result. A `pre_verify` callback asks the same completion question before the agent stops and can keep it going when the request is plainly unmet.[28]

**Policy.** Advisory only; the reasoning model decides. Notes are appended to new tool results, which is cache-safe.

### 5.7 D7: Model routing

**Problem.** A trivial lookup and a hard debugging session currently get the same model.

**Mechanism.** At the start of each turn, one Jev request asks an intent Choice, a difficulty Score over described situations (direct lookup or single command; localised change or short synthesis; multi-step reasoning, architecture, or high-stakes decision), and a stakes Noul. LangChain's `ModelRouterMiddleware` implements the same idea: choose "the least costly model that can complete the task" from criteria per model.[21] TypeSafe's intent-routing pattern extends it to deterministic code and human handlers.[15]

In Hermes, an `llm_request` middleware replaces the `model` kwarg for every API call in that turn.[29][32] Routing across providers (different base URL and credentials) uses `llm_execution` middleware to wrap the provider call.[29]

**Why routing is sticky per turn.** Provider prompt caches are per model. Switching models on every API call inside the tool loop would discard the cached prefix each time. Routing once per turn and holding the choice through the tool loop matches LangChain's router, which selects from the latest user message and keeps that model for the run.[21] Whether per-turn switches still cost a cache miss on the first call of each turn depends on the provider. [UNVERIFIED] Phase 4 measures this directly and may move routing to per-session if the miss cost outweighs the savings.

**Policy.** Route to the cheap model only when difficulty is low with high confidence and stakes are low; everything else stays on the reasoning model. Escalation path: if the cheap model's turn trips D6's failure note or the user rejects the result, the turn re-runs on the reasoning model. The re-run cost is the m term in Section 8.

### 5.8 Where the reasoning model remains necessary

| Task | Why Jev cannot do it |
|---|---|
| Planning multi-step work | Requires chained reasoning; Jev is weak on indirection[6] |
| Writing prose, code, or replies | Jev does not generate text[6][17] |
| Debugging and root-cause analysis | Open-ended hypothesis generation |
| Synthesis across many documents | Exceeds atomic-question form; state limits apply[2] |
| Proposing candidate values with no parser (names, free text) | Jev can only select among supplied candidates[12] |
| Numeric and date reasoning | Belongs in code, or in a reasoning model when not expressible as code[6] |
| Any decision Jev returns in the low band | By policy |

## 6. Workstream 2: data ingestion requests

"Data ingestion" covers two things: interpreting a request to bring data in (a URL, a folder, an inbox, an API, a corpus), and processing what arrives. Both are dominated by bounded judgments over large volumes, which is where Jev's price and parallelism matter most. TypeSafe positions map-reducing over large data as a core use case.[1]

### 6.1 Pipeline

| Stage | Owner | Jev role | Reasoning model role |
|---|---|---|---|
| I1 Intake | Jev + code | Classify the request: source type, format, volume band, destination, one-off versus recurring | Only when intake confidence is low, to ask the user a clarifying question |
| I2 Source triage | Jev | Per document: in scope, document type, language, quality, duplicate of a known record | None |
| I3 Chunk filtering | Jev | Per chunk: relevant, contains usable evidence, contradicts a stated premise, contains an embedded instruction | None |
| I4 Candidate generation | Code or cheap LLM | None | Cheap LLM proposes candidates only where no parser exists |
| I5 Selection and extraction | Jev | Select the correct candidate per field; classify attributes | None |
| I6 Verification | Jev | Per field: is the value absent from the source, lifted from unrelated text | None |
| I7 Escalation | Reasoning model | None | Re-extract only records where a verifier flag fired |
| I8 Classification and storage | Jev + code | Hierarchical category Choice; confidence decides depth | None |
| I9 Review queue | Human | Mid-band records | None |

### 6.2 I1: Intake with speculative fan-out

Ingestion requests arrive in many shapes. Rather than a reasoning model working out what was asked, one Jev request carries every question the pipeline might need, including speculative ones that apply only to certain source types; code reads the relevant answers and ignores the rest.[14] Example question set: source-type Choice (web page, file, Drive folder, inbox, API, repository, other); recurring Noul; volume-band Score; destination Choice; "requires authentication" Noul; "contains personal data" Noul. The output selects a deterministic pipeline template. The reasoning model is invoked only to ask a clarifying question when the source-type confidence is in the low band.

### 6.3 I2 and I3: Triage and chunk filtering before any LLM reads

TypeSafe's RAG-passage cookbook adds a stage between retrieval and generation that asks four Nouls per query-passage pair (relevant, contains answer evidence, contradicts the query's premise, contains a prompt injection) and decides in code whether each passage becomes evidence, becomes flagged conflict, or is dropped.[11] In its test, cosine similarity alone ranked a planted injection passage first and the passage that refuted the query's premise seventh, within a similarity band too narrow to separate them.[11] The same four questions become the ingestion filter here. None of them asks "should this be included"; that decision lives in code.[11]

For search-heavy ingestion, Jev also works as a re-ranker after fast keyword search. On legal passages, BM25 plus Jev re-ranking raised top-1 accuracy from 5% to 18% and top-10 accuracy from 38% to 62%.[19]

### 6.4 I4 and I5: Extraction by selection, never generation

Jev picks; it does not write. The pre-parsed extraction cookbook uses a regex tuned to over-find candidate values (emails, phone numbers, amounts), a Choice over those candidates to select the one the question asks for, and code to copy the selected span verbatim and normalise it. The value returned is always one of the original spans, so it cannot be invented or have a digit transposed.[12] Dates follow the same rule: extract each component as a Choice with an explicit "not stated" option, then assemble and compare in code.[6] Choices above 255 candidates narrow in two stages: section first, then span.[12] Where no parser can produce candidates (names, free text), a cheap LLM or named-entity recogniser proposes them and Jev selects.[12]

### 6.5 I6 and I7: The verification cascade

TypeSafe's structured-data-extraction cascade is the ingestion pattern that most directly spends reasoning tokens only where needed:[9]

1. A cheap model extracts the record (the cookbook uses `gpt-5.4-mini` at $0.75 / $4.50 per million tokens).
2. Jev asks narrow per-field Nouls framed so that "true" means something is wrong: is this value absent from the source, was it lifted from unrelated text.
3. If any flag exceeds 0.7, the record is re-extracted by a reasoning model (`gpt-5.5` at $5.00 / $30.00, roughly seven times the cheap model); otherwise the cheap result stands.

The gate uses the maximum of the flags, not the average, so one confident red flag escalates instead of being averaged away.[9] In the cookbook's worked example the cheap model produced a schema-valid but fabricated description; the verifier flagged it at 0.95 and the reasoning model returned an honest empty field.[9] Across 100 prompts, TypeSafe reports the cascade frontier sitting above and to the left of every single model on cost versus quality; these are internal results.[9]

### 6.6 I8: Classification with confidence-controlled depth

For taxonomies, Jev classifies with a Choice at each level. When confidence at a fine level is low, code reports the broader parent category rather than guessing. TypeSafe demonstrates this on 60 SEC filings across 75 industry groups: a 0.9 confidence cutoff split them in half, the confident half was right 90% of the time and the rest 40%, and reporting the uncertain half one level up raised it to 70%, at one request per document.[34] This is also the most direct published evidence that Jev's confidence separates reliable answers from unreliable ones. The raw probabilities are usable as features for a conventional downstream model trained on ground-truth outcomes.[2][20]

### 6.7 Ingestion requirements summary

- Code enforces the boundary: every record carries its Jev answers, confidences, policy version, and final disposition.
- No stage asks Jev to count, compute, or compare dates.[6]
- Non-text sources are converted to text before Jev sees them.[2]
- Non-English sources are flagged at I2 and held to stricter thresholds until tested.[2]

## 7. Workstream 3: additional harness use cases

TypeSafe's own use-case map names harness intelligence as a category: model routing, semantic context retrieval, error detection, guardrails, and trace classification.[20] The following apply specifically to Hermes.

| # | Use case | Hermes surface | Jev questions | Token effect |
|---|---|---|---|---|
| X1 | Group-chat and inbox triage | `pre_gateway_dispatch`[28] | Addressed to the agent; needs a reply; spam; urgency | Messages that need no reply never start an agent turn |
| X2 | Cron wake gating | Cron `monitor` diff, then a plugin check | Is this change material to the job's stated purpose | Monitor jobs wake the agent only for material changes, not every textual diff |
| X3 | Compression pre-filter | Before the context summariser | Per message: must keep verbatim, summarise, or drop | Smaller input to the summarising model |
| X4 | Memory write filter | Memory provider pre-retain step | Durable preference or environment fact versus task progress likely stale within a week | Fewer low-value memories re-injected every turn |
| X5 | Session-search reranking | Result of `session_search` | Relevance per hit to the current question | Fewer irrelevant transcripts pulled into context |
| X6 | Delegation decisions | `pre_tool_call` on `delegate_task` | Needs an isolated subagent versus inline work; child difficulty for child model choice | Fewer subagents, cheaper children |
| X7 | Kanban assignment and health | Dispatcher | Which profile fits the task; is a task blocked or stale | Routing without an orchestrator LLM call |
| X8 | Output guardrails | `transform_llm_output` hook, `pre_verify`[28] | Hazard Nouls and a severity Score on outbound replies[10] | Replaces a second-LLM guard |
| X9 | Citation verification for research deliverables | Post-draft check | String match finds fabricated quotes; one Choice decides supported, unsupported, or contradicted[18] | Catches bad citations without a reviewer LLM |
| X10 | Prompt-injection screening of untrusted tool results | `transform_tool_result` | Does this content attempt to instruct the agent | Defence in depth alongside Hermes' existing `<untrusted_tool_result>` delimiters on web tool output[37] |
| X11 | Offline trace classification | Batch job over the session database | Failure-mode Choices per session: wrong tool, loop, premature stop, user correction | Feeds the evaluation loop in Section 10 |
| X12 | Curator support | Curator review pass | Near-duplicate or overlapping skills | Replaces part of the curator's LLM review |

X8 and X9 draw directly on published cookbooks. The guardrails cookbook screens inputs and outputs with four hazard Nouls and a severity Score in one request, with named policies mapping the same probabilities to pass, review, block, or route; its point is that the probabilities stay fixed while the application chooses how much evidence it needs.[10] The citation cookbook caught all four planted failures in eight citations; the four accurate ones verified at confidence 0.93 or higher.[18]

## 8. Token economics

### 8.1 Plain-language version

Jev's own bill is tiny. The money is saved in two other places: turns that a cheaper model can handle are sent to one, and every tool result is trimmed before the expensive model reads it, which pays off again on every later call in the session because the conversation is re-sent each time. Against that, some turns sent to the cheap model will fail and be redone on the expensive one. The model below puts those pieces into one formula.

### 8.2 Model

Baseline cost of one turn:

```
C_base = K · (T_in · p_in^R + T_out · p_out^R)
```

Cost of the same turn with the harness:

```
C_harness = K · [ (1 − f) · c_R + f · c_M ]  +  m · f · K · c_R  +  N_d · T_s · p_J

where  c_R = (1 − s) · T_in · p_in^R + T_out · p_out^R
       c_M = (1 − s) · T_in · p_in^M + T_out · p_out^M
```

Fractional saving:

```
S = 1 − C_harness / C_base
```

| Symbol | Meaning |
|---|---|
| K | API calls per turn in the tool loop |
| T_in, T_out | Average input and output tokens per call |
| p_in^R, p_out^R | Reasoning model price per input and output token |
| p_in^M, p_out^M | Cheap model price per input and output token |
| f | Fraction of turns routed to the cheap model (D7) |
| s | Fraction of input tokens removed by result filtering and fewer skill loads (D1, D5) |
| m | Fraction of cheap-routed turns that fail and are re-run on the reasoning model |
| N_d | Jev decisions per turn |
| T_s | Average Jev state tokens per decision |
| p_J | Jev price per input token |
| c_R, c_M | Cost of one call on the reasoning or cheap model after filtering |
| S | Fractional saving versus baseline |

Plain reading: the first bracket is the tool loop with filtered input, split between the two models; the second term adds back the full cost of every cheap turn that had to be redone; the last term is Jev's fee.

### 8.3 Worked example

Assumptions are illustrative, not measured: K = 6 calls per turn; T_in = 25,000; T_out = 800; reasoning model at $5.00 / $30.00 per million tokens and cheap model at $0.75 / $4.50, the two prices quoted in TypeSafe's cascade cookbook;[9] Jev at $0.042 per million input tokens;[2] f = 0.4; s = 0.2; m = 0.1; N_d = 10 decisions of T_s = 4,000 tokens. Prompt-caching discounts are ignored, which overstates both baseline and harness cost in absolute terms.

| Component | Cost per turn |
|---|---|
| Baseline | $0.894 |
| Harness tool loop | $0.491 |
| Re-run of failed cheap turns | $0.030 |
| Jev decisions | $0.0017 |
| Harness total | $0.523 |
| Saving | 41.6% |

Jev's fee is about 0.19% of the baseline. The result is driven almost entirely by f and s.

### 8.4 Sensitivity

Saving S at m = 0.1, varying routing share f (rows) and filtered-token share s (columns):

| f \ s | 0.0 | 0.1 | 0.2 | 0.3 |
|---|---|---|---|---|
| 0.0 | about 0% (Jev fee only) | 8% | 17% | 25% |
| 0.2 | 15% | 22% | 29% | 36% |
| 0.4 | 30% | 36% | 42% | 47% |
| 0.6 | 45% | 49% | 54% | 59% |

Two conclusions follow. Filtering alone, with no model routing at all, is worth up to about a quarter of spend in this model, and it carries less quality risk than routing. And the harness can never cost meaningfully more than baseline on fees; the only way it loses money is a high m, which Phase 4's acceptance criteria bound directly.

## 9. Requirements

### 9.1 Functional requirements

| ID | Requirement |
|---|---|
| FR-1 | Ship as a standalone Hermes plugin (`jev-harness`) using only public plugin APIs; no edits to core files[31] |
| FR-2 | Provide a Jev client wrapper over `typesafe_sdk` with pinned model ID, client-side timeout, and retry with backoff on 429 and 529[4] |
| FR-3 | Define every decision point as a versioned spec file: question IDs, types, instructions, criteria, state builder, policy thresholds, failure direction |
| FR-4 | Log every decision to SQLite under `get_hermes_home()`: spec version, model ID returned by the API, state hash, full answers and probabilities, confidence, policy version, action taken, latency, input tokens |
| FR-5 | Cache decisions by hash of model ID, canonical state, and canonical spec; replay on hit |
| FR-6 | Support modes per decision point: `off`, `shadow` (ask Jev and log, change nothing), `advise`, `enforce` |
| FR-7 | Implement D1, D3 to D7, and X1 to X12 on the surfaces listed in Sections 5 and 7 |
| FR-8 | Implement the ingestion pipeline I1 to I9 as a plugin tool and a reusable library usable from skills and cron jobs |
| FR-9 | Redact secrets from state before transmission |
| FR-10 | Provide `hermes jev` CLI subcommands via `register_cli_command`: status, stats per decision point, replay against a new spec or threshold, export of labelled examples |
| FR-11 | Propose upstream a generic turn-resolution hook for D2; implement D2 behind it once merged |

### 9.2 Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-1 | Added latency per decision (p95) | Under 600 ms; under 1 s for two-stage decisions |
| NFR-2 | Timeout behaviour | Client timeout below `plugins.hook_callback_timeout`; advisory decisions fail open, safety decisions fall back to existing Hermes controls |
| NFR-3 | Determinism | Identical inputs under the same spec, policy, and model ID produce identical actions (verified by replay test) |
| NFR-4 | Prompt-cache safety | No mutation of system prompt, toolsets, or prior messages; verified by comparing cached-token counts with the plugin on and off |
| NFR-5 | Rate-limit headroom | Stay under 1,200 requests per minute[2] by batching questions per decision and skipping Jev for allowlisted read-only tools |
| NFR-6 | Privacy | Only minimal, redacted state leaves the machine; enterprise zero-data-retention evaluated before any client data is processed[2] |
| NFR-7 | Profile safety | All state paths via `get_hermes_home()` |

## 10. Rollout and evaluation

### 10.1 Evaluation data

Hermes' session database already holds real turns, tool calls, and outcomes. Labelled sets are built from it: user corrections and denied approvals are natural negative labels; completed turns with no correction are weak positives. Each decision point gets a held-out set before it leaves shadow mode. Calibration is checked on this data directly, by bucketing Jev's probabilities and comparing each bucket's predicted rate with its observed rate, because calibration is a vendor claim not yet independently shown.[25]

### 10.2 Phases

| Phase | Scope | Mode | Exit criteria |
|---|---|---|---|
| 0 | Client, logging, cache, specs; all decision points wired | Shadow | Two weeks of logs; latency within NFR-1; no cache-hit regression |
| 1 | D5 result filtering, D1 skill suggestion | Advise | Wrong or needless skill loads fall on replayed sessions; no increase in the agent asking for filtered-out content |
| 2 | D4 risk gating, D3 argument checks | Enforce, with the existing detector underneath | Zero missed hazards on a red-team set; human-approval prompts reduced |
| 3 | Names-only skill index experiment; X1, X2, X4 | Advise, then enforce | Input tokens per call fall with no rise in wrong loads |
| 4 | D7 model routing | Enforce for low-difficulty, low-stakes turns only | Measured m below 0.1; user-correction rate on routed turns no worse than baseline; net cost falls after cache effects |
| 5 | Ingestion pipeline I1 to I9 | Enforce with review queue | Field accuracy on a labelled sample at or above reasoning-model-only extraction |
| 6 | D2 fast path (after upstream hook) | Enforce for read-only closed-set calls | No wrong executions in a labelled sample |

### 10.3 Metrics

- Reasoning-model input and output tokens per turn, and cost per turn, with the plugin on and off.
- Per decision point: coverage (share of cases acted on automatically), error rate among automatic actions, band distribution, latency, Jev tokens.
- Agent-level: wrong and needless skill loads, iterations per turn, user corrections, approval prompts, and turns re-run after routing.

## 11. Risks and open questions

| Risk | Mitigation |
|---|---|
| Early-access capacity: rate limits change without notice, and demand has briefly exceeded serving capacity[2][26] | Fail-open design; batching; per-decision kill switches |
| Pricing sustainability: TypeSafe says it cannot yet prove pricing is not subsidised[1] | Economics are dominated by f and s, not Jev's fee (Section 8); a tenfold price rise keeps Jev under 2% of baseline in the worked example |
| Vendor-reported performance | Shadow mode and in-house labelled sets gate every phase |
| Decision quality below top models on some tasks[25] | Confidence bands; high-stakes thresholds; human gate |
| Adversarial content can move Jev's answers[6] | Defence in depth; Jev never the only safety control |
| Thresholds coupled to one model version[25] | Pinned IDs; replay-based re-tuning before any version bump |
| Correlated errors survive composition across decisions[25] | Per-decision logging; end-to-end metrics, not per-call accuracy alone |
| No explanations from Jev[25] | Log full distributions and state hashes; D-point notes state which question fired |
| Proprietary, closed model; architecture and training undisclosed[22] | Keep the plugin's decision interface model-agnostic so an alternative System One model or a local classifier can back the same specs |
| Data leaving the machine | Minimal redacted state; enterprise ZDR before client data |

Open questions:

1. Whether per-turn model switching triggers a prompt-cache miss on the first call of each turn with the configured providers. [UNVERIFIED]
2. Whether Hermes exposes its redaction helper to plugins at the pinned commit. [UNVERIFIED]
3. The exact config path a plugin should use for its own settings block. [UNVERIFIED]
4. Whether upstream will accept a generic turn-resolution hook for D2.
5. How Jev performs on this deployment's non-English content, if any.

## 12. Glossary

| Term | Meaning |
|---|---|
| Agentic harness | The deterministic software around a model that manages the conversation loop, tools, approvals, and state |
| Alias (model) | A model name such as `jev-latest` that points to whichever version is current and can move |
| Autoregressive generation | Producing output one token at a time, each conditioned on the previous ones; how language models write |
| Calibration | Property that, across many predictions, answers given probability p turn out correct about p of the time; says nothing about any single answer |
| Cascade | A pipeline that tries a cheap method first and escalates to an expensive one only when a check fails |
| Choice | Jev question type selecting one option from a defined set |
| Confidence | Jev's single-number summary of how concentrated a Choice or Score distribution is |
| Context rot | Loss of accuracy as a model's input fills with irrelevant material |
| Fan-out (speculative) | Asking every question a workflow might need in one request, then using only the relevant answers |
| Hook | A Hermes extension point where a plugin callback runs at a defined moment |
| Middleware | A Hermes extension point that may rewrite a request or wrap its execution |
| Noul | Jev question type returning the probability that a statement is true |
| Prompt caching | Provider feature that discounts repeated identical prompt prefixes; broken by any change to earlier content |
| Reasoning model | A language model that spends extra output tokens on internal deliberation before answering |
| Re-ranking | Scoring a shortlist from fast search against the query directly to reorder it |
| RLCD | Reinforcement Learning for Calibrated Decisions, TypeSafe's training method |
| RLHF | Reinforcement Learning from Human Feedback, training toward answers human raters prefer |
| Score | Jev question type rating state on ordered, described levels |
| Shadow mode | Running a new decision system alongside the old one, logging its answers without acting on them |
| State | The information Jev evaluates questions against |
| System One / System Two | Kahneman's distinction between fast intuitive judgment and slow deliberate reasoning; TypeSafe's naming source |
| Token | The unit models read and write, roughly three quarters of an English word |

## Sources

[1] https://typesafe.ai/blog/introducing-system-one-models-and-jev — TypeSafe AI - Introducing System One Models and Jev (15 Sep 2026)
[2] https://docs.typesafe.ai/models — TypeSafe docs - Models (jev-1.13.0 limits and pricing)
[3] https://docs.typesafe.ai/introduction — TypeSafe docs - Introduction and primitives
[4] https://docs.typesafe.ai/api — TypeSafe docs - API reference (POST /v1/systemone)
[5] https://docs.typesafe.ai/confidence — TypeSafe docs - Confidence
[6] https://docs.typesafe.ai/model-jaggedness/jev-1.13 — TypeSafe docs - Jev 1.13 jaggedness (known failure modes)
[7] https://docs.typesafe.ai/cookbooks/skill_suggestion — TypeSafe cookbook - Skill suggestion over the Hermes roster
[8] https://docs.typesafe.ai/cookbooks/function_calling — TypeSafe cookbook - Function calling
[9] https://docs.typesafe.ai/cookbooks/sde_cascade — TypeSafe cookbook - Structured-data-extraction cascade
[10] https://docs.typesafe.ai/cookbooks/llm_guardrails — TypeSafe cookbook - Guardrails for LLMs
[11] https://docs.typesafe.ai/cookbooks/classifying_rag_passages — TypeSafe cookbook - Classifying RAG passages
[12] https://docs.typesafe.ai/cookbooks/pre_parsed_value_extraction_cookbook — TypeSafe cookbook - Pre-parsed value extraction
[13] https://docs.typesafe.ai/cookbooks/parallel_questions — TypeSafe cookbook - Parallel questions
[14] https://docs.typesafe.ai/patterns/fan-out — TypeSafe docs - Speculative fan-out
[15] https://docs.typesafe.ai/patterns/intent-routing — TypeSafe docs - Intent routing
[16] https://docs.typesafe.ai/patterns/confidence-routing — TypeSafe docs - Confidence-gated routing
[17] https://docs.typesafe.ai/introduction/coding-agents — TypeSafe docs - Jev with coding agents
[18] https://docs.typesafe.ai/cookbooks/citation_check — TypeSafe cookbook - Double-checking citations
[19] https://docs.typesafe.ai/cookbooks/rerank_typesafe — TypeSafe cookbook - Re-ranking
[20] https://docs.typesafe.ai/concepts/use-case-map — TypeSafe docs - Example use cases
[21] https://www.langchain.com/blog/building-a-harness-with-jev — LangChain - Building a Harness with Jev (17 Sep 2026)
[22] https://en.wikipedia.org/wiki/Jev_%28AI_model%29 — Wikipedia - Jev (AI model)
[23] https://flaviocopes.com/jev — Flavio Copes - A deep dive into Jev
[24] https://theframenews.org/en/typesafe-jev-faster-cheaper-llm-alternative — The Frame News - TypeSafe launches Jev (16 Sep 2026)
[25] https://anthonymaio.substack.com/p/jev-the-language-model-that-wont — Anthony Maio - Jev: The Language Model That Won't Talk
[26] https://techcrunch.com/2026/09/18/a-new-kind-of-ai-model-from-a-chatgpt-inventor-is-thrilling-developers — TechCrunch - A new kind of AI model from a ChatGPT inventor (18 Sep 2026)
[27] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/website/docs/user-guide/features/hooks.md — Hermes Agent - Hooks reference (commit 9514d35)
[28] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/hermes_cli/plugins.py — Hermes Agent - hermes_cli/plugins.py (VALID_HOOKS, register_middleware, register_auxiliary_task)
[29] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/hermes_cli/middleware.py — Hermes Agent - hermes_cli/middleware.py (llm_request and tool_request middleware)
[30] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/tools/approval.py — Hermes Agent - tools/approval.py (_smart_approve)
[31] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/AGENTS.md — Hermes Agent - AGENTS.md (prompt caching and plugin policies)
[32] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/agent/conversation_loop.py — Hermes Agent - agent/conversation_loop.py (llm_request middleware call site)
[33] https://docs.typesafe.ai/cookbooks/semantic_find — TypeSafe cookbook - Line-by-line search
[34] https://docs.typesafe.ai/cookbooks/classification_using_confidence — TypeSafe cookbook - Classification using confidence
[35] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/tools/tool_result_storage.py — Hermes Agent - tools/tool_result_storage.py (large-result persistence)
[36] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/agent/prompt_builder.py — Hermes Agent - agent/prompt_builder.py (build_skills_system_prompt, compact_categories)
[37] https://github.com/NousResearch/hermes-agent/blob/9514d354ca47267c4c2c08dc639dd8f9331abc5d/agent/tool_dispatch_helpers.py — Hermes Agent - agent/tool_dispatch_helpers.py (untrusted_tool_result delimiters)
