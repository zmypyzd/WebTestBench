# Design: Integrate self-hunt method into WebTester (format reliability + evidence-forced judgment)

Date: 2026-06-04
Branch: `feat/selfhunt-format-and-evidence`
Status: Approved design → spec for implementation planning

## Motivation

A controlled experiment (2026-06-03) ran a white-box adversarial "self-hunt" method against records 0001/0002/0006 under eval-consistent conditions (instruction + deployed app via Playwright/DOM + source/seed; gold never read) and scored it with the project's own `eval/scoring.py` + MiniMax-M3 matcher.

Aggregate result: self-hunt avg **F1 0.157 vs product baseline 0.095** (records 0001/0002/0006).
Per record: 0001 0.000 vs 0.000 (tie); 0002 0.222 vs **0.286** (baseline higher — gold time-drift penalized correct detection as FP); 0006 **0.250** vs 0.000 (self-hunt wins — baseline detection output format-crashed to `empty_match=0`).

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
                              │                      │                    │
                   Step2: evidence schema   Step1: normalize_to_canonical (shared)
                   + evidence_lint           ─────────────────────────────┘
```

## Step 1 — Output-format reliability (ship first)

**Problem (evidence):** record 0006's `result.md` was heading-style and so malformed that `scoring.py`'s own header fallback mis-parsed it, extracting phantom `BUG-01..BUG-05` ids from `### BUG-01 · CS-02` bug-report headings → `empty_match`/0. Prompt already carries a STRICT FORMAT instruction yet 0006 still crashed, so a prompt-only fix is insufficient.

**Load-bearing change:** a single shared function `normalize_to_canonical(text) -> text` that maps any observed detection-output dialect to canonical `- [x] ID:` / `- [ ] ID:` lines:
- heading form `### FT-01 ... PASS` / `#### CS-03 — FAIL` → canonical checkbox with preserved TEST-ID and pass/fail.
- inline status `**IX-04: PASS**` → canonical.
- strip phantom bug-report ids: `### BUG-01 · CS-02` headings must NOT yield a `BUG-01` predicted item; map back to the referenced real TEST-ID (e.g. CS-02) or drop.
- idempotent on already-canonical input.

**Where it lives & is reused:**
- `eval/agent/base_agent.py`: `extract_result_file` runs `normalize_to_canonical` before writing `result_extracted.md` (so the canonical artifact is clean at source).
- `eval/scoring.py`: `_parse_pred_items` calls the same `normalize_to_canonical` (single source of truth) so scoring and extraction never diverge.
- `eval/prompt/defect_detection.py`: light reinforcement of the existing STRICT FORMAT block (non-load-bearing).

**Ablation metric (gold-independent):** per record — parsed-item count > 0 rate, `empty_match` rate, phantom-id occurrence rate. Baseline = current (0006 crashes); target = 0006-class records become parseable (empty_match → real items). No gold needed, so time-drift cannot pollute this signal.

**Flag:** `--canonicalize` (default off). A/B = baseline vs `--canonicalize` on a fixed record set including 0006.

## Step 2 — Evidence-forced adversarial judgment (ship second)

**Problem (evidence):** on 0002 the baseline acknowledged in its own report that the current date is 2026 and events are past, yet still marked FT browse/search/filter PASS — it never observed that the grid rendered "No events found". The failure is *not observing state before judging*. Prior adversarial-prompt A/B was ~no-op, so the lever must be structural.

**Load-bearing changes:**
1. **Output schema (prompt):** `eval/prompt/defect_detection.py` result template requires, for every item, an `- Evidence:` line stating the concrete observed DOM fact used to judge (e.g. `grid shows "No events found"`, `count 5 → 5 after submit`, `toast "Reservation Confirmed!" appeared`). A PASS must be supported by its evidence.
2. **Structural enforcement (code, not prompt):** new `evidence_lint` post-detection step in `eval/agent/base_agent.py` that scans the result for items marked PASS but missing/empty `Evidence`. It emits a `__EVENT__` warning listing the offenders. Configurable consequence (decided in plan): at minimum flag-and-log; optionally escalate flagged PASS items to a targeted re-check or mark FAIL. The lint is what makes this "harder than a prompt."

**Interaction with Step 1:** Step 2's `Evidence:` sub-bullets are sub-lines under the checkbox; `normalize_to_canonical` (Step 1) must preserve the checkbox line and ignore Evidence sub-bullets during parsing (Evidence is for the lint, not the matcher).

**Ablation metric (time-drift-robust):** primary = **item-level diagnosis** — on items that ARE checklist-covered, count how many "covered-but-PASS" judgments flip to a correct FAIL vs baseline, decoupled from gold's pass/fail labels. Secondary = **valid-subset F1**: scoring.py F1 restricted to records without time-drift gold invalidity. Skip whole-record F1 on time-drift-contaminated records (e.g. 0002 browse items) as a primary signal.

**Flag:** `--require-evidence` (default off).

## Ablation methodology (run once per step)

- Fixed small record set + repeats (mirror the existing tuning cadence, n≈3–6 with ≥1 repeat).
- **Step 1:** metric = structural parse-rate / empty_match / phantom-id (gold-independent). Gate: 0006-class records become parseable; no regression in already-canonical records.
- **Step 2:** metric = item-level diagnosis + valid-subset F1. Gate: covered-but-mis-PASS items flip toward correct FAIL; no new false positives introduced (FP-control).
- Each step is A/B'd and kept only if it passes its gate. Full record appended to `tuning-log.md`.

## Deliverables / order

1. `feat/selfhunt-format-and-evidence` branch (created).
2. Step 1 implementation + ablation harness + `tuning-log.md` entry → review.
3. Step 2 implementation + ablation harness + `tuning-log.md` entry → review.

## Open items for the implementation plan

- Exact `evidence_lint` consequence (flag-only vs escalate-to-FAIL vs trigger targeted re-check).
- Ablation record set selection and how "valid subset" (no time-drift) is determined without reading gold (likely: records whose seed dates are current-era, detectable from seed files).
- Whether `--canonicalize` should become default-on after Step 1 passes its gate.
