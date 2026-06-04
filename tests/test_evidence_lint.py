from agent.evidence_lint import find_unsupported_pass


def test_pass_with_evidence_is_clean():
    text = """# Test Result
## Functionality
- [X] FT-01: browse works
  - Action: open home
  - Evidence: grid shows 9 event cards
"""
    assert find_unsupported_pass(text) == []


def test_pass_without_evidence_is_flagged():
    text = """# Test Result
## Functionality
- [X] FT-01: browse works
  - Action: open home
"""
    assert find_unsupported_pass(text) == ["FT-01"]


def test_pass_with_empty_evidence_is_flagged():
    text = """# Test Result
- [X] FT-02: search
  - Evidence:
"""
    assert find_unsupported_pass(text) == ["FT-02"]


def test_fail_items_are_not_required_to_have_evidence():
    text = """# Test Result
- [ ] CS-01: reject past date
  - Bug Report:
    - Issue: accepted
"""
    assert find_unsupported_pass(text) == []


def test_multiple_items_mixed():
    text = """# Test Result
- [X] FT-01: a
  - Evidence: observed X
- [X] FT-02: b
  - Action: did Y
- [ ] CS-01: c
- [X] IX-03: d
  - Evidence: toast appeared
"""
    # FT-02 is the only PASS without evidence
    assert find_unsupported_pass(text) == ["FT-02"]


def test_bold_evidence_form_is_recognized():
    text = """# Test Result
- [X] FT-01: browse works
  - **Evidence:** grid shows 9 event cards
"""
    assert find_unsupported_pass(text) == []


def test_evidence_without_leading_dash_is_recognized():
    text = """# Test Result
- [X] FT-01: browse works
  Evidence: confirmed via DOM
"""
    assert find_unsupported_pass(text) == []


def test_bold_evidence_with_empty_value_is_still_flagged():
    text = """# Test Result
- [X] FT-02: search
  - **Evidence:**
"""
    assert find_unsupported_pass(text) == ["FT-02"]
