#!/bin/bash
# ======================================================================
# dd3r yardstick 2: mutation catch-rate re-measure with the 3-rule
# detection prompt — SAME injections as the official v2 baseline
# (summary_7app_4class_v2.json, 15/25 = 0.600), FRESH detection.
#
# How: pre-seed a NEW probe root with only each mutant's cache
# (injected.json / patch_meta.json / new_file.txt) so mutation_probe
# loads the identical injections but finds no run/ artifacts -> the
# idempotent agent pipeline re-runs detection with the CURRENT prompt.
# The baseline root outputs/_mutation_probe is never touched.
#
# Cost: ~25 valid mutants x ~22 min / concurrency 2 ≈ 4.6 h.
# Idempotent: re-run to resume (mutants with result.json are skipped
# only if you aggregate manually; run/ artifacts make detection skip).
# Run from branch tune/detection-3rules AFTER the mini-7 signal.
# ======================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

APPS=WebTestBench_0009,WebTestBench_0035,WebTestBench_0037,WebTestBench_0070,WebTestBench_0074,WebTestBench_0080,WebTestBench_0089
BASE_ROOT=outputs/_mutation_probe
NEW_ROOT=outputs/_mutation_probe_dd3r
BASELINE=summary_7app_4class_v2.json

# --- judge — MiniMax-M3, same as baseline ----------------------------
JUDGE_URL=https://api.minimaxi.com/v1/chat/completions
JUDGE_MODEL=MiniMax-M3
JUDGE_KEY="${MINIMAX_API_KEY:-}"
if [ -z "$JUDGE_KEY" ] && [ -f "$HOME/intership/minimax_api.md" ]; then
  JUDGE_KEY="$(grep -oE 'sk-[A-Za-z0-9._-]+' "$HOME/intership/minimax_api.md" | head -1 || true)"
fi
if [ -z "$JUDGE_KEY" ] || [ "${#JUDGE_KEY}" -lt 100 ]; then
  echo "ERROR: MiniMax key missing or truncated (${#JUDGE_KEY} chars, expect ~130)." >&2
  exit 1
fi

echo "### [1/3] pre-seed $NEW_ROOT with baseline mutant caches (injections only)"
seeded=0
for app in ${APPS//,/ }; do
  for k in 0 1 2 3; do
    src="$BASE_ROOT/$app/m$k"
    dst="$NEW_ROOT/$app/m$k"
    if [ -f "$src/injected.json" ]; then
      mkdir -p "$dst"
      for f in injected.json patch_meta.json new_file.txt; do
        cp -n "$src/$f" "$dst/$f" || true
      done
      seeded=$((seeded+1))
    else
      echo "  WARN: no cached mutant at $src"
    fi
  done
done
echo "  seeded $seeded/28 mutant caches"
cp -n "$BASE_ROOT/$BASELINE" "$NEW_ROOT/$BASELINE" || true

echo "### [2/3] probe run (detection prompt = 3-rule branch; judge = MiniMax-M3)"
timeout 21600 python scripts/mutation_probe.py \
  --apps "$APPS" --mutants-per-app 4 \
  --model sonnet --api_base_url "" --api_key "" --detect_model sonnet \
  --base_port 7000 --concurrency 2 --mutant-timeout 2400 \
  --judge_api_base_url "$JUDGE_URL" --judge_api_key "$JUDGE_KEY" --judge_model "$JUDGE_MODEL" \
  --out-root "$NEW_ROOT" --out summary_dd3r.json --baseline "$BASELINE" || {
    rc=$?
    echo "WARN: probe exited rc=$rc (timeout or shard failure). Re-run this script to resume idempotently."
  }

echo "### [3/3] verify the comparison is same-injection"
python3 - "$BASE_ROOT" "$NEW_ROOT" <<'PY'
import hashlib, json, os, sys
base, new = sys.argv[1], sys.argv[2]
mismatch = 0
for app in sorted(os.listdir(new)):
    appdir = os.path.join(new, app)
    if not os.path.isdir(appdir) or not app.startswith("WebTestBench_"):
        continue
    for m in sorted(os.listdir(appdir)):
        nf = os.path.join(appdir, m, "new_file.txt")
        bf = os.path.join(base, app, m, "new_file.txt")
        if os.path.exists(nf) and os.path.exists(bf):
            h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
            if h(nf) != h(bf):
                mismatch += 1
                print(f"  INJECTION DRIFT: {app}/{m}")
print("  all seeded injections identical to baseline" if not mismatch
      else f"  {mismatch} drifted injections — comparison INVALID")
PY
echo "DONE. Compare summary_dd3r.json vs official 15/25=0.600."
