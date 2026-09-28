# Roadmap

From the [PRD](PRD.md). Done items link to their results in [RESULTS.md](RESULTS.md).

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
- [x] Dashboard tab: modes with enforce behind a confirmation, shadow decisions, readiness reports, Jev cost chart, skills-audit button
- [x] Config hot-reload: mode changes reach running agents without a gateway restart
- [ ] Memory filter on weaker models and background reviews; more installs' labels
- [ ] Risk gate: "a human should confirm" cases (4 of 6 allowed); a second independent attack author
- [ ] Workstream 3 extras (PRD §7): gateway triage, cron wake gating, citation checks
- [ ] Cross-provider routing via `llm_execution` middleware
