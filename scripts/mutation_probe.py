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
# Fault classes spread across mutants; index k picks the k-th (D3 quota: ensures CS/IX coverage).
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
    or None on a hard failure (logged, never aborts the batch)."""
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
        run_dir = mdir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "event.log"
        deploy_ok = True
        log_f = open(log_path, "w", encoding="utf-8")
        try:
            agent = ClaudeCodeWebTester(
                instruction=instruction, api_config=api_config,
                server_url=server_url, local_project_dir=app_copy, output_dir=run_dir,
                event_log_stream=log_f,
                # The catch-judge reads result_extracted.md (from defect_detection);
                # the defect_hunt stage's BUGS.md is unused here, so skip it to save
                # a meaningful chunk of per-mutant wall-clock (smoke: hunt ran for free).
                hunt_rounds=0,
            )
            try:
                await agent.run()
            except Exception as exc:
                print(f"[{app_id} m{k}] detection/deploy error: {exc}")
                deploy_ok = agent.result_extracted_path.exists()
        finally:
            log_f.close()

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
