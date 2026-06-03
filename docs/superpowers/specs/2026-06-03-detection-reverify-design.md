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
3. **Re-verify prompt — disconfirmation, NOT the detection prompt verbatim.** *(Revised after
   independent opus review.)* Reusing the detection prompt would re-sample the *same* judgment
   distribution from the same model on a *systematic* judgment error ("tried the invalid action but
   misjudged whether it was blocked"), so independence alone buys near-zero recall on the diagnosed
   misses while adding FP risk. The re-verify stage instead uses a **new, sharper disconfirmation
   prompt** whose sole job is to *prove the item does NOT work*: for any constraint item it must
   demonstrate the invalid state was **actually persisted/accepted** (re-read the DOM/state after
   submit, or read the source guard); "looked blocked" is not acceptable evidence. The verbatim
   gold/reference/answer-file prohibition is preserved.
4. **Reconciliation — evidence-gated union-of-failures + pre-registered kill criterion.** *(Revised
   after review.)* A re-verify FAIL flips PASS→FAIL **only if it carries a concrete `Bug Report`**
   (a bare/empty FAIL does not flip — it keeps the first-pass PASS). All four classes remain in
   scope. The validation plan pre-registers a kill criterion (below): if enabling reverify drops
   precision more than it raises recall, the lever is judged a failure. Rationale: the `all()` RFC
   showed 23/27 FPs are detection over-marking; an ungated second blind judge OR's its false alarms
   in with zero damping (`scoring.py` `pred_pass = all(...)`), so the gate caps FP blast radius
   without sacrificing the all-classes recall scope.
5. **Architecture:** a **new pipeline stage** `defect_reverify` (Approach A), not an inner pass and
   not an offline script — matches the codebase's idempotent/resumable/event-emitting/ablatable
   stage pattern, reuses the existing browser-agent *options* (tool allowlist), and the gate
   preserves baseline + historical comparability. (The prompt is new — see Decision #3.)
6. **Granularity:** one fresh re-verify session per record, batch-re-testing all PASS items together.
   Honest cost: the PASS subset is most of the checklist in the recall-limited regime (~80%+), so a
   re-verify session is ~1.8× a detection run, i.e. **~2.8× total pipeline cost per record**, not
   "2×". Per-item sessions (≈10× cost, max independence) are a deliberately deferred knob. Batch runs
   MUST be record-isolated (the tuning-log's "按记录隔离" lesson) to survive external rate limits.

## Architecture

### Pipeline wiring (`eval/agent/claude_code.py`)

`stage_sequence` becomes:

```
server_deploy → checklist_generation → defect_detection → defect_reverify → extract_result_file
```

- `defect_reverify` runs its real logic only when the gate is on; when off it is a **no-op that
  returns True**, so baseline behavior is byte-identical.
- **Gate plumbing (the constructor currently DROPS kwargs — must add real plumbing):**
  `ClaudeCodeWebTester.__init__` takes `**kwargs` and never stores them (so today `record=...` passed
  at `run_agent.py` is silently swallowed). Implementation must: (a) add `--reverify` as a
  `store_true` in `parse_args`; (b) store `self.reverify_enabled = kwargs.get("reverify", False)` in
  `__init__`; (c) pass `reverify=args.reverify` in **both** `agent_cls(...)` constructions in
  `run_agent.py` — the `probe_agent` and the real `agent`. `scripts/run_webtester_cc*.sh` are
  unchanged; `scripts/run_*abl.sh` flip the flag for on/off A/B.

### Files touched

- **New:** `eval/prompt/defect_reverify.py` — the disconfirmation prompt (Decision #3), registered in
  `eval/prompt/__init__.py` `USER_PROMPT["defect_reverify"]`. Takes the same `$instruction`,
  `$server_url`, `$checklist` (the PASS sub-checklist) template vars; keeps the gold-file prohibition
  verbatim.
- `eval/agent/claude_code.py` — new `defect_reverify()` method; insert into `run()` `stage_sequence`;
  store `self.reverify_enabled` + reverify paths in `__init__`; `final_result_path` property.
- `eval/agent/base_agent.py` — `extract_result_file` reads `self.final_result_path`
  (`base_agent.py:64`); error-rename branch (`:74-75`) renames the extracted-from file.
- `eval/run_agent.py` — `--reverify` arg + pass `reverify=` into both `agent_cls(...)` calls.
- **New tests** under `tests/` (or repo's existing test location) — pure-function unit tests.
- `scripts/run_*abl.sh` (or a new `run_reverify_abl.sh`) — fresh-dir, record-isolated on/off A/B.

### Per-record artifact files (all under `output_dir/`, idempotent/resumable)

- `result.md` — first-pass verdict, **left untouched** (traceability).
- `result_reverify_raw.md` — the blind re-verify agent's raw `# Test Result` output.
- `result_reverified.md` — the evidence-gated union-of-failures reconciled final `# Test Result`.

`extract_result_file` (`eval/agent/base_agent.py:55`) reads `self.final_result_path` instead of the
hardcoded `self.result_path` **at `base_agent.py:64`**: a property returning `result_reverified.md`
when reverify is enabled and that file exists, else `result.md`. The error-rename branch
(`base_agent.py:74-75`, renames the extracted-from file to `result-error_*.md`) must rename the file
actually extracted (`self.final_result_path`), not a hardcoded `result.md`. So `result_extracted.md`
(the scored artifact) automatically picks up the reconciled result with no scoring-side change.

**Resume precondition for A/B (do not skip).** `run()` short-circuits at `claude_code.py:59` when
`result_extracted.md` already exists. Therefore enabling `--reverify` on an output dir from a prior
baseline run is a **no-op** — it re-scores the stale extracted file. The on-arm of any A/B MUST run
into a **fresh output dir** (the ablation harness already stages fresh per-version dirs). State this
in the validation section and in the run scripts; re-running in place yields a false null result.

## `defect_reverify()` internals

1. **Skip / gate:** if reverify disabled → no-op `return True`. Else
   `_should_skip_stage(result_reverified.md)` short-circuits on resume.
2. **Extract PASS ids — from the extracted `# Test Result` section, NOT raw `result.md`.** Raw
   `result.md` contains pre-`# Test Result` preamble/prose summaries (this is *why*
   `extract_result_file` calls `_extract_test_result_section` at `base_agent.py:64` before parsing);
   running the checkbox regex over raw text can match stray/duplicate checkbox lines in that
   preamble. So reverify first calls the same extractor on `result.md` content, then parses PASS ids
   (`- [X] ID:`) from the extracted section. **Zero PASS → nothing to re-verify:** copy `result.md` →
   `result_reverified.md` and return True.
3. **Build blind sub-checklist:** filter `checklist.md` to those TEST-IDs, emitting a
   `# Test Checklist` containing only their original `Action`/`Expected`. No first-pass verdict is
   included → genuinely blind. **ID drift:** a PASS id absent from `checklist.md` is dropped from the
   sub-checklist (→ never re-verified → keeps first-pass PASS, the safe degrade direction); **log the
   dropped-id count** rather than swallowing it silently.
4. **Fresh blind browser session:** reuse `_get_browser_agent_options(max_turns=...)` (same tool
   allowlist) but feed the **new disconfirmation prompt** (Decision #3), substituting instruction /
   server_url / sub-checklist. The prompt instructs per-item: perform the action, then **re-confirm
   the underlying state after the action** (re-read DOM/state, or read the source guard) before
   concluding — countering the batched-session "rushed shallow pass" pressure. Write the agent's
   output to `result_reverify_raw.md`.
5. **Reconcile (evidence-gated union-of-failures):** extract the re-verify `# Test Result` section
   and parse it. For each PASS TEST-ID: if the re-verify verdict is FAIL **and carries a concrete
   `Bug Report` block** → final FAIL (carry that Bug Report); a bare/empty re-verify FAIL, or a PASS
   id **absent** from the re-verify output, **keeps the first-pass PASS** (never flip on missing/
   evidence-less signal). First-pass FAIL items are **not re-tested and preserved as-is**. Write
   `result_reverified.md`.
6. **Observability:** emit `__EVENT__` progress (including dropped-id and flip counts),
   `_write_stage_success`, `_mark_stage` — identical to existing stages.

## Data flow (one record)

```
checklist.md ──┐
               ├─→ defect_detection ─→ result.md (PASS+FAIL)
               │                            │
   take PASS ids ←──────────────────────────┘
        │
        └─→ filter checklist.md → sub-checklist ─→ blind DISCONFIRMATION session ─→ result_reverify_raw.md
                                                          │
   result.md ⊕ raw  (evidence-gated union-of-failures) ──┴─→ result_reverified.md
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
  - `reconcile(pass1_items, pass2_items)` — evidence-gated flip (FAIL **with** Bug Report flips;
    bare FAIL does **not**), first-pass-FAIL preserved, missing re-verify item keeps PASS, zero-PASS
    short-circuit, degrade path.
  - PASS-id extraction — runs over the *extracted* `# Test Result` section, ignores preamble prose.
  - sub-checklist filter — PASS filtering correct, `Action`/`Expected` fidelity, dropped-id count on
    ID drift.
- **Integration A/B:** reuse `scripts/run_*abl.sh`; reverify off vs on, same n / model / matcher,
  **each arm into a fresh output dir** (resume precondition above), record-isolated. Smoke n=3 to
  prove the path, then scale.
- **Pre-registered kill criterion (Decision #4):** declared *before* the run. The lever is judged a
  **failure and not shipped** if, aggregated over the A/B set, enabling reverify drops precision by
  more than it raises recall (i.e. ΔF1 ≤ 0 driven by precision loss), or if new FPs ≥ new TPs.
  Success = recall/F1 up with precision cost inside that bound.

## Open follow-ups (out of scope here)

- Per-item re-verify sessions (max independence, ≈10× cost) if batched proves too noisy.
- Whether to also report a precision delta as the FP risk of union-of-failures materializes.
