"""Step 2 ablation: evidence-forced judgment, baseline vs --require_evidence.

Time-drift-robust. The operator runs the WebTester agent TWICE on the same
record set with a FIXED checklist (copy checklist.md from the baseline run into
the evidence run dir before its detection stage), then scores both. This script
tabulates:
  (1) item-level flips: PASS in baseline -> FAIL in evidence arm, on items
      present in BOTH arms (candidate corrected mis-judgments);
  (2) drift-free-subset F1 from score.json (records with no date-windowed empty
      state; default WebTestBench_0001);
  (3) covariates from session_meta.json (tool-call count, item coverage) so an
      F1 delta is not naively attributed to judgment alone.

Set BASE_VERSION / EVID_VERSION to the two output version dirs the operator
produced. Edit RECORDS / DRIFT_FREE as needed.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eval"))
from canonicalize import normalize_to_canonical  # noqa: E402

RECORDS = ["WebTestBench_0001", "WebTestBench_0002", "WebTestBench_0006"]
DRIFT_FREE = ["WebTestBench_0001"]
BASE_VERSION = "_evid_base"
EVID_VERSION = "_evid_on"

_CB = re.compile(r"^- \[\s*([xX ])\s*\]\s*([A-Za-z0-9_-]+):")


def _pass_map(version: str, rid: str) -> dict:
    f = REPO / "outputs" / version / rid / "result_extracted.md"
    if not f.exists():
        return {}
    out = {}
    for line in normalize_to_canonical(f.read_text(encoding="utf-8")).splitlines():
        m = _CB.match(line.strip())
        if m:
            out[m.group(2)] = (m.group(1).lower() == "x")
    return out


def _flips(rid: str) -> dict:
    b, e = _pass_map(BASE_VERSION, rid), _pass_map(EVID_VERSION, rid)
    both = set(b) & set(e)
    flips = sorted(k for k in both if b[k] is True and e[k] is False)
    return {"flips_pass_to_fail": flips, "n_common": len(both)}


def _f1(version: str, rid: str):
    p = REPO / "outputs" / version / rid / "score.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())["overall"].get("f1")


def _covariates(version: str, rid: str) -> dict:
    p = REPO / "outputs" / version / rid / "session_meta.json"
    if not p.exists():
        return {}
    try:
        meta = json.loads(p.read_text())
    except Exception:
        return {}
    # session_meta schema varies; surface a few likely-useful counters if present.
    return {k: meta.get(k) for k in ("num_tool_use", "tool_calls", "num_messages") if k in meta}


def main() -> None:
    print("== item-level flips (PASS@base -> FAIL@evidence, common items) ==")
    for rid in RECORDS:
        print(f"  {rid}: {_flips(rid)}")
    print("\n== drift-free-subset F1 (base vs evidence) ==")
    for rid in DRIFT_FREE:
        print(f"  {rid}: base={_f1(BASE_VERSION, rid)}  evidence={_f1(EVID_VERSION, rid)}")
    print("\n== covariates (base / evidence) ==")
    for rid in RECORDS:
        print(f"  {rid}: base={_covariates(BASE_VERSION, rid)}  evidence={_covariates(EVID_VERSION, rid)}")
    print("\nGate: evidence arm flips mis-PASS->correct-FAIL on judgment-miss "
          "records WITHOUT new false positives; drift-free F1 not regressed; "
          "tool-call budget not exhausted vs baseline.")


if __name__ == "__main__":
    main()
