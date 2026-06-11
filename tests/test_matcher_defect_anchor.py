"""Defect-anchored matching: failed predictions must carry a compact defect
summary (Bug Report Issue/Actual) into the matcher prompt, so the matcher can
anchor on the OBSERVED DEFECT instead of the item's surface wording.

Motivating specimens (dd3r, 2026-06-11, gold尺 m7ng):
* 0009 CS-05 "already-booked dates cannot be re-requested" FAILed with a
  double-booking defect — surface-matched to gold#9[ok] (date availability)
  instead of gold#10[BUG] (one booking per property/time) -> FP.
* 0037 FT-02 "filter by age range" FAILed with the age-misclassification
  defect — matched gold#1[ok] (generic filter) instead of gold#17[BUG].
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
import scoring  # noqa: E402
from prompt import USER_PROMPT  # noqa: E402


class _P:
    canonicalize = True
    _parse_pred_items = scoring.ScoringPipeline._parse_pred_items


FAIL_WITH_BUG_REPORT = (
    "- [ ] CS-05: Already-booked dates cannot be requested again\n"
    "  - Action: Attempt to double-book the same dates\n"
    "  - Expected: The request is blocked\n"
    "  - Bug Report:\n"
    "    - Issue: No Overlap Detection for Bookings\n"
    "    - Actual: The same dates remained selectable; a second request was double-booked without any error.\n"
    "- [X] CS-04: Past dates are rejected\n"
    "  - Action: Enter a past check-in date\n"
)


def test_failed_item_content_carries_defect_summary():
    items = _P._parse_pred_items(_P(), FAIL_WITH_BUG_REPORT)
    assert items["CS-05"]["pass"] is False
    c = items["CS-05"]["content"]
    assert "No Overlap Detection" in c
    assert "double-booked" in c
    assert c.startswith("Already-booked dates cannot be requested again")


def test_pass_item_content_untouched():
    items = _P._parse_pred_items(_P(), FAIL_WITH_BUG_REPORT)
    assert items["CS-04"]["content"] == "Past dates are rejected"
    assert items["CS-04"]["pass"] is True


def test_defect_summary_is_bounded_and_single_line():
    text = (
        "- [ ] FT-01: thing under test\n"
        "  - Bug Report:\n"
        "    - Issue: " + "i" * 1000 + "\n"
        "    - Actual: " + "a" * 1000 + "\n"
    )
    items = _P._parse_pred_items(_P(), text)
    c = items["FT-01"]["content"]
    assert len(c) <= 600
    assert "\n" not in c


def test_action_expected_evidence_lines_not_folded_in():
    items = _P._parse_pred_items(_P(), FAIL_WITH_BUG_REPORT)
    c = items["CS-05"]["content"]
    assert "Attempt to double-book" not in c   # Action stays out
    assert "The request is blocked" not in c   # Expected stays out


def test_match_prompt_has_defect_anchor_rule():
    tpl = USER_PROMPT["match_item"].substitute(instruction="i", gold_items="g", pred_items="p")
    # the new rule must reference the `| defect` annotation and state the
    # defect-over-surface priority for failed predictions
    assert "| defect" in tpl
    assert "anchor" in tpl.lower()
