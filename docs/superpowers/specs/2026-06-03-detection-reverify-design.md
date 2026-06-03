# Design: `defect_reverify` — Blind Second-Pass Re-Verification Stage

**Date:** 2026-06-03
**Branch:** feat/detection-reverify-second-pass
**Status:** Approved design → ready for implementation plan

## Problem

The P2 ablation (see `tuning-log.md` "P2 Bundle 消融结果 + 诊断", n=6) proved the detection
bottleneck is **judgment accuracy, not behavior**: the detection agent already attempts
adversarial/forbidden actions, but on covered-but-PASS constraint items it *tries the invalid
action yet misjudges whether the system actually blocked it* (concrete misses: record 0002 id12,
record 0006 id19 — both CS bugs the agent marked PASS). Prompt tweaks (P2 A/B) could not fix this;
the conclusion was that the real lever is **independent re-verification of items the first pass
marked PASS**, not more prompting.

This is the "detection second-pass re-verification" route named as the highest-leverage untried
lever in the session handoff.

## Goals & non-goals

- **Goal:** catch the missed-bug failure mode by independently re-judging every PASS item, lifting
  recall (the current main bottleneck) on the bug-oriented scoring.
- **Goal:** keep baseline runs and historical comparability intact (gated, default-off), and make
  the lever cleanly A/B-able via the existing ablation harness.
- **Non-goal:** re-judge FAIL items (already counted as bug-detected; re-testing them cannot add
  recall and only risks losing caught bugs).
- **Non-goal:** change scoring semantics (`all()` / any-fail-wins stays — see the RFC in
  `tuning-log.md`).

## Decisions (locked with user)

1. **Scope:** re-verify **all PASS items across all four classes** (FT/CS/IX/CT), not just CS.
2. **Independence:** **blind** — the re-verify agent runs in a fresh session/browser, receives only
   each item's `Action`/`Expected`, and never sees the first pass's PASS/FAIL verdict or evidence.
   Rationale: the erroneous PASS is exactly what is untrustworthy; anchoring on it defeats the pass.
3. **Reconciliation:** **union-of-failures** — for any re-checked item, *either* pass reporting FAIL
   makes the final verdict FAIL. Maximizes recall (the goal); consistent with scoring's
   any-fail-wins. Accepted cost: a re-verify false alarm can flip a gold-ok item to FP.
4. **Architecture:** a **new pipeline stage** `defect_reverify` (Approach A), not an inner pass and
   not an offline script — matches the codebase's idempotent/resumable/event-emitting/ablatable
   stage pattern, reuses the existing browser-agent options and the *same* detection prompt (near-zero
   new prompt surface), and the gate preserves baseline + historical comparability.
5. **Granularity:** one fresh re-verify session per record, batch-re-testing all PASS items together
   (cost ≈ 2× detection). Per-item sessions (≈10× cost) are a deliberately deferred knob.

## Architecture

### Pipeline wiring (`eval/agent/claude_code.py`)

`stage_sequence` becomes:

```
server_deploy → checklist_generation → defect_detection → defect_reverify → extract_result_file
```

- `defect_reverify` runs its real logic only when the gate is on; when off it is a **no-op that
  returns True**, so baseline behavior is byte-identical.
- **Gate:** add `--reverify` to `eval/run_agent.py` (default `False`); thread it into the agent
  constructor as `self.reverify_enabled`. `scripts/run_webtester_cc*.sh` are unchanged;
  `scripts/run_*abl.sh` flip the flag for on/off A/B.

### Per-record artifact files (all under `output_dir/`, idempotent/resumable)

- `result.md` — first-pass verdict, **left untouched** (traceability).
- `result_reverify_raw.md` — the blind re-verify agent's raw `# Test Result` output.
- `result_reverified.md` — the union-of-failures reconciled final `# Test Result`.

`extract_result_file` (`eval/agent/base_agent.py:55`) reads `self.final_result_path` instead of a
hardcoded `self.result_path`: a property returning `result_reverified.md` when reverify is enabled
and that file exists, else `result.md`. So `result_extracted.md` (the scored artifact) automatically
picks up the reconciled result with no scoring-side change.

## `defect_reverify()` internals

1. **Skip / gate:** if reverify disabled → no-op `return True`. Else
   `_should_skip_stage(result_reverified.md)` short-circuits on resume.
2. **Extract PASS ids:** reuse the scoring checkbox parser idiom (`- [X] ID:` = pass) on `result.md`
   to get the set of PASS TEST-IDs. **Zero PASS → nothing to re-verify:** copy `result.md` →
   `result_reverified.md` and return True.
3. **Build blind sub-checklist:** filter `checklist.md` to those TEST-IDs, emitting a
   `# Test Checklist` containing only their original `Action`/`Expected`. No first-pass verdict is
   included → genuinely blind.
4. **Fresh blind browser session:** reuse `_get_browser_agent_options(max_turns=...)` and the **same**
   `defect_detection` prompt (zero new prompt surface), feeding the sub-checklist. Write the agent's
   output to `result_reverify_raw.md`.
5. **Reconcile (union-of-failures):** parse the re-verify output; for each TEST-ID, **either pass
   FAIL → final FAIL** (carry the re-verify Bug Report on a flip). First-pass FAIL items are **not
   re-tested and are preserved as-is**. Write `result_reverified.md`.
6. **Observability:** emit `__EVENT__` progress, `_write_stage_success`, `_mark_stage` — identical to
   existing stages.

## Data flow (one record)

```
checklist.md ──┐
               ├─→ defect_detection ─→ result.md (PASS+FAIL)
               │                            │
   take PASS ids ←──────────────────────────┘
        │
        └─→ filter checklist.md → sub-checklist ─→ blind session ─→ result_reverify_raw.md
                                                          │
        result.md ⊕ raw  (union-of-failures) ────────────┴─→ result_reverified.md
                                                          → extract → result_extracted.md → scoring
```

## Error handling / edge cases

- Re-verify session `num_turns > max_turns`, or empty / missing `# Test Result` → **degrade**:
  `result_reverified.md` falls back to a copy of `result.md`. A failed re-verify must never zero out
  an existing result (lesson from the earlier metric-pollution bug).
- A PASS item **absent** from the re-verify output (not re-judged) → keep the first-pass PASS (never
  flip to FAIL on missing evidence).
- Reconciler parse failure → same degrade to `result.md`; `_mark_stage` records a warning but `run()`
  does not abort downstream stages.
- `max_turns` for re-verify: the PASS subset is typically smaller than the full checklist, so the
  existing 150 is ample; can later be scaled to subset size.

## Testing / validation

- **Unit (pure functions, no API spend):**
  - `reconcile(pass1_items, pass2_items)` — union flip, first-pass-FAIL preserved, missing re-verify
    item keeps PASS, zero-PASS short-circuit, degrade path.
  - sub-checklist filter — PASS filtering correct + `Action`/`Expected` fidelity.
- **Integration A/B:** reuse `scripts/run_*abl.sh`; reverify off vs on, same n / model / matcher;
  compare recall/F1 gain vs precision cost (aligned with the P1/P2 ablation methodology). Smoke n=3
  to prove the path, then scale.

## Open follow-ups (out of scope here)

- Per-item re-verify sessions (max independence, ≈10× cost) if batched proves too noisy.
- Whether to also report a precision delta as the FP risk of union-of-failures materializes.
