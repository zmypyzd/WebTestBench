#!/usr/bin/env python3
"""Gold writeback (time-drift repair) — append 3 verified items to 0002/0006.

Why this exists
---------------
[[gold-time-drift-invalidity]] (2026-06-03): 0002/0006 seed data is dated
2024-2025 while the apps window everything on ``new Date()``, so under the
eval clock (2026+) the seeded content is invisible/unreachable and gold's
pass=True labels penalize agents that correctly report the emptiness.

Repair strategy = the 0070 append-not-flip precedent, NOT flips: an agent can
create its own 2026-dated events/records and legitimately observe browse /
search / dashboards working — flipping #6/#7/#8 (0002) or #21 (0006) would
punish that correct fresh-data observation. The defect that is stable under
any future clock is "the PRELOADED data can never surface", recorded here as
conditional bug items (all mechanisms re-verified white-box on 2026-06-11):

* 0002 — every visitor list path (browse/search/category/date filter) ends in
  ``new Date(event.date) >= new Date()`` (Index.tsx L43) and ALL seed events
  are 2025-dated -> out-of-the-box grid is empty, search "Jazz" finds nothing.
* 0002 — the Featured section bypasses that same date filter
  (Index.tsx L15-16: ``events.filter(e => e.featured)``) and shows already-held
  2025 events — the bypass-conditional half of gold #13 (visitors cannot view
  past events), same structure as 0009 #12 -> id19.
* 0006 — Reports' month dropdown offers only the last 12 RELATIVE months
  (Reports.tsx L13-21: ``subMonths(new Date(), i)`` i=0..11), so the months
  holding ALL seeded transactions (Oct-Dec 2024) are unselectable and those
  records can never be summarized in Reports. NOTE: TaxSummary is NOT included
  — its year dropdown spans the last 5 years and reaches 2024 fine
  (TaxSummary.tsx L15-18), correcting the broader claim in the 2026-06-03 memo.

0002/0006 stay excluded from ``_eval_trusted``/mini-7 (frozen baseline sets);
this repair makes their gold valid for future full-set runs.

The dataset (``data/``) is **gitignored**; this script is the tracked,
idempotent record. Run order vs other ``gold_writeback_*.py`` does not matter.

Gold item schema: {"id": int, "content": str, "class": str, "pass": bool, "bug": str}

Usage:
    python process/gold_writeback_timedrift_3.py            # apply (backs up first)
    python process/gold_writeback_timedrift_3.py --dry-run
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"
BACKUP_SUFFIX = ".bak-gold-writeback-timedrift"

WRITEBACKS = [
    {"index": "WebTestBench_0002", "items": [
        {"content": "The platform's preloaded (seeded) events are visible and findable on the visitor side out of the box: the home event grid is not empty, and keyword search / category / date filters can return them.",
         "class": "functionality", "pass": False,
         "bug": "Index.tsx L43: every visitor list path (browse + search + category/date filter chain) ends in `new Date(event.date) >= new Date()` while ALL seed events (src/data/events.ts) are 2025-dated — under any post-2025 clock the home grid renders 'No events found' and searching an existing event (e.g. 'Jazz') yields 0 results, although the same event renders in Featured. The seeded catalog is permanently invisible to visitors; only newly created future-dated events ever surface."},
        {"content": "Already-held (past) events are hidden everywhere on the visitor side, including the Featured section.",
         "class": "constraint", "pass": False,
         "bug": "Index.tsx L15-16: featuredEvents = events.filter(e => e.featured) applies NO date window, so the 2025 (already-held) featured events render on the visitor home page while the main grid's date filter hides them — the Featured section is the bypass that violates gold #13's 'visitors cannot view events that have already been held' (which stays pass=True for the date-filtered main grid; same append-not-flip structure as 0009 #12 -> id19)."},
    ]},
    {"index": "WebTestBench_0006", "items": [
        {"content": "The Reports month selector can reach every month that actually contains records (including the preloaded Oct-Dec 2024 transactions), so existing data can always be summarized.",
         "class": "functionality", "pass": False,
         "bug": "Reports.tsx L13-21 builds the month dropdown from `subMonths(new Date(), i)` for i=0..11 — only the last 12 RELATIVE months are offered, so under the 2026 eval clock the months holding ALL seeded transactions (2024-10..12, src/data/mockData.ts) are unselectable and those records can never be summarized in Reports. (TaxSummary is NOT affected: its year dropdown spans the last 5 years and reaches 2024 — TaxSummary.tsx L15-18.)"},
    ]},
]


def apply(dataset: Path, dry_run: bool, backup: bool) -> int:
    lines = dataset.read_text(encoding="utf-8").splitlines()
    records = [json.loads(l) for l in lines if l.strip()]
    by_index = {r["index"]: r for r in records}

    applied, skipped = 0, 0
    for group in WRITEBACKS:
        rec = by_index.get(group["index"])
        if rec is None:
            print(f"[warn] {group['index']} not in dataset; skipping group")
            continue
        checklist = rec["checklist"]
        existing = {i["content"] for i in checklist}
        next_id = max(int(i["id"]) for i in checklist) + 1
        for item in group["items"]:
            if item["content"] in existing:
                print(f"SKIP  {group['index']}: already present — {item['content'][:60]}…")
                skipped += 1
                continue
            checklist.append({"id": next_id, **item})
            existing.add(item["content"])
            print(f"APPLY {group['index']} id={next_id} ({item['class']}) {item['content'][:60]}…")
            next_id += 1
            applied += 1

    if dry_run:
        print(f"\n[dry-run] would apply {applied}, skip {skipped}")
        return 0
    if applied == 0:
        print(f"\nNothing to do (all {skipped} already present).")
        return 0
    if backup:
        bak = dataset.with_name(dataset.name + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(dataset, bak)
            print(f"backup -> {bak}")
        else:
            print(f"backup already exists, not overwriting: {bak}")
    with dataset.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nwrote {dataset} (+{applied} items, {skipped} skipped)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    if not args.dataset.exists():
        print(f"dataset not found: {args.dataset}", file=sys.stderr)
        return 1
    return apply(args.dataset, args.dry_run, backup=not args.no_backup)


if __name__ == "__main__":
    raise SystemExit(main())
