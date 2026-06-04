# Mutation Catch-Rate Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a gold-independent metric (mutation catch-rate) of the WebTester detector's real bug-finding ability: inject a known bug into a passing app, run the real end-to-end detection, and measure whether it catches the bug.

**Architecture:** Three new units reusing the existing pipeline. Two `string.Template` prompts (`mutation_gen`, `mutation_catch`). A library `scripts/mutation_lib.py` holding pure metric logic (majority vote, aggregation, validity classification, cache decision) plus I/O helpers (source-only copy + node_modules symlink, LLM generate, 3-vote judge). An orchestrator `scripts/mutation_probe.py` that selects apps, generates/reuses mutants, runs `ClaudeCodeWebTester` against the mutated copy, judges the catch, and aggregates `summary.json`. Pure logic is unit-tested; the expensive I/O rides on one end-to-end smoke.

**Tech Stack:** Python 3.11+, `claude-agent-sdk` (`query`/`ClaudeAgentOptions`), pytest, existing `eval/agent` + `eval/prompt` packages, Vite/React apps under `data/WebTestBench/web_applications/`.

**Spec:** `docs/superpowers/specs/2026-06-04-mutation-catch-rate-harness-design.md` (eng-reviewed, decisions D1–D5 folded in).

---

## File Structure

| File | Responsibility |
|---|---|
| `eval/prompt/mutation_gen.py` | `PROMPT_MUTATION_GEN` — instructs the model to inject one reachable, fault-class-targeted bug and emit a patch + injection record |
| `eval/prompt/mutation_catch.py` | `PROMPT_MUTATION_CATCH` — instructs the judge to decide if detection's FAIL items contain the injected bug |
| `eval/prompt/__init__.py` | Register both new templates in `USER_PROMPT` + `__all__` |
| `scripts/mutation_lib.py` | Pure metric logic (`majority_caught`, `classify_validity`, `aggregate`, `should_regenerate`) + I/O helpers (`copy_app_sources`, `parse_injection`, `parse_catch`, `generate_mutant`, `judge_catch`) |
| `scripts/mutation_probe.py` | Orchestrator entrypoint: CLI, select→gen/reuse→gate→deploy→detect→3-vote judge→aggregate→`summary.json`/A-B |
| `tests/test_mutation_lib.py` | Unit tests for the pure metric logic (D4) |
| `tests/conftest.py` | Add `scripts/` to `sys.path` so tests can `import mutation_lib` |

**Decisions baked in:** D1 = 3-vote majority judge + audit trail. D2 = generator injects only user-reachable bugs + records `repro_steps`. D3 = per-fault-class quota (≥1 CS/IX cross-state). D4 = unit-test pure logic + 1 smoke. D5 = copy source only + symlink `node_modules`.

---

## Task 1: Catch-judge + generation prompt templates

**Files:**
- Create: `eval/prompt/mutation_gen.py`
- Create: `eval/prompt/mutation_catch.py`
- Modify: `eval/prompt/__init__.py`
- Test: `tests/test_mutation_lib.py` (created here; grows in later tasks)

- [ ] **Step 1: Create the generation prompt**

Create `eval/prompt/mutation_gen.py`:

```python
from string import Template


PROMPT_MUTATION_GEN = Template(
"""You are injecting a SINGLE realistic bug into a working web app, to test whether an automated tester can catch it.

App development instruction:
```
$instruction
```

Relevant source files (path then content):
```
$source
```

Inject exactly ONE bug of fault class `$fault_class`, where the classes mean:
- FT (Functionality): a core feature computes or returns the wrong result.
- CS (Constraint): a validation / limit / rule is wrong or missing (e.g. accepts an out-of-range value, drops a required check). Prefer a CROSS-STATE constraint (the bug only shows when you compare state before/after an action: a total, a count, a persisted value).
- IX (Interaction): an interaction sequence misbehaves (e.g. re-submit, back-then-forward, refresh loses state, ordering).
- CT (Content): wrong/empty text shown to the user.

HARD REQUIREMENTS:
1. The bug MUST be observable through a NORMAL user interaction in the running UI. Do NOT inject dead code, unreachable branches, or bugs that need devtools to see.
2. The bug MUST NOT crash the app or break the dev server / build.
3. Change as little as possible — ideally one expression or one line.

Output EXACTLY two fenced blocks and nothing else:

First, a JSON injection record:
```json
{
  "description": "one sentence: what is now wrong and where the user sees it",
  "file": "relative/path/from/app/root.tsx",
  "fault_class": "$fault_class",
  "repro_steps": "numbered steps a tester follows in the UI to observe the bug"
}
```

Then, the full new content of the changed file:
```file:relative/path/from/app/root.tsx
<entire file content after your edit>
```
""")
```

- [ ] **Step 2: Create the catch-judge prompt**

Create `eval/prompt/mutation_catch.py`:

```python
from string import Template


PROMPT_MUTATION_CATCH = Template(
"""An automated web tester produced a test result for an app. We secretly injected ONE known bug into that app. Decide whether the tester CAUGHT our injected bug.

The injected bug:
```
$injected
```

The tester's reported result (only FAIL / unchecked items indicate a bug it claims to have found):
```
$result
```

Rules:
1. CAUGHT means a FAIL item in the result describes the SAME malfunction as the injected bug — same feature, same wrong behavior. An item failing for an UNRELATED reason does NOT count.
2. An off-checklist finding (e.g. an item id like `EX-01`) counts exactly like any other FAIL item.
3. Match on intent, not wording. If unsure, answer caught=false.

Output EXACTLY one JSON object and nothing else:
```json
{"caught": true or false, "matched_item": "the FAIL item id/text that matches, or null", "reason": "one sentence"}
```
""")
```

- [ ] **Step 3: Register both in the prompt registry**

Modify `eval/prompt/__init__.py` — add the two imports, two `USER_PROMPT` entries, and two `__all__` entries:

```python
from .mutation_gen import PROMPT_MUTATION_GEN
from .mutation_catch import PROMPT_MUTATION_CATCH
```

In `USER_PROMPT` add:
```python
    "mutation_gen": PROMPT_MUTATION_GEN,
    "mutation_catch": PROMPT_MUTATION_CATCH,
```

In `__all__` add:
```python
    "PROMPT_MUTATION_GEN",
    "PROMPT_MUTATION_CATCH",
```

- [ ] **Step 4: Add the conftest path + a registration test**

Modify `tests/conftest.py` — append below the existing `EVAL_DIR` block:

```python
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
```

Create `tests/test_mutation_lib.py`:

```python
from prompt import USER_PROMPT


def test_mutation_prompts_registered_and_substitutable():
    gen = USER_PROMPT["mutation_gen"].substitute(
        instruction="x", source="y", fault_class="CS"
    )
    assert "fault class `CS`" in gen
    catch = USER_PROMPT["mutation_catch"].substitute(injected="a", result="b")
    assert "CAUGHT" in catch
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench && python -m pytest tests/test_mutation_lib.py -v`
Expected: PASS (1 test).

- [ ] **Step 6: Commit**

```bash
git add eval/prompt/mutation_gen.py eval/prompt/mutation_catch.py eval/prompt/__init__.py tests/conftest.py tests/test_mutation_lib.py
git commit -m "feat(mutation): add generation + catch-judge prompt templates"
```

---

## Task 2: Pure metric logic (majority, validity, aggregate, cache)

This is the D4 validity-critical core — a bug here silently corrupts the metric. TDD each function.

**Files:**
- Create: `scripts/mutation_lib.py`
- Test: `tests/test_mutation_lib.py`

- [ ] **Step 1: Write failing tests for the four pure functions**

Append to `tests/test_mutation_lib.py`:

```python
import mutation_lib as ml


def test_majority_caught():
    assert ml.majority_caught([True, True, False]) is True
    assert ml.majority_caught([True, False, False]) is False
    assert ml.majority_caught([True]) is True
    assert ml.majority_caught([]) is False


def test_classify_validity():
    assert ml.classify_validity(deploy_ok=False, reachable=True) == "invalid"
    assert ml.classify_validity(deploy_ok=True, reachable=False) == "suspect"
    assert ml.classify_validity(deploy_ok=True, reachable=True) == "valid"


def test_should_regenerate(tmp_path):
    mdir = tmp_path / "m0"
    mdir.mkdir()
    assert ml.should_regenerate(mdir, regen=False) is True   # no injected.json yet
    (mdir / "injected.json").write_text("{}")
    assert ml.should_regenerate(mdir, regen=False) is False  # cached -> reuse
    assert ml.should_regenerate(mdir, regen=True) is True     # forced


def test_aggregate_denominator_excludes_invalid_and_by_class():
    records = [
        {"fault_class": "CS", "validity": "valid",   "caught": True},
        {"fault_class": "CS", "validity": "valid",   "caught": False},
        {"fault_class": "IX", "validity": "valid",   "caught": True},
        {"fault_class": "FT", "validity": "invalid", "caught": False},  # excluded
        {"fault_class": "FT", "validity": "suspect", "caught": True},   # excluded
    ]
    agg = ml.aggregate(records)
    assert agg["valid"] == 3 and agg["invalid"] == 1 and agg["suspect"] == 1
    assert agg["catch_rate"] == round(2 / 3, 3)          # 2 caught of 3 valid
    assert agg["by_class"]["CS"]["catch_rate"] == 0.5    # 1 of 2
    assert agg["by_class"]["IX"]["catch_rate"] == 1.0    # 1 of 1
    assert "FT" not in agg["by_class"]                   # no valid FT mutants
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_mutation_lib.py -v`
Expected: FAIL with "No module named 'mutation_lib'".

- [ ] **Step 3: Implement the pure functions**

Create `scripts/mutation_lib.py` (pure-logic section first):

```python
"""Mutation catch-rate harness library: pure metric logic + I/O helpers.

Pure functions (unit-tested) decide whether the numbers are right. I/O helpers
(generate_mutant / judge_catch / copy_app_sources) are covered by the smoke run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "eval") not in sys.path:
    sys.path.insert(0, str(ROOT / "eval"))

CLASSES = ["functionality", "constraint", "interaction", "content"]


def majority_caught(votes: list[bool]) -> bool:
    """True iff a strict majority of votes are True (D1 3-vote judge)."""
    if not votes:
        return False
    return sum(1 for v in votes if v) > len(votes) / 2


def classify_validity(deploy_ok: bool, reachable: bool) -> str:
    """invalid if it won't deploy; suspect if not observably reachable; else valid."""
    if not deploy_ok:
        return "invalid"
    if not reachable:
        return "suspect"
    return "valid"


def should_regenerate(mutant_dir: Path, regen: bool) -> bool:
    """Reuse a cached mutant unless forced. Cache key = injected.json exists (D1/cache mechanism A)."""
    if regen:
        return True
    return not (Path(mutant_dir) / "injected.json").exists()


def aggregate(records: list[dict]) -> dict:
    """Catch-rate over VALID mutants only; per-class breakdown; validity counts.

    record = {fault_class, validity in {valid,invalid,suspect}, caught: bool, ...}
    """
    valid = [r for r in records if r["validity"] == "valid"]
    n_valid = len(valid)
    n_caught = sum(1 for r in valid if r["caught"])

    by_class: dict[str, dict] = {}
    for r in valid:
        c = r["fault_class"]
        b = by_class.setdefault(c, {"n": 0, "caught": 0})
        b["n"] += 1
        b["caught"] += 1 if r["caught"] else 0
    for c, b in by_class.items():
        b["catch_rate"] = round(b["caught"] / b["n"], 3) if b["n"] else None

    return {
        "total": len(records),
        "valid": n_valid,
        "invalid": sum(1 for r in records if r["validity"] == "invalid"),
        "suspect": sum(1 for r in records if r["validity"] == "suspect"),
        "caught": n_caught,
        "catch_rate": round(n_caught / n_valid, 3) if n_valid else None,
        "by_class": by_class,
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_mutation_lib.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mutation_lib.py tests/test_mutation_lib.py
git commit -m "feat(mutation): pure metric logic (majority, validity, aggregate, cache)"
```

---

## Task 3: Source-only copy with node_modules symlink (D5)

**Files:**
- Modify: `scripts/mutation_lib.py`
- Test: `tests/test_mutation_lib.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mutation_lib.py`:

```python
def test_copy_app_sources_excludes_node_modules_and_symlinks(tmp_path):
    src = tmp_path / "app"
    (src / "src").mkdir(parents=True)
    (src / "src" / "App.tsx").write_text("export default 1")
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / "node_modules" / "dep" / "index.js").write_text("// big dep")

    dst = tmp_path / "copy"
    ml.copy_app_sources(src, dst)

    assert (dst / "src" / "App.tsx").read_text() == "export default 1"
    nm = dst / "node_modules"
    assert nm.is_symlink()                       # not a real copy
    assert (nm / "dep" / "index.js").exists()     # resolves to the original
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_mutation_lib.py::test_copy_app_sources_excludes_node_modules_and_symlinks -v`
Expected: FAIL with "module 'mutation_lib' has no attribute 'copy_app_sources'".

- [ ] **Step 3: Implement copy_app_sources**

Append to `scripts/mutation_lib.py`:

```python
def copy_app_sources(src: Path, dst: Path) -> None:
    """Copy app source to dst EXCLUDING node_modules, then symlink node_modules
    back to the original (read-only, shared). Mutations only touch source, so
    dependencies are safely shared; per-mutant copy cost drops to near-zero (D5)."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules", ".git"))
    src_nm = src / "node_modules"
    if src_nm.exists():
        os.symlink(src_nm.resolve(), dst / "node_modules")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_mutation_lib.py::test_copy_app_sources_excludes_node_modules_and_symlinks -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/mutation_lib.py tests/test_mutation_lib.py
git commit -m "feat(mutation): source-only app copy with node_modules symlink"
```

---

## Task 4: Output parsers (injection record + catch verdict)

**Files:**
- Modify: `scripts/mutation_lib.py`
- Test: `tests/test_mutation_lib.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mutation_lib.py`:

```python
def test_parse_injection_extracts_record_and_file():
    md = '''Here you go:
```json
{"description": "total wrong", "file": "src/Cart.tsx", "fault_class": "CS", "repro_steps": "1. add item"}
```
```file:src/Cart.tsx
export const x = 2
```
'''
    rec, path, content = ml.parse_injection(md)
    assert rec["fault_class"] == "CS"
    assert rec["file"] == "src/Cart.tsx"
    assert path == "src/Cart.tsx"
    assert content.strip() == "export const x = 2"


def test_parse_catch_reads_verdict():
    md = 'verdict:\n```json\n{"caught": true, "matched_item": "EX-01", "reason": "same bug"}\n```'
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "EX-01"


def test_parse_catch_defaults_false_on_garbage():
    assert ml.parse_catch("no json here").get("caught") is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_mutation_lib.py -k "parse_" -v`
Expected: FAIL (no `parse_injection` / `parse_catch`).

- [ ] **Step 3: Implement the parsers**

Append to `scripts/mutation_lib.py`:

```python
def parse_injection(md: str) -> tuple[dict, str, str]:
    """Parse the generator output -> (record dict, file path, new file content).
    Raises ValueError if either block is missing/malformed."""
    jm = re.search(r"```json\s*(\{.*?\})\s*```", md, re.DOTALL)
    if not jm:
        raise ValueError("no json injection record found")
    record = json.loads(jm.group(1))
    fm = re.search(r"```file:([^\n]+)\n(.*?)```", md, re.DOTALL)
    if not fm:
        raise ValueError("no file block found")
    path = fm.group(1).strip()
    content = fm.group(2)
    return record, path, content


def parse_catch(md: str) -> dict:
    """Parse a judge verdict. Defaults to caught=false on any parse failure
    (conservative: an unparseable verdict must not count as a catch)."""
    m = re.search(r"\{[^{}]*\"caught\"[^{}]*\}", md, re.DOTALL)
    if not m:
        return {"caught": False, "matched_item": None, "reason": "unparseable verdict"}
    try:
        v = json.loads(m.group(0))
        v["caught"] = bool(v.get("caught", False))
        return v
    except Exception:
        return {"caught": False, "matched_item": None, "reason": "json error"}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_mutation_lib.py -k "parse_" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mutation_lib.py tests/test_mutation_lib.py
git commit -m "feat(mutation): injection + catch-verdict parsers (fail-safe to not-caught)"
```

---

## Task 5: I/O helpers (run_query, generate_mutant, judge_catch)

These wrap LLM calls; they are exercised by the smoke run, not unit tests (D4).

**Files:**
- Modify: `scripts/mutation_lib.py`

- [ ] **Step 1: Add the async I/O helpers**

Append to `scripts/mutation_lib.py`:

```python
import asyncio  # noqa: E402  (kept with the async helpers)

from claude_agent_sdk import (  # noqa: E402
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
)
from prompt import USER_PROMPT  # noqa: E402

CWD = str(ROOT / "claude_code_cwd")


async def run_query(prompt: str, model: str, max_turns: int = 5) -> str:
    """Chat-only Claude Code call via local CLI creds (mirrors coverage_probe.py)."""
    opts = ClaudeAgentOptions(
        allowed_tools=[], model=model, max_turns=max_turns,
        max_buffer_size=1024 * 1024, cwd=CWD, env={},
    )
    text, result = "", ""
    async for message in query(prompt=prompt, options=opts):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(message, ResultMessage):
            result = message.result or ""
    return result if result.strip() else text


def _gather_source(app_dir: Path, max_files: int = 12, max_bytes: int = 60_000) -> str:
    """Concatenate the app's .tsx/.ts source (excluding node_modules) for the generator,
    capped so the prompt stays bounded. Logs nothing dropped silently is the caller's job."""
    parts, total = [], 0
    files = sorted(
        p for p in (app_dir / "src").rglob("*")
        if p.suffix in (".tsx", ".ts", ".jsx", ".js") and "node_modules" not in p.parts
    )
    for p in files[:max_files]:
        body = p.read_text(encoding="utf-8", errors="ignore")
        if total + len(body) > max_bytes:
            break
        rel = p.relative_to(app_dir)
        parts.append(f"// {rel}\n{body}")
        total += len(body)
    return "\n\n".join(parts)


async def generate_mutant(app_dir: Path, instruction: str, fault_class: str, model: str) -> tuple[dict, str, str]:
    """Ask the model to inject one bug. Returns (record, rel_path, new_content)."""
    prompt = USER_PROMPT["mutation_gen"].substitute(
        instruction=instruction, source=_gather_source(app_dir), fault_class=fault_class,
    )
    out = await run_query(prompt, model)
    return parse_injection(out)


async def judge_catch(injected: dict, result_md: str, model: str, votes: int = 3) -> dict:
    """Run the catch-judge `votes` times and take the majority (D1). Returns
    {caught, votes:[...]} with every vote's verdict for the audit trail."""
    prompt = USER_PROMPT["mutation_catch"].substitute(
        injected=json.dumps(injected, ensure_ascii=False), result=result_md,
    )
    ballots = await asyncio.gather(*(run_query(prompt, model) for _ in range(votes)))
    parsed = [parse_catch(b) for b in ballots]
    return {"caught": majority_caught([p["caught"] for p in parsed]), "votes": parsed}
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench && python -c "import sys; sys.path.insert(0,'scripts'); import mutation_lib; print('ok')"`
Expected: `ok` (no import error).

- [ ] **Step 3: Re-run the unit tests (still green)**

Run: `python -m pytest tests/test_mutation_lib.py -v`
Expected: PASS (all prior tests still pass; new I/O helpers untested by design).

- [ ] **Step 4: Commit**

```bash
git add scripts/mutation_lib.py
git commit -m "feat(mutation): LLM I/O helpers (generate_mutant, 3-vote judge_catch)"
```

---

## Task 6: Orchestrator (mutation_probe.py)

**Files:**
- Create: `scripts/mutation_probe.py`

- [ ] **Step 1: Write the orchestrator**

Create `scripts/mutation_probe.py`:

```python
"""Mutation catch-rate harness (route B — gold-independent metric).

Pipeline per app x mutant:
  select -> (reuse|generate) mutant -> copy+patch -> deploy+detect -> 3-vote judge -> aggregate

Run (pilot): python scripts/mutation_probe.py --apps WebTestBench_0037,... \
    --mutants-per-app 2 --model sonnet \
    --api_base_url <url> --api_key <key> --detect_model <model>
"""
import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_lib as ml  # noqa: E402
from agent import APIConfig  # noqa: E402
from agent.claude_code import ClaudeCodeWebTester  # noqa: E402

DATASET = ROOT / "data/WebTestBench/WebTestBench.jsonl"
APPS_DIR = ROOT / "data/WebTestBench/web_applications"
OUT_DIR = ROOT / "outputs/_mutation_probe"
# Fault classes to spread across mutants; index k picks the k-th (D3 quota: ensures CS/IX coverage).
QUOTA = ["constraint", "interaction", "functionality", "content"]
CLASS_SHORT = {"constraint": "CS", "interaction": "IX", "functionality": "FT", "content": "CT"}


def load_records(app_ids: list[str]) -> dict[str, dict]:
    recs = {}
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("index") in app_ids:
                recs[r["index"]] = r
    return recs


async def run_one_mutant(app_id: str, k: int, record: dict, args, api_config: APIConfig,
                         port: int) -> dict | None:
    """Generate-or-reuse one mutant, deploy+detect, judge. Returns a result record,
    or None on a hard failure (logged, never aborts the batch -- per 1729e25 lesson)."""
    fault_long = QUOTA[k % len(QUOTA)]
    fault_class = CLASS_SHORT[fault_long]
    mdir = OUT_DIR / app_id / f"m{k}"
    mdir.mkdir(parents=True, exist_ok=True)
    app_copy = mdir / "app"
    src_app = APPS_DIR / app_id
    instruction = record.get("instruction", "")
    base = {"app": app_id, "k": k, "fault_class": fault_class, "category": record.get("category")}

    try:
        # 1) reuse or generate mutant
        if ml.should_regenerate(mdir, args.regen_mutants):
            rec, rel, content = await ml.generate_mutant(src_app, instruction, fault_class, args.model)
            (mdir / "injected.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
            (mdir / "patch_meta.json").write_text(json.dumps({"file": rel}, ensure_ascii=False))
            (mdir / "new_file.txt").write_text(content)
        else:
            rec = json.loads((mdir / "injected.json").read_text())
            rel = json.loads((mdir / "patch_meta.json").read_text())["file"]
            content = (mdir / "new_file.txt").read_text()

        # 2) copy sources + apply patch
        ml.copy_app_sources(src_app, app_copy)
        target = app_copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        # 3) deploy + full detection (reuses ClaudeCodeWebTester as-is)
        server_url = f"http://localhost:{port}/"
        agent = ClaudeCodeWebTester(
            instruction=instruction, api_config=api_config,
            server_url=server_url, local_project_dir=app_copy, output_dir=mdir / "run",
        )
        deploy_ok = True
        try:
            await agent.run()
        except Exception as exc:
            print(f"[{app_id} m{k}] detection/deploy error: {exc}")
            deploy_ok = agent.result_extracted_path.exists()

        result_md = ""
        if agent.result_extracted_path.exists():
            result_md = agent.result_extracted_path.read_text(encoding="utf-8")

        # 4) validity: deployable? reachable? (lightweight: patched a real src file)
        reachable = (src_app / rel).exists() and rel.startswith("src")
        validity = ml.classify_validity(deploy_ok=deploy_ok and bool(result_md), reachable=reachable)

        # 5) 3-vote judge (only meaningful when we have a result)
        verdict = {"caught": False, "votes": []}
        if result_md:
            verdict = await ml.judge_catch(rec, result_md, args.model)

        out = {**base, "validity": validity, "caught": verdict["caught"],
               "votes": verdict["votes"], "repro_steps": rec.get("repro_steps", "")}
        (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"[{app_id} m{k}] {fault_class} validity={validity} caught={verdict['caught']}")
        return out
    except Exception as exc:
        print(f"[{app_id} m{k}] HARD FAIL (skipped): {exc}")
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", required=True, help="comma-separated app ids (WebTestBench_NNNN)")
    ap.add_argument("--mutants-per-app", type=int, default=2)
    ap.add_argument("--regen-mutants", action="store_true", help="force regenerate cached mutants")
    ap.add_argument("--model", default="sonnet", help="model for generation + judge")
    ap.add_argument("--api_base_url", required=True)
    ap.add_argument("--api_key", required=True)
    ap.add_argument("--detect_model", required=True, help="model the detection agent runs")
    ap.add_argument("--base_port", type=int, default=7000)
    ap.add_argument("--out", default="summary.json")
    ap.add_argument("--baseline", default=None, help="baseline summary.json to A/B against")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app_ids = [a.strip() for a in args.apps.split(",") if a.strip()]
    records = load_records(app_ids)
    api_config = APIConfig(base_url=args.api_base_url, api_key=args.api_key, model=args.detect_model)

    # Unique port per (app, k) so concurrent dev servers never collide.
    tasks, port = [], args.base_port
    for app_id in app_ids:
        if app_id not in records:
            print(f"[warn] {app_id} not in dataset; skipping")
            continue
        for k in range(args.mutants_per_app):
            tasks.append(run_one_mutant(app_id, k, records[app_id], args, api_config, port))
            port += 1

    results = [r for r in await asyncio.gather(*tasks) if r is not None]
    agg = ml.aggregate(results)
    agg["records"] = results
    (OUT_DIR / args.out).write_text(json.dumps(agg, ensure_ascii=False, indent=2))

    print("\n==================== MUTATION CATCH-RATE ====================")
    print(f"valid={agg['valid']} invalid={agg['invalid']} suspect={agg['suspect']}  "
          f"caught={agg['caught']}  catch_rate={agg['catch_rate']}")
    for c, b in sorted(agg["by_class"].items()):
        print(f"  {c}: {b['caught']}/{b['n']} = {b['catch_rate']}")

    if args.baseline:
        bpath = OUT_DIR / args.baseline
        if bpath.exists():
            base = json.loads(bpath.read_text())
            d = (agg["catch_rate"] or 0) - (base.get("catch_rate") or 0)
            print(f"\nA/B vs {args.baseline}: catch_rate {base.get('catch_rate')} -> "
                  f"{agg['catch_rate']} ({d:+.3f})")
        else:
            print(f"[warn] baseline {bpath} not found")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify it parses and shows help**

Run: `cd /Users/zmy/intership/5-25/webtest_orginal/WebTestBench && python scripts/mutation_probe.py --help`
Expected: argparse help text listing `--apps`, `--mutants-per-app`, `--regen-mutants`, `--detect_model`, etc. (no import/syntax error).

- [ ] **Step 3: Commit**

```bash
git add scripts/mutation_probe.py
git commit -m "feat(mutation): orchestrator — select/gen/patch/detect/3-vote judge/aggregate"
```

---

## Task 7: End-to-end smoke (1 app × 1 mutant)

This is the single expensive-path test (D4). It needs API creds + a deployable app; run it once manually.

**Files:** none (manual verification).

- [ ] **Step 1: Pick a drift-free app from the trusted set**

Run: `head -1 data/WebTestBench/_eval_trusted.jsonl | python3 -c "import sys,json;print(json.load(sys.stdin)['index'])"`
Note the printed app id (call it `<APP>`). It is drift-free (the trusted set excludes 0002/0006).

- [ ] **Step 2: Run the harness for 1 app × 1 mutant**

Fill `XXX` from `scripts/run_webtester_cc.sh` (the same `API_BASE_URL` / `API_KEY` / `MODEL` used elsewhere):

```bash
python scripts/mutation_probe.py \
  --apps <APP> --mutants-per-app 1 --model sonnet \
  --api_base_url XXX --api_key XXX --detect_model XXX \
  --out summary_smoke.json
```

Expected console: a `[<APP> m0] CS validity=... caught=...` line, then a `MUTATION CATCH-RATE` block.

- [ ] **Step 3: Verify the artifacts are complete**

Run:
```bash
python3 -c "import json;d=json.load(open('outputs/_mutation_probe/summary_smoke.json'));print('keys',sorted(d));print('rec0',d['records'][0] if d['records'] else 'NONE')"
ls outputs/_mutation_probe/<APP>/m0/
```
Expected: `summary_smoke.json` has keys `by_class, catch_rate, caught, invalid, records, suspect, total, valid`; `records[0]` has `validity`, `caught`, `votes` (3 entries), `repro_steps`; the mutant dir contains `injected.json`, `new_file.txt`, `result.json`, and `run/result_extracted.md`.

- [ ] **Step 4: Sanity-check the judge verdict by hand**

Read `outputs/_mutation_probe/<APP>/m0/injected.json` (what we injected) and `run/result_extracted.md` (what detection reported). Confirm the `caught` verdict in `result.json` is reasonable given the two. If the judge is obviously wrong, note it — this is the calibration signal for whether 3-vote is enough.

- [ ] **Step 5: Commit the smoke artifact pointer (not the gitignored outputs)**

```bash
# outputs/ is gitignored; record the smoke result in the spec's status instead.
git commit --allow-empty -m "test(mutation): smoke run 1 app x 1 mutant verified (artifacts under outputs/, gitignored)"
```

---

## Self-Review

- **Spec coverage:** pipeline steps 1–6 → Tasks 6 (orchestrator) + 2–5 (helpers); D1 3-vote → `judge_catch` (T5) + `majority_caught` (T2); D2 reachable + repro → `PROMPT_MUTATION_GEN` (T1) + `reachable` check (T6); D3 quota → `QUOTA`/`CLASS_SHORT` (T6) + prompt (T1); D4 unit tests → T2–T4, smoke → T7; D5 copy+symlink → `copy_app_sources` (T3); cache mechanism A → `should_regenerate` (T2) + reuse branch (T6); validity gate → `classify_validity` (T2) + T6; per-mutant fault tolerance → `run_one_mutant` try/except returning None (T6); A/B → `--baseline` (T6).
- **Type consistency:** record dict shape `{app,k,fault_class,category,validity,caught,votes,repro_steps}` is produced in T6 and consumed by `aggregate` (T2) which reads `fault_class`, `validity`, `caught` — consistent. `judge_catch` returns `{caught, votes}`; `run_one_mutant` reads both — consistent. `parse_injection` returns `(record, path, content)`; callers in T6 unpack 3 — consistent.
- **Placeholder scan:** no TBD/TODO; every code step shows full code; commands have expected output.

---

## Notes for the implementer

- **Port range:** `--base_port 7000` is used so mutant dev servers don't collide with the normal pipeline's `base_port 6000` space. Each `(app, k)` gets a distinct port.
- **`detect_model` vs `model`:** `--model` drives the cheap chat-only generation + judge; `--detect_model` is what the heavyweight detection agent runs (keep it the same model the real pipeline uses, so the catch-rate reflects the real detector).
- **Playwright MCP** must be installed/pinned to `0.0.61` (see CLAUDE.md) for the detection stage inside the smoke run.
- **Cost:** the smoke is one real detection run (browser, up to 150 turns) plus a few cheap chat calls. Budget accordingly before scaling past the pilot.
```
