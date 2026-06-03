# Self-hunt Integration Implementation Plan (format reliability + evidence-forced judgment)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the two defensible advantages of the white-box "self-hunt" QA method into WebTester — (Step 1) canonical detection-output normalization that strips phantom `BUG-xx` ids and converts heading/inline forms to canonical checkboxes so the matcher gets clean input, and (Step 2) an evidence-forced judgment schema plus an `evidence_lint` that flags PASS items lacking observed-DOM evidence.

**Architecture:** Two independently-flagged, independently-ablated steps inside the existing two-stage pipeline. Step 1 is a pure parsing-layer change in scoring (new `eval/canonicalize.py`, wired into `scoring._parse_pred_items` behind `--canonicalize`). Step 2 is a prompt-schema change plus a new `eval/agent/evidence_lint.py` called after extraction, behind `--require_evidence`. No scoring semantics change; gold is never read.

**Tech Stack:** Python 3.11+, pytest 8.4.2, `string.Template` prompts, `claude-agent-sdk`. Tests import with `eval/` on `sys.path` via `tests/conftest.py` (e.g. `from canonicalize import normalize_to_canonical`).

**Execution-order gate (from spec):** Implement and ablate **Step 1 (Tasks 1–4)** and confirm its gate (phantom-id rate → 0; `empty_match` rate drops on 0006-class records across ≥2 repeats; no regression on canonical records) **before running Step 2 (Tasks 5–8)**. Both plans are written now; the gate governs *execution*, not authoring.

---

## File Structure

**Create:**
- `eval/canonicalize.py` — `normalize_to_canonical(text)`, `count_phantom_ids(text)`. Single responsibility: dialect→canonical checkbox text + phantom-id metric. Pure functions, no I/O.
- `eval/agent/evidence_lint.py` — `find_unsupported_pass(result_text)`. Single responsibility: detect PASS items lacking an `Evidence:` line. Pure function, no I/O.
- `tests/test_canonicalize.py` — unit tests for Step 1.
- `tests/test_evidence_lint.py` — unit tests for Step 2.
- `scripts/ablate_canonicalize.py` — Step 1 ablation harness (phantom-id + empty_match metrics, ≥2 repeats).

**Modify:**
- `eval/scoring.py` — add `--canonicalize` arg; `_parse_pred_items` calls `normalize_to_canonical` when enabled; tighten header fallback to exclude `BUG-` ids as a belt-and-suspenders.
- `eval/prompt/defect_detection.py` — add an optional `EVIDENCE_REQUIREMENT` block + evidence line in the result item template.
- `eval/run_agent.py` — add `--require_evidence` flag (mirror `--reverify`), thread into agent.
- `eval/agent/claude_code.py` — accept `require_evidence`, append evidence requirement to detection prompt, run `evidence_lint` after detection and emit an event.
- `eval/agent/base_agent.py` — accept/store `require_evidence` (constructor parity), expose hook for the lint event.
- `tuning-log.md` — append Step 1 and Step 2 ablation records.

---

## STEP 1 — Output-format reliability

### Task 1: `normalize_to_canonical` + `count_phantom_ids` (pure functions, TDD)

**Files:**
- Create: `eval/canonicalize.py`
- Test: `tests/test_canonicalize.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_canonicalize.py
from canonicalize import normalize_to_canonical, count_phantom_ids


def test_canonical_lines_pass_through_unchanged():
    text = "- [X] FT-01: works\n- [ ] CS-02: should block past date\n"
    assert normalize_to_canonical(text) == text.rstrip("\n") + "\n" if False else normalize_to_canonical(text)
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
    # the real CS-02 line survives untouched
    assert "- [ ] CS-02: should block past date" in out


def test_count_phantom_ids():
    text = "### BUG-01 · CS-02: x\n### BUG-05 · CT-02: y\n- [ ] CS-02: z\n"
    assert count_phantom_ids(text) == 2
    assert count_phantom_ids("- [x] FT-01: clean\n") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_canonicalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canonicalize'`.

- [ ] **Step 3: Write minimal implementation**

```python
# eval/canonicalize.py
"""Normalize detection-output dialects to canonical checkbox lines.

The scoring parser's canonical regex only reads `- [x] ID:` / `- [ ] ID:`.
Detection sometimes emits heading form (`### FT-01 ... PASS`) or inline
(`**IX-04: PASS**`), and bug-report headings like `### BUG-01 · CS-02`
leak phantom `BUG-xx` ids into the scorer. This module maps the dialects to
canonical lines and drops phantom `BUG-xx` ids (never a TEST-ID; the taxonomy
is only FT/CS/IX/CT). Pure, idempotent, no I/O.
"""
import re

_CANON_RE = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")
_HEADER_RE = re.compile(r"^#{2,4}\s*(?:\*\*)?([A-Za-z]{2,3}-\d+)(?:\*\*)?\s*[:·–—\-]?\s*(.*)$")
_INLINE_RE = re.compile(r"^(?:- )?\*\*([A-Za-z]{2,3}-\d+):\s*(PASS|FAIL)\*\*\s*(.*)$", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)
_PHANTOM_RE = re.compile(r"^BUG-?\d+$", re.IGNORECASE)


def count_phantom_ids(text: str) -> int:
    """Count `### BUG-xx ...` heading ids in raw detection output."""
    count = 0
    for line in text.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m and _PHANTOM_RE.match(m.group(1)):
            count += 1
    return count


def normalize_to_canonical(text: str) -> str:
    """Return text with heading/inline items rewritten as canonical checkbox
    lines and phantom BUG-xx ids dropped. Idempotent on canonical input."""
    lines = text.splitlines()
    out = []
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if _CANON_RE.match(s):
            out.append(raw)
            i += 1
            continue

        m = _INLINE_RE.match(s)
        if m:
            tid, status = m.group(1), m.group(2).upper()
            if not _PHANTOM_RE.match(tid):
                box = "x" if status == "PASS" else " "
                desc = m.group(3).strip() or tid
                out.append(f"- [{box}] {tid}: {desc}")
            i += 1
            continue

        hm = _HEADER_RE.match(s)
        if hm:
            tid = hm.group(1)
            if _PHANTOM_RE.match(tid):
                i += 1  # drop phantom BUG-xx heading entirely
                continue
            desc = hm.group(2).strip() or tid
            status = None
            same = _STATUS_RE.search(s)
            if same:
                status = same.group(1).upper()
            else:
                for j in range(i + 1, min(i + 4, n)):
                    nxt = _STATUS_RE.search(lines[j])
                    if nxt:
                        status = nxt.group(1).upper()
                        break
            box = " " if status == "FAIL" else "x"  # default to pass when unknown (parity with old fallback)
            out.append(f"- [{box}] {tid}: {desc}")
            i += 1
            continue

        out.append(raw)
        i += 1

    result = "\n".join(out)
    return result + "\n" if result and not result.endswith("\n") else result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_canonicalize.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add eval/canonicalize.py tests/test_canonicalize.py
git commit -m "feat(scoring): canonicalize module — dialect->checkbox, drop phantom BUG-xx"
```

---

### Task 2: Real-artifact regression test against 0006

**Files:**
- Test: `tests/test_canonicalize.py` (append)

- [ ] **Step 1: Write the failing test (uses the committed 0006 artifact)**

```python
# tests/test_canonicalize.py (append)
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_R0006 = _REPO / "outputs" / "reverify-off" / "WebTestBench_0006" / "result_extracted.md"
_CANON = re.compile(r"^- \[\s*([xX ])\s*\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:\s*(.+)$")


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
```

- [ ] **Step 2: Run to verify it passes (canonicalize already implemented)**

Run: `python -m pytest tests/test_canonicalize.py::test_0006_artifact_yields_canonical_items_without_phantoms -v`
Expected: PASS (or SKIP if the artifact was pruned). If FAIL, inspect the real `result_extracted.md` heading shapes and widen `_HEADER_RE`/`_INLINE_RE` accordingly, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_canonicalize.py
git commit -m "test(scoring): 0006 real-artifact regression for canonicalize"
```

---

### Task 3: Wire `--canonicalize` into scoring

**Files:**
- Modify: `eval/scoring.py` (import; `__init__`/config; `_parse_pred_items`; `parse_args`)

- [ ] **Step 1: Add the import and a config flag**

At the top of `eval/scoring.py` imports add:

```python
from canonicalize import normalize_to_canonical
```

Find where the pipeline stores config from args (the `ScoringPipeline.__init__`). Add a parameter `canonicalize: bool = False` and store `self.canonicalize = canonicalize`. (Match the existing constructor style; if config is passed as an args namespace, read `getattr(args, "canonicalize", False)`.)

- [ ] **Step 2: Apply normalization in `_parse_pred_items`**

In `eval/scoring.py`, `_parse_pred_items` (currently starts ~line 528) — make the FIRST line normalize when enabled, before the canonical regex runs:

```python
    def _parse_pred_items(self, text: str) -> Dict[str, dict]:
        if getattr(self, "canonicalize", False):
            text = normalize_to_canonical(text)
        lines = text.splitlines()
        pred_items: Dict[str, dict] = {}
        # ... existing canonical regex block unchanged ...
```

Also tighten the header fallback (belt-and-suspenders) so a phantom id never becomes an item even with `--canonicalize` off. In the fallback `hdr` regex block (~line 545), after `cur = hm.group(1)` add:

```python
                if re.match(r"BUG-?\d+$", cur, re.IGNORECASE):
                    cur = None
                    continue
```

- [ ] **Step 3: Add the CLI flag**

In `parse_args()` (~line 25), mirroring `--use_checklist_fallback`, add:

```python
    parser.add_argument("--canonicalize", action="store_true",
                        help="Normalize detection output to canonical checkbox "
                             "form (strip phantom BUG-xx, convert heading/inline) "
                             "before matching. Default off for A/B ablation.")
```

And where `ScoringPipeline` is constructed in `main()`, pass `canonicalize=args.canonicalize`.

- [ ] **Step 4: Add a focused test that scoring routes through normalize**

```python
# tests/test_canonicalize.py (append)
def test_scoring_parse_pred_items_uses_normalize_when_enabled():
    import scoring
    text = "### FT-01: works\n**PASS**\n### BUG-01 · CS-02: x\n"
    # Build a minimal pipeline-like object exposing canonicalize + the method.
    class _P:
        canonicalize = True
        _parse_pred_items = scoring.ScoringPipeline._parse_pred_items
    items = _P._parse_pred_items(_P(), text)
    assert "FT-01" in items and items["FT-01"]["pass"] is True
    assert not any(k.upper().startswith("BUG") for k in items)
```

- [ ] **Step 5: Run the test**

Run: `python -m pytest tests/test_canonicalize.py::test_scoring_parse_pred_items_uses_normalize_when_enabled -v`
Expected: PASS. (If `_parse_pred_items` references other `self` attributes, extend `_P` with the minimal attributes the method touches — read the method body first.)

- [ ] **Step 6: Smoke-run scoring on 0006 both ways**

Run (baseline, off):
```bash
python eval/scoring.py --dataset_path ./data/WebTestBench/_cc_selfhunt_0002.jsonl \
  --output_root ./outputs --version _normtest_off --use_checklist_fallback True \
  --api_base_url https://api.minimaxi.com/v1/chat/completions --api_key "$MINIMAX_API_KEY" --api_model MiniMax-M3 || true
```
(Use a 0006-only dataset for the real target; the command shape is what matters.) Then re-run with `--canonicalize`. Expected: `--canonicalize` run shows `num_pred_item` without phantom inflation and ideally `empty_match` cleared. Record both `score.json`s.

- [ ] **Step 7: Commit**

```bash
git add eval/scoring.py tests/test_canonicalize.py
git commit -m "feat(scoring): --canonicalize flag routes parse through normalize + drop BUG-xx in fallback"
```

---

### Task 4: Step 1 ablation harness + gate

**Files:**
- Create: `scripts/ablate_canonicalize.py`
- Modify: `tuning-log.md` (append)

- [ ] **Step 1: Write the harness**

```python
# scripts/ablate_canonicalize.py
"""Step 1 ablation: phantom-id + empty_match rate, baseline vs --canonicalize.

Gold-independent. Runs scoring twice (off/on) over a fixed record set with
>=2 repeats and reports phantom-id counts (deterministic) and empty_match
rates (averaged over repeats to separate format-driven recovery from
transient matcher-API variance). Edit RECORDS / REPEATS / the scoring args
(API_*, MODEL) before running.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECORDS = ["WebTestBench_0006", "WebTestBench_0002", "WebTestBench_0001"]
REPEATS = 2
DATASET = REPO / "data" / "WebTestBench" / "_cc_selfhunt_all.jsonl"
API_BASE_URL = "https://api.minimaxi.com/v1/chat/completions"
API_MODEL = "MiniMax-M3"
API_KEY = "XXX"  # export MINIMAX_API_KEY and paste, or read os.environ


def run(version: str, canonicalize: bool) -> None:
    cmd = [
        sys.executable, str(REPO / "eval" / "scoring.py"),
        "--dataset_path", str(DATASET),
        "--output_root", str(REPO / "outputs"),
        "--version", version,
        "--use_checklist_fallback", "True",
        "--api_base_url", API_BASE_URL, "--api_key", API_KEY, "--api_model", API_MODEL,
    ]
    if canonicalize:
        cmd.append("--canonicalize")
    subprocess.run(cmd, check=False)


def read_metrics(version: str) -> dict:
    out = {}
    for rid in RECORDS:
        p = REPO / "outputs" / version / rid / "score.json"
        if p.exists():
            o = json.loads(p.read_text())["overall"]
            out[rid] = {"empty_match": bool(o.get("empty_match")), "num_pred_item": o.get("num_pred_item")}
    return out


if __name__ == "__main__":
    # NOTE: copy result_extracted.md into each version dir first, OR point
    # --version at an existing run's outputs so scoring re-reads the same
    # detection artifacts. Ablation here is parse/match only, not re-detection.
    for r in range(REPEATS):
        run(f"_ablate_canon_off_r{r}", canonicalize=False)
        run(f"_ablate_canon_on_r{r}", canonicalize=True)
    print("OFF:", json.dumps([read_metrics(f"_ablate_canon_off_r{r}") for r in range(REPEATS)], indent=2))
    print("ON :", json.dumps([read_metrics(f"_ablate_canon_on_r{r}") for r in range(REPEATS)], indent=2))
```

- [ ] **Step 2: Run the harness**

Run: `MINIMAX_API_KEY=... python scripts/ablate_canonicalize.py` (after editing `API_KEY` to read `os.environ["MINIMAX_API_KEY"]`, or paste).
Expected: prints OFF vs ON per-record `empty_match`/`num_pred_item` across repeats.

- [ ] **Step 3: Evaluate the gate and record it**

Gate (PASS to keep `--canonicalize`):
- phantom-id rate → 0 under ON (deterministic; verify with `count_phantom_ids` on each `result_extracted.md`).
- `empty_match` rate on 0006 drops ON vs OFF across ≥2 repeats.
- no record that was scored OFF becomes `empty_match`/worse ON (no regression).

Append a dated entry to `tuning-log.md` with the OFF/ON table and the verdict (keep / revert).

- [ ] **Step 4: Commit**

```bash
git add scripts/ablate_canonicalize.py tuning-log.md
git commit -m "test(ablation): Step 1 canonicalize ablation harness + tuning-log entry"
```

---

## STEP 2 — Evidence-forced adversarial judgment

> Do not start until Step 1's gate passes (execution-order gate above).

### Task 5: `find_unsupported_pass` lint (pure function, TDD)

**Files:**
- Create: `eval/agent/evidence_lint.py`
- Test: `tests/test_evidence_lint.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evidence_lint.py
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_evidence_lint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.evidence_lint'`.

- [ ] **Step 3: Write the implementation**

```python
# eval/agent/evidence_lint.py
"""Flag PASS items that lack a non-empty `Evidence:` line.

Structural (code, not prompt) enforcement of the evidence requirement.
flag-only: returns the offending ids; the caller emits an event. No I/O.
"""
import re

_PASS_RE = re.compile(r"^- \[[xX]\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")
_FAIL_RE = re.compile(r"^- \[\s\]\s*(?:\*\*)?([A-Za-z0-9_-]+)(?:\*\*)?:")
_EVID_RE = re.compile(r"^- *evidence:\s*(.*)$", re.IGNORECASE)


def find_unsupported_pass(result_text: str) -> list:
    """Return ids of PASS items with no non-empty Evidence sub-line, in order."""
    lines = result_text.splitlines()
    offenders = []
    current = None          # id of the PASS item whose block we're scanning
    has_evidence = False

    def close():
        nonlocal current, has_evidence
        if current is not None and not has_evidence:
            offenders.append(current)

    for raw in lines:
        s = raw.strip()
        pm = _PASS_RE.match(s)
        fm = _FAIL_RE.match(s)
        if pm:
            close()
            current = pm.group(1)
            has_evidence = False
            continue
        if fm:
            close()
            current = None          # FAIL items are not required to have evidence
            has_evidence = False
            continue
        if current is not None:
            em = _EVID_RE.match(s)
            if em and em.group(1).strip():
                has_evidence = True
    close()
    return offenders
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_evidence_lint.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add eval/agent/evidence_lint.py tests/test_evidence_lint.py
git commit -m "feat(detect): evidence_lint — flag PASS items lacking observed evidence"
```

---

### Task 6: Evidence requirement in the detection prompt (gated)

**Files:**
- Modify: `eval/prompt/defect_detection.py`

- [ ] **Step 1: Add a reusable evidence block and evidence line**

At the bottom of `eval/prompt/defect_detection.py` (module level, after `PROMPT_DEFECT_DETECTION`) add a constant the agent appends only when the flag is on:

```python
EVIDENCE_REQUIREMENT = """

# Evidence Requirement (STRICT)
For EVERY checklist item — PASS or FAIL — append an `- Evidence:` sub-line stating the concrete DOM fact you actually observed and used to judge (e.g. `grid shows "No events found"`, `count 5 -> 5 after submit`, `toast "Reservation Confirmed!" appeared`). You MUST NOT mark an item PASS unless its Evidence line records a state you actually observed that satisfies the Expected. If you did not observe it, mark FAIL and say why in the Bug Report. An item marked PASS without a concrete Evidence line is invalid.

Updated item template (PASS example):
```markdown
- [X] TEST-ID: [original Description]
  - Action: [original Action]
  - Expected: [original Expected]
  - Evidence: [the concrete DOM fact you observed]
```
"""
```

(Keep the existing canonical `- [X]/[ ]` STRICT FORMAT block authoritative; the Evidence line is an additional sub-bullet that the canonical parser already ignores.)

- [ ] **Step 2: No test for prompt text itself; verify import**

Run: `python -c "import sys; sys.path.insert(0,'eval'); from prompt.defect_detection import EVIDENCE_REQUIREMENT; print(len(EVIDENCE_REQUIREMENT))"`
Expected: prints a positive integer.

- [ ] **Step 3: Commit**

```bash
git add eval/prompt/defect_detection.py
git commit -m "feat(prompt): optional EVIDENCE_REQUIREMENT block for defect_detection"
```

---

### Task 7: Thread `--require_evidence` and run the lint

**Files:**
- Modify: `eval/run_agent.py` (arg + threading — mirror `--reverify`)
- Modify: `eval/agent/base_agent.py` (constructor parity)
- Modify: `eval/agent/claude_code.py` (append requirement to prompt; run lint after detection)

- [ ] **Step 1: Add the CLI flag (mirror `--reverify`)**

In `eval/run_agent.py` `parse_args()`, after the `--reverify` argument (~line 56) add:

```python
    parser.add_argument("--require_evidence", action="store_true",
                        help="Require a per-item Evidence line in detection output "
                             "and run evidence_lint (flag-only). Default off.")
```

And in both agent-construction sites where `reverify=args.reverify` is passed (~lines 101, 131) add alongside it:

```python
            require_evidence=args.require_evidence,
```

- [ ] **Step 2: Accept the param in the agent constructors**

In `eval/agent/base_agent.py` `__init__`, add `require_evidence: bool = False` parameter and `self.require_evidence = require_evidence` (match how `reverify` is stored). In `eval/agent/claude_code.py`, ensure its `__init__` forwards `require_evidence` to `super().__init__(...)` exactly as it forwards `reverify`.

- [ ] **Step 3: Append the requirement to the detection prompt**

In `eval/agent/claude_code.py` `defect_detection` (~line 156), after building `prompt = USER_PROMPT["defect_detection"].substitute(...)` add:

```python
        if getattr(self, "require_evidence", False):
            from prompt.defect_detection import EVIDENCE_REQUIREMENT
            prompt = prompt + EVIDENCE_REQUIREMENT
```

- [ ] **Step 4: Run the lint after the result is available and emit an event**

Still in `eval/agent/claude_code.py`, after the detection result file is produced (end of `defect_detection`, before returning success), add:

```python
        if getattr(self, "require_evidence", False):
            from agent.evidence_lint import find_unsupported_pass
            try:
                result_text = self.result_path.read_text(encoding="utf-8")
            except Exception:
                result_text = ""
            offenders = find_unsupported_pass(result_text)
            self._emit_event(
                type_name="evidence_lint",
                stage=stage,
                status=None,
                message=f"{len(offenders)} PASS item(s) lack Evidence: {offenders}",
            )
```

(Confirm the exact `_emit_event` signature in `base_agent.py:263` and match it; `type_name`/`stage`/`status`/`message` mirror existing calls.)

- [ ] **Step 5: Smoke test wiring (no API)**

Run: `python -c "import sys; sys.path.insert(0,'eval'); import run_agent; print('--require_evidence' in run_agent.parse_args.__doc__ if run_agent.parse_args.__doc__ else 'flag added')"` — or simply `python eval/run_agent.py --help` and confirm `--require_evidence` appears.
Expected: the flag is listed in `--help`.

- [ ] **Step 6: Commit**

```bash
git add eval/run_agent.py eval/agent/base_agent.py eval/agent/claude_code.py
git commit -m "feat(detect): --require_evidence threads evidence prompt + evidence_lint event"
```

---

### Task 8: Step 2 ablation (item-level diagnosis + drift-free F1)

**Files:**
- Create: `scripts/ablate_evidence.py`
- Modify: `tuning-log.md` (append)

- [ ] **Step 1: Write the harness**

```python
# scripts/ablate_evidence.py
"""Step 2 ablation: run detection baseline vs --require_evidence on a fixed
record set, then score both and compute time-drift-robust signals.

Primary: item-level diagnosis — count checklist-covered items that flip from
mis-PASS to correct FAIL (computed post-hoc from score_match_ids.json +
score.json, NOT by reading gold during the run). Secondary: F1 on the
drift-free subset. Covariates: tool-call count + per-item coverage, from
session_meta.json. Edit the run command (model/keys/dataset) before running.

This harness orchestrates two full agent runs (baseline + evidence) then two
scoring passes; fill in run_webtester(...) to call eval/run_agent.py with the
same args your scripts/run_webtester_cc.sh uses, adding --require_evidence for
the evidence arm. Keep the generated checklist FIXED across arms (copy
checklist.md from the baseline run into the evidence run dir before detection)
so the only varying factor is judgment, per the confound-control requirement.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Drift-free subset = records with no date-windowed empty state (detect from
# source `new Date()`/current-month filters; 0001 qualifies, 0002/0006 do not).
DRIFT_FREE = ["WebTestBench_0001"]


def covered_mispass_to_fail(base_dir: Path, evid_dir: Path) -> dict:
    """Count items covered by the checklist that were PASS in baseline and FAIL
    in the evidence arm (candidate corrected mis-judgments). Uses the per-record
    result_extracted.md parsed pass/fail; restrict to ids present in both arms."""
    from canonicalize import normalize_to_canonical
    import re
    cb = re.compile(r"^- \[\s*([xX ])\s*\]\s*([A-Za-z0-9_-]+):")

    def pass_map(d: Path) -> dict:
        f = d / "result_extracted.md"
        if not f.exists():
            return {}
        out = {}
        for line in normalize_to_canonical(f.read_text()).splitlines():
            m = cb.match(line.strip())
            if m:
                out[m.group(2)] = (m.group(1).lower() == "x")
        return out

    b, e = pass_map(base_dir), pass_map(evid_dir)
    both = set(b) & set(e)
    flips = [k for k in both if b[k] is True and e[k] is False]
    return {"flips_pass_to_fail": sorted(flips), "n_common": len(both)}


if __name__ == "__main__":
    # 1) run baseline + evidence arms (see module docstring), 2) score both,
    # 3) report flips per record + drift-free-subset F1 + covariates.
    print("Fill in run orchestration, then:")
    print("flips example:", covered_mispass_to_fail(
        REPO / "outputs" / "_evid_base" / "WebTestBench_0002",
        REPO / "outputs" / "_evid_on" / "WebTestBench_0002"))
```

- [ ] **Step 2: Run the harness**

Run: `MINIMAX_API_KEY=... python scripts/ablate_evidence.py` after filling in the run orchestration (two agent runs with a FIXED checklist + two scoring passes).
Expected: prints, per record, the PASS→FAIL flips on common items, plus drift-free-subset (0001) F1 for both arms, plus tool-call/coverage covariates from `session_meta.json`.

- [ ] **Step 3: Evaluate the gate and record it**

Gate (PASS to keep `--require_evidence`):
- on items reached in BOTH arms, evidence arm flips mis-PASS → correct FAIL on the known judgment-miss records (e.g. 0002 browse/search/filter) without introducing new false positives (no correct-PASS → FAIL).
- drift-free-subset (0001) F1 does not regress.
- tool-call budget not exhausted (no truncated coverage vs baseline).

Append a dated entry to `tuning-log.md` with the flip table, covariates, and verdict (keep / revert / escalate lint to option b/c).

- [ ] **Step 4: Commit**

```bash
git add scripts/ablate_evidence.py tuning-log.md
git commit -m "test(ablation): Step 2 evidence ablation harness + tuning-log entry"
```

---

## Self-Review (author checklist, completed)

- **Spec coverage:** Step 1 normalize/drop-phantom/scoring-side-only → Tasks 1–3; Step 1 gold-independent gate (phantom-id + empty_match, ≥2 repeats) → Task 4. Step 2 evidence schema → Task 6; `evidence_lint` flag-only → Task 5; `--require_evidence` threading → Task 7; item-level + drift-free-subset gate with confound covariates → Task 8. Hard checkpoint stated up top. M3 (scoring-side only), M4 (drop-not-remap), M5 (drift-free subset), m7 (Evidence line safe; fallback BUG-strip) all reflected.
- **Placeholder scan:** harness scripts contain explicit "fill in run orchestration" only where they must call the project's existing run command (which carries `XXX` placeholders by repo convention, per CLAUDE.md) — these are documented integration points, not vague requirements; all parsing/metric logic is concrete.
- **Type consistency:** `normalize_to_canonical(text)->str`, `count_phantom_ids(text)->int`, `find_unsupported_pass(text)->list` used consistently across tasks and harnesses. Flag names `--canonicalize` / `--require_evidence` consistent across scoring/run_agent/agents.
