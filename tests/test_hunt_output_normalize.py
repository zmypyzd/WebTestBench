"""Regression tests anchored to REAL hunt output captured from a live smoke run
(record 0006). The model emitted a Chinese `# 系统错误报告` header preceded by a
conversational preamble — exactly the case the original validator/extraction missed.
"""
from pathlib import Path

from agent.base_agent import BaseAgent

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_hunt_output_0006.md"


def _validator():
    return BaseAgent._has_required_bugs


def _slicer():
    return BaseAgent._slice_bug_report


def test_real_chinese_header_output_is_accepted():
    # The real run produced `# 系统错误报告` + many `## BUG-NNN` blocks. Must validate.
    text = _FIXTURE.read_text(encoding="utf-8")
    assert _validator()(None, text) is True


def test_english_header_still_accepted():
    text = "# Bug Report\n\n## BUG-001: boom\n- severity: High\n"
    assert _validator()(None, text) is True


def test_chinese_header_minimal_accepted():
    text = "# 系统错误报告\n\n## BUG-001: 越界\n- 严重级别: High\n"
    assert _validator()(None, text) is True


def test_header_without_bug_block_still_fails():
    assert _validator()(None, "# 系统错误报告\n\n无问题\n") is False


def test_slice_trims_conversational_preamble():
    # Real fixture starts with "All console warnings captured ... Here is the ... report:"
    text = _FIXTURE.read_text(encoding="utf-8")
    sliced = _slicer()(None, text)
    first = next(l for l in sliced.splitlines() if l.strip())
    assert first.strip() == "# 系统错误报告"
    assert "All console warnings captured" not in sliced
    # content is preserved from the header onward
    assert "## BUG-001" in sliced


def test_slice_noop_when_already_clean():
    text = "# Bug Report\n\n## BUG-001: x\n"
    assert _slicer()(None, text).startswith("# Bug Report")


def test_slice_falls_back_to_first_bug_heading_when_no_report_header():
    text = "chatty preamble line\n\n## BUG-001: x\n- severity: Low\n"
    sliced = _slicer()(None, text)
    assert sliced.startswith("## BUG-001")
