"""Normalize detection-output dialects to canonical checkbox lines.

The scoring parser's canonical regex only reads `- [x] ID:` / `- [ ] ID:`.
Detection sometimes emits heading form (`### FT-01 ... PASS`) or inline
(`**IX-04: PASS**`), and bug-report headings like `### BUG-01 · CS-02`
leak phantom `BUG-xx` ids into the scorer. This module maps the dialects to
canonical lines and drops phantom `BUG-xx` ids (never a TEST-ID; the taxonomy
is only FT/CS/IX/CT). Pure, idempotent, no I/O.
"""
import re

_CANON_RE = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")
_HEADER_RE = re.compile(r"^#{2,4}\s*(?:\*\*)?([A-Za-z]{2,3}-\d+)(?:\*\*)?\s*[:·–—\-]?\s*(.*)$")
_INLINE_RE = re.compile(r"^(?:- )?\*\*([A-Za-z]{2,3}-\d+):\s*(PASS|FAIL)\*\*\s*(.*)$", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)
_PHANTOM_RE = re.compile(r"^BUG-\d+$", re.IGNORECASE)
_DEDICATED_STATUS_RE = re.compile(r"^\*{0,2}(?:status:\s*)?(pass|fail)\*{0,2}$", re.IGNORECASE)


def count_phantom_ids(text: str) -> int:
    """Count `### BUG-xx ...` heading ids in raw detection output."""
    count = 0
    for line in text.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m and _PHANTOM_RE.match(m.group(1)):
            count += 1
    return count


def normalize_to_canonical(text: str) -> str:
    """Return text with heading/inline items rewritten as canonical checkbox
    lines and phantom BUG-xx ids dropped. Idempotent on canonical input."""
    lines = text.splitlines()
    out = []
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if _CANON_RE.match(s):
            out.append(raw)
            i += 1
            continue

        m = _INLINE_RE.match(s)
        if m:
            tid, status = m.group(1), m.group(2).upper()
            if not _PHANTOM_RE.match(tid):
                box = "x" if status == "PASS" else " "
                desc = m.group(3).strip() or tid
                out.append(f"- [{box}] {tid}: {desc}")
            i += 1
            continue

        hm = _HEADER_RE.match(s)
        if hm:
            tid = hm.group(1)
            if _PHANTOM_RE.match(tid):
                i += 1  # drop phantom BUG-xx heading entirely
                continue
            desc = hm.group(2).strip() or tid
            status = None
            same = _STATUS_RE.search(s)
            if same:
                status = same.group(1).upper()
            else:
                for j in range(i + 1, min(i + 4, n)):
                    nxt = _DEDICATED_STATUS_RE.match(lines[j].strip())
                    if nxt:
                        status = nxt.group(1).upper()
                        break
            box = " " if status == "FAIL" else "x"  # default to pass when unknown (parity with old fallback)
            out.append(f"- [{box}] {tid}: {desc}")
            i += 1
            continue

        out.append(raw)
        i += 1

    result = "\n".join(out)
    return result + "\n" if result and not result.endswith("\n") else result
