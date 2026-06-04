from prompt import USER_PROMPT
from prompt.defect_hunt import PROMPT_DEFECT_HUNT


def test_prompt_registered():
    assert "defect_hunt" in USER_PROMPT


def test_substitutes_all_placeholders():
    out = PROMPT_DEFECT_HUNT.substitute(
        instruction="build a ledger app",
        server_url="http://localhost:6006",
        project_dir="/abs/path/to/project",
        hunt_rounds=3,
    )
    assert "build a ledger app" in out
    assert "http://localhost:6006" in out
    assert "/abs/path/to/project" in out
    # no leftover unfilled placeholders
    assert "$" not in out
    assert "3" in out  # hunt_rounds substituted


def test_prompt_is_checklist_free_and_emits_bug_report():
    src = PROMPT_DEFECT_HUNT.template
    assert "$checklist" not in src          # hunt is checklist-free by design
    assert "# Bug Report" in src            # distinct header, not "# Test Result"
    assert "# Test Result" not in src
    # iron law: never read gold/answer files (check the actual prohibitive phrase)
    assert '"gold"' in src and "NEVER read" in src
