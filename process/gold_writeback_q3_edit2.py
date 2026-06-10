#!/usr/bin/env python3
"""Gold writeback (Q3 flip-candidate resolution) — append 2 edit-path bypass bugs to 0070.

Why this exists
---------------
The Q3 gold-blind white-box hunt (see ``gold_writeback_q3_7.py``) confirmed two
``real`` bugs whose surfaces are nominally covered by EXISTING gold items that
are marked ``pass: true``:

- gold #8 "Unable to save rule sets with an undefined step count of zero"
  vs 0070-C2: the save guard resolves the rule set through the FILTERED list,
  so a retained search query bypasses the zero-steps check entirely.
- gold #7 "No name provided, rule set cannot be saved"
  vs 0070-C3: the create path validates names, but the rename/edit path has
  zero validation and lets names be blanked.

Resolution: **append conditional items, do NOT flip #7/#8.** The constraints
those items describe genuinely hold on the unfiltered/create paths, so flipping
them would turn a correct PASS observation (agent tested the normal path) into
a false negative. Appending keeps both behaviors scoreable: normal-path PASS
matches #7/#8 (TN), bypass-path FAIL matches the new items (TP).

The third flip candidate (0074 #13 vs C6, card view omitting an empty first
text field) is ``likely``-grade and was deliberately deferred, per the P4
precedent of not writing back ``likely`` finds.

The dataset (``data/``) is **gitignored**; this script is the tracked,
idempotent record. See ``tuning-log.md`` and ``outputs/_q3_blindspot/report.md``.

Usage:
    python process/gold_writeback_q3_edit2.py            # apply (backs up first)
    python process/gold_writeback_q3_edit2.py --dry-run  # show what would change
    python process/gold_writeback_q3_edit2.py --dataset <path> --no-backup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"
BACKUP_SUFFIX = ".bak-gold-writeback-q3edit"

WRITEBACKS = [
    {"index": "WebTestBench_0070", "items": [
        # 0070-C2 (real, CS/high) — conditional bypass of gold #8
        {"content": "Saving is blocked for a rule set with zero steps even while a search filter is active — the save guard evaluates the rule set itself, not the filtered list.",
         "class": "constraint", "pass": False,
         "bug": "Index.tsx handleSave (L56-71) resolves the target via getFilteredRuleSets().find(rs => rs.id === ruleSetId) (L57) instead of the raw ruleSets array; the search query is intentionally retained across tab switches (useWorkflowStore.ts L12-16, L122-124), so a retained query that does not match the edited rule set's name makes find() return undefined and the zero-steps guard is skipped — an empty (0-step) rule set saves successfully, bypassing the constraint that gold #8 verifies on the unfiltered path."},
        # 0070-C3 (real, CS/medium) — edit-path bypass of gold #7
        {"content": "Renaming a rule set or a step enforces the same non-empty validation as creation: names cannot be blanked to empty or whitespace-only values via the edit path.",
         "class": "constraint", "pass": False,
         "bug": "StepEditor.tsx: the Rule Set Name input (L67-71) and the per-step name inputs (L122-126) call onUpdateName/onUpdateStepName on every keystroke with zero validation, unlike the validated create path (RuleSetList.tsx L108 disables Create on !newName.trim()) and add-step path (StepEditor.tsx L44-49, L152) — rule-set and step names can be blanked and then saved, bypassing the constraint that gold #7 verifies on the create path."},
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
        print("\nNo changes — gold already contains both writebacks (or record missing).")
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
