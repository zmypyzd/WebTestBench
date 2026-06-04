from agent.base_agent import BaseAgent


def _validator():
    # _has_required_bugs only reads `content`; bind the unbound method.
    return BaseAgent._has_required_bugs


def test_valid_bug_report_passes():
    fn = _validator()
    text = "# Bug Report\n\n## BUG-001: dashboard shows $0\n- severity: High\n"
    assert fn(None, text) is True


def test_header_without_bug_block_fails():
    fn = _validator()
    text = "# Bug Report\n\nNo issues found.\n"
    assert fn(None, text) is False


def test_bug_block_without_header_fails():
    fn = _validator()
    text = "## BUG-001: orphaned\n- severity: Low\n"
    assert fn(None, text) is False


def test_empty_and_none_fail():
    fn = _validator()
    assert fn(None, "") is False
    assert fn(None, None) is False
