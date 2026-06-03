"""
Checklist-coverage probe (option b).

Isolates the checklist_generation bottleneck: for a category-spanning sample,
run ONLY checklist_generation (chat-only, via local CLI creds), match generated
items to gold items with PROMPT_MATCH_ITEM, then report per-class coverage --
i.e. of the gold items in each class, how many did the generated checklist even
mention. No app deploy, no browser, no external API key.

Run: python scripts/coverage_probe.py [--per-cat 2] [--model sonnet]
"""
import argparse
import ast
import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from claude_agent_sdk import (
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
)
from prompt import USER_PROMPT  # noqa: E402
from prompt.match_item import PROMPT_MATCH_ITEM  # noqa: E402

DATASET = ROOT / "data/WebTestBench/WebTestBench.jsonl"
OUT_DIR = ROOT / "outputs/_coverage_probe"
CWD = str(ROOT / "claude_code_cwd")
CLASSES = ["functionality", "constraint", "interaction", "content"]


def load_dataset():
    records = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def sample(records, per_cat):
    """First `per_cat` records of each category, in dataset order."""
    by_cat = defaultdict(list)
    for r in records:
        by_cat[r.get("category")].append(r)
    picked = []
    for cat in sorted(by_cat):
        picked.extend(by_cat[cat][:per_cat])
    return picked


async def run_query(prompt, model, max_turns=5):
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


def parse_pred(md):
    """Parse '- [ ] FT-01: desc' -> {id: desc}. Handles optional **bold** ids,
    matching scoring.py's _parse_checklist_md regex so counts agree."""
    items = {}
    pat = re.compile(r"^- \[\s*[xX ]\s*\]\s*(?:\*\*)?([A-Z]{2}-\d+)(?:\*\*)?:\s*(.+)$")
    for line in md.splitlines():
        m = pat.match(line.strip())
        if m:
            items[m.group(1)] = m.group(2).strip()
    return items


def parse_gold(record):
    """-> {gold_id(str): {text, class, pass}}"""
    gold = {}
    for it in record.get("checklist", []):
        gid = str(it.get("id"))
        gold[gid] = {
            "text": it.get("content", ""),
            "class": it.get("class", ""),
            "pass": bool(it.get("pass", True)),
            "bug": it.get("bug", ""),
        }
    return gold


def parse_match_list(text):
    """Extract the [(pred,gold), ...] list literal from match output."""
    m = re.search(r"\[\s*\(.*\)\s*\]", text, re.DOTALL)
    raw = m.group(0) if m else text.strip()
    raw = raw.replace("None", "None").replace("none", "None")
    try:
        return ast.literal_eval(raw)
    except Exception:
        # be lenient: pull tuples one by one
        pairs = []
        for mm in re.finditer(r"\(\s*['\"]([^'\"]+)['\"]\s*,\s*(None|['\"][^'\"]+['\"])\s*\)", text):
            g = mm.group(2)
            g = None if g == "None" else g.strip("'\"")
            pairs.append((mm.group(1), g))
        return pairs


async def process(record, model, sem):
    rid = str(record.get("index"))
    cat = record.get("category")
    instruction = record.get("instruction", "")
    gold = parse_gold(record)

    async with sem:
        # 1) generate checklist
        gen_prompt = USER_PROMPT["checklist_generation"].substitute(instruction=instruction)
        checklist_md = await run_query(gen_prompt, model)
        pred = parse_pred(checklist_md)

        # 2) match pred -> gold
        gold_items_str = "\n".join(f'"{g}": "{d["text"]}"' for g, d in gold.items())
        pred_items_str = "\n".join(f'"{p}": "{t}"' for p, t in pred.items())
        match_prompt = PROMPT_MATCH_ITEM.substitute(
            instruction=instruction, gold_items=gold_items_str, pred_items=pred_items_str,
        )
        match_text = await run_query(match_prompt, model)
        matches = parse_match_list(match_text)

    covered = {g for _, g in matches if g}  # gold ids covered by >=1 pred
    phantom = sum(1 for _, g in matches if not g)  # preds matching nothing

    # save artifacts
    rec_dir = OUT_DIR / rid
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "checklist.md").write_text(checklist_md, encoding="utf-8")
    (rec_dir / "match.json").write_text(
        json.dumps({"matches": matches, "raw": match_text}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[{rid}] {cat}: gold={len(gold)} pred={len(pred)} "
          f"covered={len(covered)} phantom_pred={phantom}")
    return {
        "id": rid, "cat": cat, "gold": gold,
        "n_pred": len(pred), "covered": covered, "phantom": phantom,
        "uncovered": [g for g in gold if g not in covered],
    }


def compare_and_gate(new, base):
    """A/B-compare new run vs baseline; print deltas and the three review gates."""
    def d(a, b):
        if a is None or b is None:
            return None
        return round(a - b, 1)

    def cell(bv, nv):
        dd = d(nv, bv)
        return "n/a" if dd is None else f"{bv}->{nv} ({dd:+})"

    print("\n==================== A/B vs BASELINE (Δ = new - baseline) ====================")
    print(f"{'class':14} {'cov%':>20} {'bug_cov%':>20} {'ok_cov%(FP surf)':>20}")
    for c in CLASSES:
        n, b = new["by_class"][c], base["by_class"].get(c, {})
        print(f"{c:14} "
              f"{cell(b.get('coverage_micro_%'), n.get('coverage_micro_%')):>20} "
              f"{cell(b.get('bug_coverage_%'), n.get('bug_coverage_%')):>20} "
              f"{cell(b.get('ok_coverage_%'), n.get('ok_coverage_%')):>20}")

    nt, bt = new["totals"], base.get("totals", {})
    print(f"\nTotal bug coverage: {bt.get('bug_coverage_%')}% -> {nt.get('bug_coverage_%')}%  |  "
          f"Uncovered bugs: {base.get('total_uncovered_bugs')} -> {new['total_uncovered_bugs']}")

    # ---- Gates ----
    print("\n==================== GATES ====================")
    # G1: gains concentrated in BUGS, not in ok-items (FP surface)
    d_bug = d(nt.get("bug_coverage_%"), bt.get("bug_coverage_%")) or 0.0
    # overall ok-coverage delta
    def ok_cov_pct(s):
        t = s.get("totals", {})
        return round(100 * t["ok_covered"] / t["ok_items"], 1) if t.get("ok_items") else None
    d_ok = d(ok_cov_pct(new), ok_cov_pct(base)) or 0.0
    g1 = d_bug >= d_ok
    print(f"G1 FP-control  : Δbug_cov ({d_bug:+}) >= Δok_cov ({d_ok:+})  -> {'PASS' if g1 else 'FAIL'}")
    print(f"   (we want bug coverage to rise more than ok-item coverage, else we just enlarge the FP surface)")

    # G2: FT coverage must not regress
    ft_n = new["by_class"]["functionality"].get("coverage_micro_%") or 0.0
    ft_b = base["by_class"]["functionality"].get("coverage_micro_%") or 0.0
    g2 = ft_n >= ft_b - 1.0  # allow 1pt noise
    print(f"G2 FT no-regress: FT cov {ft_b}% -> {ft_n}%  -> {'PASS' if g2 else 'FAIL'}")

    # G3: FP-surface readout — net newly-covered ok items
    net_ok = nt.get("ok_covered", 0) - bt.get("ok_covered", 0)
    net_bug = (new["total_gold_bugs"] - new["total_uncovered_bugs"]) - \
              (base.get("total_gold_bugs", 0) - base.get("total_uncovered_bugs", 0))
    print(f"G3 readout      : net newly-covered BUG items = {net_bug:+}, "
          f"net newly-covered OK items (FP surface) = {net_ok:+}")

    verdict = "PASS" if (g1 and g2 and net_bug > 0) else "REVIEW"
    print(f"\nOVERALL A/B VERDICT: {verdict}  "
          f"({'coverage gains are bug-concentrated, FT held, bugs newly covered' if verdict=='PASS' else 'inspect before promoting to pipeline'})")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-cat", type=int, default=2)
    ap.add_argument("--model", type=str, default="sonnet")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=str, default="summary.json",
                    help="output filename under outputs/_coverage_probe/")
    ap.add_argument("--baseline", type=str, default=None,
                    help="baseline summary filename to A/B-compare against (e.g. summary_baseline.json)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = sample(load_dataset(), args.per_cat)
    print(f"Sampled {len(records)} records, model={args.model}\n")

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(process(r, args.model, sem) for r in records))

    # ---- aggregate ----
    # per class: total gold items, covered items; AND restricted to gold BUGS
    agg = {c: {"gold": 0, "cov": 0, "bug": 0, "bug_cov": 0} for c in CLASSES}
    per_record_cov = {c: [] for c in CLASSES}  # for scoring-style averaged coverage
    uncovered_bugs = []  # the items that GUARANTEE a false negative

    for res in results:
        rec_cls = {c: {"gold": 0, "cov": 0} for c in CLASSES}
        for gid, g in res["gold"].items():
            c = g["class"]
            if c not in agg:
                continue
            agg[c]["gold"] += 1
            rec_cls[c]["gold"] += 1
            is_cov = gid in res["covered"]
            if is_cov:
                agg[c]["cov"] += 1
                rec_cls[c]["cov"] += 1
            if not g["pass"]:  # gold bug
                agg[c]["bug"] += 1
                if is_cov:
                    agg[c]["bug_cov"] += 1
                else:
                    uncovered_bugs.append({
                        "id": res["id"], "cat": res["cat"], "class": c,
                        "text": g["text"], "bug": g["bug"],
                    })
        for c in CLASSES:
            if rec_cls[c]["gold"]:
                per_record_cov[c].append(rec_cls[c]["cov"] / rec_cls[c]["gold"])

    def pct(n, d):
        return round(100 * n / d, 1) if d else None

    summary = {
        "n_records": len(results),
        "model": args.model,
        "by_class": {
            c: {
                "gold_items": agg[c]["gold"],
                "covered": agg[c]["cov"],
                "coverage_micro_%": pct(agg[c]["cov"], agg[c]["gold"]),
                "coverage_macro_%": (round(100 * sum(per_record_cov[c]) / len(per_record_cov[c]), 1)
                                     if per_record_cov[c] else None),
                "gold_bugs": agg[c]["bug"],
                "bug_covered": agg[c]["bug_cov"],
                "bug_coverage_%": pct(agg[c]["bug_cov"], agg[c]["bug"]),
                "uncovered_bugs": agg[c]["bug"] - agg[c]["bug_cov"],
                # ok (pass=True) items: covering these is FP surface, not bug-finding
                "ok_items": agg[c]["gold"] - agg[c]["bug"],
                "ok_covered": agg[c]["cov"] - agg[c]["bug_cov"],
                "ok_coverage_%": pct(agg[c]["cov"] - agg[c]["bug_cov"],
                                     agg[c]["gold"] - agg[c]["bug"]),
            } for c in CLASSES
        },
        "totals": {
            "gold_bugs": sum(agg[c]["bug"] for c in CLASSES),
            "uncovered_bugs": len(uncovered_bugs),
            "bug_coverage_%": pct(sum(agg[c]["bug_cov"] for c in CLASSES),
                                  sum(agg[c]["bug"] for c in CLASSES)),
            "ok_items": sum(agg[c]["gold"] - agg[c]["bug"] for c in CLASSES),
            "ok_covered": sum(agg[c]["cov"] - agg[c]["bug_cov"] for c in CLASSES),
        },
        "total_gold_bugs": sum(agg[c]["bug"] for c in CLASSES),
        "total_uncovered_bugs": len(uncovered_bugs),
        "uncovered_bugs_detail": uncovered_bugs,
    }
    (OUT_DIR / args.out).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n==================== COVERAGE BY CLASS ====================")
    print(f"{'class':14} {'gold':>5} {'cov%(micro)':>11} {'cov%(macro)':>11} "
          f"{'bugs':>5} {'bug_cov%':>9} {'uncov_bug':>9}")
    for c in CLASSES:
        s = summary["by_class"][c]
        print(f"{c:14} {s['gold_items']:>5} {str(s['coverage_micro_%']):>11} "
              f"{str(s['coverage_macro_%']):>11} {s['gold_bugs']:>5} "
              f"{str(s['bug_coverage_%']):>9} {s['uncovered_bugs']:>9}")
    print(f"\nTotal gold bugs: {summary['total_gold_bugs']}  |  "
          f"Uncovered (guaranteed-FN) bugs: {summary['total_uncovered_bugs']}")
    print(f"\nFull report -> {OUT_DIR/args.out}")

    if args.baseline:
        base_path = OUT_DIR / args.baseline
        if base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
            compare_and_gate(summary, base)
        else:
            print(f"\n[warn] baseline {base_path} not found; skipping A/B compare")


if __name__ == "__main__":
    asyncio.run(main())
