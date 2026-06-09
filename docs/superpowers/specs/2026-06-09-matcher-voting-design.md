# Matcher Voting (union τ=1) — Design

**Date:** 2026-06-09 · **Branch:** `tune/matcher-voting`

## Problem

`eval/scoring.py` matches predicted checklist items to gold via a single stochastic
LLM call (`PROMPT_MATCH_ITEM`, MiniMax-M3). The matcher is *conservative* ("intent
over wording; do NOT force matches"), so a single run produces **false-negative
matches** — it leaves real (pred → gold) pairs unmatched.

Measured impact: after writing 5 verified bugs into gold (5 mini apps), the recall
gain only appears when the matcher links the detecting `EX-01` item to the new gold
item. Across 3 fresh runs the *landed* set was {2/5, 4/5, 5/5} — the union over runs is
**5/5 (every pair IS matchable)**, but any single run undercounts, dragging mean recall
from a true ~0.365 down to as low as 0.273. The matcher run is now the binding
constraint on measured recall, not detection or gold completeness.

## Goal / Success Criteria

1. Running the matcher K times and taking the **union** of matches recovers the
   conservative misses, making the real recall reliably visible.
2. **Non-breaking:** `--match_votes 1` (default) reproduces current behavior and
   numbers exactly (single ballot; aggregate of one ballot is the identity).
3. **Ablation-able:** K=1 vs K=3 is a clean A/B.
4. The aggregation is a **pure, unit-tested function**.

## Design Decisions

- **D1 — Aggregation = union τ=1 (user-approved).** A `(pred → gold)` match counts if
  it appears in **≥1** of the K ballots. Rationale: the diagnosed failure mode is
  false-negatives from a conservative matcher; union directly recovers them (e.g. a
  pair that matched in only 1 of 3 runs). Accepted tradeoff: union can admit a rare
  false-positive match, but the matcher errs toward `None`, so wrong-positive matches
  are unlikely.
  - **Multi-gold resolution:** if one `pred` matched *different* gold ids across
    ballots, keep the **most frequent** (mode); ties broken by **smallest gold id**
    (deterministic → reproducible). This preserves the downstream invariant of one
    gold per pred and avoids inflating coverage via one-pred-to-many-gold.
  - Output preserves predicted order (one `(pred_id, gold_id|None)` per predicted id).

- **D2 — Flag `--match_votes K` (int, default 1).** K=1 ⇒ exactly the current single
  call, zero extra cost. K>1 ⇒ K independent matcher calls + union aggregation.

- **D3 — Refactor `_get_matches`.** Extract the existing "build prompt → `_call_api` →
  parse (`ast.literal_eval` + `_clean_match_answer`) → retry" into a private
  `_match_once() -> list[(pred_id, gold_id|None)] | None`. `_get_matches` calls it K
  times, **drops failed ballots** (a single failed/None ballot does not veto the
  record), and aggregates the survivors via the pure `aggregate_ballots`. If **all K**
  ballots fail → return `None` (same as today's failure path). The existing cache-read
  guard and the "empty cache + non-empty preds ⇒ rematch" rule are preserved.

- **D4 — Cache schema.** `score_match_ids.json` gains `"votes": K` (and `"ballots":
  [...]` raw per-run pairs for audit). Cache is reused only when
  `stored_source == source` **and** `stored_votes == K`; otherwise re-match (so
  switching K invalidates stale single-run caches automatically).

- **D5 — Testing.** Pure `aggregate_ballots` unit tests: union recovers a 1-of-3
  match; mode resolves multi-gold; tie → smallest id; all-`None` → `None`; empty input
  → `[]`; K=1 is identity. Plus a manual smoke: re-score the 5-app new-gold subset with
  `--match_votes 3` and confirm 5/5 land and mean recall ~0.36 stably.

## Non-Goals

- Not changing the matcher **prompt** or the **confusion-matrix** logic.
- Not changing the **default** behavior (voting is opt-in; default K=1).
- Not touching detection / gold content.

## Files

- `eval/scoring.py` — `aggregate_ballots` (pure), `_match_once`, modified
  `_get_matches`, cache fields, `--match_votes` arg + plumb through `ScoringPipeline`.
- `tests/test_scoring_voting.py` — new unit tests for `aggregate_ballots`.

## Risks

- **Union false-positive match** — accepted (matcher errs conservative). If observed,
  fall back to a threshold τ via a follow-up flag (out of scope now, YAGNI).
- **Cost** — K× matcher API calls per record when K>1. Default K=1 = no change.
