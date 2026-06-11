"""Mutation catch-rate harness library: pure metric logic + I/O helpers.

Pure functions (unit-tested) decide whether the numbers are right. I/O helpers
(generate_mutant / judge_catch / copy_app_sources) are covered by the smoke run.
"""
from __future__ import annotations

import asyncio
import hashlib
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


async def ballots_with_reballot(cast, votes: int = 3, max_reballots: int = 2) -> dict:
    """Cast `votes` judge ballots via the zero-arg async `cast`, re-casting any
    UNPARSEABLE ballot up to `max_reballots` extra times. A seat that stays
    unparseable is DISCARDED — it must never occupy a majority slot as a fake
    'no' vote (P0-1, 2026-06-11 pipeline audit: 4/75 dd3r ballots were
    unparseable and decided 0074 m3 as a false miss). Majority is strict over
    the VALID seats only; zero valid seats -> conservative caught=False.

    First round is cast in parallel; the (rare) re-ballots run sequentially.
    Returns {caught, votes:[valid parse_catch dicts], discarded_unparseable:int}.
    """
    first = await asyncio.gather(*(cast() for _ in range(votes)))
    seats, discarded = [], 0
    for raw in first:
        verdict = parse_catch(raw)
        attempts = 0
        while verdict.get("unparseable") and attempts < max_reballots:
            verdict = parse_catch(await cast())
            attempts += 1
        if verdict.get("unparseable"):
            discarded += 1
        else:
            seats.append(verdict)
    return {
        "caught": majority_caught([s["caught"] for s in seats]),
        "votes": seats,
        "discarded_unparseable": discarded,
    }


def find_duplicate_mutant(app_out_dir: Path, k: int) -> int | None:
    """Return the smallest j<k whose cached injection (target file + patched
    content) is byte-identical to m{k}'s, else None. (P0-3: the dd3r run scored
    0009 m0/m1 — the SAME single-character edit — as two valid mutants in two
    fault classes, double-counting one injection in numerator and denominator.)"""
    d = Path(app_out_dir)

    def sig(j: int):
        mj = d / f"m{j}"
        pm, nf = mj / "patch_meta.json", mj / "new_file.txt"
        if not (pm.exists() and nf.exists()):
            return None
        try:
            rel = json.loads(pm.read_text(encoding="utf-8")).get("file")
        except Exception:
            return None
        return (rel, hashlib.sha256(nf.read_bytes()).hexdigest())

    mine = sig(k)
    if mine is None:
        return None
    for j in range(k):
        if sig(j) == mine:
            return j
    return None


def injection_sha(mutant_dir: Path) -> str:
    """Stable digest of a cached injection (target file path + patched content).
    Stored in result.json so a resume can prove the verdict still belongs to the
    injection on disk (P0-4)."""
    d = Path(mutant_dir)
    rel = json.loads((d / "patch_meta.json").read_text(encoding="utf-8")).get("file", "")
    h = hashlib.sha256()
    h.update(str(rel).encode("utf-8"))
    h.update(b"\x00")
    h.update((d / "new_file.txt").read_bytes())
    return h.hexdigest()


def cached_result_ok(mutant_dir: Path) -> dict | None:
    """Return the stored result.json verdict iff it can be reused VERBATIM on
    resume — P0-4: a completed mutant's judge votes must not be re-rolled (the
    documented resume flow re-judged every finished mutant, letting verdicts
    drift between runs and allowing a NEW injection to be judged against a STALE
    result.md). Reusable when the stored injection_sha matches the injection on
    disk, or when the result is legacy (no sha; re-deriving it would itself
    re-roll votes). None when result.json is absent/corrupt or the injection
    changed after the verdict."""
    d = Path(mutant_dir)
    rp = d / "result.json"
    if not rp.exists():
        return None
    try:
        res = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return None
    stored = res.get("injection_sha")
    if stored is None:
        return res
    try:
        return res if stored == injection_sha(d) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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


def _first_json_object(s: str) -> str | None:
    """Return the first balanced top-level ``{...}`` object in ``s``, or None.

    A string-aware brace-depth scan: braces inside JSON string literals (and
    backslash-escaped quotes) are ignored, so a verdict whose ``reason`` prose
    contains braces — balanced ('fails when {count} shown') OR unbalanced
    ('use a } to close') — is extracted whole instead of being truncated. This
    is what the old bare-brace regex `{[^{}]*"caught"[^{}]*}` could not do."""
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None  # unbalanced — no complete object


def is_rate_limited(event_log_text: str | None) -> bool:
    """True when a detection event log shows the provider rate-limiting (HTTP 429).

    A rate-limited run produces no result through no fault of the mutant, so
    callers must NOT classify it as a normal 'invalid' — that silently shrinks
    the catch-rate denominator (the 2026-06-11 dd3r incident burned 22 mutants
    this way). Matches the claude_agent_sdk event shape `"api_error_status": 429`.
    """
    if not event_log_text:
        return False
    return re.search(r'"api_error_status"\s*:\s*429\b', event_log_text) is not None


def parse_catch(md: str) -> dict:
    """Parse a judge verdict. Defaults to caught=false on any parse failure
    (conservative: an unparseable verdict must not count as a catch).

    Order: fenced ```json block, then a balanced-brace scan, then the legacy
    bare-brace regex as a last resort. The bare regex `{[^{}]*"caught"[^{}]*}`
    forbids nested braces and would drop a genuine catch when a verdict's reason
    contains braces (e.g. 'fails when {count} shown') — MiniMax-M3 (D1 HTTP path,
    reasoning off) emits RAW unfenced JSON and is MORE likely to put braces in
    prose, which is exactly the downward bias the balanced scan fixes. Handles
    '' / None without raising (judge_catch_http feeds '' on total HTTP failure)."""
    if not md:
        return {"caught": False, "matched_item": None, "reason": "empty verdict",
                "unparseable": True}

    # 1) Prefer a fenced ```json ... ``` block — tolerates braces inside reason prose.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", md, re.DOTALL)
    if fenced:
        try:
            v = json.loads(fenced.group(1))
            v["caught"] = bool(v.get("caught", False))
            return v
        except Exception:
            pass  # fall through

    # 2) Balanced-brace scan — recovers an UNFENCED verdict whose reason prose
    #    contains braces (the bare regex below cannot span them).
    obj = _first_json_object(md)
    if obj is not None:
        try:
            v = json.loads(obj)
            v["caught"] = bool(v.get("caught", False))
            return v
        except Exception:
            pass  # fall through to the legacy regex

    # 3) Legacy bare brace match (no-nested-brace) as a final conservative resort.
    m = re.search(r"\{[^{}]*\"caught\"[^{}]*\}", md, re.DOTALL)
    if not m:
        return {"caught": False, "matched_item": None, "reason": "unparseable verdict",
                "unparseable": True}
    try:
        v = json.loads(m.group(0))
        v["caught"] = bool(v.get("caught", False))
        return v
    except Exception:
        return {"caught": False, "matched_item": None, "reason": "json error",
                "unparseable": True}


# ---------- mutant pre-checks (reachability + manifestation) ----------
# White-box diagnosis of the 4-class run (2026-06-10, see tuning-log) found the
# validity judge passing mutants that cannot manifest: a dead-code file (0070 m2,
# NavLink.tsx with zero importers) and CSS-only edits neutralized at every call
# site / invisible to a11y snapshots (0070 m0/m1, `disabled:pointer-events-none`).
# These static checks reject such mutants BEFORE the ~22-min detection run.

_IMPORT_RE = re.compile(
    r"""(?:\bimport\s+[^'"]*?\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|\bexport\s+[^'"]*?\bfrom\s*)['"]([^'"]+)['"]""",
)
_JS_EXTS = (".tsx", ".ts", ".jsx", ".js")
# a11y-tree-affecting utility classes: removing/adding these changes what the
# accessibility snapshot contains, so such a CSS-only mutant CAN manifest.
_A11Y_OBSERVABLE_CLASSES = {"hidden", "sr-only", "invisible", "collapse", "not-sr-only"}
_CSS_TOKEN_RE = re.compile(r"^!?[a-z0-9:_\-\[\]()./%#]+$")


def _resolve_import(app_dir: Path, importer: Path, spec: str) -> Path | None:
    """Resolve an import specifier to a source file inside the app, or None.

    Handles './x'/'../x' (relative to the importer) and the '@/x' -> src/x vite
    alias used across the app corpus. Package imports return None. Tries the bare
    path, every JS extension, and directory index files."""
    if spec.startswith("@/"):
        base = app_dir / "src" / spec[2:]
    elif spec.startswith("."):
        base = (importer.parent / spec).resolve()
    else:
        return None  # package import
    candidates = [base] if base.suffix in _JS_EXTS else []
    candidates += [base.with_name(base.name + ext) for ext in _JS_EXTS]
    candidates += [base / f"index{ext}" for ext in _JS_EXTS]
    for c in candidates:
        if c.is_file():
            return c
    return None


def is_reachable(app_dir: Path, rel_file: str) -> bool:
    """BFS the import graph from the app's entry files; True iff `rel_file` is
    reached. CONSERVATIVE: if no entry file is found, return True (cannot
    analyze -> never invalidate). The entry files themselves are reachable."""
    app_dir = Path(app_dir)
    entries = [p for name in ("main", "index", "App")
               for ext in _JS_EXTS
               if (p := app_dir / "src" / f"{name}{ext}").is_file()]
    if not entries:
        return True
    target = (app_dir / rel_file).resolve()
    seen: set[Path] = set()
    queue = [e.resolve() for e in entries]
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == target:
            return True
        try:
            body = cur.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for spec in _IMPORT_RE.findall(body):
            nxt = _resolve_import(app_dir, cur, spec)
            if nxt is not None and nxt.resolve() not in seen:
                queue.append(nxt.resolve())
    return target in seen


_STR_LIT_RE = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", re.DOTALL)


def manifestation_verdict(old_content: str, new_content: str) -> str | None:
    """Return an invalidity reason for a mutation that cannot manifest in an
    accessibility snapshot, else None (no objection).

    Flags ONLY the proven-no-op shape: the change is confined to string-literal
    contents AND every changed token is a css-utility-shaped class that does NOT
    affect the a11y tree (pointer-events/hover/opacity/...). Display-text edits
    (capitalized words, spaces, punctuation) and any code change pass."""
    stripped_old = _STR_LIT_RE.sub("''", old_content)
    stripped_new = _STR_LIT_RE.sub("''", new_content)
    if stripped_old != stripped_new:
        return None  # code changed -> can manifest

    def _tokens(s: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lit in _STR_LIT_RE.findall(s):
            for tok in lit[1:-1].split():
                counts[tok] = counts.get(tok, 0) + 1
        return counts

    old_t, new_t = _tokens(old_content), _tokens(new_content)
    changed = {t for t in set(old_t) | set(new_t) if old_t.get(t, 0) != new_t.get(t, 0)}
    if not changed:
        return None  # literals reordered/identical -> assume observable content change
    for tok in changed:
        # Utility classes are dashed/variant-prefixed/arbitrary-value shaped
        # ('pointer-events-none', 'disabled:opacity-50', 'w-[12px]'). A bare
        # lowercase word ('one', 'flex') is ambiguous -> treat as observable.
        if not _CSS_TOKEN_RE.match(tok) or not re.search(r"[-:\[]", tok):
            return None  # not clearly css-utility-shaped -> observable
        if tok.split(":")[-1] in _A11Y_OBSERVABLE_CLASSES:
            return None  # a11y-tree-affecting class -> observable
    return "css_only_not_a11y_observable"


def precheck_mutant(app_dir: Path, rel_file: str, new_content: str) -> tuple[str, str | None]:
    """Static pre-checks against the PRISTINE app. Returns (validity, reason):
    ('invalid', 'unreachable_file' | 'css_only_not_a11y_observable') or ('valid', None)."""
    app_dir = Path(app_dir)
    if not is_reachable(app_dir, rel_file):
        return "invalid", "unreachable_file"
    old_path = app_dir / rel_file
    if old_path.is_file():
        reason = manifestation_verdict(
            old_path.read_text(encoding="utf-8", errors="ignore"), new_content)
        if reason:
            return "invalid", reason
    return "valid", None


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


# (asyncio imported at module head)

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
    {caught, votes:[...], discarded_unparseable} with every VALID vote's verdict
    for the audit trail; unparseable ballots are re-cast and, if persistent,
    discarded rather than seated as fake 'no' votes (P0-1)."""
    prompt = USER_PROMPT["mutation_catch"].substitute(
        injected=json.dumps(injected, ensure_ascii=False), result=result_md,
    )

    async def cast() -> str:
        return await run_query(prompt, model)

    return await ballots_with_reballot(cast, votes=votes)


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

    Each blocking requests.post runs via asyncio.to_thread, and the first round
    of `votes` calls is gathered in parallel (mirroring judge_catch) so the event
    loop stays responsive under --concurrency>1 (a bare blocking post would freeze
    the loop up to votes*120s and serialize every mutant). Unparseable ballots
    (HTTP-dead OR 200-with-garbage, e.g. finish_reason=length truncation) are
    re-cast and, if persistent, DISCARDED instead of seated as fake 'no' votes —
    majority is strict over valid seats only (P0-1; previously a dead/garbage
    vote occupied a seat and decided 0074 m3 as a false miss)."""
    prompt = USER_PROMPT["mutation_catch"].substitute(
        injected=json.dumps(injected, ensure_ascii=False), result=result_md,
    )

    async def cast() -> str:
        return await asyncio.to_thread(_judge_http_one, prompt, judge_cfg)

    out = await ballots_with_reballot(cast, votes=votes)
    out["caught"] = bool(out["caught"])
    return out
