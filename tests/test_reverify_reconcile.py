from agent.reverify_reconcile import parse_pass_items
from agent.reverify_reconcile import parse_result_items
from agent.reverify_reconcile import build_sub_checklist
from agent.reverify_reconcile import reconcile


def test_parse_pass_items_extracts_only_passes_from_test_result_section():
    text = """Some preamble prose.
- [ ] CS-99: stray preamble checkbox that must be ignored

# Test Result

## Functionality
- [X] FT-01: works
- [ ] FT-02: broken
  - Bug Report:
    - Issue: x

## Constraint
- [x] CS-01: blocked correctly
"""
    pass_ids = parse_pass_items(text)
    assert pass_ids == {"FT-01", "CS-01"}


def test_parse_result_items_captures_pass_fail_and_bug_report_presence():
    text = """# Test Result

## Constraint
- [ ] CS-01: should block past date
  - Action: enter 2020-01-01
  - Expected: rejected
  - Bug Report:
    - Issue: Invalid Date Accepted
    - Actual: row created with past date
- [ ] CS-02: bare fail, no bug report
- [X] CS-03: genuinely fine
"""
    items = parse_result_items(text)
    assert items["CS-01"]["pass"] is False
    assert items["CS-01"]["has_bug_report"] is True
    assert items["CS-02"]["pass"] is False
    assert items["CS-02"]["has_bug_report"] is False
    assert items["CS-03"]["pass"] is True
    assert items["CS-03"]["has_bug_report"] is False


def test_build_sub_checklist_keeps_only_pass_ids_and_logs_dropped():
    checklist_md = """# Test Checklist

## Functionality
- [ ] FT-01: do thing
  - Action: click
  - Expected: modal opens

## Constraint
- [ ] CS-01: block past date
  - Action: enter 2020-01-01
  - Expected: rejected
"""
    # FT-01 passed; CS-99 passed in result but is absent from checklist (ID drift).
    sub, dropped = build_sub_checklist(checklist_md, {"FT-01", "CS-99"})
    assert "FT-01: do thing" in sub
    assert "modal opens" in sub          # Action/Expected fidelity preserved
    assert "CS-01" not in sub            # not a PASS id -> excluded
    assert sub.startswith("# Test Checklist")
    assert dropped == ["CS-99"]          # drifted id reported, not silently swallowed


def _pass1(extra=""):
    # First-pass # Test Result: FT-01 pass, FT-02 fail(with bug), CS-01 pass.
    return """# Test Result

## Functionality
- [X] FT-01: works
  - Action: a
  - Expected: b
- [ ] FT-02: already broken
  - Bug Report:
    - Issue: pre-existing
""" + extra + """
## Constraint
- [X] CS-01: claimed blocked
  - Action: enter past date
  - Expected: rejected
"""


def test_reconcile_flips_pass_to_fail_only_with_bug_report():
    pass1 = _pass1()
    reverify = """# Test Result
## Constraint
- [ ] CS-01: not actually blocked
  - Bug Report:
    - Issue: Invalid Date Accepted
    - Actual: row persisted
"""
    final, stats = reconcile(pass1, reverify)
    assert "- [ ] CS-01" in final           # flipped
    assert "Invalid Date Accepted" in final  # carries re-verify bug report
    assert "- [X] FT-01" in final            # untouched pass
    assert "- [ ] FT-02" in final            # first-pass fail preserved verbatim
    assert stats["flipped"] == ["CS-01"]
    assert stats["considered"] == 2   # FT-01 and CS-01 are the two PASS items


def test_reconcile_bare_fail_without_bug_report_does_not_flip():
    reverify = """# Test Result
## Constraint
- [ ] CS-01: vibes say broken
"""
    final, stats = reconcile(_pass1(), reverify)
    assert "- [X] CS-01" in final            # kept PASS (no evidence)
    assert stats["flipped"] == []


def test_reconcile_missing_item_in_reverify_keeps_pass():
    reverify = "# Test Result\n## Functionality\n- [X] FT-01: fine\n"  # CS-01 absent
    final, stats = reconcile(_pass1(), reverify)
    assert "- [X] CS-01" in final
    assert stats["flipped"] == []


def test_reconcile_does_not_retest_or_alter_first_pass_fails():
    reverify = "# Test Result\n## Functionality\n- [X] FT-02: looks fine now\n"
    final, _ = reconcile(_pass1(), reverify)
    assert "- [ ] FT-02" in final            # first-pass FAIL stays FAIL


def test_reconcile_negated_bug_report_mention_does_not_flip():
    reverify = """# Test Result
## Constraint
- [ ] CS-01: still fails per tester but
  - Note: No Bug Report was produced because it actually works
"""
    final, stats = reconcile(_pass1(), reverify)
    assert "- [X] CS-01" in final   # not flipped: no real Bug Report block
    assert stats["flipped"] == []
