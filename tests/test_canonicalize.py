import re
from pathlib import Path

from canonicalize import normalize_to_canonical, count_phantom_ids

_REPO = Path(__file__).resolve().parent.parent
_R0006 = _REPO / "outputs" / "reverify-off" / "WebTestBench_0006" / "result_extracted.md"
_CANON = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")


def test_canonical_lines_pass_through_unchanged():
    text = "- [X] FT-01: works\n- [ ] CS-02: should block past date\n"
    out = normalize_to_canonical(text)
    # idempotent: re-normalizing yields the same thing
    assert normalize_to_canonical(out) == out
    assert "- [X] FT-01: works" in out
    assert "- [ ] CS-02: should block past date" in out


def test_heading_form_becomes_canonical_checkbox():
    text = "### FT-01: browse works\n**PASS**\n\n### CS-02 - reject past date\nStatus: FAIL\n"
    out = normalize_to_canonical(text)
    assert "- [x] FT-01: browse works" in out
    assert "- [ ] CS-02: reject past date" in out


def test_inline_status_becomes_canonical():
    text = "**IX-04: PASS** featured reflects upcoming\n"
    out = normalize_to_canonical(text)
    assert "- [x] IX-04:" in out


def test_phantom_bug_ids_are_dropped_not_remapped():
    text = (
        "### BUG-01 · CS-02: invalid date accepted\n"
        "### BUG-04 · FT-04 / FT-05 / FT-06: filters broken\n"
        "- [ ] CS-02: should block past date\n"
    )
    out = normalize_to_canonical(text)
    assert "BUG-01" not in out
    assert "BUG-04" not in out
    assert "- [ ] CS-02: should block past date" in out


def test_count_phantom_ids():
    text = "### BUG-01 · CS-02: x\n### BUG-05 · CT-02: y\n- [ ] CS-02: z\n"
    assert count_phantom_ids(text) == 2
    assert count_phantom_ids("- [x] FT-01: clean\n") == 0


def test_empty_string_is_safe():
    assert normalize_to_canonical("") == ""
    assert count_phantom_ids("") == 0


def test_heading_with_no_status_anywhere_defaults_to_pass():
    out = normalize_to_canonical("### FT-09: organizer can create event\n")
    assert "- [x] FT-09: organizer can create event" in out


def test_body_prose_fail_does_not_flip_prior_heading_pass():
    text = (
        "### FT-01: upload succeeds\n"
        "This sentence mentions the FAIL case for reference only.\n"
        "### FT-02: export works\n"
        "PASS\n"
    )
    out = normalize_to_canonical(text)
    assert "- [x] FT-01: upload succeeds" in out   # not flipped to FAIL by prose
    assert "- [x] FT-02: export works" in out


def test_heading_form_is_idempotent():
    once = normalize_to_canonical("### IX-03: live total updates\n**PASS**\n")
    twice = normalize_to_canonical(once)
    assert twice == once


def test_0006_artifact_yields_canonical_items_without_phantoms():
    if not _R0006.exists():
        import pytest
        pytest.skip("0006 artifact not present in this checkout")
    raw = _R0006.read_text(encoding="utf-8")
    assert count_phantom_ids(raw) >= 1  # baseline really does contain phantoms
    norm = normalize_to_canonical(raw)
    ids = [m.group(2) for line in norm.splitlines() if (m := _CANON.match(line.strip()))]
    assert ids, "normalize must yield canonical items for the 0006 heading-form output"
    assert not any(re.match(r"BUG-?\d+", i, re.IGNORECASE) for i in ids), "no phantom BUG ids survive"
    assert any(i.startswith(("FT-", "CS-", "IX-", "CT-")) for i in ids)
    fail_ids = [m.group(2) for line in norm.splitlines()
                if (m := _CANON.match(line.strip())) and m.group(1).strip() == ""]
    assert fail_ids, "0006 has real FAIL items; normalize must preserve at least one (not default-all-pass)"


def test_result_label_status_is_recognized():
    out = normalize_to_canonical("### FT-01: add income\n**Result: PASS**\n")
    assert "- [x] FT-01: add income" in out
    out2 = normalize_to_canonical("### CS-01: reject zero amount\n**Result: FAIL**\n")
    assert "- [ ] CS-01: reject zero amount" in out2


def test_prose_still_not_treated_as_status_with_result_form():
    text = "### FT-01: works\nThe result could fail in edge cases, noted for reference.\n### FT-02: ok\n**Result: PASS**\n"
    out = normalize_to_canonical(text)
    assert "- [x] FT-01: works" in out  # prose 'fail' must not flip it


def test_scoring_parse_pred_items_uses_normalize_when_enabled():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
    import scoring
    text = "### FT-01: works\n**Result: PASS**\n### BUG-01 · CS-02: x\n"

    class _P:
        canonicalize = True
        _parse_pred_items = scoring.ScoringPipeline._parse_pred_items

    items = _P._parse_pred_items(_P(), text)
    assert "FT-01" in items and items["FT-01"]["pass"] is True
    assert not any(k.upper().startswith("BUG") for k in items)


def test_scoring_fallback_ignores_prose_pass_fail_when_canonicalize_off():
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
    import scoring
    text = ("### FT-01: browse works\n"
            "  - Evidence: expected no FAIL state, grid renders fine\n")

    class _P:
        canonicalize = False
        _parse_pred_items = scoring.ScoringPipeline._parse_pred_items

    items = _P._parse_pred_items(_P(), text)
    assert "FT-01" in items
    assert items["FT-01"]["pass"] is True   # prose 'FAIL' must NOT flip it


def test_evidence_line_with_fail_word_does_not_flip_heading_under_normalize():
    out = normalize_to_canonical(
        "### FT-01: browse works\n**Result: PASS**\n  - Evidence: no FAIL state observed\n"
    )
    assert "- [x] FT-01: browse works" in out
    assert "- [ ] FT-01" not in out
