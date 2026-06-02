#!/bin/bash
set -x
# ======================================================================
ts=`date +%Y_%m_%d_%H_%M`
log_dir=./logs/scoring
mkdir -p $log_dir
# ======================================================================
API_BASE_URL=https://api.minimaxi.com/v1/chat/completions  # MiniMax OpenAI-compatible chat-completions endpoint (scoring posts directly to this URL)
API_KEY=${MINIMAX_API_KEY:-XXX}  # export MINIMAX_API_KEY=sk-... in your shell; never commit the real key
API_MODEL=MiniMax-M3
# ======================================================================
DATASET_PATH=./data/WebTestBench/WebTestBench.jsonl
OUTPUT_ROOT=./outputs

VERSION=claudecode-gpt-5.1
# ======================================================================
USE_CHECKLIST_Fallback=True

python eval/scoring.py \
    --dataset_path $DATASET_PATH \
    --output_root $OUTPUT_ROOT \
    --version $VERSION \
    --use_checklist_fallback $USE_CHECKLIST_Fallback \
    --api_base_url $API_BASE_URL \
    --api_key $API_KEY \
    --api_model $API_MODEL 2>&1 | tee ${log_dir}/log_${ts}_${VERSION}.log
