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
2. **Generate mutant (LLM, chat-only)** — feed `(instruction + relevant source files)` to the model; it injects **one** realistic, observable, non-crashing bug and emits:
   - a patch (changed files / unified diff), and
   - a structured **injection record**: `{description, file, fault_class ∈ {FT,CS,IX,CT}, how_to_observe}`.
   M distinct mutants per app (default M=2, `--mutants-per-app`).
3. **Patch into a copy** — **never mutate the original app**. Copy to a scratch dir (`outputs/_mutation_probe/<app>/m<k>/app/`), apply the patch there, deploy the copy.
4. **Run full detection** — run the existing `ClaudeCodeWebTester` (its own checklist generation + defect detection, incl. the P1-A EX mechanism) against the mutated deployment → `result_extracted.md`. **This is the expensive step**, one full detection run per mutant.
5. **Judge catch** — a new catch-judge prompt (PROMPT_MATCH_ITEM style) decides whether any FAIL item in `result_extracted.md` corresponds to the injection record's bug (on-checklist FAIL **or** `EX-NN` both count) → `{caught: bool, matched_item, reason}`.
6. **Aggregate** — catch-rate overall + **by fault class FT/CS/IX/CT** + by category; write `summary.json` + a console table (coverage_probe.py style); support A/B against a baseline summary.

### Reused components (do not rewrite)
- Deploy / wait-for-server / kill-process → `BaseAgent` (`server_deploy`, `kill_local_server`).
- Full detection → `ClaudeCodeWebTester` (checklist generation + defect detection + EX).
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
- **Observable (lightweight):** minimal reality check on the model's self-reported `how_to_observe` — at least that the patch touched a source file that is actually rendered/executed (not a comment / unreferenced file). Doubtful → mark `suspect`.
- **Accounting:** catch-rate **denominator = valid mutants only**; invalid/suspect mutants are **explicitly logged, never silently dropped** (research "no silent caps" principle). `summary.json` records `valid / invalid / suspect` counts.

> **Honest caveat (in scope):** the observability check is NOT a 100% automatic strict oracle — strict verification would need another deploy+reproduce (another expensive run). The first version uses a lightweight heuristic + flags and **exposes** the uncertain cases to a human rather than pretending coverage.

## 5. Output format

`outputs/_mutation_probe/summary.json` (and console table, coverage_probe-style):
- Per mutant: `{app, k, fault_class, category, valid|invalid|suspect, caught, matched_item, reason}`.
- Aggregate: `catch_rate` overall, `by_fault_class` (FT/CS/IX/CT), `by_category`; counts `valid / invalid / suspect`.
- A/B mode (`--baseline <summary.json>`): per-class catch-rate deltas + a verdict line (mirrors coverage_probe's `compare_and_gate`).

## 6. Error handling

Per-mutant fault tolerance (as in the P1-A harness, commit `1729e25`): if a single mutant fails (generation error / deploy failure / detection exception), mark it and skip — **never abort the whole batch**.

## 7. Scope (first version)

- **Pilot:** ~3 apps × 2 mutants ≈ 6 full detection runs, to prove the pipeline runs end-to-end, catch-rate computes, and the judge is sane — before scaling.
- **Parameterized:** scale is CLI-driven (`--apps`, `--mutants-per-app`), default small.
- **Apps:** drawn from `_eval_trusted.jsonl` (drift-free; excludes 0002/0006).

## 8. Testing

End-to-end smoke with 1 app × 1 mutant: confirm `summary.json` fields are complete and the catch verdict is reasonable, then open up scale.

## 9. Open questions / risks

1. **Judge reliability:** the catch-judge (matched via MiniMax-M3-class matcher) is itself stochastic; a wrong "caught/missed" call corrupts the metric. Mitigation: log `matched_item` + `reason` for every verdict so calls are auditable; revisit a multi-vote judge if noise is high.
2. **Mutation realism vs the gold distribution:** LLM mutants may not resemble the bug types gold/real users hit. The metric measures catch-rate on *our* fault distribution; keep the fault-class spread visible so skew is detectable.
3. **Observability false-negatives:** a `suspect`-flagged but actually-valid mutant shrinks the denominator. Acceptable for v1 given the explicit flagging; tighten later.
4. **Cost:** each mutant = one full browser+150-turn detection run. Scaling beyond the pilot needs an explicit budget decision.
