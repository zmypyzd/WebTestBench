from agent.reverify_reconcile import parse_pass_items
from agent.reverify_reconcile import parse_result_items


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
