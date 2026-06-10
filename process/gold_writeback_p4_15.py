#!/usr/bin/env python3
"""Gold writeback (Phase 4) — append 15 verified off-checklist bugs to WebTestBench gold.

Why this exists
---------------
Phase-4 work (2026-06-10) generalized the direction-B verify->writeback loop to the
28-record ``_eval_trusted.jsonl`` set: P1-A off-checklist detection ran on all 28
apps, surfacing 34 ``EX-NN`` candidates; 29 (excluding the 5 already written back by
``gold_writeback_5apps.py``) were verified **gold-blind** by an adversarial
white-box workflow (verify 3 lenses -> independent skeptic refute). 21 confirmed
(15 ``real`` + 6 ``likely``), 0 refuted; the persistence/data-loss class was judged
against each app's dev instruction and mostly excluded as expected-prototype
limitation. This script writes back the **15 ``real``** (clear instruction/expectation
violations); the 6 ``likely`` were intentionally NOT written back.

The dataset (``data/``) is **gitignored**, so a fresh clone does NOT carry this
change. This script is the tracked, idempotent record of it. Run it AFTER
``gold_writeback_5apps.py`` (independent records, order does not matter).

See ``tuning-log.md`` and ``outputs/_p4_verify/`` (per-candidate verdicts).

Gold item schema (per record's ``checklist`` list):
    {"id": int, "content": str, "class": str, "pass": bool, "bug": str}
``pass: false`` means "a real bug exists here". ``class`` is the full word
(functionality / constraint / interaction / content).

Idempotent: an item is skipped if its ``content`` already exists in that record.
New ids are assigned as ``max(existing ids) + 1`` (then +2, ... for multi-bug apps).

Usage:
    python process/gold_writeback_p4_15.py            # apply (backs up first)
    python process/gold_writeback_p4_15.py --dry-run  # show what would change
    python process/gold_writeback_p4_15.py --dataset <path> --no-backup
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "WebTestBench" / "WebTestBench.jsonl"

# Grouped by record index; each record may receive multiple gold items.
WRITEBACKS = [
    {"index": "WebTestBench_0001", "items": [
        {"content": "The product price-range filter exposes both a minimum and a maximum draggable handle so visitors can constrain the upper price bound.",
         "class": "functionality", "pass": False,
         "bug": "ui/slider.tsx:18 renders only one <SliderPrimitive.Thumb>, but ProductFilters.tsx:148-156 passes value={[min,max]} to a dual-handle range slider; Radix maps thumbs positionally, so only the minimum is draggable and the maximum stays pinned at the highest product price ($2,495), making 'filter by price range' unusable on the upper bound."},
        {"content": "The affiliate purchase link is validated as a URL so the 'Buy Now' button navigates to an external retailer.",
         "class": "constraint", "pass": False,
         "bug": "AdminPage.tsx:79 handleSubmit validates only name/shortDescription with no URL-format check on affiliateLink (plain text input, no type=url/pattern); a value like 'amazon' saves and ProductDetailPage renders href verbatim, so 'Buy Now' resolves to an in-app 404 (/amazon) instead of an external link."},
    ]},
    {"index": "WebTestBench_0014", "items": [
        {"content": "The required event-title field rejects whitespace-only input so every created event has a real name.",
         "class": "constraint", "pass": False,
         "bug": "CreateEvent.tsx:105 checks `!formData.title` without .trim() (the HTML5 required attr also accepts spaces), so a whitespace-only title passes validation and createEvent stores it verbatim, producing an event rendered with an empty <h1> heading."},
        {"content": "Submitting an RSVP on a self-created event actually records the response and is reflected in the guest summary.",
         "class": "functionality", "pass": False,
         "bug": "EventDetail.tsx:50 hardcodes currentGuestId='g1'; handleRSVP calls updateGuestRSVP which only mutates an EXISTING guest, but user-created events start with an empty guests array, so the RSVP is silently dropped while a 'RSVP updated successfully!' toast fires."},
    ]},
    {"index": "WebTestBench_0020", "items": [
        {"content": "Edits to subscription plans and subscribers persist across in-app navigation, matching the 'saved successfully' confirmation.",
         "class": "functionality", "pass": False,
         "bug": "No shared store/context/localStorage exists; each page seeds local useState from hardcoded arrays (Plans.tsx:26, Subscribers.tsx:23), so navigating to another route unmounts the page and every saved edit reverts to seed values despite a success toast (loss occurs on ordinary intra-session navigation, not just refresh)."},
    ]},
    {"index": "WebTestBench_0021", "items": [
        {"content": "A customer status changed on the detail page is reflected in the customer list view.",
         "class": "functionality", "pass": False,
         "bug": "Customers.tsx:35 and CustomerDetail.tsx:56-58 each hold an independent useState copy of the same static seed with no shared/lifted state, so a status change on the detail page (setCustomer) never propagates to the list and is lost on navigating back."},
    ]},
    {"index": "WebTestBench_0024", "items": [
        {"content": "Embedded story videos show culturally-relevant clips matching the story content.",
         "class": "content", "pass": False,
         "bug": "stories.ts:115 sets the sole videoUrl to 'https://www.youtube.com/embed/dQw4w9WgXcQ' (Rick Astley meme) on 'The Carnival of Masks'; StoryDetail renders it live in an iframe, so the only embedded video is an irrelevant placeholder."},
    ]},
    {"index": "WebTestBench_0046", "items": [
        {"content": "Search matches only page title, tags, and citation notes (the stated scope), not arbitrary page body content.",
         "class": "functionality", "pass": False,
         "bug": "KnowledgeContext.tsx:97-118 getFilteredPages also matches p.content (body) at lines 102-108, so e.g. 'JTB' returns 'The Knowledge Definition Debate' via body text alone, over-matching beyond the instruction's title/tags/citations scope."},
    ]},
    {"index": "WebTestBench_0056", "items": [
        {"content": "Opening the Add Asset or Edit Asset dialog shows the form without crashing the app.",
         "class": "functionality", "pass": False,
         "bug": "AssetFormDialog.tsx:112 uses `<SelectItem value=\"\">` ('No owner'); Radix Select v2 (@radix-ui/react-select ^2.2.5) throws on an empty-string Item value with no error boundary, so clicking Add/Edit Asset blanks the page (React tree unmounts) and add/edit become entirely inaccessible."},
    ]},
    {"index": "WebTestBench_0060", "items": [
        {"content": "The citation-trend chart's range badge reflects the currently selected time window.",
         "class": "content", "pass": False,
         "bug": "CitationChart.tsx:227-229 hardcodes the badge to `{paper.year} - {currentYear}`, ignoring viewState.timeRange; the chart data IS filtered by the selected tab (5y/1y) but the badge always shows the full publication range, mismatching the chart."},
    ]},
    {"index": "WebTestBench_0062", "items": [
        {"content": "A field's validation error clears once the user corrects that field.",
         "class": "interaction", "pass": False,
         "bug": "RegistrationModal.tsx form has no noValidate and email uses native HTML5 validation; field errors are only cleared on a full successful submit, so after fixing the name field a subsequent submit blocked by browser-native email validation leaves the stale 'Name must be at least 2 characters' error showing."},
    ]},
    {"index": "WebTestBench_0064", "items": [
        {"content": "A study session presents every card in the set exactly once, without skipping cards.",
         "class": "functionality", "pass": False,
         "bug": "Study.tsx:20 recomputes the sorted card list on every render (no useMemo) while currentIndex advances independently; marking a card changes its status and re-sorts the array (getCardsToReview priority sort), so an unreviewed card can be shifted past the current index and silently skipped."},
    ]},
    {"index": "WebTestBench_0066", "items": [
        {"content": "A task's status change persists when navigating away from and back to the task detail page.",
         "class": "functionality", "pass": False,
         "bug": "TaskDetail.tsx:37-38 re-initializes status from the static annotationTasks module array on every mount; handleApprove/handleFlag only call setStatus+toast and never write to any store, so re-navigating to the task shows the original status."},
    ]},
    {"index": "WebTestBench_0076", "items": [
        {"content": "Transaction-rule thresholds reject invalid (e.g. negative) amounts.",
         "class": "constraint", "pass": False,
         "bug": "RuleFormDialog.tsx:59 validates only non-emptiness (no numeric/positivity check), so a threshold of -100 saves; useRules.ts:44-45 evaluates `order.amount > threshold`, making every positive transaction trigger the rule."},
    ]},
    {"index": "WebTestBench_0088", "items": [
        {"content": "A proposal in the Discussion stage shows a countdown and auto-advances to Voting after the required duration.",
         "class": "functionality", "pass": False,
         "bug": "Seeded proposal id '2' has status:'discussion' but no discussionStartedAt; ProposalContext.tsx:48 guards the auto-transition on discussionStartedAt being present, so the proposal shows no countdown and never advances to Voting (instruction promises discussion lasts >=5min then auto-transitions)."},
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
        print("\nNo changes — gold already contains all 15 writebacks (or records missing).")
        return
    if args.dry_run:
        print(f"\n[dry-run] would append {len(applied)} item(s); nothing written.")
        return

    if not args.no_backup:
        backup = ds.with_suffix(ds.suffix + ".bak-gold-writeback-p4")
        if not backup.exists():
            shutil.copy2(ds, backup)
            print(f"\nbackup -> {backup}")
        else:
            print(f"\nbackup exists, not overwriting -> {backup}")
    ds.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"wrote {len(applied)} new gold item(s) to {ds}")


if __name__ == "__main__":
    main()
