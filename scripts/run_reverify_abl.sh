#!/bin/bash
# Reverify on/off A/B. Isolates the defect_reverify stage by PRE-PLACING checklist.md +
# result.md from a baseline claude_code run into each arm's fresh version dir, so detection
# is held fixed (skipped) and ONLY defect_reverify varies. Fresh dirs are mandatory:
# run() short-circuits when result_extracted.md exists, which would make the ON arm a no-op.
set -uo pipefail
cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench

# ---- EDIT THESE ----
DATA=data/WebTestBench/_reverifyabl.jsonl   # small held-out JSONL (smoke n~3 first; NOT the P1/P2-tuned 14)
PROJ=data/WebTestBench/web_applications
SRC=outputs/XXX_BASELINE_VERSION            # existing baseline run dir holding checklist.md + result.md per record
RECS="XXX XXX XXX"                           # record numeric suffixes, e.g. 0001 0002 0005
MODEL=sonnet                                 # reverify (browser) model
ABASE=""                                     # detection/reverify api_base_url (provider)
AKEY=""                                      # detection/reverify api_key
BASE_PORT=6000
# --------------------

KEY=$(grep '^sk-' /Users/zmy/intership/minimax_api.md | head -1 | tr -d '[:space:]')
SBASE=https://api.minimaxi.com/v1/chat/completions
SMODEL=MiniMax-M3

# Pre-place baseline checklist.md + result.md into an arm's fresh version dir.
place () {
  for r in $RECS; do
    mkdir -p outputs/$1/WebTestBench_$r
    cp "$SRC/WebTestBench_$r/checklist.md" "outputs/$1/WebTestBench_$r/checklist.md"
    cp "$SRC/WebTestBench_$r/result.md"    "outputs/$1/WebTestBench_$r/result.md"
  done
}

# Run one arm, one record at a time (per-record isolation survives rate limits).
run_arm () {  # $1=version  $2=extra flags (e.g. --reverify)
  for r in $RECS; do
    tmp=$(mktemp)
    grep "WebTestBench_$r" "$DATA" | head -1 > "$tmp"
    if [ ! -s "$tmp" ]; then echo "[skip $r: not in $DATA]"; rm -f "$tmp"; continue; fi
    python eval/run_agent.py --agent claude_code --data_jsonl_path "$tmp" --project_root "$PROJ" \
      --output_root outputs --log_root logs/eval --version "$1" --base_port "$BASE_PORT" \
      --api_base_url "$ABASE" --api_key "$AKEY" --model "$MODEL" $2
    echo "[$1 $r exit $?]"
    rm -f "$tmp"
  done
}

echo "########## OFF ARM (baseline: no reverify) ##########"
place reverify-off
run_arm reverify-off ""

echo "########## ON ARM (--reverify) ##########"
place reverify-on
run_arm reverify-on "--reverify"

echo "########## SCORE BOTH ARMS ##########"
for V in reverify-off reverify-on; do
  python eval/scoring.py --dataset_path "$DATA" --output_root outputs --version "$V" \
    --api_base_url "$SBASE" --api_key "$KEY" --api_model "$SMODEL" > /tmp/score_$V.log 2>&1
  echo "[scored $V]"
done
echo "Compare outputs/reverify-off/score_avg.json vs outputs/reverify-on/score_avg.json"
echo "Apply the pre-registered kill criterion: ship only if recall/F1 rise with precision cost"
echo "inside bound (NOT new-FPs >= new-TPs)."
