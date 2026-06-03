# Design: Integrate self-hunt method into WebTester (format reliability + evidence-forced judgment)

Date: 2026-06-04
Branch: `feat/selfhunt-format-and-evidence`
Status: Approved design → spec for implementation planning

## Motivation

A controlled experiment (2026-06-03) ran a white-box adversarial "self-hunt" method against records 0001/0002/0006 under eval-consistent conditions (instruction + deployed app via Playwright/DOM + source/seed; gold never read) and scored it with the project's own `eval/scoring.py` + MiniMax-M3 matcher.

Aggregate result: self-hunt avg **F1 0.157 vs product baseline 0.095** (records 0001/0002/0006).
Per record: 0001 0.000 vs 0.000 (tie); 0002 0.222 vs **0.286** (baseline higher — gold time-drift penalized correct detection as FP); 0006 **0.250** vs 0.000 (self-hunt wins).

**Corrected failure model for 0006 (verified 2026-06-04, fixes an earlier wrong premise):** the baseline did NOT "parse to zero." Re-running both scoring parsers on `outputs/reverify-off/WebTestBench_0006/result_extracted.md`: the canonical checkbox regex yields 0 items, but the header fallback (`scoring.py:545-562`) recovers **30 items** — 25 real FT/CS/IX/CT ids (with mostly-correct PASS/FAIL) **plus 5 phantom `BUG-01..BUG-05`** scraped from `### BUG-0x · <ID>` bug-report headings. `score.json` confirms `num_pred_item: 30, empty_match: true, f1: 0`, and `score_match_ids.json` was never written. So **the failure is at the LLM matcher layer (it returned no usable match → `empty_match` path), not the parser.** Step 1's job is therefore to feed the matcher cleaner input (canonical checkbox, no phantom `BUG-xx`, no duplicate heading noise) — self-hunt's 0006 output was canonical with only 11 items and matched fine. That cleaner-input-helps-matcher link is a **hypothesis to be validated by the Step 1 ablation**, not an established fact; `empty_match` may also be partly transient matcher-API variance (the call has retry but can still return None), which the ablation must control for with repeats.

**Honest calibration:** the self-hunt method does NOT uniformly beat the baseline. The aggregate win is driven entirely by 0006, where the baseline's detection emitted malformed (non-canonical) output that `scoring.py` parsed to zero items. The second, partially-supported advantage is judgment: on 0002 the baseline marked browse/search/filter PASS without observing that the grid was empty ("No events found"), whereas self-hunt judged from the observed DOM.

Therefore the integration ports the two *defensible* advantages, each delivered and ablated independently:
- **Step 1 — output-format reliability** (the proven 0006 win; also the "next higher-leverage route" noted in prior tuning).
- **Step 2 — evidence-forced adversarial judgment** (targets the 0002 "rubber-stamp PASS without observing" failure). Prior prompt-only A/B for adversarial judgment was ~no-op, so Step 2 must be more structural than a prompt tweak.

## Non-goals / constraints

- Do NOT change scoring semantics (bug-oriented confusion matrix) or read gold.
- Do NOT wholesale-replace the two-stage pipeline (checklist_generation → defect_detection). Minimal, additive changes inside it.
- Each step ships behind an ablation flag (default off) so A/B is clean and rollback is trivial.
- Time-drift gold invalidity is a known benchmark issue (see `memory: gold-time-drift-invalidity`); it is NOT fixed here, but Step 2's measurement is chosen to see through it.

## Architecture

Two phases, each independently flagged and ablated. All changes are additive and live inside the existing pipeline (`eval/agent/`, `eval/prompt/`, `eval/scoring.py`).

```
checklist_generation ──> defect_detection ──> extract_result_file ──> scoring
                              │                                          │
                   Step2: evidence schema (prompt)        Step1: normalize_to_canonical
                   + evidence_lint (base_agent)           (scoring-side only, single site)
```

## Step 1 — Output-format reliability (ship first)

**Problem (evidence, corrected):** record 0006's detection output is heading-style; the canonical regex parses 0 items and the header fallback recovers 30 — including 5 phantom `BUG-0x` ids from bug-report headings. Parsing succeeds; the **matcher** then returns empty → `empty_match`/F1 0. Prompt already carries a STRICT FORMAT instruction yet 0006 still emitted heading form, so a prompt-only fix is insufficient. Goal: give the matcher canonical, phantom-free input.

**Load-bearing change:** a single shared function `normalize_to_canonical(text) -> text` that maps any observed detection-output dialect to canonical `- [x] ID:` / `- [ ] ID:` lines:
- heading form `### FT-01 ... PASS` / `#### CS-03 — FAIL` → canonical checkbox with preserved TEST-ID and pass/fail.
- inline status `**IX-04: PASS**` → canonical.
- **drop phantom bug-report ids — do NOT remap.** `### BUG-0x · <ID>` headings are never TEST-IDs (the taxonomy is only FT/CS/IX/CT) and some reference multiple ids (e.g. `BUG-04 · FT-04 / FT-05 / FT-06`). Remapping would collide with the real items already recovered from the canonical "## Test Items" section and could silently drop ids. So: strip any `BUG-?\d+` id entirely; the real ids are recovered from the checklist section. Tighten the fallback header regex to exclude the `BUG-` prefix.
- idempotent on already-canonical input (verified: the canonical checkbox parser already round-trips canonical lines unchanged).

**Where it lives (single site — revised per review M3):**
- `eval/scoring.py`: `_parse_pred_items` calls `normalize_to_canonical` as the single source of truth. **Do NOT** also normalize in `extract_result_file` — that stage is skipped on resumed/existing runs (`_should_skip_stage`, idempotency per CLAUDE.md), so an extract-side normalize would (a) never fire on the hundreds of already-generated `outputs/` runs, (b) mutate the stored `result_extracted.md` artifact away from the raw agent output, and (c) create two normalization timings (fresh vs resumed) — exactly the divergence we want to avoid. Scoring-side-only is idempotent w.r.t. existing outputs and keeps `result_extracted.md` a faithful record.
- `eval/prompt/defect_detection.py`: light reinforcement of the existing STRICT FORMAT block (non-load-bearing).

**Ablation metric (gold-independent, revised per review B2):** "parsed-item-count > 0" is already green at baseline (0006 parses 30 via fallback), so it is NOT a usable gate. Primary gates instead:
- **phantom-id occurrence rate → 0** (real, directly measurable, deterministic).
- **`empty_match` rate drops on 0006-class records** — run **≥2 repeats** to separate format-driven matcher recovery from transient matcher-API variance.
No gold needed for either, so time-drift cannot pollute this signal. (Phantom-id elimination is the guaranteed win; matcher recovery is the hypothesis under test.)

**Flag:** `--canonicalize` (default off). A/B = baseline vs `--canonicalize` on a fixed record set including 0006, ≥2 repeats.

## Step 2 — Evidence-forced adversarial judgment (ship second)

**Problem (evidence):** on 0002 the baseline acknowledged in its own report that the current date is 2026 and events are past, yet still marked FT browse/search/filter PASS — it never observed that the grid rendered "No events found". The failure is *not observing state before judging*. Prior adversarial-prompt A/B was ~no-op, so the lever must be structural.

**Load-bearing changes:**
1. **Output schema (prompt):** `eval/prompt/defect_detection.py` result template requires, for every item, an `- Evidence:` line stating the concrete observed DOM fact used to judge (e.g. `grid shows "No events found"`, `count 5 → 5 after submit`, `toast "Reservation Confirmed!" appeared`). A PASS must be supported by its evidence.
2. **Structural enforcement (code, not prompt):** new `evidence_lint` post-detection step in `eval/agent/base_agent.py` that scans the result for items marked PASS but missing/empty `Evidence`. **DECIDED (2026-06-04): consequence = flag-only (option a)** — emit a `__EVENT__` warning listing the offenders and record the count; do NOT auto-escalate to re-check or auto-flip to FAIL in the first ablation. Rationale: cleanest ablation of the evidence-requirement itself, no compounded side effects; revisit (b) escalate-to-recheck / (c) flip-to-FAIL only if the Step 2 ablation shows the requirement alone is insufficient. The lint (even flag-only) is what makes this "harder than a prompt" — it produces a measurable evidence-coverage signal per run.

**Interaction with Step 1:** Step 2's `Evidence:` sub-bullets are sub-lines under the checkbox; `normalize_to_canonical` (Step 1) must preserve the checkbox line and ignore Evidence sub-bullets during parsing (Evidence is for the lint, not the matcher).

**Ablation metric (time-drift-robust, revised per review M5/m6/m9):**
- **Primary = item-level diagnosis (post-hoc):** on checklist-covered items, count how many "covered-but-PASS" judgments flip to a correct FAIL vs baseline. This is computed *post-hoc from scoring artifacts + the match cache*, not read during the run (same no-cheating posture as the experiment) — it is decoupled from gold's *aggregate F1* but, honestly, still uses gold's labels to define "covered" and "correct"; it is an internal analysis metric, not a black-box one.
- **Secondary = drift-free-subset F1:** scoring.py F1 restricted to records with **no date-windowed empty state** — detectable from source (e.g. a `new Date()` filter that hides all current data), NOT a claim that those records' gold is "valid." Reframed from "valid subset" because a seed-date heuristic cannot certify gold validity (0002 vs 0006 prove gold handles the same drift class inconsistently). Whole-record F1 on drift-contaminated records (e.g. 0002 browse items) is reported but not gated on.
- **Confound controls (required):** the evidence requirement is *designed* to change agent behavior (more `browser_snapshot`/observation), so a raw F1 delta cannot be attributed to "better judgment" alone. Hold the checklist fixed across arms; record **tool-call count** and **per-item coverage** as covariates; report the flip metric **conditioned on items the agent actually reached in both arms**. Also watch the 100-tool-call / `max_turns=150` budget (`defect_detection.py:15`): more observation could exhaust it on large checklists.

**Flag:** `--require-evidence` (default off).

**Parser-safety note (review m7):** `- Evidence:` sub-bullets are correctly ignored by the canonical checkbox regex and preserved as continuation lines by `_extract_test_result_section`. One residual risk: for any *non-canonical* item still routed through the header fallback, an Evidence line containing the word "fail"/"pass" could flip the nearest-token status. Low probability, and Step 1's normalize reduces fallback reliance; the plan should still confine the fallback's `PASS|FAIL` search to the header line, not following sub-bullets.

## Ablation methodology (run once per step)

- Fixed small record set + repeats (mirror the existing tuning cadence, n≈3–6 with ≥2 repeats).
- **Step 1:** metric = phantom-id rate + `empty_match` rate (gold-independent). Gate: phantom-id rate → 0 (guaranteed); `empty_match` rate drops on 0006-class records across repeats (hypothesis); no regression on already-canonical records (idempotency).
- **Step 2:** metric = item-level diagnosis (post-hoc) + drift-free-subset F1, with tool-call/coverage covariates. Gate: covered-but-mis-PASS items flip toward correct FAIL on items reached in both arms; no new false positives (FP-control); budget not exhausted.
- **Hard checkpoint (review n10):** Step 1 must be implemented, ablated, and pass its gate **before** Step 2 is written. The two share no code (Step 1 = `scoring.normalize_to_canonical`; Step 2 = prompt + `evidence_lint`), so each gets its own implementation plan.
- Each step is A/B'd and kept only if it passes its gate. Full record appended to `tuning-log.md`.

## Deliverables / order

1. `feat/selfhunt-format-and-evidence` branch (created).
2. Step 1 implementation + ablation harness + `tuning-log.md` entry → review.
3. Step 2 implementation + ablation harness + `tuning-log.md` entry → review.

## Open items for the implementation plan

- ~~Exact `evidence_lint` consequence~~ — RESOLVED 2026-06-04: flag-only (option a) for the first Step 2 ablation; escalation deferred.
- ~~"valid subset" determination~~ — REFRAMED per review M5 to "drift-free subset" (records with no date-windowed empty state, detectable from source `new Date()` filters), explicitly NOT a gold-validity claim.
- Ablation record-set selection (which records + repeat count) for each step's harness.
- Whether `--canonicalize` should become default-on after Step 1 passes its gate — note (review n11): turning it on retroactively changes scores of all existing `outputs/` runs on re-score, so any cross-run comparison must be rebased explicitly.

## Independent review (2026-06-04)

An independent Opus reviewer flagged a **BLOCKER**: the original spec's "0006 parses to zero / format-crash" premise was factually wrong — verified that 0006 parses 30 items via the header fallback and fails at the matcher (`empty_match`). This spec has been revised accordingly: corrected failure model, Step 1 gate re-pointed to phantom-id/`empty_match` rate (not parse-rate>0), normalize collapsed to scoring-side only (idempotency, M3), `BUG-xx` dropped-not-remapped (M4), Step 2 "valid subset"→"drift-free subset" with confound controls (M5/m6/m9), hard checkpoint between steps (n10). Verdict moved from RECONSIDER toward SHIP-WITH-CHANGES.
