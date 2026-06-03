#!/bin/bash
# Robust re-run of the P2 arm (p2abl-p2-r1) that failed overnight on an external
# limit. Per-record isolation: each record runs as its own run_agent invocation so
# one transient failure (sys.exit(1)) does NOT abort the rest. Checklists are already
# pre-placed; detection prompt is the full P2 (A/B/C). Completed records are skipped
# (idempotent). Scores at the end. Compares against the already-complete base-r1.
set -uo pipefail
cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench

PROJ=data/WebTestBench/web_applications
CL=outputs/p2abl-cl
RECS="0001 0002 0005 0006 0007 0024"
V=p2abl-p2-r1
KEY=$(grep '^sk-' /Users/zmy/intership/minimax_api.md | head -1 | tr -d '[:space:]')
SBASE=https://api.minimaxi.com/v1/chat/completions
SMODEL=MiniMax-M3

# ensure full P2 detection prompt is in place
cp /tmp/defect_detection_P2.py eval/prompt/defect_detection.py

for r in $RECS; do
  mkdir -p outputs/$V/WebTestBench_$r
  cp $CL/WebTestBench_$r/checklist.md outputs/$V/WebTestBench_$r/checklist.md
  if [ -f outputs/$V/WebTestBench_$r/result_extracted.md ]; then echo "[skip $r: done]"; continue; fi
  echo "########## $V / $r ##########"
  python3 -c "import json,sys; [open('data/WebTestBench/_one_$r.jsonl','w').write(l) for l in open('data/WebTestBench/_p2abl.jsonl') if str(json.loads(l)['index'])=='WebTestBench_$r']"
  for attempt in 1 2; do
    python eval/run_agent.py --agent claude_code --data_jsonl_path data/WebTestBench/_one_$r.jsonl \
      --project_root $PROJ --output_root outputs --log_root logs/eval \
      --version $V --base_port 6000 --api_base_url "" --api_key "" --model sonnet
    code=$?
    if [ -f outputs/$V/WebTestBench_$r/result_extracted.md ]; then echo "[$r ok attempt $attempt]"; break; fi
    echo "[$r failed attempt $attempt code=$code]"; sleep 20
  done
  rm -f data/WebTestBench/_one_$r.jsonl
done

echo "########## SCORE $V ##########"
python eval/scoring.py --dataset_path data/WebTestBench/_p2abl.jsonl --output_root outputs \
  --version $V --api_base_url "$SBASE" --api_key "$KEY" --api_model "$SMODEL" > /tmp/score_$V.log 2>&1
echo "[scored $V]"
echo "########## P2 REDO DONE ##########"
