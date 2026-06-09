# Mutation Harness Hardening + Minimal Pilot — Design

**Date:** 2026-06-09 · **Branch:** `tune/mutation-harness-hardening`

## Problem

The mutation catch-rate harness (`scripts/mutation_probe.py` + `mutation_lib.py`,
PR#6) is a gold-independent detection-quality metric: inject a known bug → run the
real `ClaudeCodeWebTester` detection → a 3-vote judge decides if detection caught
it. It was smoke-validated (1 app × 1 mutant) but has three open issues that bias
results or make it operationally unsafe to scale:

1. **Self-eval bias.** `judge_catch` runs via the local Claude Code CLI (sonnet) —
   "sonnet judging sonnet" against sonnet detection. Not independent.
2. **node_modules write-through.** `copy_app_sources` symlinks the mutant copy's
   `node_modules` back to the original app; `server_deploy` (base_agent.py:242)
   **unconditionally** runs `npm install`, which writes through the symlink and can
   corrupt the original app's deps.
3. **No bound on runtime.** The orchestrator `asyncio.gather`s all mutants with no
   concurrency cap and no per-mutant timeout; a slow/hung MiniMax or a stuck
   detection run can stall the whole batch for hours (we hit a multi-hour hang on a
   scoring batch this session).

## Goal / Success Criteria

1. Judge is **independent** (MiniMax-M3 via API), removing the sonnet-self-eval bias.
2. Each mutant copy has an **isolated `node_modules`**; `npm install` can never touch
   the original app.
3. The batch is **bounded**: a concurrency cap + a per-mutant timeout, so no single
   slow/hung run stalls everything.
4. **Non-breaking:** with no judge API config the judge falls back to the current CLI
   path; existing `mutation_lib` unit tests stay green.
5. A **minimal pilot** (3 apps × 1 CS mutant) runs end-to-end on the hardened harness
   and yields a gold-independent CS catch-rate + validity counts + audit trail.

## Design

### D1 — Independent judge (MiniMax-M3)

- New CLI on `mutation_probe.py`: `--judge_api_base_url`, `--judge_api_key`,
  `--judge_model` (default `MiniMax-M3`).
- New `mutation_lib.judge_catch_http(injected, result_md, judge_cfg, votes=3)`:
  builds the same OpenAI-compatible POST as `scoring._call_api`
  (`Authorization: Bearer <key>`, JSON `{model, messages:[{role:user, content:prompt}]}`,
  `requests.post(..., timeout=120)`, retry on failure), runs `votes` times, parses
  each ballot with the existing `parse_catch`, aggregates with `majority_caught`.
  Returns `{caught, votes:[...]}` (same shape as `judge_catch`).
- `run_one_mutant` routes to `judge_catch_http` when a judge base_url is provided,
  else falls back to the existing CLI `judge_catch` (**non-breaking**).
- A small pure helper `use_http_judge(judge_cfg) -> bool` makes the routing
  unit-testable.

### D2 — node_modules isolation (APFS clonefile, copy-on-write)

- In `copy_app_sources`, replace `os.symlink(node_modules)` with an APFS **clonefile**
  copy: `cp -c -R <src>/node_modules <dst>/node_modules` (copy-on-write — near-zero
  cost + space until written, writes isolated from the source).
- Fallback chain: clonefile fails (non-APFS) → real recursive copy (`shutil.copytree`/
  `cp -R`). **Never** fall back to symlink (that re-introduces the write-through bug).
  Log which path was used.
- Result: `npm install` in the mutant copy writes only to the isolated clone.
- Rejected alternatives: plain copy (slow, large disk — defeats the near-zero-cost
  design on every mutant); skip-install (unsafe — vite still writes `.vite` cache
  through a shared node_modules).

### D3 — Timeout + concurrency

- `--concurrency N` (default 2): an `asyncio.Semaphore(N)` wraps `run_one_mutant`.
- `--mutant-timeout S` (default 2400 = 40 min): `asyncio.wait_for` wraps the detection
  `agent.run()`. On `TimeoutError`: mark the mutant `invalid` (excluded from the
  catch-rate denominator), **force `agent.kill_local_server()`** so the dev server is
  reaped, log it, and let the batch continue.
- (Judge HTTP calls already carry `timeout=120`.)

### Pilot (after hardening)

- **3 apps × 1 CS mutant** — `--mutants-per-app 1` (QUOTA[0] = constraint). Apps:
  `WebTestBench_0009`, `0037`, `0080` (known to deploy; adjustable).
- Run params: `--concurrency 2 --mutant-timeout 2400`, judge via MiniMax-M3,
  `--base_port` in a free range, wrapped in an outer shell `timeout` as a backstop.
- Output: CS catch_rate + validity counts (valid/invalid/suspect) + per-mutant
  3-vote audit. This is a pipeline-validation + first measurement, not a verdict.

## Non-Goals

- Not changing the mutation generator prompt or the catch-judge prompt.
- Not changing `base_agent.server_deploy` (isolation is solved on the copy side).
- Not scaling beyond the 3-app probe this round.

## Files

- `scripts/mutation_lib.py` — `judge_catch_http`, `use_http_judge`, clonefile copy in
  `copy_app_sources`; `import requests`.
- `scripts/mutation_probe.py` — judge CLI args + routing, `--concurrency` semaphore,
  `--mutant-timeout` `wait_for` + kill-on-timeout.
- `tests/test_mutation_lib.py` — tests for `use_http_judge` routing and the judge-HTTP
  aggregation (mock the HTTP call; reuse `majority_caught`/`parse_catch`). Existing 11
  tests stay green.

## Risks

- **Pilot cost** — 3 × (~25–45 min detection) ≈ 1.5–2 h, plus CLI rate limits. Bounded
  by `--mutant-timeout` + outer shell `timeout`; concurrency 2 keeps load modest.
- **clonefile portability** — `cp -c` is APFS-specific; the real-copy fallback covers
  other filesystems (slower but correct).
- **Judge HTTP cost** — 3 votes × MiniMax-M3 (~60-80 s/call) per mutant; cheap vs the
  detection run.
