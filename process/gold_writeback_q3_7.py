#!/usr/bin/env python3
"""Gold writeback (Q3 blind-spot) — append 7 verified off-gold bugs to 0070/0074.

Why this exists
---------------
The 4-class mutation run (2026-06-10) showed WebTestBench_0070/0074 catching 0/4
injected mutants each. White-box diagnosis traced most of that to harness-side
mutant invalidity, but the follow-up gold-blind white-box hunt (ultracode
workflow ``wf_ca63f84b-c4c``: 2 apps x 3 lenses -> semantic dedupe -> 3-lens
adversarial verify, default stance refute, >=2 concessions to confirm) found
9 ``real`` + 2 ``likely`` bugs; gold comparison marked 7 of the ``real`` ones as
genuinely off-gold (no existing item covers the same surface AND failure mode).
This script writes back those **7 real off-gold bugs**. The 2 ``likely``
(0074 card-view first-field omission; 0074 label-input association) were
deliberately NOT written back, per the P4 precedent for ``likely``-grade finds.

The dataset (``data/``) is **gitignored**; this script is the tracked,
idempotent record. Run order vs the other ``gold_writeback_*.py`` scripts does
not matter (disjoint records / content-keyed idempotency).

See ``tuning-log.md`` and ``outputs/_q3_blindspot/{report.md,synthesis_data.json}``.

Gold item schema (per record's ``checklist`` list):
    {"id": int, "content": str, "class": str, "pass": bool, "bug": str}
``pass: false`` means "a real bug exists here". ``class`` is the full word
(functionality / constraint / interaction / content).

Idempotent: an item is skipped if its ``content`` already exists in that record.
New ids are assigned as ``max(existing ids) + 1`` (then +2, ... for multi-bug apps).

Usage:
    python process/gold_writeback_q3_7.py            # apply (backs up first)
    python process/gold_writeback_q3_7.py --dry-run  # show what would change
    python process/gold_writeback_q3_7.py --dataset <path> --no-backup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"
BACKUP_SUFFIX = ".bak-gold-writeback-q3"

# Grouped by record index; each record may receive multiple gold items.
WRITEBACKS = [
    {"index": "WebTestBench_0070", "items": [
        # 0070-C1 (real, CS/high)
        {"content": "Modifying a previously saved rule set marks it unsaved again, so the Edit-Save-Simulate order is enforced for every round of edits and stale unsaved edits cannot run in simulation.",
         "class": "constraint", "pass": False,
         "bug": "useWorkflowStore.ts: addStep (L50-60), removeStep (L62-69), updateStepName (L71-77) and updateRuleSetName (L35-39) set updatedAt but never reset hasBeenSaved:false; only saveRuleSet (L79-83) ever writes it (to true), and canSimulate (L109-112) checks only hasBeenSaved && steps.length>0 — so after the first save (including the default rule sets shipped with hasBeenSaved:true) any later modification runs in simulation immediately without re-saving; the order constraint is one-shot."},
        # 0070-C5 (real, IX/medium)
        {"content": "When a simulation finishes, the final step is rendered as completed — no step remains in a running 'Processing...' state once the completion banner appears.",
         "class": "interaction", "pass": False,
         "bug": "SimulationView.tsx L20-28: the animation interval clears itself when prev >= steps.length-1, so simulatedStepIndex permanently halts at the last index; isCurrent (L113) stays true for the final step, rendering pulsing 'Processing...' (L147-151) plus the ring highlight (L123) forever while the 'completed successfully' banner shows simultaneously — a contradictory terminal state."},
    ]},
    {"index": "WebTestBench_0074", "items": [
        # 0074-C1 (real, IX/high)
        {"content": "Deleting the field currently used for sorting cleanly resets the sort state: drag-to-reorder becomes available again, the saved manual order is applied, and no dangling sort indicator is shown.",
         "class": "interaction", "pass": False,
         "bug": "useDatabase.ts deleteField (L34-43) removes the field and strips its values but never resets tableView/cardView.sortConfig when sortConfig.fieldId is the deleted field; getViewEntries (L114-145) then still takes the `if (sortConfig.fieldId)` branch where fields.find() returns undefined — no sort runs AND the saved manual-order else-branch (L137-145) is skipped, drag-reorder stays disabled and the status bar shows a blank 'Sorted by  (ascending)'."},
        # 0074-C3 (real, FT/medium)
        {"content": "Quick search matches values as they are displayed on screen (formatted dates like 'Jan 15, 2026', checkbox labels like 'Yes'/'No'), not hidden raw storage formats.",
         "class": "functionality", "pass": False,
         "bug": "useDatabase.ts search filter (L104-111) runs String(value).toLowerCase().includes(query) over RAW stored entry.values: dates are stored 'yyyy-MM-dd' but displayed via format(parseISO(v),'MMM d, yyyy') (TableView.tsx L70-75, CardView.tsx L77-87) and checkbox booleans are displayed as Yes/No badges — so typing the visible text 'jan' yields 'No entries found' while the invisible strings 'true'/'false' match checkbox state."},
        # 0074-C4 (real, CS/medium)
        {"content": "Renaming an existing field enforces the same naming constraints as adding one: empty names are rejected and duplicate field names are not allowed.",
         "class": "constraint", "pass": False,
         "bug": "FieldConfigDialog.tsx: the add-field path validates non-empty trimmed names (L43-49 `if (newFieldName.trim())`), but the inline rename Input for existing fields (L76-80) pipes raw e.target.value into onUpdateField with no trim/non-empty/uniqueness check — a field renamed to '' persists (blank but clickable sort column header, blank Sort-dropdown entry) and duplicate field names are accepted on both paths."},
        # 0074-C5 (real, CS/high)
        {"content": "A data-import workflow is available and gated on completed field configuration, as the instruction promises ('Field configuration must be completed before importing data').",
         "class": "constraint", "pass": False,
         "bug": "No import capability exists anywhere in src/ (no import-data/upload/csv/file-input/FileReader code); the toolbar (Index.tsx L112-132) offers only Search/Sort/view-toggle/Add Entry, while the Fields dialog footer still reads 'Configure fields before importing data.' — the instruction-promised workflow and its ordering constraint are entirely absent yet referenced by the UI."},
        # 0074-C7 (real, CT/low)
        {"content": "A cleared date value displays the '—' empty marker (same as a never-set date) and empty date values sort consistently with other empty values.",
         "class": "content", "pass": False,
         "bug": "EntryDialog.tsx L85 stores e.target.value verbatim for dates ('' when the picker is cleared) instead of null (the number input at L75 correctly maps ''->null); formatValue (TableView.tsx L61, CardView.tsx L62) shows '—' only for null/undefined, and for '' the date branch's format(parseISO('')) throws into the blank fallback — a fully blank cell, with '' sorting FIRST ascending while null dates sort last."},
    ]},
]


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

    by_index = {w["index"]: w["items"] for w in WRITEBACKS}
    lines = ds.read_text(encoding="utf-8").splitlines()

    out_lines = []
    applied, skipped, missing = [], [], set(by_index)
    for line in lines:
        if not line.strip():
            out_lines.append(line)
            continue
        rec = json.loads(line)
        idx = rec.get("index")
        items = by_index.get(idx)
        if items is None:
            out_lines.append(line)  # untouched record kept verbatim
            continue
        missing.discard(idx)
        checklist = rec.setdefault("checklist", [])
        changed = False
        for spec in items:
            existing_contents = {it.get("content") for it in checklist}
            if spec["content"] in existing_contents:
                skipped.append((idx, spec["content"][:48]))
                continue
            existing_ids = [int(it["id"]) for it in checklist
                            if str(it.get("id", "")).lstrip("-").isdigit()]
            new_id = (max(existing_ids) + 1) if existing_ids else 1
            checklist.append({"id": new_id, **spec})
            applied.append((idx, new_id, spec["class"]))
            changed = True
        out_lines.append(json.dumps(rec, ensure_ascii=False) if changed else line)

    print(f"dataset: {ds}")
    for idx, new_id, cls in applied:
        print(f"  APPLY  {idx}: append gold bug as id={new_id} ({cls})")
    for idx, c in skipped:
        print(f"  SKIP   {idx}: already present (idempotent) — {c}...")
    for idx in sorted(missing):
        print(f"  [warn] {idx}: record not found in dataset")

    if not applied:
        print("\nNo changes — gold already contains all 7 writebacks (or records missing).")
        return
    if args.dry_run:
        print(f"\n[dry-run] would append {len(applied)} item(s); nothing written.")
        return

    if not args.no_backup:
        backup = ds.with_suffix(ds.suffix + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(ds, backup)
            print(f"\nbackup -> {backup}")
        else:
            print(f"\nbackup exists, not overwriting -> {backup}")
    ds.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"wrote {len(applied)} new gold item(s) to {ds}")


if __name__ == "__main__":
    main()
