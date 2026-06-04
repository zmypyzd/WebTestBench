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
    """Reuse a cached mutant unless forced. Cache key = injected.json exists (cache mechanism A)."""
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


def parse_injection(md: str) -> tuple[dict, str, str]:
    """Parse the generator output -> (record dict, file path, new file content).
    Raises ValueError if either block is missing/malformed."""
    jm = re.search(r"```json\s*(\{.*?\})\s*```", md, re.DOTALL)
    if not jm:
        raise ValueError("no json injection record found")
    record = json.loads(jm.group(1))
    # Known limitation: a literal ``` inside the file content truncates the
    # non-greedy match. Degrades safely -> truncated file fails to deploy ->
    # the mutant is marked invalid and excluded from the catch-rate denominator
    # (never inflates the rate). Acceptable for the pilot; revisit before scale.
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
    capped so the prompt stays bounded."""
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
