"""Step 1 ablation: phantom-id + empty_match rate, baseline vs --canonicalize.

Gold-independent. Assumes two version dirs already contain the SAME baseline
detection artifacts (result_extracted.md), differing only by whether scoring
was run with --canonicalize. Run scoring yourself (see RUN COMMANDS below) for
>=2 repeats, then call this script to tabulate phantom-id counts (deterministic,
recomputed here from the artifacts) and empty_match flags (read from score.json).

RUN COMMANDS (executed by the operator, not this script):
  export MINIMAX_API_KEY=sk-...
  COMMON="--dataset_path ./data/WebTestBench/_cc_selfhunt_all.jsonl \
    --output_root ./outputs --use_checklist_fallback True \
    --api_base_url https://api.minimaxi.com/v1/chat/completions \
    --api_key $MINIMAX_API_KEY --api_model MiniMax-M3"
  python eval/scoring.py $COMMON --version _canon_off --no-canonicalize  # OFF arm (normalization is default-ON)
  python eval/scoring.py $COMMON --version _canon_on                      # ON arm = default
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eval"))
from canonicalize import count_phantom_ids  # noqa: E402

RECORDS = ["WebTestBench_0001", "WebTestBench_0002", "WebTestBench_0006"]
OFF_VERSION = "_canon_off"
ON_VERSION = "_canon_on"


def _empty_match(version: str, rid: str) -> "bool | None":
    p = REPO / "outputs" / version / rid / "score.json"
    if not p.exists():
        return None
    return bool(json.loads(p.read_text())["overall"].get("empty_match"))


def _phantoms(version: str, rid: str) -> "int | None":
    p = REPO / "outputs" / version / rid / "result_extracted.md"
    if not p.exists():
        return None
    return count_phantom_ids(p.read_text(encoding="utf-8"))


def main() -> None:
    print(f"{'record':22} {'phantom(raw)':>12} {'empty_match OFF':>16} {'empty_match ON':>15}")
    for rid in RECORDS:
        ph = _phantoms(OFF_VERSION, rid)
        em_off = _empty_match(OFF_VERSION, rid)
        em_on = _empty_match(ON_VERSION, rid)
        print(f"{rid:22} {str(ph):>12} {str(em_off):>16} {str(em_on):>15}")
    print("\nGate: phantom(raw)->0 after ON normalize is guaranteed; "
          "empty_match should drop OFF->ON on phantom/heading-form records (e.g. 0006).")


if __name__ == "__main__":
    main()
