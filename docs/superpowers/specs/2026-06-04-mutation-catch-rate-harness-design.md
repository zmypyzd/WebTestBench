# Mutation Catch-Rate Harness — Design Spec

> **Date:** 2026-06-04
> **Status:** Approved design (pre-implementation)
> **Owner:** zmy
> **Route:** This is the build of mainline route **(B) — measure the detector's *real* bug-finding ability** (user decision 2026-06-04). It is NOT route (A) "maximize benchmark gold-recall". See memory `p1a-offchecklist-merged` (strategic fork, resolved) and `gold-incompleteness-diagnostic`.

## 1. Purpose & motivation

WebTestBench's gold checklist is **known-incomplete and can be stale**, so `eval/scoring.py`'s gold-based precision/recall **cannot** be trusted as the authority on whether the detector is actually getting better at finding bugs. The clearest evidence: the P1-A `EX-NN` off-checklist items were genuinely real bugs but matched **0 gold** — measurable gold-recall could not see them at all.

This harness builds a **gold-independent metric**: inject a *known* bug into an app whose detection currently passes, run the **real** end-to-end detection, and measure whether it catches the bug. Because we author the bug, we own the ground truth — no gold is read, and the metric is immune to gold incompleteness/drift.

**Success criterion:** a reproducible **mutation catch-rate** (overall + by fault class FT/CS/IX/CT) that (a) does not depend on the gold checklist, and (b) supports a controlled A/B between two `defect_detection` prompt versions on an identical, cached set of injected bugs.

### Why this measures what gold-recall can't
The detector gets credit for a catch **whether** it fails an item on its own self-generated checklist **or** surfaces the bug as an `EX-NN` off-checklist finding. That is exactly the real bug-finding capability (including the P1-A off-checklist machinery) that a gold-bounded metric structurally cannot see.

## 2. Non-goals

- **Not** a replacement for `scoring.py`. Gold-recall stays as a demoted regression guard at most.
- **Not** route (A): we are not tuning the detector to flatter gold.
- **Not** a fully-automated "is this mutation observable" oracle. First version uses a lightweight heuristic + explicit flagging; rigorous observability would require a second expensive deploy+reproduce pass (deferred).
- **Not** hand-authored AST mutation operators (considered, rejected in favor of LLM-generated mutations for cross-app coverage). May revisit as a hardening step.

## 3. Architecture

### Pipeline (per app × per mutant)

```
select app  →  generate mutant  →  patch into copy  →  run full detection  →  judge catch  →  aggregate
   (1)             (2)                  (3)                  (4)                 (5)            (6)
```

1. **Select apps** — N drift-free apps from the trusted set (`_eval_trusted.jsonl`, which already hard-excludes time-drifted 0002/0006), spanning categories. Default N=3, `--apps`. Requirement: detection runs cleanly on the un-mutated app (does not pre-crash).
2. **Generate mutant (LLM, chat-only)** — feed `(instruction + relevant source files)` to the model; it injects **one** realistic, **user-reachable**, non-crashing bug and emits:
   - a patch (changed files / unified diff), and
   - a structured **injection record**: `{description, file, fault_class ∈ {FT,CS,IX,CT}, repro_steps}`.
   M distinct mutants per app (default M=2, `--mutants-per-app`).
   - **Reachability constraint (D2):** the generator may inject only bugs observable through a normal UI interaction, and must record concrete `repro_steps`. This keeps a `missed` verdict defensibly a detector blind spot rather than an unreachable-corner artifact, and gives every `missed` a repro for human triage.
   - **Fault-class quota (D3):** the M mutants per app must span fault classes, with **at least one CS or IX cross-state** mutant (the detector's known-weakest classes — see memory `checklist-coverage-bottleneck`). Each mutant is tagged with its `fault_class` so the by-class catch-rate is meaningful.
3. **Patch into a copy** — **never mutate the original app**. Copy to a scratch dir (`outputs/_mutation_probe/<app>/m<k>/app/`), apply the patch there, deploy the copy.
   - **Copy strategy (D5):** copy **source only, excluding `node_modules`**, and **symlink** the copy's `node_modules` back to the original app's (read-only, shared). The mutation only edits source, so dependencies are safely shared; this drops the per-mutant copy cost from hundreds of MB to near-zero. (Smoke-verify the symlink works with Vite on this platform.)
   - **Port allocation:** the harness assigns each concurrent mutant a **unique port** (a `base_port + offset` per mutant, since the agent constructor takes `server_url` directly — see §3 "Reused components"), so concurrent dev servers never collide.
4. **Run full detection** — run the existing `ClaudeCodeWebTester` (its own checklist generation + defect detection, incl. the P1-A EX mechanism) against the mutated deployment → `result_extracted.md`. **This is the expensive step**, one full detection run per mutant.
5. **Judge catch (3-vote majority, D1)** — a new catch-judge prompt (PROMPT_MATCH_ITEM style) decides whether any FAIL item in `result_extracted.md` corresponds to the injection record's bug (on-checklist FAIL **or** `EX-NN` both count). Run the judge **3 times and take the majority** → `caught: bool`. Persist **every vote's** `{caught, matched_item, reason}` as an audit trail. Rationale: the judge is the final link in the ground-truth verdict and a single stochastic mis-call corrupts the metric; the judge is a cheap chat-only call (orders of magnitude cheaper than a detection run), so 3× is small money for a single-point-of-failure mitigation.
6. **Aggregate** — catch-rate overall + **by fault class FT/CS/IX/CT** + by category; write `summary.json` + a console table (coverage_probe.py style); support A/B against a baseline summary.

### Reused components (do not rewrite)
- Deploy / wait-for-server / kill-process → `BaseAgent` (`server_deploy`, `kill_local_server`).
- Full detection → `ClaudeCodeWebTester`. **Integration confirmed (eng review):** the agent constructor (`run_agent.py:100`) already accepts `local_project_dir`, `server_url`, and `output_dir` directly, so the harness points it at the mutated copy + a chosen port with **no agent refactor** — the `record_id[-4:]` port derivation lives in `run_agent.py`, not the agent, so the harness owns port assignment.
- chat-only LLM invocation → copy `coverage_probe.py`'s `run_query`.
- Match-judge prompt paradigm → `PROMPT_MATCH_ITEM`.

### New units (single responsibility, clear interfaces)

| Unit | Location | Responsibility | Input → Output | Depends on |
|---|---|---|---|---|
| **Mutation generator** | `scripts/mutation_lib.py` (functions) | Given an app, have the LLM inject one bug; write patch + injection record; apply patch to a copy | `(app_dir, instruction, k)` → `outputs/_mutation_probe/<app>/m<k>/{patch.diff, injected.json, app/}` | coverage_probe `run_query`; `shutil` copy |
| **Catch-judge prompt** | `eval/prompt/mutation_catch.py` (new template, registered in `USER_PROMPT`) | Decide whether the FAIL items contain the injected bug | `(injected_record, result_extracted_md)` → `{caught, matched_item, reason}` | none (pure prompt) |
| **Harness orchestrator** | `scripts/mutation_probe.py` (entry; mirrors coverage_probe main/CLI/aggregate/AB) | select apps → (reuse/generate) mutants → validity gate → deploy → detection → judge → aggregate `summary.json` + table + A/B | CLI: `--apps --mutants-per-app --regen-mutants --baseline --model` | all of the above |

### Data flow (one line)
`dataset record → mutation_lib makes mutated copy → validity gate → ClaudeCodeWebTester runs the mutated deployment → result_extracted.md → mutation_catch judge → caught bool → aggregate summary.json`

## 4. Two validity-critical mechanisms

These determine whether the metric is trustworthy at all.

### A. Mutants are generated once and CACHED
For an A/B of "new prompt vs old prompt" to be a controlled comparison, **both sides must run against the same set of mutants** — otherwise the comparison measures mutation randomness, not the prompt (the analog of coverage_probe caching its baseline summary).
- After generation, the patch + injection record are **persisted** (`outputs/_mutation_probe/<app>/m<k>/`: `patch.diff` + `injected.json`).
- The harness **reuses existing mutants by default**, re-running only detection + judge; `--regen-mutants` forces regeneration.
- Result: detection-prompt A/B is a true controlled comparison — same known bugs, which prompt version catches more.

### B. Invalid mutations are detected and excluded — never silently counted
An LLM-injected bug may be ① dead code (no effect) or ② app-breaking (white screen / dev server won't start). Both pollute catch-rate. Each mutant passes a **validity gate**:
- **Deployable:** after patching, dev server still starts (reuse BaseAgent's wait logic); fails → mark `invalid: build_broken`.
- **Observable (lightweight):** minimal reality check on the model's `repro_steps` — at least that the patch touched a source file that is actually rendered/executed (not a comment / unreferenced file). Doubtful → mark `suspect`. The D2 reachability constraint (generator may only inject user-reachable bugs + must record `repro_steps`) is the primary defense here; this gate is the cheap backstop. Strict auto-reachability verification (a second deploy+reproduce pass) is deferred to scale-up.
- **Accounting:** catch-rate **denominator = valid mutants only**; invalid/suspect mutants are **explicitly logged, never silently dropped** (research "no silent caps" principle). `summary.json` records `valid / invalid / suspect` counts.

> **Honest caveat (in scope):** the observability check is NOT a 100% automatic strict oracle — strict verification would need another deploy+reproduce (another expensive run). The first version uses a lightweight heuristic + flags and **exposes** the uncertain cases to a human rather than pretending coverage.

## 5. Output format

`outputs/_mutation_probe/summary.json` (and console table, coverage_probe-style):
- Per mutant: `{app, k, fault_class, category, valid|invalid|suspect, caught, votes:[{caught, matched_item, reason} ×3], repro_steps}` (the 3-vote audit trail per D1).
- Aggregate: `catch_rate` overall, `by_fault_class` (FT/CS/IX/CT), `by_category`; counts `valid / invalid / suspect`.
- A/B mode (`--baseline <summary.json>`): per-class catch-rate deltas + a verdict line (mirrors coverage_probe's `compare_and_gate`).

## 6. Error handling

Per-mutant fault tolerance (as in the P1-A harness, commit `1729e25`): if a single mutant fails (generation error / deploy failure / detection exception), mark it and skip — **never abort the whole batch**.

## 7. Scope (first version)

- **Pilot:** ~3 apps × 2 mutants ≈ 6 full detection runs, to prove the pipeline runs end-to-end, catch-rate computes, and the judge is sane — before scaling.
- **Parameterized:** scale is CLI-driven (`--apps`, `--mutants-per-app`), default small.
- **Apps:** drawn from `_eval_trusted.jsonl` (drift-free; excludes 0002/0006).

## 8. Testing (D4 — pure-logic unit tests + 1 smoke)

The expensive I/O (deploy, detection, real LLM gen/judge) is covered by **one end-to-end smoke** (1 app × 1 mutant: confirm `summary.json` fields complete + catch verdict reasonable). The validity-critical **pure logic** gets **unit tests** (cheap, and a bug here silently corrupts the metric / makes A/B compare different mutant sets):
- **Aggregation math:** catch-rate denominator = valid mutants only (invalid/suspect excluded); `by_class` (FT/CS/IX/CT) breakdown.
- **3-vote majority** aggregation (D1).
- **Cache reuse decision:** existing mutant → reuse (no regen); `--regen-mutants` → force regen (validity-critical mechanism A).
- **Per-mutant fault tolerance:** a single mutant failing is marked + skipped, never aborts the batch.
- **Validity gate:** dev-server-fails → `invalid: build_broken`; reachability heuristic → `suspect`.

This is the "engineered enough" coverage for an eval script: test what determines whether the numbers are right; let the expensive I/O ride on the smoke.

## 9. Open questions / risks

1. **Judge reliability:** the catch-judge (matched via MiniMax-M3-class matcher) is itself stochastic; a wrong "caught/missed" call corrupts the metric. Mitigation: log `matched_item` + `reason` for every verdict so calls are auditable; revisit a multi-vote judge if noise is high.
2. **Mutation realism vs the gold distribution:** LLM mutants may not resemble the bug types gold/real users hit. The metric measures catch-rate on *our* fault distribution; keep the fault-class spread visible so skew is detectable.
3. **Observability false-negatives:** a `suspect`-flagged but actually-valid mutant shrinks the denominator. Acceptable for v1 given the explicit flagging; tighten later.
4. **Cost:** each mutant = one full browser+150-turn detection run. Scaling beyond the pilot needs an explicit budget decision.
5. **Generator-output parsing (impl-review):** `parse_injection`'s file-block regex truncates if the injected file content itself contains a literal ` ``` `. This degrades *safely* — a truncated file fails to compile → deploy fails → the mutant is marked `invalid` and excluded from the catch-rate denominator (never inflates the rate). Acceptable for the pilot; revisit (e.g. length-prefixed or sentinel-delimited file blocks) before scaling to richer apps.

## 10. Eng-review decisions (2026-06-04, /plan-eng-review)

Five issues raised, all resolved (D1–D5). Folded into §§2–8 above.

- **D1 — judge reliability:** 3-vote majority catch-judge + per-vote audit trail (was single-vote). The judge is the metric's single point of failure; 3 cheap chat-only calls buy ground-truth credibility.
- **D2 — reachability validity:** constrain the generator to inject only user-reachable bugs + record `repro_steps`; "missed" stays a defensible blind-spot signal, and every missed mutant carries a repro for human triage. Auto-reachability verification deferred.
- **D3 — mutation distribution:** per-fault-class quota (M mutants span classes, ≥1 CS/IX cross-state) so the metric measures the detector's known-weakest classes, not just easy constant-flips.
- **D4 — testing:** pure-logic unit tests (aggregation/denominator/by_class, cache reuse, 3-vote, fault tolerance, validity gate) + 1 end-to-end smoke. "Engineered enough" for an eval script.
- **D5 — copy cost:** copy source only + symlink `node_modules`; per-mutant copy cost → near-zero.

**Integration finding:** `ClaudeCodeWebTester` is reusable as-is against a copied dir + chosen port (constructor takes them directly) — no agent refactor. Low scope risk.

**Outside voice:** skipped (user choice).

## What already exists (reuse map)
| Sub-problem | Existing code | Disposition |
|---|---|---|
| Deploy / wait / teardown | `BaseAgent.server_deploy`, `kill_local_server` | Reuse |
| Full detection | `ClaudeCodeWebTester` | Reuse, no refactor |
| chat-only LLM call | `coverage_probe.py` `run_query` | Copy |
| match-judge paradigm | `PROMPT_MATCH_ITEM` | Reuse pattern |
| aggregate / A-B / gates | `coverage_probe.py` `main`/`compare_and_gate` | Reuse pattern |

## NOT in scope (deferred, with rationale)
- **Auto-reachability verification** (a second deploy+reproduce pass per mutant) — deferred to scale-up; D2 constraint + human triage covers v1.
- **Hand-authored AST mutation operators** — rejected in favor of LLM generation for cross-app coverage; possible hardening later.
- **Scaling past the 3×2 pilot** — gated on an explicit budget decision (cost = one detection run per mutant).
- **Difficulty-graded mutation axis** (review option D3-B) — rejected; fault-class quota chosen instead (grounded in the existing taxonomy).
- **Replacing `scoring.py`** — gold-recall stays as a demoted regression guard at most.

## Failure modes (per new codepath)
| Codepath | Realistic failure | Test? | Error handling? | Silent? |
|---|---|---|---|---|
| mutant generation | LLM emits no-op / unparseable patch | unit (parse) + validity gate | mark `invalid`/`suspect`, skip | No (logged) |
| patch + symlink | symlink unsupported / Vite can't resolve deps | smoke | deploy fails → `invalid: build_broken` | No (logged) |
| deploy (copy) | port collision under concurrency | — | unique port per mutant (design) | No (deploy error) |
| detection run | browser/150-turn timeout or crash | smoke | per-mutant try/except, skip | No (logged) |
| catch-judge | stochastic mis-call | unit (majority) | 3-vote majority + audit trail | No (votes persisted) |
| aggregation | denominator counts invalid mutants | unit | — | **would be silent → unit test is the guard** |

No critical gap (no failure mode that is silent AND untested AND unhandled), provided the D4 aggregation unit test lands.

## Parallelization strategy
| Step | Module | Depends on |
|---|---|---|
| mutation_lib (gen + apply) | `scripts/mutation_lib.py` | — |
| catch-judge prompt | `eval/prompt/mutation_catch.py` | — |
| harness orchestrator | `scripts/mutation_probe.py` | both above |

Lane A: `mutation_lib.py` (independent). Lane B: `mutation_catch.py` (independent). Then C: `mutation_probe.py` (depends on A+B). **Launch A + B in parallel, merge, then C.** No shared module between A and B → no conflict.

## Implementation Tasks
Synthesized from this review. Each derives from a specific finding.

- [ ] **T1 (P1, human: ~3h / CC: ~25min)** — mutation_lib — LLM mutation generator with reachability + fault-class quota
  - Surfaced by: D2 + D3 — reachable-only injection, `repro_steps`, ≥1 CS/IX per app, fault_class tag
  - Files: `scripts/mutation_lib.py`, `eval/prompt/` (a generation prompt template)
  - Verify: generates a parseable patch + `injected.json` for 1 app; unit test on parse
- [ ] **T2 (P1, human: ~1h / CC: ~10min)** — prompt — catch-judge template (PROMPT_MATCH_ITEM style)
  - Surfaced by: pipeline step 5 — judge FAIL items vs injection record
  - Files: `eval/prompt/mutation_catch.py` (registered in `USER_PROMPT`)
  - Verify: smoke spot-check on 1 known caught + 1 known missed
- [ ] **T3 (P1, human: ~4h / CC: ~35min)** — mutation_probe — orchestrator: select→gen/reuse→gate→deploy→detect→3-vote judge→aggregate
  - Surfaced by: D1 (3-vote), D5 (source-copy + symlink), validity gate, cache reuse
  - Files: `scripts/mutation_probe.py`
  - Verify: end-to-end smoke 1 app × 1 mutant; `summary.json` fields complete
- [ ] **T4 (P1, human: ~2h / CC: ~15min)** — tests — pure-logic unit tests (D4)
  - Surfaced by: D4 — aggregation/valid-denominator/by_class, 3-vote majority, cache reuse/--regen, fault tolerance, validity gate
  - Files: `scripts/` test module (match repo test convention if any)
  - Verify: unit tests pass

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 5 issues, 0 critical gaps, all resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — (no UI surface) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — design ready to implement. Outside voice skipped (user choice). Next: `writing-plans` to produce the implementation plan.
