"""
Independent verification of the scoring metric implementation.

Re-implements coverage + the bug-oriented confusion matrix (P/R/F1) and per-class
metrics FROM THE DEFINITIONS (does NOT import scoring.py), then asserts the
recomputed values equal the stored score.json for known records. If they match,
scoring.py's metric implementation is corroborated by an independent oracle.

Definitions (per CLAUDE.md / scoring.py docstring):
  gold pass=False  => a real bug exists at that item.
  A gold item is "predicted fail" iff ANY matched pred item is fail.
  TP = gold-bug AND pred-fail ; FP = gold-ok AND pred-fail
  FN = gold-bug AND (pred-pass OR uncovered) ; TN otherwise
  coverage = (# gold items matched by >=1 pred) / (# gold items)
  per-class: same matrix restricted to gold items of that class; only classes
             that contain >=1 bug are scored.

Run: python scripts/verify_metrics.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "data/WebTestBench/WebTestBench.jsonl"
CLASSES = ["functionality", "constraint", "interaction", "content"]
# same line format scoring.py uses (handles optional ** bold around id)
PRED_RE = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")


def load_gold(record_id):
    with open(DATASET) as f:
        for line in f:
            r = json.loads(line)
            if str(r.get("index")) == record_id:
                return {str(it["id"]): {"pass": bool(it["pass"]), "class": it.get("class")}
                        for it in r["checklist"]}
    raise SystemExit(f"record {record_id} not found in dataset")


def parse_pred(path):
    pred = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PRED_RE.match(line.strip())
        if m:
            pred[m.group(2).strip()] = {"pass": m.group(1).lower() == "x"}
    return pred


def confusion(match_ids, gold, pred):
    gold_to_preds = {}
    for pid, gid in match_ids or []:
        if gid is not None:
            gold_to_preds.setdefault(str(gid), []).append(pid)
    tp = fp = fn = tn = 0
    for gid, meta in gold.items():
        pids = gold_to_preds.get(gid)
        if pids is None:                       # uncovered
            if meta["pass"]:
                tn += 1
            else:
                fn += 1
            continue
        pred_pass = all(pred[p]["pass"] for p in pids)   # KeyError surfaces matcher hallucinations
        if meta["pass"]:
            tn += 1 if pred_pass else 0
            fp += 0 if pred_pass else 1
        else:
            fn += 1 if pred_pass else 0
            tp += 0 if pred_pass else 1
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 4), round(r, 4), round(f, 4), (tp, fp, fn, tn)


def coverage(match_ids, gold):
    matched = {str(g) for _, g in (match_ids or []) if g is not None}
    return round(len(matched & set(gold)) / len(gold), 4) if gold else 0.0


def verify_record(version, record_id):
    d = ROOT / "outputs" / version / record_id
    if not d.exists():
        print(f"  [skip] {version}/{record_id} not present")
        return True
    gold = load_gold(record_id)
    pred = parse_pred(d / "result_extracted.md")
    match_ids = json.loads((d / "score_match_ids.json").read_text())["matches"]
    stored = json.loads((d / "score.json").read_text())

    ok = True
    cov = coverage(match_ids, gold)
    p, r, f, conf = confusion(match_ids, gold, pred)
    s = stored["overall"]
    checks = [
        ("coverage", cov, s["coverage"]),
        ("precision", p, s["precision"]),
        ("recall", r, s["recall"]),
        ("f1", f, s["f1"]),
    ]
    # per-class (only classes with a bug are scored in score.json)
    for cls in CLASSES:
        sub = {g: m for g, m in gold.items() if m["class"] == cls}
        if not sub or not any(not m["pass"] for m in sub.values()):
            continue
        cp, cr, cf, _ = confusion(match_ids, sub, pred)
        sc = stored.get(cls) or {}
        checks += [
            (f"{cls}.precision", cp, sc.get("precision")),
            (f"{cls}.recall", cr, sc.get("recall")),
            (f"{cls}.f1", cf, sc.get("f1")),
        ]

    print(f"  {version}/{record_id}  conf(tp,fp,fn,tn)={conf}")
    for name, got, exp in checks:
        match = (got == exp) or (exp is not None and abs(got - exp) < 1e-4)
        flag = "OK " if match else "MISMATCH"
        if not match:
            ok = False
        print(f"    [{flag}] {name:24} recomputed={got}  stored={exp}")
    return ok


def main():
    print("Independent metric verification (oracle reimplementation vs stored score.json)\n")
    targets = [("claudecode-opus", "WebTestBench_0013"),
               ("claudecode-sonnet", "WebTestBench_0013")]
    all_ok = True
    for version, rid in targets:
        all_ok &= verify_record(version, rid)
    print()
    if all_ok:
        print("RESULT: PASS — scoring.py metrics reproduce exactly from the definitions.")
    else:
        print("RESULT: FAIL — discrepancy found; scoring.py metric implementation suspect.")
        sys.exit(1)


if __name__ == "__main__":
    main()
