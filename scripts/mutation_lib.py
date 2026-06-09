"""Mutation catch-rate harness library: pure metric logic + I/O helpers.

Pure functions (unit-tested) decide whether the numbers are right. I/O helpers
(generate_mutant / judge_catch / copy_app_sources) are covered by the smoke run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

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
    """Reuse a cached mutant only if ALL of its files are present; otherwise
    regenerate. A partial write (crash mid-generation) -> regenerate, not a
    confusing reuse-then-FileNotFoundError (cache mechanism A)."""
    if regen:
        return True
    d = Path(mutant_dir)
    return not all((d / f).exists() for f in ("injected.json", "patch_meta.json", "new_file.txt"))


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
    """Copy app source to dst EXCLUDING node_modules, then clone node_modules into
    the copy as a REAL, write-isolated directory (D2).

    Previously node_modules was symlinked back to the shared source. That re-uses
    the original tree, so base_agent's unconditional `npm install` in the mutant
    copy wrote THROUGH the symlink and corrupted the shared dependencies across
    concurrent mutants. We now make an APFS clonefile copy (`cp -c -R`,
    copy-on-write: near-zero cost AND write-isolated), falling back to a real
    recursive copy on non-APFS/unsupported targets. We NEVER fall back to a
    symlink (that would re-introduce the write-through bug)."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules", ".git"))

    src_nm = src / "node_modules"
    dst_nm = dst / "node_modules"
    if not src_nm.exists():
        # Apps without node_modules: skip. base_agent's unconditional `npm install`
        # will create it. (A `cp` of a missing source returns exit 1, which must
        # NOT be misread as "clonefile unsupported", so we guard it out here.)
        print(f"[copy_app_sources] no node_modules in {src}; skipping (npm install will create it)")
        return

    # The clone MUST run AFTER rmtree+copytree so dst_nm does NOT pre-exist: BSD
    # `cp -R <src> <dst>` nests as dst/node_modules/node_modules when dst already
    # exists. copytree(ignore=node_modules) above guarantees dst_nm is absent.
    try:
        proc = subprocess.run(
            ["cp", "-c", "-R", str(src_nm), str(dst_nm)],
            capture_output=True, text=True,
        )
        cp_failed = proc.returncode != 0
        cp_err = proc.stderr
        cp_exc = False
    except FileNotFoundError:
        # `cp` binary absent (non-POSIX host) — only an exception path.
        cp_failed = True
        cp_err = "cp binary not found"
        cp_exc = True

    if not cp_failed:
        print(f"[copy_app_sources] node_modules cloned via APFS clonefile (cp -c) -> {dst_nm}")
        return

    # BSD `cp -c` on a non-APFS / unsupported target returns NON-ZERO (not a Python
    # exception); FileNotFoundError is the only exception path. Either way, fall
    # back to a REAL recursive copy. Clean up any partial dst_nm first.
    print(f"[copy_app_sources] clonefile (cp -c) unsupported ({'exception' if cp_exc else 'rc!=0'}): "
          f"{cp_err.strip()!r}; falling back to a real recursive copy")
    if dst_nm.exists() or dst_nm.is_symlink():
        shutil.rmtree(dst_nm, ignore_errors=True)
    try:
        # symlinks=True: node_modules holds ~25 relative self-contained symlinks
        # (.bin/*, scoped pkgs). Preserve them rather than dereference — the default
        # symlinks=False can fail on broken links or balloon the copy size.
        shutil.copytree(src_nm, dst_nm, symlinks=True)
    except Exception as exc:
        # Even the real-copy fallback failed. RAISE — never leave dst without a real
        # node_modules and silently proceed to npm install (which would write
        # through to the shared tree or fail mid-deploy).
        raise RuntimeError(
            f"copy_app_sources: failed to clone AND real-copy node_modules "
            f"from {src_nm} to {dst_nm}: {exc}"
        ) from exc
    print(f"[copy_app_sources] node_modules real-recursive-copied -> {dst_nm}")


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
    (conservative: an unparseable verdict must not count as a catch).

    Tries a fenced ```json block FIRST, then falls back to the bare brace regex.
    The brace regex `{[^{}]*"caught"[^{}]*}` forbids nested braces and would drop a
    genuine catch when a verdict's reason contains braces (e.g. 'fails when {count}
    shown') — MiniMax-M3 (D1 HTTP path) reasoning verdicts are MORE likely to emit
    braces in prose. Preferring the fenced block avoids that downward bias while
    staying conservative for truly garbage input. Handles '' / None without
    raising (judge_catch_http feeds '' on total HTTP failure)."""
    if not md:
        return {"caught": False, "matched_item": None, "reason": "empty verdict"}

    # 1) Prefer a fenced ```json ... ``` block — tolerates braces inside reason prose.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", md, re.DOTALL)
    if fenced:
        try:
            v = json.loads(fenced.group(1))
            v["caught"] = bool(v.get("caught", False))
            return v
        except Exception:
            pass  # fall through to the brace regex

    # 2) Fall back to the bare brace match (no-nested-brace, conservative).
    m = re.search(r"\{[^{}]*\"caught\"[^{}]*\}", md, re.DOTALL)
    if not m:
        return {"caught": False, "matched_item": None, "reason": "unparseable verdict"}
    try:
        v = json.loads(m.group(0))
        v["caught"] = bool(v.get("caught", False))
        return v
    except Exception:
        return {"caught": False, "matched_item": None, "reason": "json error"}


def use_http_judge(judge_cfg) -> bool:
    """Route to the independent HTTP judge (D1) IFF a judge base_url is present.

    `judge_cfg` is an APIConfig-shaped object (.base_url/.api_key/.model, parallel
    to scoring._call_api) or a dict, or None. We key off base_url presence ONLY —
    never off model, because --judge_model defaults to a non-None 'MiniMax-M3', so
    keying off model would force a broken URL-less HTTP call by DEFAULT and break
    the behavior-preserving default (CLI judge_catch). Handles None with no
    AttributeError; returns False when no base_url so run_one_mutant falls back to
    the existing CLI judge_catch byte-for-byte.

    This pure helper sits ABOVE the heavy `import asyncio`/claude_agent_sdk block so
    the routing decision is unit-testable without importing the Claude SDK."""
    if judge_cfg is None:
        return False
    if isinstance(judge_cfg, dict):
        base_url = judge_cfg.get("base_url")
    else:
        base_url = getattr(judge_cfg, "base_url", None)
    return bool(base_url)


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


def _judge_http_one(prompt: str, judge_cfg, retry: int = 5) -> str:
    """One blocking ballot via an OpenAI-compatible endpoint (mirrors
    scoring._call_api). Returns the assistant text STRING, or '' if all retries
    exhausted — the caller feeds that string through parse_catch, which on '' / a
    bad string conservatively returns caught=False. NEVER returns a response
    object (would bloat/break json.dumps of result.json)."""
    if isinstance(judge_cfg, dict):
        base_url = judge_cfg.get("base_url")
        api_key = judge_cfg.get("api_key")
        model = judge_cfg.get("model")
    else:
        base_url = getattr(judge_cfg, "base_url", None)
        api_key = getattr(judge_cfg, "api_key", None)
        model = getattr(judge_cfg, "model", None)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": model,
        # _call_api uses the list `[{type:text,text:prompt}]` content shape (also
        # OpenAI-compatible); keep it identical since some endpoints reject a bare
        # string. We feed the assistant's returned `content` STRING to parse_catch.
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        # reasoning off so MiniMax-M3 emits the JSON verdict instead of burning the
        # budget in <think>; max_tokens headroom to fully emit the verdict object.
        "reasoning": {"effort": "none"},
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    for attempt in range(1, retry + 1):
        try:
            response = requests.post(url=base_url, headers=headers, json=data, timeout=120)
            resp = response.json()
            if response.status_code != 200 or "choices" not in resp:
                print(f"[judge_catch_http][attempt {attempt}/{retry}] bad response: {resp}")
            else:
                content = resp["choices"][0]["message"]["content"]
                return content if isinstance(content, str) else json.dumps(content)
        except Exception as exc:
            print(f"[judge_catch_http][attempt {attempt}/{retry}] exception: {exc}")
        time.sleep(1)
    # All retries exhausted: return '' so parse_catch yields a conservative
    # caught=False ballot. The vote is NEVER skipped (len(votes) stays == votes).
    return ""


async def judge_catch_http(injected: dict, result_md: str, judge_cfg, votes: int = 3) -> dict:
    """Independent HTTP catch-judge (D1, default MiniMax-M3). Runs `votes` ballots
    against an OpenAI-compatible endpoint, parses each with the EXISTING parse_catch
    and aggregates with the EXISTING majority_caught — returning the SAME shape as
    judge_catch: {caught: bool, votes:[parse_catch dicts]}.

    Each blocking requests.post runs via asyncio.to_thread, and the `votes` calls
    are gathered in parallel (mirroring judge_catch's asyncio.gather) so the event
    loop stays responsive under --concurrency>1 (a bare blocking post would freeze
    the loop up to votes*120s and serialize every mutant). A dead HTTP vote counts
    as a no-catch ballot, never a phantom catch; len(votes) always == `votes`."""
    prompt = USER_PROMPT["mutation_catch"].substitute(
        injected=json.dumps(injected, ensure_ascii=False), result=result_md,
    )
    ballots = await asyncio.gather(
        *(asyncio.to_thread(_judge_http_one, prompt, judge_cfg) for _ in range(votes))
    )
    parsed = [parse_catch(b) for b in ballots]
    return {"caught": bool(majority_caught([p["caught"] for p in parsed])), "votes": parsed}
