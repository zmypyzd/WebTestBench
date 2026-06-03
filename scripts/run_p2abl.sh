#!/bin/bash
# P2 detection ablation (Bundle, 2 arms x 2 repeats x 6 records = 24 detection runs).
# Holds NEW-prompt checklists fixed (pre-placed); varies ONLY the detection prompt:
#   BASE = P2-0 tooling fix, NO A/B/C   vs   P2 = + adversarial/evidence/format (A/B/C).
# Checklist generation is skipped (checklist.md pre-placed). Scores all 4 with MiniMax.
set -uo pipefail
cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench

DATA=data/WebTestBench/_p2abl.jsonl
PROJ=data/WebTestBench/web_applications
CL=outputs/p2abl-cl
RECS="0001 0002 0005 0006 0007 0024"
KEY=$(grep '^sk-' /Users/zmy/intership/minimax_api.md | head -1 | tr -d '[:space:]')
SBASE=https://api.minimaxi.com/v1/chat/completions
SMODEL=MiniMax-M3
BASE_PROMPT=/tmp/defect_detection_BASE.py
P2_PROMPT=/tmp/defect_detection_P2.py
DET=eval/prompt/defect_detection.py

place () { for r in $RECS; do mkdir -p outputs/$1/WebTestBench_$r; cp $CL/WebTestBench_$r/checklist.md outputs/$1/WebTestBench_$r/checklist.md; done; }
run_det () { python eval/run_agent.py --agent claude_code --data_jsonl_path $DATA --project_root $PROJ \
  --output_root outputs --log_root logs/eval --version "$1" --base_port "$2" \
  --api_base_url "" --api_key "" --model sonnet; echo "[$1 exit $?]"; }

for V in p2abl-base-r1 p2abl-base-r2 p2abl-p2-r1 p2abl-p2-r2; do place $V; done

echo "########## BASE ARM (detection = P2-0 only, no A/B/C) ##########"
cp "$BASE_PROMPT" "$DET"
run_det p2abl-base-r1 6000
run_det p2abl-base-r2 6000

echo "########## P2 ARM (detection = +A/B/C) ##########"
cp "$P2_PROMPT" "$DET"
run_det p2abl-p2-r1 6000
run_det p2abl-p2-r2 6000

# restore final repo state = P2 prompt
cp "$P2_PROMPT" "$DET"

echo "########## SCORE all 4 ##########"
for V in p2abl-base-r1 p2abl-base-r2 p2abl-p2-r1 p2abl-p2-r2; do
  python eval/scoring.py --dataset_path $DATA --output_root outputs --version $V \
    --api_base_url "$SBASE" --api_key "$KEY" --api_model "$SMODEL" > /tmp/score_$V.log 2>&1
  echo "[scored $V]"
done
echo "########## P2 ABLATION DONE ##########"
