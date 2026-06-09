#!/usr/bin/env python3
"""Gold writeback — append 5 verified off-checklist bugs to the WebTestBench gold.

Why this exists
---------------
Direction-B work (2026-06-09) found 5 real bugs the gold checklist was missing:
P1-A off-checklist detection surfaced them as ``EX-NN`` findings, they were then
verified **gold-blind** by white-box source reproduction (4 confirmed real + 1
likely, 0 refuted), and folded into the gold ``checklist`` of their records.

The dataset (``data/``) is **gitignored**, so a fresh clone does NOT carry that
change. This script is the *tracked, idempotent* record of it: run it once on a
fresh clone to reproduce the exact gold state used by the matcher-voting work.

See ``tuning-log.md`` ("方向 B 全链", 2026-06-09) and
``docs/superpowers/specs/2026-06-09-matcher-voting-design.md``.

Gold item schema (per record's ``checklist`` list):
    {"id": int, "content": str, "class": str, "pass": bool, "bug": str}
``pass: false`` means "a real bug exists here"; scoring reads content/pass/class.

Idempotent: a record is skipped if an item with the same ``content`` already
exists. The new id is assigned as ``max(existing ids) + 1`` (the ``expected_id``
below is what it was when authored; a mismatch is warned, not fatal).

Usage:
    python process/gold_writeback_5apps.py            # apply (backs up first)
    python process/gold_writeback_5apps.py --dry-run  # show what would change
    python process/gold_writeback_5apps.py --dataset <path> --no-backup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"

# Each entry: the target record index, the id it had when authored (sanity only),
# and the gold item to append (id is assigned dynamically at apply time).
WRITEBACKS = [
    {
        "index": "WebTestBench_0009",
        "expected_id": 18,
        "item": {
            "content": "Bookings display the same check-in and check-out dates the guest selected on the property page.",
            "class": "functionality",
            "pass": False,
            "bug": "BookingForm.tsx:68-69 serializes the picked local-midnight date via toISOString().split('T')[0], and ManageBookings.tsx:89/163 re-parses it with new Date(...) as UTC midnight; in any positive-UTC-offset timezone the displayed check-in/out is one day earlier than selected (select Jun 17 -> shows Jun 16).",
        },
    },
    {
        "index": "WebTestBench_0035",
        "expected_id": 16,
        "item": {
            "content": "Submitting the booth contact form actually records/delivers the message; the success confirmation reflects a real effect.",
            "class": "functionality",
            "pass": False,
            "bug": "MessageForm stores messages in a module-level array (messageStore.ts) with no persistence/backend and getMessagesForBooth is dead code; after an 800ms simulated delay the 'Message sent!' toast confirms a delivery that never happens.",
        },
    },
    {
        "index": "WebTestBench_0037",
        "expected_id": 17,
        "item": {
            "content": "Filtering pets by an age group returns only pets whose displayed age falls within that group's stated range.",
            "class": "functionality",
            "pass": False,
            "bug": "PetsPage.tsx:50 filters on a hand-authored pet.ageGroup string never derived from the numeric age; seed pets are mislabeled (Luna 3 & Shadow 3 tagged 'adult', Duke 7 tagged 'senior'), so 'Adult (4-7 years)' surfaces two pets shown as 3 years and omits 7-year-old Duke.",
        },
    },
    {
        "index": "WebTestBench_0080",
        "expected_id": 19,
        "item": {
            "content": "Searching a specific error term (e.g. 'CORS') returns a relevantly filtered subset rather than nearly all entries.",
            "class": "functionality",
            "pass": False,
            "bug": "useFuzzySearch.ts:26-45 uses character-subsequence matching with no minimum-score threshold, so short common-letter queries match almost everything -- 'CORS' returns 12/12 and 'undefined' 10/12 (both are the app's own suggested queries, Index.tsx:58).",
        },
    },
    {
        "index": "WebTestBench_0089",
        "expected_id": 27,
        "item": {
            "content": "Generating a template preview with the optional End Date left blank renders the date line cleanly, with no raw Markdown characters.",
            "class": "content",
            "pass": False,
            "bug": "templates.ts:73 wraps the range as *{{startDate}} - {{endDate}}*; with endDate blank (required:false) the substitution yields '*2020 - *', which CommonMark (micromark 4.0.2) renders as literal asterisks instead of italics.",
        },
    },
]


def build_item(existing_ids, spec):
    """Return (item_dict, assigned_id) with id = max(existing)+1."""
    new_id = (max(existing_ids) + 1) if existing_ids else 1
    item = {"id": new_id, **spec["item"]}
    return item, new_id


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                    help="path to WebTestBench.jsonl (default: data/WebTestBench/WebTestBench.jsonl)")
    ap.add_argument("--dry-run", action="store_true", help="print planned changes, write nothing")
    ap.add_argument("--no-backup", action="store_true", help="do not write a .bak backup before applying")
    args = ap.parse_args()

    ds = args.dataset
    if not ds.exists():
        sys.exit(f"[error] dataset not found: {ds}\n"
                 f"        Obtain WebTestBench.jsonl (HuggingFace dataset) first; data/ is gitignored.")

    by_index = {w["index"]: w for w in WRITEBACKS}
    lines = ds.read_text(encoding="utf-8").splitlines()

    out_lines = []
    applied, skipped, missing = [], [], set(by_index)
    for line in lines:
        if not line.strip():
            out_lines.append(line)
            continue
        rec = json.loads(line)
        idx = rec.get("index")
        spec = by_index.get(idx)
        if spec is None:
            out_lines.append(line)  # untouched record kept verbatim
            continue
        missing.discard(idx)
        checklist = rec.setdefault("checklist", [])
        existing_contents = {it.get("content") for it in checklist}
        if spec["item"]["content"] in existing_contents:
            skipped.append(idx)
            out_lines.append(line)  # already present -> verbatim, idempotent
            continue
        existing_ids = [int(it["id"]) for it in checklist if str(it.get("id", "")).lstrip("-").isdigit()]
        item, new_id = build_item(existing_ids, spec)
        checklist.append(item)
        warn = "" if new_id == spec["expected_id"] else f"  [warn] id {new_id} != authored {spec['expected_id']}"
        applied.append((idx, new_id, warn))
        out_lines.append(json.dumps(rec, ensure_ascii=False))

    print(f"dataset: {ds}")
    for idx, new_id, warn in applied:
        print(f"  APPLY  {idx}: append gold bug as id={new_id}{warn}")
    for idx in skipped:
        print(f"  SKIP   {idx}: writeback already present (idempotent)")
    for idx in sorted(missing):
        print(f"  [warn] {idx}: record not found in dataset")

    if not applied:
        print("\nNo changes — gold already contains all 5 writebacks (or records missing).")
        return

    if args.dry_run:
        print(f"\n[dry-run] would append {len(applied)} item(s); nothing written.")
        return

    if not args.no_backup:
        backup = ds.with_suffix(ds.suffix + ".bak-gold-writeback")
        if not backup.exists():
            shutil.copy2(ds, backup)
            print(f"\nbackup -> {backup}")
        else:
            print(f"\nbackup exists, not overwriting -> {backup}")
    ds.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"wrote {len(applied)} new gold item(s) to {ds}")


if __name__ == "__main__":
    main()
