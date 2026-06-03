# Session Handoff

**Saved:** 2026-06-03T07:14:30Z (UTC) / 2026-06-03 15:14 Asia/Shanghai (CST)
**Branch:** main
**Head:** c49d4fc — Merge pull request #1 from zmypyzd/tuning/p1-checklist-coverage-and-metric-fix

## Current task
Tuning the WebTester baseline to find more bugs. P1 (checklist coverage) + a scoring metric fix + P2 (detection) + the all() RFC are DONE and merged to `main`. The task is now picking up the **routes not yet walked** (see below) — nothing is mid-edit.

## Next concrete step
Decide which untried route to run next (the user wants these remembered). Highest-leverage untried lever: **detection second-pass re-verification** — add a stage/pass that independently re-checks every checklist item the detection marked PASS (especially constraint items), to catch the "agent tried the invalid action but misjudged whether it was blocked" failure mode that P2's prompt tweaks could NOT fix. Start by reading `eval/agent/claude_code.py:132` (`defect_detection`) and `tuning-log.md` "P2 Bundle 消融结果 + 诊断".

## Status of play (this session)
- [x] Diagnosed checklist-coverage bottleneck (~43% of gold bugs unreachable at checklist stage; worst CS/IX)
- [x] Verified scoring.py metrics correct via independent oracle `scripts/verify_metrics.py` (PASS)
- [x] P1 shipped: intent-driven CS/IX enumeration; coverage A/B + real-metric ablation (mean F1 0.167→0.463)
- [x] Found + fixed a metric pollution bug (silent all-pass fallback on unparseable detection output)
- [x] P2 shipped: tooling consistency (P2-0) + adversarial/evidence/format prompt (A/B/C); diagnosed A/B as ~no-op
- [x] all() RFC: keep `all()` (granularity FP minor 4/27; majority-vote would hurt recall)
- [x] Shipped via /gstack-ship → PR #1 merged to `zmypyzd/WebTestBench` main; removed local `upstream` remote; gh default → fork

## WIP / uncommitted
Working tree clean — all work committed and merged (3 commits: 961b0b6 P1+metric-fix, 2573e55 P2, 02463ba all() RFC). Untracked `outputs/`, `data/` are gitignored ablation artifacts (kept locally, not in repo).

## Decisions made
- Rejected app-grounding for the checklist ("white-box trap" suppresses missing-validation CS bugs) — did prompt-only P1 instead.
- P1 cap raised 20→25 ADDITIVELY (do not steal FT slots) — additive avoids regressing FT coverage.
- P2-0 chose "allow white-box consistently" (not MCP-only lock) — matches user rule "any means except reading ground-truth"; `allowed_tools` is auto-approve not a gate, so block via `disallowed_tools`.
- Dropped P2 item "D" (suppress checklist over-splitting) — it targets a scoring artifact and fights P1's coverage goal.
- Keep scoring `all()` semantics — quantified, granularity FP is minor and majority-vote hurts the recall-limited regime.
- Ablation uses sonnet detection + MiniMax-M3 matcher; key read from machine-local `/Users/zmy/intership/minimax_api.md` (NOT committed).
- Ship to the FORK explicitly (`gh pr create --repo zmypyzd/...`) — gh defaults PRs to upstream for forks; this caused an accidental upstream PR (now closed).

## Open questions (ask user before acting)
- **Routes not yet walked — which to run next?**
  1. Detection second-pass re-verification of covered-but-PASS items (untried, likely the real recall lever).
  2. Larger-n confirmation of P1/P2 (current n small: P1 n=3 clean, P2 n=6/1-repeat) — expensive (~$2/record detection, rate-limited).
  3. Micro-tuning noted but not done: rescue Content (CT) coverage (regressed −11.4 under P1), suppress phantom predictions (~6–13/record).
- **Upstream fork detachment** — GitHub still shows "forked from friedrichor/WebTestBench". Full detach needs either (1) GitHub Support "detach fork" ticket (recommended, keeps URL) or (2) DIY: push to a fresh non-fork repo + delete the fork. User leaning unresolved.
- **Branch cleanup** — merged `tuning/p1-checklist-coverage-and-metric-fix` (local+remote) and stale `chore/native-cli-and-minimax-scoring` can be deleted; awaiting user OK.

## Read these first
1. `tuning-log.md` — full investigation + all ablation numbers + the "待办（审查推荐顺序）" list
2. `eval/agent/claude_code.py` — pipeline stages; `defect_detection` ~line 132, `_get_browser_agent_options` ~line 414
3. `eval/scoring.py` — `_parse_pred_items` (robust parser) + `_compute_metrics` (bug-oriented matrix / all())
4. `scripts/verify_metrics.py` — metric oracle; run to confirm scoring still correct
5. `scripts/run_p2abl.sh` + `scripts/gen_checklists.py` — ablation harness pattern to reuse

## Already invoked this session
- 2× independent opus reviewers (P1 design, P2 design) — verdicts folded into `tuning-log.md`; both judged code-verified.
- `scripts/coverage_probe.py` — checklist coverage A/B; results in `outputs/_coverage_probe/summary_*.json`.
- Full-pipeline ablations (run_p1abl / run_p2abl / run_p2_redo) — results in `outputs/p1abl-*`, `outputs/p2abl-*` (gitignored).
- /gstack-ship — pushed branch, opened+merged PR #1 on the fork; closed accidental upstream PR #3.

## Verify state on resume
```
git log --oneline -4 && git remote -v && python scripts/verify_metrics.py | tail -1
```
