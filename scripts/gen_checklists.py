"""
Pre-generate checklist.md for a record subset into a pipeline version dir,
using the CURRENTLY-installed checklist_generation prompt (chat-only, CLI creds).

This lets the expensive defect_detection stage run later without depending on
the prompt file content (the pipeline skips checklist_generation when
checklist.md already exists). Used to ablate old-prompt vs new-prompt detection.

Run: python scripts/gen_checklists.py --data <jsonl> --version <ver> [--model sonnet]
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from claude_agent_sdk import (  # noqa: E402
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
)
from prompt import USER_PROMPT  # noqa: E402

CWD = str(ROOT / "claude_code_cwd")


async def run_query(prompt, model):
    opts = ClaudeAgentOptions(
        allowed_tools=[], model=model, max_turns=5,
        max_buffer_size=1024 * 1024, cwd=CWD, env={},
    )
    text, result = "", ""
    async for message in query(prompt=prompt, options=opts):
        if isinstance(message, AssistantMessage):
            for b in message.content:
                if isinstance(b, TextBlock):
                    text += b.text
        elif isinstance(message, ResultMessage):
            result = message.result or ""
    out = result if result.strip() else text
    # keep only from the '# Test Checklist' header (mirror pipeline extraction intent)
    idx = out.find("# Test Checklist")
    return out[idx:] if idx != -1 else out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--output_root", default=str(ROOT / "outputs"))
    args = ap.parse_args()

    records = [json.loads(l) for l in Path(args.data).read_text().splitlines() if l.strip()]
    print(f"Generating {len(records)} checklists -> {args.output_root}/{args.version}/ (model={args.model})")

    async def one(r):
        rid = str(r["index"])
        prompt = USER_PROMPT["checklist_generation"].substitute(instruction=r["instruction"])
        md = await run_query(prompt, args.model)
        d = Path(args.output_root) / args.version / rid
        d.mkdir(parents=True, exist_ok=True)
        (d / "checklist.md").write_text(md, encoding="utf-8")
        n_items = md.count("- [")
        print(f"  {rid}: {n_items} items -> {d/'checklist.md'}")

    sem = asyncio.Semaphore(5)
    async def guarded(r):
        async with sem:
            await one(r)
    await asyncio.gather(*(guarded(r) for r in records))
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
