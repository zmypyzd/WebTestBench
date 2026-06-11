#!/usr/bin/env python3
"""Gold writeback (dd3r FP audit) — append 4 verified off-gold bugs to 0009/0037/0074.

Why this exists
---------------
The 3-rule detection prompt (merge ``f3a836c``) produced 6 NEW false positives
on the mini-7 gold yardstick. White-box gold-blind audit confirmed 5 of them as
real bugs; 4 are genuinely off-gold and are written back here:

* 0037 — hardcoded plural in the result-count label.
* 0037 — pet cards lack the shelter NAME (gold #13's own text demands it on
  cards AND detail pages; the detail page complies). Appended as a conditional
  card-specific item per the 0070 append-not-flip precedent — flipping #13
  would punish agents that correctly observed detail-page compliance.
* 0009 — /edit-listing has no role guard (traveler can edit any listing).
* 0074 — misleading empty-state CTA during an active zero-result search.

NOT written back:
* 0037 age-group misclassification (Luna/Shadow age-3 stored 'adult'): covered
  by the broad wording of existing gold #17 — the dd3r FT-02 FP is a matcher
  one-pred-one-gold granularity artifact (EX-01 took #17), not gold absence.
* 0074 card-title label omission: factually accurate but the value-as-title
  card pattern is a common design choice — ``likely`` grade, deferred per the
  P4/Q3 precedent for likely-grade finds.

The dataset (``data/``) is **gitignored**; this script is the tracked,
idempotent record. Run order vs the other ``gold_writeback_*.py`` scripts does
not matter (content-keyed idempotency).

See ``tuning-log.md`` ("检测提示词三规则 (dd3r)" section, 2026-06-11).

Gold item schema (per record's ``checklist`` list):
    {"id": int, "content": str, "class": str, "pass": bool, "bug": str}
``pass: false`` means "a real bug exists here". ``class`` is the full word
(functionality / constraint / interaction / content).

Idempotent: an item is skipped if its ``content`` already exists in that record.
New ids are assigned as ``max(existing ids) + 1`` (then +2, ... per record).

Usage:
    python process/gold_writeback_dd3r_4.py            # apply (backs up first)
    python process/gold_writeback_dd3r_4.py --dry-run  # show what would change
    python process/gold_writeback_dd3r_4.py --dataset <path> --no-backup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"
BACKUP_SUFFIX = ".bak-gold-writeback-dd3r"

# Grouped by record index; each record may receive multiple gold items.
WRITEBACKS = [
    {"index": "WebTestBench_0009", "items": [
        # dd3r 0009 EX-02 (real, CS/high)
        {"content": "Listing management pages are restricted to the owner role: in traveler mode the edit-listing page cannot be opened or submitted (including via direct URL such as /edit-listing/1).",
         "class": "constraint", "pass": False,
         "bug": "EditListing.tsx contains zero userRole checks and App.tsx L27 mounts /edit-listing/:id with no route guard; in traveler mode a direct navigation renders the fully pre-populated edit form with a working 'Save Changes' button — any traveler who knows a property id can edit any listing, violating the owner-only management separation the instruction's role framing promises."},
    ]},
    {"index": "WebTestBench_0037", "items": [
        # dd3r 0037 EX-02 (real, CT/low)
        {"content": "The result count label uses correct singular/plural wording matching the number of pets shown ('1 adorable pet available for adoption' when exactly one matches).",
         "class": "content", "pass": False,
         "bug": "PetsPage.tsx L100 hardcodes the plural: `{filteredAndSortedPets.length} adorable pets available for adoption` — with exactly one match (Baby filter -> Mochi alone, or Senior filter -> Duke alone) the header reads '1 adorable pets'; the wording never adjusts to singular."},
        # dd3r 0037 CT-01 (real, CT/medium; conditional sibling of gold #13, append-not-flip)
        {"content": "Each pet card in the list view itself shows the NAME of the shelter the pet belongs to (not only the city/state location shown next to the map-pin icon).",
         "class": "content", "pass": False,
         "bug": "PetCard.tsx renders only pet.location (L83, MapPin row); pet.shelterName appears nowhere in the card component — the shelter name is only visible on the detail page (e.g. AdoptionInquiryForm L43/L56/L68). Gold #13 requires cards AND detail pages to indicate shelter name+location; the detail-page half complies, so #13 stays pass=True and this card-specific item records the failing half (0070 append-not-flip precedent)."},
    ]},
    {"index": "WebTestBench_0074", "items": [
        # dd3r 0074 EX-01 (real, CT/medium)
        {"content": "When a search or filter yields zero matches while entries exist in the database, the empty-state message communicates 'no matches for this query' and does NOT show the empty-database call-to-action ('Add one to get started').",
         "class": "content", "pass": False,
         "bug": "TableView.tsx L203-207 and CardView.tsx L225-229 render the static 'No entries found. Add one to get started.' whenever the filtered entries prop is empty — during an active search (e.g. query 'x' with 6 stored entries) the message appears simultaneously with the status bar's '0 entries matching \"x\"', misleading users into believing the database is empty and prompting duplicate data entry."},
    ]},
]


def apply(dataset: Path, dry_run: bool, backup: bool) -> int:
    lines = dataset.read_text(encoding="utf-8").splitlines()
    records = [json.loads(l) for l in lines if l.strip()]
    by_index = {r["index"]: r for r in records}

    planned, applied, skipped = [], 0, 0
    for group in WRITEBACKS:
        rec = by_index.get(group["index"])
        if rec is None:
            print(f"[warn] {group['index']} not in dataset; skipping group")
            continue
        checklist = rec["checklist"]
        existing_contents = {i["content"] for i in checklist}
        next_id = max(int(i["id"]) for i in checklist) + 1
        for item in group["items"]:
            if item["content"] in existing_contents:
                print(f"SKIP  {group['index']}: already present — {item['content'][:60]}…")
                skipped += 1
                continue
            new_item = {"id": next_id, **item}
            planned.append((group["index"], new_item))
            checklist.append(new_item)
            existing_contents.add(item["content"])
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
