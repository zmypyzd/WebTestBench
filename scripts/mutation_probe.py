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
import os
import signal
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


def _force_reap_server(agent, app_id: str, k: int) -> None:
    """Hard-kill the mutant's dev server on timeout/early-failure (D3 cleanup).

    kill_local_server -> _kill_exist_port does only `lsof -ti:PORT | xargs kill -9`
    and never kills the setsid process GROUP of dev_server_process (launched with
    preexec_fn=os.setsid). vite/npm children (esbuild, node) may hold the port under
    a different PID, or the port may be momentarily unbound (still starting) so lsof
    returns nothing while the process survives and re-binds, leaking orphans across
    the batch. So we (a) call kill_local_server (idempotent; targets the per-mutant
    UNIQUE port, can't kill a sibling mutant) AND (b) killpg the setsid group."""
    if agent is None:
        return
    try:
        agent.kill_local_server()
    except Exception as exc:
        print(f"[{app_id} m{k}] kill_local_server failed: {exc}")
    proc = getattr(agent, "dev_server_process", None)
    if proc is not None and getattr(proc, "pid", None):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            print(f"[{app_id} m{k}] killpg'd dev server group (pid {proc.pid})")
        except ProcessLookupError:
            pass  # already gone
        except Exception as exc:
            print(f"[{app_id} m{k}] killpg failed (pid {proc.pid}): {exc}")


async def run_one_mutant(app_id: str, k: int, record: dict, args, api_config: APIConfig,
                         port: int, judge_cfg, sem: asyncio.Semaphore) -> dict | None:
    """Generate-or-reuse one mutant, deploy+detect, judge. Returns a result record
    (incl. timeout-invalid records, which MUST be returned not dropped), or None on
    a hard failure (logged, never aborts the batch).

    The ENTIRE body runs under `sem` (D3 concurrency throttle): deploy is the heavy/
    contended resource (concurrent dev servers + npm). Ports are pre-assigned per
    (app,k) so the semaphore never causes a port collision."""
    async with sem:
        fault_long = QUOTA[k % len(QUOTA)]
        fault_class = CLASS_SHORT[fault_long]
        mdir = OUT_DIR / app_id / f"m{k}"
        mdir.mkdir(parents=True, exist_ok=True)
        app_copy = mdir / "app"
        src_app = APPS_DIR / app_id
        instruction = record.get("instruction", "")
        base = {"app": app_id, "k": k, "fault_class": fault_class, "category": record.get("category")}
        # Bind agent BEFORE the try so cleanup never NameErrors when construction raises (D3).
        agent = None

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

            # 1b) static pre-checks against the PRISTINE app (reachability +
            # manifestation; see ml.precheck_mutant). An unmanifestable mutant must
            # not burn a ~22-min detection run nor sit in the denominator as a fake
            # 'valid' miss (the 0070 m0/m1/m2 scar from the 4-class run).
            pre_validity, pre_reason = ml.precheck_mutant(src_app, rel, content)
            if pre_validity == "invalid":
                out = {**base, "validity": "invalid", "caught": False, "votes": [],
                       "repro_steps": rec.get("repro_steps", ""),
                       "reason": f"precheck:{pre_reason}"}
                (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
                print(f"[{app_id} m{k}] {fault_class} validity=invalid caught=False "
                      f"(PRECHECK {pre_reason})")
                return out

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
            timed_out = False
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
                    # Wrap ONLY agent.run() in the timeout — never the judge / copy /
                    # result read, so a slow judge is not mis-marked a detection timeout.
                    # LIMITATION (D3): base_agent._deploy_local_server runs npm install +
                    # time.sleep(20) SYNCHRONOUSLY in the awaited chain with no to_thread,
                    # so wait_for's timer can't fire while the loop is blocked there. The
                    # pilot MUST use an outer shell `timeout` as the hard wall-clock
                    # backstop; --mutant-timeout bounds only the async detection portion.
                    await asyncio.wait_for(agent.run(), timeout=args.mutant_timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    print(f"[{app_id} m{k}] DETECTION TIMEOUT after {args.mutant_timeout}s — "
                          f"marking invalid, force-reaping server")
                    # agent.run()'s own finally:kill_local_server may not interrupt a
                    # synchronous in-flight subprocess; force-reap again (idempotent).
                    _force_reap_server(agent, app_id, k)
                except Exception as exc:
                    print(f"[{app_id} m{k}] detection/deploy error: {exc}")
                    deploy_ok = agent.result_extracted_path.exists() if agent is not None else False
            finally:
                log_f.close()

            # On timeout: drive validity -> 'invalid' (the ONLY mechanism that
            # excludes a mutant from aggregate()'s denominator), SKIP the judge
            # entirely (no trustworthy result_md), and BUILD+WRITE+RETURN the record
            # (returning None would drop it from agg['records'] & agg['invalid']).
            if timed_out:
                out = {**base, "validity": "invalid", "caught": False, "votes": [],
                       "repro_steps": rec.get("repro_steps", ""), "timeout": True,
                       "reason": "detection_timeout"}
                (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
                print(f"[{app_id} m{k}] {fault_class} validity=invalid caught=False (TIMEOUT)")
                return out

            result_md = ""
            if agent is not None and agent.result_extracted_path.exists():
                result_md = agent.result_extracted_path.read_text(encoding="utf-8")

            # 4) validity: deployable? reachable? (lightweight: patched a real src file)
            reachable = (src_app / rel).exists() and rel.startswith("src")
            validity = ml.classify_validity(deploy_ok=deploy_ok and bool(result_md), reachable=reachable)

            # 5) 3-vote judge (only meaningful when we have a result). Route to the
            # independent HTTP judge when a judge base_url is given, else the CLI
            # judge (NON-BREAKING default). BOTH branches are awaited identically.
            # A judge-HTTP failure on a VALID result yields caught=False (majority of
            # conservative ballots) and stays IN the denominator — it is NEVER marked
            # invalid (that would shrink the denominator and bias catch_rate upward).
            verdict = {"caught": False, "votes": []}
            if result_md:
                if ml.use_http_judge(judge_cfg):
                    verdict = await ml.judge_catch_http(rec, result_md, judge_cfg)
                else:
                    verdict = await ml.judge_catch(rec, result_md, args.model)

            out = {**base, "validity": validity, "caught": verdict["caught"],
                   "votes": verdict["votes"], "repro_steps": rec.get("repro_steps", "")}
            (mdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
            print(f"[{app_id} m{k}] {fault_class} validity={validity} caught={verdict['caught']}")
            return out
        except Exception as exc:
            print(f"[{app_id} m{k}] HARD FAIL (skipped): {exc}")
            # Best-effort reap in case a server was launched before the failure.
            _force_reap_server(agent, app_id, k)
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
    # D1 independent judge (default MiniMax-M3). Behavior-preserving defaults: with
    # NO --judge_api_base_url the judge falls back to the existing CLI judge_catch.
    # --judge_model's non-None default stays INERT unless base_url is set (routing
    # keys off base_url only, never off model).
    ap.add_argument("--judge_api_base_url", default=None,
                    help="OpenAI-compatible base_url for the independent HTTP judge; "
                         "if unset the judge falls back to the CLI judge_catch")
    ap.add_argument("--judge_api_key", default=None, help="API key for the HTTP judge")
    ap.add_argument("--judge_model", default="MiniMax-M3", help="model for the HTTP judge")
    # D3 concurrency + per-mutant detection timeout.
    ap.add_argument("--concurrency", type=int, default=2,
                    help="max concurrent mutants (asyncio.Semaphore); default 2 is a "
                         "safety throttle vs the old unbounded gather")
    ap.add_argument("--mutant-timeout", type=int, default=2400,
                    help="seconds bounding the async detection (agent.run()); on timeout "
                         "the mutant is marked invalid and excluded from the denominator")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app_ids = [a.strip() for a in args.apps.split(",") if a.strip()]
    records = load_records(app_ids)
    api_config = APIConfig(base_url=args.api_base_url, api_key=args.api_key, model=args.detect_model)
    # Build judge_cfg (APIConfig-shaped) ONLY when a judge base_url is present, else
    # None — so use_http_judge routing is deterministic and defaults to the CLI judge.
    judge_cfg = (
        APIConfig(base_url=args.judge_api_base_url, api_key=args.judge_api_key, model=args.judge_model)
        if args.judge_api_base_url else None
    )
    # Semaphore created INSIDE the running loop (async main), not at import time.
    sem = asyncio.Semaphore(args.concurrency)

    # Unique port per (app, k) so concurrent dev servers never collide.
    tasks, port = [], args.base_port
    for app_id in app_ids:
        if app_id not in records:
            print(f"[warn] {app_id} not in dataset; skipping")
            continue
        for k in range(args.mutants_per_app):
            tasks.append(run_one_mutant(app_id, k, records[app_id], args, api_config, port,
                                        judge_cfg, sem))
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
