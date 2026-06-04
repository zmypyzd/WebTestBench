#!/bin/bash
# ======================================================================
# P1-A A/B: off-checklist adversarial exploration in defect_detection.
#
# Isolates ONE variable — the defect_detection prompt — on the 7-record
# mini eval set. Everything else is held fixed:
#   * baseline  = main's OLD detection prompt           -> version p1base
#   * treatment = this branch's P1-A detection prompt   -> version p1exp
#   * treatment REUSES baseline's generated checklist.md per record, so the
#     checklist_generation stage is skipped and detection is the only diff.
#   * --hunt_rounds 0   : skip defect_hunt (writes BUGS.md, NOT scored -> pure cost here)
#   * NO --reverify      : skip defect_reverify (a separate CS/IX lever; would
#                          confound P1-A's delta). Default-off; left off on purpose.
#   * judge = MiniMax-M3 (held fixed across both versions)
#   * model = sonnet via local Claude Code CLI creds (empty API_BASE_URL/KEY)
#
# Run from the tune/* branch. Leaves run_webtester_cc.sh untouched.
# ======================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

DATA=./data/WebTestBench/_eval_mini.jsonl
PROJ=./data/WebTestBench/web_applications
OUT=./outputs
LOG=./logs/eval
MODEL=sonnet
BASE_PORT=6000
JOBS="${JOBS:-7}"   # parallel detection processes (7 mini records have distinct ports -> full parallel safe)
DDFILE=eval/prompt/defect_detection.py
BASE_VER=p1base
EXP_VER=p1exp
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# --- judge (scoring) — MiniMax-M3, held fixed -------------------------
SC_URL=https://api.minimaxi.com/v1/chat/completions
SC_MODEL=MiniMax-M3
SC_KEY="${MINIMAX_API_KEY:-}"
if [ -z "$SC_KEY" ] && [ -f "$HOME/intership/minimax_api.md" ]; then
  SC_KEY="$(grep -oE 'sk-[A-Za-z0-9._-]+' "$HOME/intership/minimax_api.md" | head -1 || true)"
fi
if [ -z "$SC_KEY" ]; then
  echo "ERROR: MiniMax key not found. export MINIMAX_API_KEY=sk-... (or put it in ~/intership/minimax_api.md) before running." >&2
  exit 1
fi

run_detect () {  # $1 = version label — shards the mini set across $JOBS parallel processes
  local ver="$1"
  local n; n="$(grep -c . "$DATA")"
  local per=$(( (n + JOBS - 1) / JOBS ))
  local prefix="${DATA}.${ver}.part_"
  rm -f "${prefix}"*
  split -l "$per" -d -a 3 "$DATA" "$prefix"
  local pids=()
  for part in "${prefix}"*; do
    [ -s "$part" ] || continue
    python eval/run_agent.py --agent claude_code \
      --data_jsonl_path "$part" --project_root "$PROJ" \
      --output_root "$OUT" --log_root "$LOG" \
      --version "$ver" --base_port "$BASE_PORT" \
      --api_base_url "" --api_key "" --model "$MODEL" \
      --hunt_rounds 0 &
    pids+=("$!")
  done
  local failed=0
  for p in ${pids[@]+"${pids[@]}"}; do wait "$p" || failed=$((failed+1)); done
  rm -f "${prefix}"*
  # Tolerate per-shard failures (transient API/socket errors, run_agent sys.exit(1)).
  # The pipeline is idempotent: completed records skip on re-run, and scoring's
  # --use_checklist_fallback covers any record left without a result. Never abort
  # the whole A/B for one bad record.
  if [ "$failed" -gt 0 ]; then
    echo "WARN: $failed/$JOBS shard(s) for '$ver' exited non-zero (likely transient). Continuing; re-run to backfill."
  fi
  return 0
}

score () {  # $1 = version label
  python eval/scoring.py --dataset_path "$DATA" --output_root "$OUT" \
    --version "$1" --use_checklist_fallback True \
    --api_base_url "$SC_URL" --api_key "$SC_KEY" --api_model "$SC_MODEL"
}

echo "### [1/5] BASELINE — restore main's old detection prompt -> run $BASE_VER"
git show "main:$DDFILE" > "$DDFILE.tmp" && mv "$DDFILE.tmp" "$DDFILE"
trap 'git checkout "$BRANCH" -- "$DDFILE" 2>/dev/null || true' EXIT  # always restore P1-A file
run_detect "$BASE_VER"
git checkout "$BRANCH" -- "$DDFILE"   # restore P1-A prompt for treatment
trap - EXIT

echo "### [2/5] reuse baseline checklists for treatment (detection = only variable)"
for d in "$OUT/$BASE_VER"/*/; do
  id="$(basename "$d")"
  if [ -f "$d/checklist.md" ]; then
    mkdir -p "$OUT/$EXP_VER/$id"
    cp -n "$d/checklist.md" "$OUT/$EXP_VER/$id/checklist.md"
  fi
done

echo "### [3/5] TREATMENT — P1-A detection prompt -> run $EXP_VER"
run_detect "$EXP_VER"

echo "### completeness check (result_extracted.md per version)"
exp_total="$(grep -c . "$DATA")"
for v in "$BASE_VER" "$EXP_VER"; do
  got="$(ls "$OUT/$v"/*/result_extracted.md 2>/dev/null | wc -l | tr -d ' ')"
  echo "  $v: $got/$exp_total records have result_extracted.md"
done

echo "### [4/5] SCORING both (judge=MiniMax-M3, fixed)"
score "$BASE_VER"
score "$EXP_VER"

echo "### [5/5] COMPARE"
python3 - "$OUT/$BASE_VER/score_avg.json" "$OUT/$EXP_VER/score_avg.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1])); e=json.load(open(sys.argv[2]))
def g(d,*ks):
    for k in ks: d=d.get(k,{}) if isinstance(d,dict) else {}
    return d if not isinstance(d,dict) else None
def row(name,bv,ev):
    bv=0.0 if bv is None else bv; ev=0.0 if ev is None else ev
    print(f"{name:28}{bv:>8.3f}{ev:>8.3f}{ev-bv:>+9.3f}")
print(f"\n{'metric':28}{'p1base':>8}{'p1exp':>8}{'delta':>9}")
print("-"*53)
for label,keys in [("overall F1",("overall","f1")),("overall recall",("overall","recall")),
                   ("overall precision",("overall","precision")),
                   ("no_missing F1",("overall_no_missing","f1"))]:
    bb=b; ee=e
    for k in keys: bb=bb.get(k,{}); ee=ee.get(k,{})
    bb=bb if isinstance(bb,(int,float)) else None
    ee=ee if isinstance(ee,(int,float)) else None
    row(label,bb,ee)
print("-- by_class (recall) --")
bc=b.get("by_class",{}); ec=e.get("by_class",{})
for cls in ["constraint","interaction","functionality","content"]:
    row(f"  {cls} recall", (bc.get(cls,{}) or {}).get("recall"), (ec.get(cls,{}) or {}).get("recall"))
print("\nFull: %s  vs  %s"%(sys.argv[1],sys.argv[2]))
PY
echo "DONE."
