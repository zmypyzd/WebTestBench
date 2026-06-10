#!/usr/bin/env python3
"""Audit (and optionally refresh) derivative subset JSONLs against the current gold.

Why this exists
---------------
Subset JSONLs under ``data/WebTestBench/`` are full-record COPIES made at
extraction time. Gold writebacks (``process/gold_writeback_*.py``) only update
the main ``WebTestBench.jsonl``, so every previously-extracted subset silently
keeps the OLD checklist — scoring against such a file uses stale gold and
understates recall. This bit us on 2026-06-10: ``_eval_trusted.jsonl`` (the
28-record working set) was 20/28 records stale after the P4+Q3 writebacks.

Two kinds of subset files exist — know which one you are touching:
- LIVING subsets (``_eval_trusted.jsonl``, ``_eval_mini.jsonl``): pointers to
  "the current gold for these records". REFRESH these after every writeback.
- FROZEN A/B inputs (``_gwb5_old/new.jsonl``, ``_p4q1/*``): deliberately pinned
  to a historical gold state for controlled comparisons. NEVER refresh; their
  staleness is the experiment design.

Usage:
    python process/check_subset_staleness.py                 # audit everything
    python process/check_subset_staleness.py --refresh data/WebTestBench/_eval_trusted.jsonl [more...]
        # rewrites each file's records from current gold (same record set &
        # order, whole record replaced), backing up to <file>.pre-refresh.bak
"""
import argparse
import glob
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "WebTestBench"
GOLD = DATA / "WebTestBench.jsonl"

# Deliberately pinned to historical gold; refreshing would destroy the A/B design.
FROZEN_BY_DESIGN = {"_gwb5_old.jsonl", "_gwb5_new.jsonl"}
FROZEN_DIRS = {"_p4q1"}


def load_gold():
    gold = {}
    for line in GOLD.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            gold[r["index"]] = r
    return gold


def audit(gold):
    files = sorted(glob.glob(str(DATA / "*.jsonl")) + glob.glob(str(DATA / "*" / "*.jsonl")))
    rows = []
    for f in files:
        p = Path(f)
        if p == GOLD or ".bak" in p.name:
            continue
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        stale = [r["index"] for r in recs
                 if r.get("index") in gold and r.get("checklist") != gold[r["index"]]["checklist"]]
        unknown = [r.get("index") for r in recs if r.get("index") not in gold]
        frozen = p.name in FROZEN_BY_DESIGN or p.parent.name in FROZEN_DIRS
        rows.append((p, len(recs), stale, unknown, frozen))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", nargs="*", type=Path, default=None,
                    help="subset files to rewrite from current gold (backs up first)")
    args = ap.parse_args()

    gold = load_gold()

    if args.refresh:
        for p in args.refresh:
            if not p.exists():
                sys.exit(f"[error] not found: {p}")
            if p.name in FROZEN_BY_DESIGN or p.parent.name in FROZEN_DIRS:
                sys.exit(f"[error] {p} is a frozen A/B input — refusing to refresh.")
            recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            missing = [r["index"] for r in recs if r["index"] not in gold]
            if missing:
                sys.exit(f"[error] {p}: records not in gold: {missing}")
            backup = p.with_suffix(p.suffix + ".pre-refresh.bak")
            if not backup.exists():
                shutil.copy2(p, backup)
                print(f"backup -> {backup}")
            fresh = [json.dumps(gold[r["index"]], ensure_ascii=False) for r in recs]
            p.write_text("\n".join(fresh) + "\n", encoding="utf-8")
            print(f"refreshed {p} ({len(fresh)} records from current gold)")
        print()

    rows = audit(gold)
    bad = 0
    print(f"{'file':50s} {'recs':>4s} {'stale':>5s}  status")
    for p, n, stale, unknown, frozen in rows:
        rel = str(p.relative_to(DATA))
        if frozen:
            status = "FROZEN-BY-DESIGN (do not refresh)"
        elif stale:
            status = "STALE -> refresh or delete: " + " ".join(i[-4:] for i in stale[:8])
            bad += 1
        else:
            status = "ok"
        if unknown:
            status += f" [not-in-gold: {len(unknown)}]"
        print(f"{rel:50s} {n:>4d} {len(stale):>5d}  {status}")
    if bad:
        print(f"\n{bad} non-frozen file(s) stale. Living subsets should be refreshed after every gold writeback.")
        sys.exit(1)
    print("\nAll non-frozen subsets in sync with current gold.")


if __name__ == "__main__":
    main()
