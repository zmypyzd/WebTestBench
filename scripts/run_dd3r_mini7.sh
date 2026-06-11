#!/bin/bash
# ======================================================================
# dd3r: detection-prompt 3 rules (verbatim text-node audit / state-word
# cross-check / same-control double activation) — treatment-only run.
#
# Baseline arm is NOT re-run: the official mini-7 baseline is
#   outputs/p1exp_mini7  (p1exp detection artifacts, rescored 2026-06-11
#   with current gold + MiniMax-M3 K=3 + polarity matcher)
#   P/R/F1 = 0.7631/0.2909/0.3817
# This script runs ONLY the treatment:
#   * reuses p1exp_mini7's checklist.md per record (checklist stage fixed,
#     detection prompt is the only variable)
#   * --hunt_rounds 0, model = sonnet via local CLI creds (empty API url/key)
#   * scoring = MiniMax-M3, --match_votes 3 (same as baseline)
# Run from branch tune/detection-3rules.
# ======================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

DATA=./data/WebTestBench/_eval_mini.jsonl
PROJ=./data/WebTestBench/web_applications
OUT=./outputs
LOG=./logs/eval
MODEL=sonnet
BASE_PORT=6000
JOBS="${JOBS:-7}"
VER=dd3r
BASE_VER=p1exp_mini7

# --- judge (scoring) — MiniMax-M3, held fixed -------------------------
SC_URL=https://api.minimaxi.com/v1/chat/completions
SC_MODEL=MiniMax-M3
SC_KEY="${MINIMAX_API_KEY:-}"
if [ -z "$SC_KEY" ] && [ -f "$HOME/intership/minimax_api.md" ]; then
  SC_KEY="$(grep -oE 'sk-[A-Za-z0-9._-]+' "$HOME/intership/minimax_api.md" | head -1 || true)"
fi
if [ -z "$SC_KEY" ]; then
  echo "ERROR: MiniMax key not found. export MINIMAX_API_KEY=sk-... before running." >&2
  exit 1
fi
# lesson on file: a truncated key prefix 401s — demand the full ~130-char value
if [ "${#SC_KEY}" -lt 100 ]; then
  echo "ERROR: MiniMax key looks truncated (${#SC_KEY} chars, expect ~130)." >&2
  exit 1
fi

echo "### [1/4] reuse $BASE_VER checklists (detection = only variable)"
for d in "$OUT/$BASE_VER"/*/; do
  id="$(basename "$d")"
  if [ -f "$d/checklist.md" ]; then
    mkdir -p "$OUT/$VER/$id"
    cp -n "$d/checklist.md" "$OUT/$VER/$id/checklist.md" || true
  fi
done

echo "### [2/4] detection with 3-rule prompt -> version $VER"
n="$(grep -c . "$DATA")"
per=$(( (n + JOBS - 1) / JOBS ))
prefix="${DATA}.${VER}.part_"
rm -f "${prefix}"*
split -l "$per" -d -a 3 "$DATA" "$prefix"
pids=()
for part in "${prefix}"*; do
  [ -s "$part" ] || continue
  python eval/run_agent.py --agent claude_code \
    --data_jsonl_path "$part" --project_root "$PROJ" \
    --output_root "$OUT" --log_root "$LOG" \
    --version "$VER" --base_port "$BASE_PORT" \
    --api_base_url "" --api_key "" --model "$MODEL" \
    --hunt_rounds 0 &
  pids+=("$!")
done
failed=0
for p in ${pids[@]+"${pids[@]}"}; do wait "$p" || failed=$((failed+1)); done
rm -f "${prefix}"*
if [ "$failed" -gt 0 ]; then
  echo "WARN: $failed/$JOBS shard(s) exited non-zero (likely transient). Pipeline is idempotent — re-run to backfill."
fi

echo "### completeness check"
got="$(ls "$OUT/$VER"/*/result_extracted.md 2>/dev/null | wc -l | tr -d ' ')"
echo "  $VER: $got/$n records have result_extracted.md"

echo "### [3/4] scoring (judge=MiniMax-M3, K=3 — same as baseline)"
python eval/scoring.py --dataset_path "$DATA" --output_root "$OUT" \
  --version "$VER" --use_checklist_fallback True \
  --api_base_url "$SC_URL" --api_key "$SC_KEY" --api_model "$SC_MODEL" \
  --match_votes 3

echo "### match-cache health (votes+matches, NOT score.json — zero-fallback also writes score.json)"
python3 - "$OUT/$VER" <<'PY'
import json, os, sys
root = sys.argv[1]
bad = []
for rid in sorted(os.listdir(root)):
    p = os.path.join(root, rid, "score_match_ids.json")
    if not os.path.isdir(os.path.join(root, rid)):
        continue
    if not os.path.exists(p):
        bad.append((rid, "no score_match_ids.json")); continue
    d = json.load(open(p))
    if d.get("votes") != 3:
        bad.append((rid, f"votes={d.get('votes')}")); continue
    m = d.get("matches") or d.get("match_ids") or {}
    if not m:
        bad.append((rid, "empty matches"))
for rid, why in bad:
    print(f"  BAD {rid}: {why}")
print("  all-records match cache OK" if not bad else f"  {len(bad)} record(s) need re-scoring")
PY

echo "### [4/4] compare vs official baseline $BASE_VER (0.3817)"
python3 - "$OUT/$BASE_VER/score_avg.json" "$OUT/$VER/score_avg.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1])); e = json.load(open(sys.argv[2]))
def row(name, bv, ev):
    bv = 0.0 if bv is None else bv; ev = 0.0 if ev is None else ev
    print(f"{name:30}{bv:>8.4f}{ev:>8.4f}{ev-bv:>+9.4f}")
print(f"\n{'metric':30}{'mini7':>8}{'dd3r':>8}{'delta':>9}")
print("-" * 55)
for label, keys in [("overall F1", ("overall", "f1")), ("overall recall", ("overall", "recall")),
                    ("overall precision", ("overall", "precision")),
                    ("overall coverage", ("overall", "coverage")),
                    ("no_missing F1", ("overall_no_missing", "f1"))]:
    bb = b; ee = e
    for k in keys: bb = bb.get(k, {}); ee = ee.get(k, {})
    bb = bb if isinstance(bb, (int, float)) else None
    ee = ee if isinstance(ee, (int, float)) else None
    row(label, bb, ee)
print("-- by_class (f1 / recall) --")
bc = b.get("by_class", {}); ec = e.get("by_class", {})
for cls in ["functionality", "constraint", "interaction", "content"]:
    row(f"  {cls} f1", (bc.get(cls, {}) or {}).get("f1"), (ec.get(cls, {}) or {}).get("f1"))
    row(f"  {cls} recall", (bc.get(cls, {}) or {}).get("recall"), (ec.get(cls, {}) or {}).get("recall"))
print(f"\nFull: {sys.argv[1]}  vs  {sys.argv[2]}")
PY
echo "DONE."
