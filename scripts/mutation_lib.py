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
