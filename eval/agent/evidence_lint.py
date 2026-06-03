"""Flag PASS items that lack a non-empty `Evidence:` line.

Structural (code, not prompt) enforcement of the evidence requirement.
flag-only: returns the offending ids in order; the caller emits an event. No I/O.
"""
import re

_PASS_RE = re.compile(r"^- \[[xX]\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")
_FAIL_RE = re.compile(r"^- \[\s\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")
_EVID_RE = re.compile(r"^- *evidence:\s*(.*)$", re.IGNORECASE)


def find_unsupported_pass(result_text: str) -> list:
    """Return ids of PASS items with no non-empty Evidence sub-line, in order."""
    lines = result_text.splitlines()
    offenders = []
    current = None
    has_evidence = False

    def close():
        nonlocal current, has_evidence
        if current is not None and not has_evidence:
            offenders.append(current)

    for raw in lines:
        s = raw.strip()
        pm = _PASS_RE.match(s)
        fm = _FAIL_RE.match(s)
        if pm:
            close()
            current = pm.group(1)
            has_evidence = False
            continue
        if fm:
            close()
            current = None
            has_evidence = False
            continue
        if current is not None:
            em = _EVID_RE.match(s)
            if em and em.group(1).strip():
                has_evidence = True
    close()
    return offenders
