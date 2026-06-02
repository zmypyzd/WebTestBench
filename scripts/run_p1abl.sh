#!/bin/bash
# P1 real-metric ablation: run detection for both arms (sonnet, CLI creds),
# then score both with MiniMax matcher. Run AFTER the smoke (old/0002) finishes.
# Old arm uses pre-placed OLD-prompt checklists; new arm uses NEW-prompt checklists.
set -uo pipefail
cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench

DATA=data/WebTestBench/_p1abl3.jsonl
PROJ=data/WebTestBench/web_applications
KEY=$(grep '^sk-' /Users/zmy/intership/minimax_api.md | head -1 | tr -d '[:space:]')
BASE=https://api.minimaxi.com/v1/chat/completions
SMODEL=MiniMax-M3

echo "########## OLD ARM detection (port 6000; 0002 skipped if done) ##########"
python eval/run_agent.py --agent claude_code --data_jsonl_path $DATA \
  --project_root $PROJ --output_root outputs --log_root logs/eval \
  --version p1abl-old --base_port 6000 \
  --api_base_url "" --api_key "" --model sonnet
echo "old arm exit: $?"

echo "########## NEW ARM detection (port 7000) ##########"
python eval/run_agent.py --agent claude_code --data_jsonl_path $DATA \
  --project_root $PROJ --output_root outputs --log_root logs/eval \
  --version p1abl-new --base_port 7000 \
  --api_base_url "" --api_key "" --model sonnet
echo "new arm exit: $?"

echo "########## SCORE OLD ARM ##########"
python eval/scoring.py --dataset_path $DATA --output_root outputs \
  --version p1abl-old --api_base_url "$BASE" --api_key "$KEY" --api_model "$SMODEL"

echo "########## SCORE NEW ARM ##########"
python eval/scoring.py --dataset_path $DATA --output_root outputs \
  --version p1abl-new --api_base_url "$BASE" --api_key "$KEY" --api_model "$SMODEL"

echo "########## DONE — compare score_avg.json ##########"
echo "OLD:"; cat outputs/p1abl-old/score_avg.json
echo "NEW:"; cat outputs/p1abl-new/score_avg.json
