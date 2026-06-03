"""Pure, dependency-free helpers for the defect_reverify stage.

Stdlib-only on purpose: importable in unit tests without the Claude SDK.
Mirrors the canonical checkbox parsing used by eval/scoring.py:_parse_pred_items.
"""
import re
from typing import Dict, List, Set, Tuple

# Canonical result-item checkbox, identical to scoring.py:_parse_pred_items (line 533).
_CHECKBOX = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")

# Checklist item line, e.g. "- [ ] FT-01: do thing". Same id-capture as _CHECKBOX.
_CHECKLIST_ITEM = re.compile(r"^- \[\s*[xX ]\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")


def _extract_test_result_section(text: str) -> str:
    """Return the text from the first '# Test Result' header onward, else ''."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("# Test Result"):
            return "\n".join(lines[i:])
    return ""


def parse_pass_items(test_result_text: str) -> Set[str]:
    """TEST-IDs marked PASS ('- [X]') within the '# Test Result' section only."""
    section = _extract_test_result_section(test_result_text)
    pass_ids: Set[str] = set()
    for line in section.splitlines():
        m = _CHECKBOX.match(line.strip())
        if m and m.group(1).lower() == "x":
            pass_ids.add(m.group(2).strip())
    return pass_ids


def parse_result_items(test_result_text: str) -> Dict[str, dict]:
    """Map TEST-ID -> {pass: bool, has_bug_report: bool, block: List[str]}.

    `block` is the item's checkbox line plus its indented continuation lines,
    so a flip can carry the re-verify Bug Report verbatim into the final result.
    """
    section = _extract_test_result_section(test_result_text)
    lines = section.splitlines()
    items: Dict[str, dict] = {}

    cur: str = None
    for line in lines:
        m = _CHECKBOX.match(line.strip())
        if m:
            cur = m.group(2).strip()
            items[cur] = {"pass": m.group(1).lower() == "x", "block": [line], "has_bug_report": False}
            continue
        if cur is not None:
            # Indented continuation line belongs to the current item.
            if line.strip() == "" or line[:1].isspace():
                items[cur]["block"].append(line)
                if re.match(r"^\s*-\s+bug report\s*[:\-]", line, re.IGNORECASE):
                    items[cur]["has_bug_report"] = True
            else:
                cur = None
    return items


def build_sub_checklist(checklist_md: str, pass_ids: Set[str]) -> Tuple[str, List[str]]:
    """Filter `checklist.md` to the PASS ids, preserving each item's Action/Expected.

    Returns (sub_checklist_markdown, dropped_ids). `dropped_ids` are PASS ids with
    no matching item in the checklist (ID drift) -> caller logs the count.
    No first-pass verdict is included, so the re-verify session stays blind.
    """
    lines = checklist_md.splitlines()
    out: List[str] = ["# Test Checklist", ""]
    found: Set[str] = set()

    i = 0
    n = len(lines)
    while i < n:
        m = _CHECKLIST_ITEM.match(lines[i].strip())
        if m and m.group(1).strip() in pass_ids:
            tid = m.group(1).strip()
            found.add(tid)
            out.append(lines[i])
            # Pull the item's indented continuation lines (Action/Expected/...).
            j = i + 1
            while j < n and (lines[j].strip() == "" or lines[j][:1].isspace()):
                if lines[j].strip() == "" and (j + 1 >= n or not lines[j + 1][:1].isspace()):
                    break
                out.append(lines[j])
                j += 1
            i = j
            continue
        i += 1

    dropped = sorted(pass_ids - found)
    return "\n".join(out).rstrip() + "\n", dropped


def reconcile(pass1_text: str, reverify_text: str) -> Tuple[str, dict]:
    """Evidence-gated union-of-failures.

    Walk the first-pass '# Test Result' line by line, rewriting only PASS items
    that the re-verify pass FAILed *with a concrete Bug Report*. First-pass FAIL
    items and untouched PASS items are emitted verbatim. Returns (final_text, stats).
    """
    reverify_items = parse_result_items(reverify_text)
    all_lines = pass1_text.splitlines(keepends=True)
    section_line_idx = next(
        (i for i, ln in enumerate(all_lines) if ln.strip().startswith("# Test Result")),
        None,
    )
    if section_line_idx is None:
        return pass1_text, {"flipped": [], "considered": 0}
    head = "".join(all_lines[:section_line_idx])
    body_lines = "".join(all_lines[section_line_idx:]).splitlines()

    out: List[str] = []
    flipped: List[str] = []
    considered = 0
    i = 0
    n = len(body_lines)
    while i < n:
        line = body_lines[i]
        m = _CHECKBOX.match(line.strip())
        if not m:
            out.append(line)
            i += 1
            continue
        tid = m.group(2).strip()
        is_pass = m.group(1).lower() == "x"
        if not is_pass:
            # First-pass FAIL: emit verbatim, do not re-test.
            out.append(line)
            i += 1
            continue
        considered += 1
        rv = reverify_items.get(tid)
        if rv and rv["pass"] is False and rv["has_bug_report"]:
            # Flip: replace this PASS item's whole block with the re-verify block.
            flipped.append(tid)
            out.extend(rv["block"])
            i += 1
            # Skip the original item's indented continuation lines.
            while i < n and (body_lines[i].strip() == "" or body_lines[i][:1].isspace()):
                i += 1
        else:
            out.append(line)
            i += 1

    final = head + "\n".join(out)
    if not final.endswith("\n"):
        final += "\n"
    return final, {"flipped": flipped, "considered": considered}
