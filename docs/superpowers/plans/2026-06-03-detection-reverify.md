# Detection Second-Pass Re-Verification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a gated `defect_reverify` pipeline stage that, blind to the first pass's verdicts, re-judges every PASS item with a disconfirmation prompt and reconciles via evidence-gated union-of-failures, to lift bug-detection recall.

**Architecture:** A new pipeline stage `defect_reverify` runs after `defect_detection` and before `extract_result_file`. It extracts the PASS TEST-IDs from the first-pass result, builds a sub-checklist of only those items, runs a fresh blind browser session under a *new disconfirmation prompt*, then reconciles: a re-verify FAIL flips PASS→FAIL only if it carries a concrete Bug Report. All pure parse/build/reconcile logic lives in a dependency-free module (`eval/agent/reverify_reconcile.py`) so it is unit-testable without the Claude SDK. The stage is gated behind `--reverify` (default off) so baseline runs and historical comparability are untouched.

**Tech Stack:** Python 3.11+, `claude-agent-sdk` (existing), Playwright MCP (existing), `pytest` (new dev dependency for the pure-function tests).

**Spec:** `docs/superpowers/specs/2026-06-03-detection-reverify-design.md`

---

## File structure

| File | Responsibility | New/Modify |
|---|---|---|
| `eval/agent/reverify_reconcile.py` | Pure functions: parse PASS ids from `# Test Result`, build blind sub-checklist from `checklist.md`, evidence-gated reconcile. Stdlib-only (no SDK import) → unit-testable. | **New** |
| `eval/prompt/defect_reverify.py` | The disconfirmation prompt `Template`. | **New** |
| `eval/prompt/__init__.py` | Register `USER_PROMPT["defect_reverify"]`. | Modify |
| `eval/agent/base_agent.py` | `reverify_enabled` (default False), `result_reverified_path`, `final_result_path` property; `extract_result_file` reads `final_result_path` + error-rename of the extracted-from file. | Modify |
| `eval/agent/claude_code.py` | Store `self.reverify_enabled` from kwargs; new `defect_reverify()` method; insert into `run()` `stage_sequence`. | Modify |
| `eval/run_agent.py` | `--reverify` arg; pass `reverify=` into both `agent_cls(...)` calls. | Modify |
| `tests/conftest.py` | Put `eval/` on `sys.path` for tests. | **New** |
| `tests/test_reverify_reconcile.py` | Unit tests for the pure functions. | **New** |
| `scripts/run_reverify_abl.sh` | Fresh-dir, record-isolated on/off A/B harness. | **New** |

Implementation order: the pure module + tests first (Tasks 1–4, no API spend), then the prompt (Task 5), then the wiring (Tasks 6–9), then the A/B harness (Task 10).

---

## Task 1: Pure module skeleton + `parse_pass_items`

**Files:**
- Create: `eval/agent/reverify_reconcile.py`
- Create: `tests/conftest.py`
- Create: `tests/test_reverify_reconcile.py`

- [ ] **Step 1: Add pytest dev dependency note + conftest**

Create `tests/conftest.py` so tests can import the `eval/`-rooted modules (the codebase puts `eval/` on `sys.path`; mirror that for tests):

```python
import sys
from pathlib import Path

# eval/ modules import as top-level (e.g. `from agent.reverify_reconcile import ...`),
# matching how run_agent.py runs with eval/ on sys.path.
EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))
```

- [ ] **Step 2: Write the failing test for `parse_pass_items`**

Create `tests/test_reverify_reconcile.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py::test_parse_pass_items_extracts_only_passes_from_test_result_section -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.reverify_reconcile'`

- [ ] **Step 4: Implement `parse_pass_items`**

Create `eval/agent/reverify_reconcile.py`:

```python
"""Pure, dependency-free helpers for the defect_reverify stage.

Stdlib-only on purpose: importable in unit tests without the Claude SDK.
Mirrors the canonical checkbox parsing used by eval/scoring.py:_parse_pred_items.
"""
import re
from typing import Dict, List, Set

# Canonical result-item checkbox, identical to scoring.py:_parse_pred_items (line 533).
_CHECKBOX = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")


def _extract_test_result_section(text: str) -> str:
    """Return the text from the first '# Test Result' header onward, else ''."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("# Test Result"):
            return "\n".join(lines[i:])
    return ""


def parse_pass_items(test_result_text: str) -> Set[str]:
    """TEST-IDs marked PASS ('- [X]') within the '# Test Result' section only."""
    section = _extract_test_result_section(test_result_text)
    pass_ids: Set[str] = set()
    for line in section.splitlines():
        m = _CHECKBOX.match(line.strip())
        if m and m.group(1).lower() == "x":
            pass_ids.add(m.group(2).strip())
    return pass_ids
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add eval/agent/reverify_reconcile.py tests/conftest.py tests/test_reverify_reconcile.py
git commit -m "feat(reverify): pure parse_pass_items + test scaffold"
```

---

## Task 2: `parse_result_items` (full verdict + bug-report presence)

The reconciler needs, for every item in a `# Test Result`, its pass/fail and whether a FAIL carries a Bug Report. This parses the re-verify output.

**Files:**
- Modify: `eval/agent/reverify_reconcile.py`
- Test: `tests/test_reverify_reconcile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reverify_reconcile.py`:

```python
from agent.reverify_reconcile import parse_result_items


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py::test_parse_result_items_captures_pass_fail_and_bug_report_presence -v`
Expected: FAIL with `ImportError: cannot import name 'parse_result_items'`

- [ ] **Step 3: Implement `parse_result_items`**

Add to `eval/agent/reverify_reconcile.py`:

```python
def parse_result_items(test_result_text: str) -> Dict[str, dict]:
    """Map TEST-ID -> {pass: bool, has_bug_report: bool, block: List[str]}.

    `block` is the item's checkbox line plus its indented continuation lines,
    so a flip can carry the re-verify Bug Report verbatim into the final result.
    """
    section = _extract_test_result_section(test_result_text)
    lines = section.splitlines()
    items: Dict[str, dict] = {}
    order: List[str] = []

    cur: str = None
    for line in lines:
        m = _CHECKBOX.match(line.strip())
        if m:
            cur = m.group(2).strip()
            items[cur] = {"pass": m.group(1).lower() == "x", "block": [line], "has_bug_report": False}
            order.append(cur)
            continue
        if cur is not None:
            # Indented continuation line belongs to the current item.
            if line.strip() == "" or line[:1].isspace():
                items[cur]["block"].append(line)
                if "bug report" in line.lower():
                    items[cur]["has_bug_report"] = True
            else:
                cur = None
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add eval/agent/reverify_reconcile.py tests/test_reverify_reconcile.py
git commit -m "feat(reverify): parse_result_items with bug-report detection"
```

---

## Task 3: `build_sub_checklist` (blind PASS-only sub-checklist)

**Files:**
- Modify: `eval/agent/reverify_reconcile.py`
- Test: `tests/test_reverify_reconcile.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reverify_reconcile.py`:

```python
from agent.reverify_reconcile import build_sub_checklist


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py::test_build_sub_checklist_keeps_only_pass_ids_and_logs_dropped -v`
Expected: FAIL with `ImportError: cannot import name 'build_sub_checklist'`

- [ ] **Step 3: Implement `build_sub_checklist`**

Add to `eval/agent/reverify_reconcile.py`:

```python
from typing import Tuple

# Checklist item line, e.g. "- [ ] FT-01: do thing". Same id-capture as _CHECKBOX.
_CHECKLIST_ITEM = re.compile(r"^- \[\s*[xX ]\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")


def build_sub_checklist(checklist_md: str, pass_ids: Set[str]) -> Tuple[str, List[str]]:
    """Filter `checklist.md` to the PASS ids, preserving each item's Action/Expected.

    Returns (sub_checklist_markdown, dropped_ids). `dropped_ids` are PASS ids with
    no matching item in the checklist (ID drift) -> caller logs the count.
    No first-pass verdict is included, so the re-verify session stays blind.
    """
    lines = checklist_md.splitlines()
    out: List[str] = ["# Test Checklist", ""]
    found: Set[str] = set()

    i = 0
    n = len(lines)
    while i < n:
        m = _CHECKLIST_ITEM.match(lines[i].strip())
        if m and m.group(1).strip() in pass_ids:
            tid = m.group(1).strip()
            found.add(tid)
            out.append(lines[i])
            # Pull the item's indented continuation lines (Action/Expected/...).
            j = i + 1
            while j < n and (lines[j].strip() == "" or lines[j][:1].isspace()):
                if lines[j].strip() == "" and (j + 1 >= n or not lines[j + 1][:1].isspace()):
                    break
                out.append(lines[j])
                j += 1
            i = j
            continue
        i += 1

    dropped = sorted(pass_ids - found)
    return "\n".join(out).rstrip() + "\n", dropped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add eval/agent/reverify_reconcile.py tests/test_reverify_reconcile.py
git commit -m "feat(reverify): build_sub_checklist with dropped-id reporting"
```

---

## Task 4: `reconcile` (evidence-gated union-of-failures) — the core decision

**Files:**
- Modify: `eval/agent/reverify_reconcile.py`
- Test: `tests/test_reverify_reconcile.py`

- [ ] **Step 1: Write the failing tests (all reconcile rules)**

Append to `tests/test_reverify_reconcile.py`:

```python
from agent.reverify_reconcile import reconcile


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py -k reconcile -v`
Expected: FAIL with `ImportError: cannot import name 'reconcile'`

- [ ] **Step 3: Implement `reconcile`**

Add to `eval/agent/reverify_reconcile.py`:

```python
def reconcile(pass1_text: str, reverify_text: str) -> Tuple[str, dict]:
    """Evidence-gated union-of-failures.

    Walk the first-pass '# Test Result' line by line, rewriting only PASS items
    that the re-verify pass FAILed *with a concrete Bug Report*. First-pass FAIL
    items and untouched PASS items are emitted verbatim. Returns (final_text, stats).
    """
    reverify_items = parse_result_items(reverify_text)
    section_start = pass1_text.find("# Test Result")
    if section_start == -1:
        return pass1_text, {"flipped": [], "considered": 0}
    head = pass1_text[:section_start]
    body_lines = pass1_text[section_start:].splitlines()

    out: List[str] = []
    flipped: List[str] = []
    considered = 0
    i = 0
    n = len(body_lines)
    while i < n:
        line = body_lines[i]
        m = _CHECKBOX.match(line.strip())
        if not m:
            out.append(line)
            i += 1
            continue
        tid = m.group(2).strip()
        is_pass = m.group(1).lower() == "x"
        if not is_pass:
            # First-pass FAIL: emit verbatim, do not re-test.
            out.append(line)
            i += 1
            continue
        considered += 1
        rv = reverify_items.get(tid)
        if rv and rv["pass"] is False and rv["has_bug_report"]:
            # Flip: replace this PASS item's whole block with the re-verify block.
            flipped.append(tid)
            out.extend(rv["block"])
            i += 1
            # Skip the original item's indented continuation lines.
            while i < n and (body_lines[i].strip() == "" or body_lines[i][:1].isspace()):
                i += 1
        else:
            out.append(line)
            i += 1

    final = head + "\n".join(out)
    if not final.endswith("\n"):
        final += "\n"
    return final, {"flipped": flipped, "considered": considered}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=eval python -m pytest tests/test_reverify_reconcile.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add eval/agent/reverify_reconcile.py tests/test_reverify_reconcile.py
git commit -m "feat(reverify): evidence-gated union-of-failures reconcile"
```

---

## Task 5: The disconfirmation prompt

**Files:**
- Create: `eval/prompt/defect_reverify.py`
- Modify: `eval/prompt/__init__.py`

- [ ] **Step 1: Create the prompt**

Create `eval/prompt/defect_reverify.py`:

```python
from string import Template


PROMPT_DEFECT_REVERIFY = Template(
"""# Role
You are a SKEPTICAL second-opinion QA auditor. Each item in the checklist below was already
claimed to PASS by a first tester. Trust nothing. Your SOLE job is to find every item that does
NOT actually hold up under independent re-testing. You have no access to the first tester's notes
or verdicts — judge only from the running app's observed behavior and its source.

# Execution Standards

## 1. Interaction Strategy
- Tool Use: **Prefer Playwright tools** to drive and inspect the page. You MAY ALSO use `Bash`/`Read`/`Grep` to read the application's source code and seed/default data to design sharper tests or confirm what the code actually does. Do NOT use `Write`/`Edit` to modify the application under test. Never search for or read any "gold", "reference", "answer", or "expected-bugs" file — judge only from the running app's observed behavior and its source.
- DOM-Only verification: Do NOT use screenshots. Verify against DOM attributes (text, id, class, roles) and observed page state, not pixels.
- Limited Budget: Operate within a budget of max 100 turns/tool-calls total. Plan first; re-test with as few operations as possible.
- Navigation: Only navigate if an item explicitly requires it. Disable page refresh unless the page crashes.

## 2. Disconfirmation Logic (this is the whole point)
- For EVERY item, actively try to make it FAIL, then decide.
- **Re-confirm state after the action.** After performing the action, RE-READ the underlying state (the DOM after submit, the list/row that should/should not exist, the source guard) before concluding. A control that merely *looks* disabled, a toast you did not actually see fire, or a submit you did not confirm was rejected is NOT evidence of a block.
- **Constraint items (something that should be PREVENTED — past/invalid date, duplicate, empty/required, out-of-range, forbidden role/state action, double-booking):** PASS only if you DEMONSTRATE the system blocked it (validation message, disabled control, rejected submit, AND no state change). If the forbidden action SUCCEEDS — the bad state is actually persisted/accepted — that is a FAIL. "Looked blocked" is not acceptable; show the state did not change.
- Symmetry guard: do NOT manufacture a FAIL you cannot reproduce. A FAIL REQUIRES a concrete, reproducible observation. If an item genuinely holds up, mark it PASS.

## 3. Workflow
1. Navigate to the Target URL.
2. For each checklist item: perform the Action, attempt to break/bypass it, re-read the resulting state, then decide.

# Output Format (Markdown)
Output the FULL list below with updated statuses. Do not summarize; return the complete list.

**STRICT FORMAT (required for automated scoring):** every result item MUST be a single Markdown checkbox line beginning with `- [X] <TEST-ID>:` (pass) or `- [ ] <TEST-ID>:` (fail), preserving the original TEST-ID. Do NOT use heading lines or `**PASS**`/`**FAIL**` markers. Keep every TEST-ID from the checklist exactly once.

If PASS: `- [X] TEST-ID: [original Description]`
If FAIL: keep `- [ ]` and append a `Bug Report` block immediately after:

```markdown
- [ ] TEST-ID: [original Description]
  - Action: [original Action]
  - Expected: [original Expected]
  - Bug Report:
    - Issue: [problem type]
    - Actual: [the observed deviation you reproduced]
```

## Output Template

```markdown
# Test Result

## Functionality
[use the result item template for each FT-xx present below]

## Constraint
[use the result item template for each CS-xx present below]

## Interaction
[use the result item template for each IX-xx present below]

## Content
[use the result item template for each CT-xx present below]
```

# Input

## User Instruction
$instruction

## Application URL
$server_url

## Test Checklist (items previously claimed to PASS — re-verify each)
```markdown
$checklist
```

# Output
""")
```

- [ ] **Step 2: Register the prompt**

Modify `eval/prompt/__init__.py`:

```python
from .checklist_generation import PROMPT_CHECKLIST_GENERATION
from .defect_detection import PROMPT_DEFECT_DETECTION
from .defect_detection_based_gold import PROMPT_DEFECT_DETECTION_BASED_GOLD
from .defect_reverify import PROMPT_DEFECT_REVERIFY
from .match_item import PROMPT_MATCH_ITEM


USER_PROMPT = {
    "checklist_generation": PROMPT_CHECKLIST_GENERATION,
    "defect_detection": PROMPT_DEFECT_DETECTION,
    "defect_detection_based_gold": PROMPT_DEFECT_DETECTION_BASED_GOLD,
    "defect_reverify": PROMPT_DEFECT_REVERIFY,
    "match_item": PROMPT_MATCH_ITEM,
}

__all__ = [
    "PROMPT_CHECKLIST_GENERATION",
    "PROMPT_DEFECT_DETECTION",
    "PROMPT_DEFECT_DETECTION_BASED_GOLD",
    "PROMPT_DEFECT_REVERIFY",
    "PROMPT_MATCH_ITEM",
]
```

- [ ] **Step 3: Verify it imports and substitutes**

Run: `PYTHONPATH=eval python -c "from prompt import USER_PROMPT; print(USER_PROMPT['defect_reverify'].substitute(instruction='i', server_url='u', checklist='c')[:40])"`
Expected: prints `# Role\nYou are a SKEPTICAL second-opinion QA` (first 40 chars), no `KeyError`.

- [ ] **Step 4: Commit**

```bash
git add eval/prompt/defect_reverify.py eval/prompt/__init__.py
git commit -m "feat(reverify): disconfirmation prompt for second-pass re-verification"
```

---

## Task 6: BaseAgent — reverify paths + `final_result_path` + extract uses it

**Files:**
- Modify: `eval/agent/base_agent.py:50-53` (path attrs) and `:55-88` (extract_result_file)

- [ ] **Step 1: Add path attrs + property after the existing path block (`base_agent.py:53`)**

After `self.session_meta_path = self.output_dir / "session_meta.json"`, add:

```python
        self.result_reverified_path = self.output_dir / "result_reverified.md"
        # Set True by subclasses when the --reverify gate is on; gates final_result_path.
        self.reverify_enabled: bool = False

    @property
    def final_result_path(self) -> Path:
        """The result file extract_result_file should consume.

        When reverify is enabled and produced a reconciled file, that is canonical;
        otherwise fall back to the first-pass result.md. This is the ONLY place the
        downstream artifact source is decided, so scoring needs no change.
        """
        if self.reverify_enabled and self.result_reverified_path.exists():
            return self.result_reverified_path
        return self.result_path
```

(Note: the `@property` must be a class-level method — place it as a method on `BaseAgent`, not inside `__init__`. Put the two assignments at the end of `__init__`, and the `final_result_path` property immediately after `__init__` ends.)

- [ ] **Step 2: Point `extract_result_file` at `final_result_path`**

In `eval/agent/base_agent.py:64`, change:

```python
            content = self._load_file_content(self.result_path)
```
to:
```python
            content = self._load_file_content(self.final_result_path)
```

And in the error-rename branch (`base_agent.py:71-75`), change the three `self.result_path` references to `self.final_result_path` so the file actually extracted is the one renamed on failure:

```python
            if self.final_result_path.exists():
                timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                error_path = self.final_result_path.with_name(f"{self.final_result_path.stem}-error_{timestamp}.md")
                self.final_result_path.rename(error_path)
```

- [ ] **Step 3: Verify the module still imports**

Run: `PYTHONPATH=eval python -c "from agent.base_agent import BaseAgent; print('ok')"`
Expected: prints `ok` (no SyntaxError / IndentationError).

- [ ] **Step 4: Commit**

```bash
git add eval/agent/base_agent.py
git commit -m "feat(reverify): final_result_path property feeds extract_result_file"
```

---

## Task 7: `--reverify` flag plumbing in run_agent.py

**Files:**
- Modify: `eval/run_agent.py` (`parse_args` ~line 54, both `agent_cls(...)` ~lines 92 and 120)

- [ ] **Step 1: Add the flag in `parse_args`**

After the `--model` argument (`run_agent.py:54`), add:

```python
    parser.add_argument("--reverify", action="store_true",
                        help="Enable the blind second-pass defect_reverify stage (default off).")
```

- [ ] **Step 2: Pass `reverify=` into BOTH constructors**

The `probe_agent` (`run_agent.py:92`) only checks `result_extracted_path` and does not need it, but pass it for consistency so `final_result_path` resolves identically in both. Add `reverify=args.reverify,` to the `probe_agent = agent_cls(...)` kwargs and to the real `agent = agent_cls(...)` kwargs (the real one already passes `record=record`):

```python
        probe_agent = agent_cls(
            instruction=instruction,
            api_config=api_config,
            server_url=server_url,
            local_project_dir=local_project_dir,
            output_dir=output_dir,
            event_log_stream=None,
            reverify=args.reverify,
        )
```
```python
        agent = agent_cls(
            instruction=instruction,
            api_config=api_config,
            server_url=server_url,
            local_project_dir=local_project_dir,
            output_dir=output_dir,
            event_log_stream=log_f,
            record=record,
            reverify=args.reverify,
        )
```

- [ ] **Step 3: Verify args parse**

Run: `PYTHONPATH=eval python eval/run_agent.py --help 2>&1 | grep -A1 reverify`
Expected: shows `--reverify` and its help text.

- [ ] **Step 4: Commit**

```bash
git add eval/run_agent.py
git commit -m "feat(reverify): --reverify flag threaded into both agent constructions"
```

---

## Task 8: ClaudeCodeWebTester — store the gate

**Files:**
- Modify: `eval/agent/claude_code.py:36-54` (`__init__`)

- [ ] **Step 1: Store `reverify_enabled` from kwargs**

In `ClaudeCodeWebTester.__init__`, after `self.result_path = self.output_dir / "result.md"` (`claude_code.py:46`), add:

```python
        self.result_reverify_raw_path = self.output_dir / "result_reverify_raw.md"
        # BaseAgent declares reverify_enabled/result_reverified_path; the gate value
        # arrives via kwargs from run_agent.py (constructor previously dropped kwargs).
        self.reverify_enabled = bool(kwargs.get("reverify", False))
```

- [ ] **Step 2: Verify import + default-off**

Run: `PYTHONPATH=eval python -c "from agent.claude_code import ClaudeCodeWebTester; import inspect; print('reverify' in inspect.getsource(ClaudeCodeWebTester.__init__))"`
Expected: prints `True`.

- [ ] **Step 3: Commit**

```bash
git add eval/agent/claude_code.py
git commit -m "feat(reverify): store reverify gate in ClaudeCodeWebTester"
```

---

## Task 9: `defect_reverify()` stage + wire into `run()`

**Files:**
- Modify: `eval/agent/claude_code.py` — new method after `defect_detection` (`:177`); add to `stage_sequence` (`:65-70`); add import.

- [ ] **Step 1: Import the pure helpers**

At the top of `eval/agent/claude_code.py`, after `from prompt import USER_PROMPT` (`:14`), add:

```python
from agent.reverify_reconcile import parse_pass_items, build_sub_checklist, reconcile
```

- [ ] **Step 2: Insert the stage into `stage_sequence`**

In `run()` (`claude_code.py:65-70`), change:

```python
        stage_sequence = [
            self.server_deploy,
            self.checklist_generation,
            self.defect_detection,
            self.extract_result_file,
        ]
```
to:
```python
        stage_sequence = [
            self.server_deploy,
            self.checklist_generation,
            self.defect_detection,
            self.defect_reverify,
            self.extract_result_file,
        ]
```

- [ ] **Step 3: Add the `defect_reverify` method**

Insert immediately after `defect_detection` ends (`claude_code.py:177`), mirroring its structure:

```python
    async def defect_reverify(self) -> bool:
        stage = "defect_reverify"
        target_file = self.result_reverified_path
        self.current_stage = stage

        # Gate: no-op when disabled -> baseline byte-identical.
        if not self.reverify_enabled:
            return True

        if self._should_skip_stage(target_file, stage):
            return True

        if not self.result_path.exists() or not self.checklist_path.exists():
            self._mark_stage(stage=stage, status="error", message="reverify needs result.md + checklist.md.")
            return False

        self._write_stage_success(stage, True)
        self._mark_stage(stage=stage, status="running", message="🚀 Defect Re-Verify ...")

        pass1_text = self._load_file_content(self.result_path)
        checklist_md = self._load_file_content(self.checklist_path)
        pass_ids = parse_pass_items(pass1_text)

        # Nothing PASSed -> nothing to re-verify; reconciled == first pass.
        if not pass_ids:
            self.write_markdown(target_file, pass1_text)
            self._emit_file_event(stage, target_file)
            print_green("✅ Re-Verify skipped (no PASS items).")
            return True

        sub_checklist, dropped = build_sub_checklist(checklist_md, pass_ids)
        if dropped:
            self._emit_event(type_name="reverify_dropped_ids", stage=stage,
                             payload=dict(count=len(dropped), ids=dropped))
        if sub_checklist.strip() == "# Test Checklist":
            # All PASS ids drifted; nothing concrete to re-test -> keep first pass.
            self.write_markdown(target_file, pass1_text)
            self._emit_file_event(stage, target_file)
            return True

        prompt = USER_PROMPT["defect_reverify"].substitute(
            instruction=self.instruction, server_url=self.server_url, checklist=sub_checklist,
        )
        self.event_log_stream.write(f"{'-'*20} REVERIFY PROMPT {'-'*20}\n{prompt}\n{'-'*50}\n")
        options = self._get_browser_agent_options(max_turns=self.max_turns)

        result_message = ""
        num_turns = 0
        async for message in query(prompt=prompt, options=options):
            self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
            self._handle_message(message, stage=stage)
            if isinstance(message, ResultMessage):
                result_message = message.result
                num_turns = message.num_turns

        # Degrade on any failure: reconciled := first pass (never destroy caught bugs).
        if num_turns > self.max_turns:
            self.write_markdown(target_file, pass1_text)
            self._mark_stage(stage=stage, status="error", message="reverify exceeded turn budget; kept first pass.")
            self._emit_file_event(stage, target_file)
            return True

        reverify_raw, _ = self._extract_final_result(result_message, stage="defect_detection")
        self.write_markdown(self.result_reverify_raw_path, reverify_raw)

        if not self._has_required_result(reverify_raw):
            self.write_markdown(target_file, pass1_text)
            self._mark_stage(stage=stage, status="error", message="reverify output missing '# Test Result'; kept first pass.")
            self._emit_file_event(stage, target_file)
            return True

        try:
            reconciled, stats = reconcile(pass1_text, reverify_raw)
        except Exception as exc:
            self.write_markdown(target_file, pass1_text)
            self._mark_stage(stage=stage, status="error", message=f"reconcile failed ({exc}); kept first pass.")
            self._emit_file_event(stage, target_file)
            return True

        self._emit_event(type_name="reverify_flips", stage=stage,
                         payload=dict(flipped=stats["flipped"], considered=stats["considered"]))
        self.write_markdown(target_file, reconciled)

        if self._verify_output_file(target_file):
            self._emit_file_event(stage, target_file)
            print_green(f"✅ Re-Verify Completed (flipped {len(stats['flipped'])}/{stats['considered']}).")
            return True
        self._mark_stage(stage=stage, status="error", message=f"Stage {stage} did not produce {target_file}.")
        return False
```

- [ ] **Step 4: Verify import + method presence**

Run: `PYTHONPATH=eval python -c "from agent.claude_code import ClaudeCodeWebTester as C; print(hasattr(C, 'defect_reverify'))"`
Expected: prints `True`.

- [ ] **Step 5: Full unit suite still green**

Run: `PYTHONPATH=eval python -m pytest tests/ -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Commit**

```bash
git add eval/agent/claude_code.py
git commit -m "feat(reverify): defect_reverify stage wired into pipeline"
```

---

## Task 10: A/B ablation harness (fresh-dir, record-isolated)

**Files:**
- Create: `scripts/run_reverify_abl.sh`

This mirrors the existing `scripts/run_p2abl.sh` pattern but runs two arms — reverify OFF vs ON — each into its OWN version dir (the resume precondition: a shared/stale dir makes the ON arm a no-op).

- [ ] **Step 1: Read the existing ablation harness to copy conventions**

Run: `sed -n '1,60p' scripts/run_p2abl.sh`
Expected: shows how `API_BASE_URL`/`API_KEY`/`MODEL`, dataset path, per-version output dirs, and per-record isolation are set. Copy those exact conventions (do not invent new ones).

- [ ] **Step 2: Write `scripts/run_reverify_abl.sh`**

Create `scripts/run_reverify_abl.sh` (fill the `XXX` placeholders the repo convention uses, matching `run_p2abl.sh`):

```bash
#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="XXX"
API_KEY="XXX"
MODEL="XXX"

DATA_JSONL="XXX"          # small held-out JSONL (smoke n=3 first)
PROJECT_ROOT="XXX"        # data/WebTestBench/web_applications
OUTPUT_ROOT="outputs"
LOG_ROOT="logs"
BASE_PORT=6000

# Two arms into DISTINCT version dirs. Fresh dirs are mandatory: run() short-circuits
# when result_extracted.md already exists, so reusing a baseline dir makes ON a no-op.
declare -A ARMS=(
  [reverify-off]=""
  [reverify-on]="--reverify"
)

# Per-record isolation: run_agent sys.exit(1)s on the first record error, so iterate
# records one JSONL-line at a time to survive external rate limits (tuning-log lesson).
for arm in "${!ARMS[@]}"; do
  flag="${ARMS[$arm]}"
  while IFS= read -r line; do
    tmp="$(mktemp)"; printf '%s\n' "$line" > "$tmp"
    python eval/run_agent.py --agent claude_code \
      --data_jsonl_path "$tmp" \
      --project_root "$PROJECT_ROOT" \
      --output_root "$OUTPUT_ROOT" --log_root "$LOG_ROOT" \
      --version "$arm" --base_port "$BASE_PORT" \
      --api_base_url "$API_BASE_URL" --api_key "$API_KEY" --model "$MODEL" \
      $flag || echo "record failed in $arm, continuing"
    rm -f "$tmp"
  done < "$DATA_JSONL"
done

echo "Done. Score each arm:"
echo "  bash scripts/run_scoring.sh   # set --version reverify-off then reverify-on"
echo "Compare overall_no_missing precision/recall/F1; apply the pre-registered kill criterion."
```

- [ ] **Step 3: Shellcheck / syntax check**

Run: `bash -n scripts/run_reverify_abl.sh`
Expected: no output (syntax OK).

- [ ] **Step 4: Commit**

```bash
git add scripts/run_reverify_abl.sh
git commit -m "test(reverify): fresh-dir record-isolated on/off A/B harness"
```

---

## Task 11: Smoke run + scoring (validation gate — costs API)

> This is the integration validation. It spends API budget and is rate-limited — run on a small held-out set first.

- [ ] **Step 1: Edit the `XXX` placeholders** in `scripts/run_reverify_abl.sh` with real API creds, a smoke JSONL of ~3 records (NOT from the 14 tuned in P1/P2), and the web-app project root.

- [ ] **Step 2: Smoke the path on ONE record, ON arm**

Run a single record through with `--reverify` and confirm the new artifacts appear:
```bash
ls outputs/reverify-on/<record_id>/result_reverify_raw.md outputs/reverify-on/<record_id>/result_reverified.md
```
Expected: both files exist; `session_meta.json` has a `defect_reverify` entry; the log shows a `reverify_flips` event.

- [ ] **Step 3: Run both arms** via `bash scripts/run_reverify_abl.sh`.

- [ ] **Step 4: Score both arms** (`scripts/run_scoring.sh` with `--version reverify-off`, then `reverify-on`), then compare `score_avg.json` `overall_no_missing`.

- [ ] **Step 5: Apply the pre-registered kill criterion** (spec Decision #4 / validation): ship only if recall/F1 rise with precision cost inside bound (NOT new-FPs ≥ new-TPs). Record the numbers in `tuning-log.md`.

- [ ] **Step 6: Final verification** — confirm baseline is untouched with the gate off:
```bash
PYTHONPATH=eval python -m pytest tests/ -v
```
Expected: PASS. And confirm a no-`--reverify` run still produces `result_extracted.md` from `result.md` exactly as before (the property falls back when `result_reverified.md` is absent).

---

## Self-review notes (author)

- **Spec coverage:** Decisions #1 (all PASS, all 4 classes — sub-checklist filters by PASS only, no class filter) ✓; #2 blind (build_sub_checklist injects no verdict) ✓; #3 disconfirmation prompt (Task 5) ✓; #4 evidence-gated union + kill criterion (Task 4 + Task 11 step 5) ✓; #5 new stage gated (Tasks 6–9) ✓; #6 honest cost + record isolation (Task 10) ✓. Error-handling degrade paths (timeout/empty/parse-fail/missing-item) ✓. Resume/fresh-dir precondition (Task 10 comment + Task 11) ✓. Gold-file prohibition preserved verbatim in Task 5 prompt ✓.
- **Type consistency:** `reconcile` returns `(str, dict)` with `stats["flipped"]`/`stats["considered"]` — used consistently in Task 4 tests and Task 9 method. `build_sub_checklist` returns `(str, List[str])` — consumed as `sub_checklist, dropped` in Task 9. `parse_pass_items` → `Set[str]`, `parse_result_items` → `Dict[str, dict]` with `pass`/`has_bug_report`/`block` keys, all consistent.
- **Placeholder scan:** the only `XXX` are the repo-standard script credential placeholders (intentional, matching every other `scripts/*.sh`).
