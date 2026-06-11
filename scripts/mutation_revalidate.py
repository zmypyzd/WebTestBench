"""Re-validate + re-judge EXISTING mutant dirs after harness fixes (no detection re-run).

Why this exists
---------------
The 4-class run (2026-06-10, `summary_7app_4class.json`) reported catch_rate
12/28 = 0.429, but white-box diagnosis showed (a) three 0070 mutants that cannot
manifest (dead file / a11y-invisible CSS edit) passed validity, and (b) the
judge ruled 0074 m0 a miss although the report FAILed the mutated surface
quoting the mutated predicate (one-sided symptom). After landing
`ml.precheck_mutant` and the partial-match judge rule, this tool re-derives the
baseline from the EXISTING artifacts: pre-checks decide validity statically, and
valid mutants are re-judged from their archived `run/result_extracted.md` with
the updated prompt — detection itself is untouched (same agent behavior, so the
metric stays comparable).

Per mutant dir: precheck(pristine app, patch) -> invalid? else re-judge (3-vote
HTTP judge). The original result.json is preserved once as result.json.bak-v1.

Usage:
    python scripts/mutation_revalidate.py \
        --apps WebTestBench_0009,...,WebTestBench_0089 --mutants 4 \
        --judge_api_base_url <openai-compatible chat-completions URL> \
        --judge_api_key <key> [--judge_model MiniMax-M3] \
        --out summary_7app_4class_v2.json [--concurrency 4] [--no-rejudge]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_lib as ml  # noqa: E402

APPS_DIR = ROOT / "data/WebTestBench/web_applications"
OUT_DIR = ROOT / "outputs/_mutation_probe"
QUOTA = ["constraint", "interaction", "functionality", "content"]
CLASS_SHORT = {"constraint": "CS", "interaction": "IX", "functionality": "FT", "content": "CT"}


async def revalidate_one(app_id: str, k: int, judge_cfg, rejudge: bool,
                         sem: asyncio.Semaphore) -> dict | None:
    mdir = OUT_DIR / app_id / f"m{k}"
    if not (mdir / "injected.json").exists():
        print(f"[{app_id} m{k}] no mutant artifacts; skipping")
        return None
    rec = json.loads((mdir / "injected.json").read_text())
    rel = json.loads((mdir / "patch_meta.json").read_text())["file"]
    content = (mdir / "new_file.txt").read_text()
    old = json.loads((mdir / "result.json").read_text()) if (mdir / "result.json").exists() else {}
    fault_class = old.get("fault_class") or CLASS_SHORT[QUOTA[k % len(QUOTA)]]
    base = {"app": app_id, "k": k, "fault_class": fault_class,
            "category": old.get("category"), "repro_steps": rec.get("repro_steps", "")}

    # Preserve the pre-fix verdict once for the audit trail.
    bak = mdir / "result.json.bak-v1"
    if (mdir / "result.json").exists() and not bak.exists():
        bak.write_text((mdir / "result.json").read_text())

    # 0) duplicate-injection check (P0-3): byte-identical injections must not
    # double-count in numerator and denominator (the dd3r 0009 m0/m1 scar).
    dup = ml.find_duplicate_mutant(OUT_DIR / app_id, k)
    if dup is not None:
        out = {**base, "validity": "invalid", "caught": False, "votes": [],
               "reason": f"precheck:duplicate_of_m{dup}"}
        (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"[{app_id} m{k}] {fault_class} validity=invalid (PRECHECK duplicate_of_m{dup})")
        return out

    # 1) static pre-checks against the pristine app
    validity, reason = ml.precheck_mutant(APPS_DIR / app_id, rel, content)
    if validity == "invalid":
        out = {**base, "validity": "invalid", "caught": False, "votes": [],
               "reason": f"precheck:{reason}"}
        (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"[{app_id} m{k}] {fault_class} validity=invalid (PRECHECK {reason})")
        return out

    # 2) a timeout-invalid mutant has no trustworthy result_md: keep it invalid.
    if old.get("reason") == "detection_timeout":
        out = {**base, "validity": "invalid", "caught": False, "votes": [],
               "reason": "detection_timeout", "timeout": True}
        (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"[{app_id} m{k}] {fault_class} validity=invalid (kept: detection_timeout)")
        return out

    result_path = mdir / "run" / "result_extracted.md"
    if not result_path.exists():
        out = {**base, "validity": "invalid", "caught": False, "votes": [],
               "reason": "missing_result"}
        (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"[{app_id} m{k}] {fault_class} validity=invalid (missing result_extracted.md)")
        return out

    # 3) re-judge with the updated partial-match prompt (or keep the old verdict).
    if rejudge:
        async with sem:
            verdict = await ml.judge_catch_http(rec, result_path.read_text(encoding="utf-8"),
                                                judge_cfg)
    else:
        verdict = {"caught": bool(old.get("caught", False)), "votes": old.get("votes", [])}

    out = {**base, "validity": "valid", "caught": verdict["caught"], "votes": verdict["votes"]}
    (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    flip = "" if bool(old.get("caught")) == verdict["caught"] else \
        f"  <<< FLIP ({old.get('caught')} -> {verdict['caught']})"
    print(f"[{app_id} m{k}] {fault_class} validity=valid caught={verdict['caught']}{flip}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apps", required=True, help="comma-separated app ids")
    ap.add_argument("--mutants", type=int, default=4, help="mutants per app (m0..m{N-1})")
    ap.add_argument("--judge_api_base_url", required=True)
    ap.add_argument("--judge_api_key", required=True)
    ap.add_argument("--judge_model", default="MiniMax-M3")
    ap.add_argument("--out", default="summary_revalidated.json")
    ap.add_argument("--baseline", default=None, help="old summary.json to print the A/B against")
    ap.add_argument("--concurrency", type=int, default=4, help="concurrent judge calls")
    ap.add_argument("--no-rejudge", action="store_true",
                    help="apply pre-checks only; keep existing judge verdicts")
    ap.add_argument("--out-root", default=None,
                    help="override the probe root to revalidate (default outputs/_mutation_probe; "
                         "e.g. outputs/_mutation_probe_dd3r)")
    args = ap.parse_args()

    global OUT_DIR
    if args.out_root:
        p = Path(args.out_root)
        OUT_DIR = p if p.is_absolute() else ROOT / p

    judge_cfg = {"base_url": args.judge_api_base_url, "api_key": args.judge_api_key,
                 "model": args.judge_model}
    sem = asyncio.Semaphore(args.concurrency)
    app_ids = [a.strip() for a in args.apps.split(",") if a.strip()]
    tasks = [revalidate_one(a, k, judge_cfg, not args.no_rejudge, sem)
             for a in app_ids for k in range(args.mutants)]
    results = [r for r in await asyncio.gather(*tasks) if r is not None]

    agg = ml.aggregate(results)
    agg["records"] = results
    (OUT_DIR / args.out).write_text(json.dumps(agg, ensure_ascii=False, indent=2))

    print("\n==================== REVALIDATED CATCH-RATE ====================")
    print(f"valid={agg['valid']} invalid={agg['invalid']} suspect={agg['suspect']}  "
          f"caught={agg['caught']}  catch_rate={agg['catch_rate']}")
    for c, b in sorted(agg["by_class"].items()):
        print(f"  {c}: {b['caught']}/{b['n']} = {b['catch_rate']}")

    if args.baseline:
        bpath = OUT_DIR / args.baseline
        if bpath.exists():
            old = json.loads(bpath.read_text())
            print(f"\nA/B vs {args.baseline}: catch_rate {old.get('catch_rate')} -> "
                  f"{agg['catch_rate']} | valid {old.get('valid')} -> {agg['valid']} | "
                  f"caught {old.get('caught')} -> {agg['caught']}")
        else:
            print(f"[warn] baseline {bpath} not found")


if __name__ == "__main__":
    asyncio.run(main())
