#!/bin/bash
set -euo pipefail
set -x
# ======================================================================
# Use your locally logged-in Claude Code CLI (native Anthropic models).
# Leave API_BASE_URL / API_KEY empty -> the SDK falls back to CLI credentials.
API_BASE_URL=
API_KEY=
MODEL=sonnet      # native Claude model alias: sonnet / opus / haiku (change to opus for max capability)
VERSION=claudecode-${MODEL##*/}
# ======================================================================
DATA_JSONL_PATH=./data/WebTestBench/WebTestBench.jsonl
PROJECT_ROOT=./data/WebTestBench/web_applications
OUTPUT_ROOT=./outputs
LOG_ROOT=./logs/eval
# ======================================================================
BASE_PORT=6000

python eval/run_agent.py \
    --agent claude_code \
    --data_jsonl_path $DATA_JSONL_PATH \
    --project_root $PROJECT_ROOT \
    --output_root $OUTPUT_ROOT \
    --log_root $LOG_ROOT \
    --version $VERSION \
    --base_port $BASE_PORT \
    --api_base_url "$API_BASE_URL" \
    --api_key "$API_KEY" \
    --model "$MODEL"
