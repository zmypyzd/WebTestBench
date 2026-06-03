from agent.reverify_reconcile import parse_pass_items


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
