# defect_hunt Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default-on, independent `defect_hunt` pipeline stage that ports the chaos-qa-hunter white-box adversarial methodology into the repo, producing a per-record `BUGS.md` without touching scoring.

**Architecture:** A new browser-agent stage runs last in `stage_sequence` (server still alive). The harness captures the agent's final `# Bug Report` text and writes `BUGS.md` itself (agent has no Write tool). A `hunt_rounds` int flag (default 3, `0` disables) is threaded like the existing `reverify`/`require_evidence` flags. Two `result_extracted.md` early-return gates are relaxed so a default-on hunt still runs on already-scored datasets.

**Tech Stack:** Python 3.11+, `claude-agent-sdk`, Playwright MCP, pytest. Tests live in `tests/` with `conftest.py` putting `eval/` on `sys.path` (import as `from agent...`, `from prompt...`).

**Spec:** `docs/superpowers/specs/2026-06-04-defect-hunt-stage-design.md`

---

### Task 1: `bugs_path` attribute + `_has_required_bugs` validator

**Files:**
- Modify: `eval/agent/base_agent.py:51-55` (path attrs), after `_has_required_result` (`base_agent.py:408-418`)
- Test: `tests/test_has_required_bugs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_has_required_bugs.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_has_required_bugs.py -v`
Expected: FAIL with `AttributeError: type object 'BaseAgent' has no attribute '_has_required_bugs'`

- [ ] **Step 3: Add the `bugs_path` attribute**

In `eval/agent/base_agent.py`, after line 55 (`self.result_reverified_path = ...`), add:
```python
        self.bugs_path = self.output_dir / "BUGS.md"
```

- [ ] **Step 4: Add the validator**

In `eval/agent/base_agent.py`, immediately after the `_has_required_result` method (ends at line ~418), add:
```python
    def _has_required_bugs(self, content: str | None) -> bool:
        """Valid hunt output: a `# Bug Report` heading AND at least one BUG-NNN block."""
        if not content:
            return False
        has_header = False
        has_bug = False
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("#") and s.lstrip("#").strip().startswith("Bug Report"):
                has_header = True
            if s.lstrip("#").strip().upper().startswith("BUG-"):
                has_bug = True
        return has_header and has_bug
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_has_required_bugs.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add eval/agent/base_agent.py tests/test_has_required_bugs.py
git commit -m "feat(agent): bugs_path + _has_required_bugs validator for defect_hunt"
```

---

### Task 2: `PROMPT_DEFECT_HUNT` template + registration

**Files:**
- Create: `eval/prompt/defect_hunt.py`
- Modify: `eval/prompt/__init__.py`
- Test: `tests/test_defect_hunt_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_defect_hunt_prompt.py`:
```python
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


def test_prompt_is_checklist_free_and_emits_bug_report():
    src = PROMPT_DEFECT_HUNT.template
    assert "$checklist" not in src          # hunt is checklist-free by design
    assert "# Bug Report" in src            # distinct header, not "# Test Result"
    assert "# Test Result" not in src
    # iron law: never read gold/answer files
    assert "gold" in src.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_defect_hunt_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'prompt.defect_hunt'`

- [ ] **Step 3: Create the prompt template**

Create `eval/prompt/defect_hunt.py`:
```python
from string import Template


PROMPT_DEFECT_HUNT = Template(
"""# Role
You are chaos-qa-hunter: an adversarial white-box QA engineer. Your sole job is to
break the running web application and find as many real, reproducible bugs as
possible. You are a BREAKER, not a fixer.

# Iron Laws (no exceptions)
- NEVER modify any source file. NEVER fix or suggest fixes (another agent does that).
- NEVER read any "gold", "reference", "answer", or "expected-bugs" file. Judge only
  from the running app's observed behavior and its own source/seed data.
- Tools: use Playwright tools to drive/inspect the page (DOM-only, no screenshots),
  and `Bash`/`Read`/`Grep` to read the application's source and seed/default data.
  You may NOT use Write/Edit (they are disabled).

# White-box Recon (Phase 1) — READ THE SOURCE FIRST
The application source lives at: $project_dir
List and read every source file under that path (use absolute paths; your shell cwd
is NOT the project dir):
```bash
find $project_dir -type f \\( -name "*.js" -o -name "*.ts" -o -name "*.jsx" -o -name "*.tsx" -o -name "*.py" \\) | grep -v node_modules
```
Build a mental inventory: functions, branches (if/else/switch), numeric/length/empty
boundary checks, state machines, input entry points, and the seed/default data and
the assumptions it encodes about the runtime environment. Derive the application's
attack anchors YOURSELF from this source — do not assume any specific bug class.

# Attack-Surface Map (Phase 2) — prioritize entry points
P0 user input + auth/permission paths; P1 state transitions + persistence;
P2 error-handling paths + concurrency; P3 config/env dependencies.

# Systematic Attack (Phase 3) — apply these eight vectors
1. Boundary values (0, -1, MAX_INT, NaN, Infinity; "", whitespace, 10k-char string,
   control chars, emoji, injection strings)
2. State-machine abuse (skip steps, go back, double-submit, concurrent mutex ops)
3. Missing/null/wrong-type required fields
4. Error-path triggers (duplicate keys, missing FKs, deleted resources, over-quota)
5. Concurrency / races
6. Large data (huge files, long lists, deep JSON)
7. Injection (XSS, SQLi, path traversal, template injection)
8. Project hygiene (orphan/dead links, clone-drift between near-identical files,
   cross-component naming/protocol inconsistency, a11y/WCAG quick checks)

# Bounded Multi-Round Loop
Run at most $hunt_rounds rounds. After each round, update the coverage snapshot at
the top of the report. STOP early when EITHER: (a) a full round finds no new
High/Critical bug, OR (b) you are near your turn budget. Round focus: R1 normal flow
+ P0 boundaries; R2 state machine + missing values; R3 concurrency + error paths +
project hygiene.

# Output (your FINAL message — emitted ALL AT ONCE, not appended to any file)
Emit the COMPLETE report as your final message under a top-level `# Bug Report`
header. The harness writes it to BUGS.md; you must NOT write files yourself.

```markdown
# Bug Report
> Target: $server_url  ·  Source: $project_dir

## Coverage Snapshot
| dimension | covered | total | % |
|---|---|---|---|
| functions | X | Y | Z% |
| attack vectors | X | 8 | Z% |

## BUG-001: [one-line description]
- severity: Critical / High / Medium / Low
- type: Crash / Logic / Security / UX / Data / Performance
- repro: [exact steps incl. precise input values — another agent must 100% reproduce]
- expected: [...]
- actual: [observed deviation incl. error text]
- code: `path/to/file:line` — [what that line does]
- vector: boundary / state-machine / concurrency / missing-value / injection / large-data / project-hygiene / other

## BUG-002: ...
```
If you genuinely find zero bugs after exhausting the rounds, still emit `# Bug Report`
with the coverage snapshot and a `## BUG-000: none found` block stating what you tried.

# Input
## User Instruction
$instruction
## Application URL
$server_url
## Source Path
$project_dir

# Output
""")
```

- [ ] **Step 4: Register the prompt**

In `eval/prompt/__init__.py`: add the import after the other `from .` imports:
```python
from .defect_hunt import PROMPT_DEFECT_HUNT
```
Add to the `USER_PROMPT` dict:
```python
    "defect_hunt": PROMPT_DEFECT_HUNT,
```
Add to `__all__`:
```python
    "PROMPT_DEFECT_HUNT",
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_defect_hunt_prompt.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add eval/prompt/defect_hunt.py eval/prompt/__init__.py tests/test_defect_hunt_prompt.py
git commit -m "feat(prompt): PROMPT_DEFECT_HUNT — checklist-free white-box adversarial template"
```

---

### Task 3: Thread the `hunt_rounds` flag

**Files:**
- Modify: `eval/agent/claude_code.py:60` (after `self.max_turns = 150`)
- Modify: `eval/run_agent.py:58` (after `--require_evidence` arg) and the two construction sites (`run_agent.py:97`, `run_agent.py:127`)
- Test: `tests/test_hunt_rounds_flag.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hunt_rounds_flag.py`:
```python
import tempfile
from agent.base_agent import APIConfig
from agent.claude_code import ClaudeCodeWebTester


def _make(**kw):
    with tempfile.TemporaryDirectory() as d:
        return ClaudeCodeWebTester(
            instruction="x",
            api_config=APIConfig(base_url="u", api_key="k", model="m"),
            server_url="http://localhost:6006",
            output_dir=d,
            **kw,
        )


def test_hunt_rounds_defaults_to_3():
    agent = _make()
    assert agent.hunt_rounds == 3


def test_hunt_rounds_override_zero():
    agent = _make(hunt_rounds=0)
    assert agent.hunt_rounds == 0


def test_bugs_path_is_under_output_dir():
    agent = _make()
    assert agent.bugs_path.name == "BUGS.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hunt_rounds_flag.py -v`
Expected: FAIL with `AttributeError: 'ClaudeCodeWebTester' object has no attribute 'hunt_rounds'`

- [ ] **Step 3: Read the flag in `__init__`**

In `eval/agent/claude_code.py`, immediately after `self.max_turns = 150` (line 60), add:
```python
        # defect_hunt stage: number of adversarial rounds (0 disables the stage).
        self.hunt_rounds = int(kwargs.get("hunt_rounds", 3))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hunt_rounds_flag.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Add the CLI argument**

In `eval/run_agent.py`, after the `--require_evidence` argument block (ends ~line 60), add:
```python
    parser.add_argument("--hunt_rounds", type=int, default=3,
                        help="Adversarial defect_hunt rounds producing BUGS.md "
                             "(default 3; 0 disables the stage). Does not affect scoring.")
```

- [ ] **Step 6: Thread it into both construction sites**

In `eval/run_agent.py`, the probe agent (`run_agent.py:97`) — add inside the kwargs, next to `require_evidence=args.require_evidence,`:
```python
            hunt_rounds=args.hunt_rounds,
```
And the real agent (`run_agent.py:127`) — add the same line next to its `require_evidence=args.require_evidence,`:
```python
            hunt_rounds=args.hunt_rounds,
```

- [ ] **Step 7: Commit**

```bash
git add eval/agent/claude_code.py eval/run_agent.py tests/test_hunt_rounds_flag.py
git commit -m "feat(agent): thread hunt_rounds flag (default 3, 0 disables)"
```

---

### Task 4: Relax the two `result_extracted.md` early-return gates

**Files:**
- Modify: `eval/agent/claude_code.py:68-69` (top of `run()`)
- Modify: `eval/run_agent.py:107-108` (probe gate)

> Why: `extract_result_file` runs before `defect_hunt`, so once `result_extracted.md`
> exists both gates short-circuit and a default-on hunt never runs on already-scored
> data. Relax them to "skip only when result exists AND (hunt disabled OR BUGS.md exists)".

- [ ] **Step 1: Relax the `run()` gate**

In `eval/agent/claude_code.py`, replace lines 68-69:
```python
        if self._should_skip_stage(self.result_extracted_path, stage="eval"):
            return True
```
with:
```python
        eval_done = self._should_skip_stage(self.result_extracted_path, stage="eval")
        hunt_pending = self.hunt_rounds > 0 and not self.bugs_path.exists()
        if eval_done and not hunt_pending:
            return True
```

- [ ] **Step 2: Relax the probe gate**

In `eval/run_agent.py`, replace lines 107-108:
```python
        if probe_agent.result_extracted_path.exists():
            return
```
with:
```python
        hunt_pending = args.hunt_rounds > 0 and not probe_agent.bugs_path.exists()
        if probe_agent.result_extracted_path.exists() and not hunt_pending:
            return
```

- [ ] **Step 3: Sanity-check imports/attrs resolve**

Run: `cd eval && python -c "import run_agent; from agent.claude_code import ClaudeCodeWebTester; print('ok')" && cd ..`
Expected: prints `ok` (no NameError/ImportError)

- [ ] **Step 4: Commit**

```bash
git add eval/agent/claude_code.py eval/run_agent.py
git commit -m "fix(pipeline): relax result_extracted gates so default-on hunt runs on resume"
```

---

### Task 5: The `defect_hunt` stage method + wiring

**Files:**
- Modify: `eval/agent/claude_code.py` — add `defect_hunt` to `stage_sequence` (`claude_code.py:74-82`); add the method (after `defect_reverify`); add `check_final_fun` entry (`claude_code.py:432-436`)

- [ ] **Step 1: Register in `stage_sequence`**

In `eval/agent/claude_code.py`, in `run()`'s `stage_sequence` list, add `self.defect_hunt` as the LAST entry (after `self.extract_result_file,`):
```python
            self.extract_result_file,
            self.defect_hunt,
```

- [ ] **Step 2: Add the `check_final_fun` entry**

In `_extract_final_result` (`claude_code.py:432-436`), add to the `check_final_fun` dict:
```python
            "defect_hunt": self._has_required_bugs,
```

- [ ] **Step 3: Add the stage method**

In `eval/agent/claude_code.py`, after the `defect_reverify` method, add (mirrors `defect_detection`; harness writes the file; always returns True = best-effort):
```python
    async def defect_hunt(self) -> bool:
        stage = "defect_hunt"
        target_file = self.bugs_path
        self.current_stage = stage

        if self.hunt_rounds <= 0:
            return True
        if self._should_skip_stage(target_file, stage):
            return True

        self._write_stage_success(stage, True)
        self._mark_stage(stage=stage, status="running", message="🔪 Defect Hunt (chaos-qa) ...")

        project_dir = os.path.abspath(self.local_project_dir) if self.local_project_dir else "."
        prompt = USER_PROMPT["defect_hunt"].substitute(
            instruction=self.instruction,
            server_url=self.server_url,
            project_dir=project_dir,
            hunt_rounds=self.hunt_rounds,
        )
        options = self._get_browser_agent_options(max_turns=self.max_turns)

        result_message = ""
        num_turns = 0
        async for message in query(prompt=prompt, options=options):
            self._log_session_id(message, session_name=stage, stage=stage, prompt=prompt)
            self._handle_message(message, stage=stage)
            if isinstance(message, ResultMessage):
                result_message = message.result
                num_turns = message.num_turns

        if num_turns > self.max_turns:
            self.write_markdown(target_file, "")
        else:
            final_result, from_result_message = self._extract_final_result(result_message, stage=stage)
            self._record_final_result_source(stage, from_result_message)
            self.write_markdown(target_file, final_result)

        if self._verify_output_file(target_file):
            self._emit_file_event(stage, target_file)
            print_green("✅ Defect Hunt Completed.")
        else:
            self._mark_stage(stage=stage, status="error", message=f"Stage {stage} produced no {target_file}.")
        return True
```

- [ ] **Step 4: Confirm dependencies resolve (no new code needed)**

`self.local_project_dir` is already stored by `BaseAgent.__init__` (`base_agent.py:42`) and
`os` is already imported at `claude_code.py:1`, so the method above needs no extra setup.
Verify with: `cd eval && python -c "from agent.claude_code import ClaudeCodeWebTester; print('import ok')" && cd ..`
Expected: prints `import ok` (no ImportError).

- [ ] **Step 5: Run the full unit suite (no regressions)**

Run: `python -m pytest tests/ -v`
Expected: PASS (all existing + new tests green)

- [ ] **Step 6: Commit**

```bash
git add eval/agent/claude_code.py
git commit -m "feat(agent): defect_hunt stage — harness-written BUGS.md, best-effort, last in sequence"
```

---

### Task 6: Live smoke test (dual-track invariant)

**Files:** none (verification only). Requires Claude Code CLI + Playwright MCP logged in, web apps unpacked, and provider env set (see CLAUDE.md).

- [ ] **Step 1: Pick a single record with a known defect**

Use a one-line JSONL for record 0006 (known to have bugs). If `data/WebTestBench/_cc_selfhunt_0002.jsonl` style single-record files exist, create the 0006 equivalent by extracting its line:
```bash
grep '"index": "WebTestBench_0006"' data/WebTestBench/WebTestBench.jsonl > /tmp/hunt_smoke_0006.jsonl
wc -l /tmp/hunt_smoke_0006.jsonl   # expect 1
```

- [ ] **Step 2: Capture the pre-existing scored artifact hash (if any)**

```bash
md5 outputs/_hunt_smoke/WebTestBench_0006/result_extracted.md 2>/dev/null || echo "no prior result"
```

- [ ] **Step 3: Run the agent with `--hunt_rounds 1`**

Run (fill in API_BASE_URL/API_KEY/MODEL per scripts/):
```bash
python eval/run_agent.py --agent claude_code \
  --data_jsonl_path /tmp/hunt_smoke_0006.jsonl \
  --project_root data/WebTestBench/web_applications \
  --output_root ./outputs --log_root ./logs --version _hunt_smoke \
  --base_port 6000 --hunt_rounds 1 \
  --api_base_url <URL> --api_key <KEY> --model <MODEL>
```

- [ ] **Step 4: Verify BUGS.md was produced and is valid**

Run:
```bash
test -s outputs/_hunt_smoke/WebTestBench_0006/BUGS.md && head -5 outputs/_hunt_smoke/WebTestBench_0006/BUGS.md
```
Expected: file is non-empty and starts with `# Bug Report`.

- [ ] **Step 5: Verify dual-track invariant — scoring artifacts unaffected**

Run `python -m pytest`-style check is not applicable; instead confirm the scored file exists and (if a prior hash existed) is unchanged:
```bash
md5 outputs/_hunt_smoke/WebTestBench_0006/result_extracted.md
```
Expected: `result_extracted.md` still present and well-formed; BUGS.md is purely additive. (Scoring never reads BUGS.md — confirm `grep -rn "BUGS.md" eval/scoring.py` returns nothing.)

- [ ] **Step 6: Verify resume behavior — re-run does NOT regenerate, but a missing BUGS.md WOULD**

```bash
rm outputs/_hunt_smoke/WebTestBench_0006/BUGS.md
# re-run the same command from Step 3; confirm defect_hunt executes again
# even though result_extracted.md already exists (gate relaxation from Task 4).
```
Expected: the run does not short-circuit; `BUGS.md` is regenerated.

- [ ] **Step 7: Commit any doc note (optional)**

If you add a usage note to `scripts/run_webtester_cc.sh`:
```bash
git add scripts/run_webtester_cc.sh
git commit -m "docs(scripts): note --hunt_rounds flag and --hunt_rounds 0 escape hatch"
```

---

## Notes for the implementer

- **TDD where unit-testable:** Tasks 1–3 are pure-Python and test-first. Tasks 4–5 edit async pipeline glue that needs a live server + SDK; they are verified by import-sanity checks plus the Task 6 live smoke. Do not fake the SDK in unit tests.
- **Best-effort semantics:** `defect_hunt` always returns `True` so it never flips `run()`'s success flag. An internal error still leaves `stage_success.defect_hunt=False` in `session_meta.json` — that observability trace is intentional.
- **Never enable Write:** the agent must not write files; the harness writes `BUGS.md` from the agent's final `# Bug Report` text. Keep `Write`/`Edit` in `disallowed_tools`.
- **Header disjointness:** hunt emits `# Bug Report`; detection emits `# Test Result`. Never collide them.
