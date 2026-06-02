# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

WebTestBench is a **benchmark for evaluating computer-use agents on end-to-end automated web testing**. The harness drives an agent (the "WebTester" baseline) against AI-generated web applications, has it produce PASS/FAIL test results, and scores those results against a gold checklist. This is an evaluation/research codebase, not a web application itself — the web apps under test live under `data/WebTestBench/web_applications/` (gitignored) and are deployed/torn down automatically by the harness.

## Prerequisites & environment

- Python `>=3.11`, Node.js `18+` (required by Claude Code).
- Install Python deps: `pip install -r requirements.txt` (includes `claude-agent-sdk`).
- Claude Code CLI plus the Playwright MCP server must be installed and logged in:
  - `npm install -g @anthropic-ai/claude-code`
  - `claude mcp add playwright npx @playwright/mcp@0.0.61` — **pin to `0.0.61`; do NOT use `@latest`** (latest breaks Claude Code's MCP access). The version is hardcoded in `eval/agent/claude_code.py` (`_get_browser_agent_options`); if you bump it, update that file too.
- API access is routed through an OpenAI-compatible provider (OpenRouter by default). Models are selected by setting `ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL` all to the same target model, and pointing `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` at the provider while keeping `ANTHROPIC_API_KEY=""`.

## Common commands

All scripts in `scripts/` have `API_BASE_URL`, `API_KEY`, `MODEL` placeholders set to `XXX` at the top — **edit these before running**. Dataset/output/log paths are also defined there.

```bash
# Unpack the web apps under test (one-time, before evaluation)
python process/unzip_web_applications.py [--overwrite]

# Run the WebTester agent over the dataset (single process)
bash scripts/run_webtester_cc.sh

# Same, sharded across processes (JOBS=8 by default, splits the JSONL)
bash scripts/run_webtester_cc_parallel.sh

# Score agent outputs against the gold checklist (LLM-based matching)
bash scripts/run_scoring.sh

# Oracle scoring (uses gold-based defect detection variant)
bash scripts/run_scoring_oracle.sh
```

Run the entrypoints directly to control args (see `parse_args` in each):

```bash
python eval/run_agent.py --agent claude_code --data_jsonl_path <jsonl> \
  --project_root <web_apps_dir> --output_root <out> --log_root <logs> \
  --version <label> --base_port 6000 --api_base_url <url> --api_key <key> --model <model>

python eval/scoring.py --dataset_path <jsonl> --output_root <out> --version <label> \
  --use_checklist_fallback True --api_base_url <url> --api_key <key> --api_model <model>
```

To run a **single sample**, point `--data_jsonl_path` at a JSONL file containing just that one record. There is no per-test runner — granularity is one dataset record per line.

## Architecture

### Two-stage agent pipeline

`eval/run_agent.py` is the entrypoint. It reads the dataset JSONL line by line and, per record, instantiates an agent and calls `agent.run()`. Outputs and logs are grouped under `<output_root>/<version>/<record_id>/` and `<log_root>/<version>/<record_id>/`.

The agent runs a fixed `stage_sequence` (see `ClaudeCodeWebTester.run` in `eval/agent/claude_code.py`):

1. **`server_deploy`** (in `BaseAgent`) — kills any process on the target port, runs `npm install` + `npm run dev` for the record's local project, and waits (up to ~80s) for the server to respond. Each record gets a deterministic port: `base_port + int(record_id[-4:])`. Skipped when `server_url` is a remote http URL rather than `http://localhost`.
2. **`checklist_generation`** — chat-only Claude Code session (no browser, `max_turns=5`) that turns the development instruction into a structured `# Test Checklist` markdown → `checklist.md`.
3. **`defect_detection`** — Claude Code session **with the Playwright MCP server** (`max_turns=150`) that drives the deployed app per the checklist and emits a `# Test Result` markdown → `result.md`.
4. **`extract_result_file`** (in `BaseAgent`) — parses the `# Test Result` section out of `result.md` → `result_extracted.md`, the canonical scored artifact.

`finally` always calls `kill_local_server`. The pipeline is **idempotent/resumable**: each stage calls `_should_skip_stage`, which short-circuits if its output file already exists, so re-running continues where a prior run stopped.

### Agent class hierarchy & registry

- `BaseAgent` (`eval/agent/base_agent.py`) holds shared logic: server deploy/teardown, result extraction, file verification, markdown writing, and the structured **event system** (`_emit_event` writes `__EVENT__ {json}` lines to stdout and the per-record log; this is how external tooling observes progress).
- `ClaudeCodeWebTester` (`eval/agent/claude_code.py`) implements the two LLM stages and Claude Agent SDK wiring. `_get_chat_agent_options` vs `_get_browser_agent_options` are the key difference — the browser variant attaches the Playwright MCP server and restricts tools to `PlaywrightTools`.
- `claude_code_gold.py` is a variant used by the oracle path.
- Agents are looked up by key via `AGENT_REGISTRY` in `eval/agent/__init__.py` (`AVAILABLE_AGENTS`). `--agent claude_code` selects `ClaudeCodeWebTester`. The registry imports lazily and warns (not fails) on import errors, so a broken agent module won't crash other agents.

### Tool allowlist

`eval/tools.py` defines `PlaywrightTools` — the exact allowlist of `mcp__playwright__browser_*` tools the defect-detection stage may use. `browser_take_screenshot` is explicitly **disallowed** (the agent works from accessibility snapshots via `browser_snapshot`, not pixels). Edit this list to widen/narrow agent capabilities.

### Prompts

`eval/prompt/` holds `string.Template` prompts registered in `USER_PROMPT`. The checklist taxonomy is central to scoring — items are classed as **Functionality (FT) / Constraint (CS) / Interaction (IX) / Content (CT)**, and these four classes (`functionality`, `constraint`, `interaction`, `content`) are the per-class scoring buckets.

### Scoring

`eval/scoring.py` (`ScoringPipeline`) compares predicted items in `result_extracted.md` against the gold `checklist` field in each dataset record. The flow per record: parse gold + predicted items → **LLM-match** predicted to gold (`PROMPT_MATCH_ITEM`, cached in `score_match_ids.json`) → compute precision/recall/F1 + coverage.

Key semantics to understand before touching this file:
- The confusion matrix is **bug-oriented**: a gold item with `pass=False` means "a real bug exists here". TP = gold-bug AND prediction-fail; FP = gold-ok AND prediction-fail; FN = gold-bug AND (prediction-pass or uncovered); TN otherwise. So precision/recall measure *bug-detection*, not raw item agreement.
- Metrics are reported `overall`, `overall_no_missing` (excludes records with missing results / empty matches), `by_category` (7 app categories), and `by_class` (the four checklist classes — only classes containing at least one gold bug are scored).
- `--use_checklist_fallback` lets a record fall back to `checklist.md` (all items treated as pass) for matching/coverage when `result_extracted.md` is missing or empty.
- Outputs: per-record `score.json`, plus aggregate `score_avg.json`, `<version>_score.xlsx`, and `missing_results.json` at the version root.

### Per-record output files

Inside `<output_root>/<version>/<record_id>/`:
- `checklist.md` — generated test checklist
- `result.md` / `result_extracted.md` — raw / extracted test results
- `session_meta.json` — Claude Code session IDs, token usage, cost, per-stage `stage_success`, message statistics
- `dev_server.log` — `npm run dev` output
- `score.json` / `score_match_ids.json` — scoring artifacts

## Conventions & gotchas

- `data/`, `outputs/`, `logs/`, and `claude_code_cwd/` are gitignored. The dataset (`data/WebTestBench/WebTestBench.jsonl`) and web apps are obtained separately (HuggingFace dataset / `process/unzip_web_applications.py`).
- A dataset record's `index` field is the `record_id` and must exist; its last 4 digits drive the dev-server port, so they need to be numeric.
- Stages write progress as `__EVENT__`-prefixed JSON to stdout — don't strip or reformat these if downstream tooling depends on them.
- `run_agent.py` calls `sys.exit(1)` on the first record that raises, so a single hard failure stops the whole run (this is intentional for catching setup errors early).
