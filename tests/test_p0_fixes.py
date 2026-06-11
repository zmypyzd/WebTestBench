"""P0 fixes from the 2026-06-11 adversarial pipeline audit (outputs/_pipeline_audit/):

P0-1 judge re-ballot: an unparseable judge ballot must be re-cast, and if it
     stays unparseable it must NOT occupy a majority seat (0074 m3 false miss).
P0-2 match-cache gold fingerprint: cached matches must not be reused after the
     gold (or preds) change; stale pred ids must not KeyError _compute_metrics.
P0-3 mutant dedup: byte-identical injections must not double-count (0009 m0/m1).
P0-4 resume verdict reuse: an existing result.json (matching injection) is
     returned verbatim instead of re-rolling judge votes.
"""
import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mutation_lib as ml  # noqa: E402
import scoring  # noqa: E402


# ---------- P0-1: judge re-ballot ----------

VALID_TRUE = '{"caught": true, "matched_item": "CS-01", "reason": "ok"}'
VALID_FALSE = '{"caught": false, "matched_item": null, "reason": "no"}'
GARBAGE = "I think therefore I am, no JSON here"


def _cast_from(queue):
    async def cast():
        return queue.pop(0)
    return cast


def test_parse_catch_flags_unparseable():
    assert ml.parse_catch(GARBAGE).get("unparseable") is True
    assert ml.parse_catch("").get("unparseable") is True
    assert ml.parse_catch(VALID_TRUE).get("unparseable", False) is False


def test_reballot_replaces_unparseable_seat():
    # seat 2 garbage -> re-ballot succeeds -> 3 valid seats, none discarded
    out = asyncio.run(ml.ballots_with_reballot(
        _cast_from([VALID_TRUE, GARBAGE, VALID_TRUE, VALID_FALSE]), votes=3))
    assert len(out["votes"]) == 3
    assert out["discarded_unparseable"] == 0
    assert out["caught"] is True  # 2 true vs 1 false


def test_persistent_unparseable_seat_is_dropped_not_a_no_vote():
    # one seat garbage through every retry -> dropped; majority over the 2 valid
    out = asyncio.run(ml.ballots_with_reballot(
        _cast_from([VALID_TRUE, GARBAGE, VALID_TRUE, GARBAGE, GARBAGE]),
        votes=3, max_reballots=2))
    assert len(out["votes"]) == 2
    assert out["discarded_unparseable"] == 1
    assert out["caught"] is True  # 2/2 valid say caught — old code: 2/3 -> also True,
    # but with [TRUE, GARBAGE, FALSE] old code gives 1/3 False while truth is 1/2 tie
    out2 = asyncio.run(ml.ballots_with_reballot(
        _cast_from([VALID_TRUE, GARBAGE, VALID_FALSE, GARBAGE, GARBAGE]),
        votes=3, max_reballots=2))
    assert len(out2["votes"]) == 2 and out2["discarded_unparseable"] == 1
    assert out2["caught"] is False  # 1/2 is not a strict majority — conservative


def test_all_unparseable_is_conservative_false():
    out = asyncio.run(ml.ballots_with_reballot(
        _cast_from([GARBAGE] * 9), votes=3, max_reballots=2))
    assert out["votes"] == []
    assert out["discarded_unparseable"] == 3
    assert out["caught"] is False


# ---------- P0-2: match-cache fingerprint + stale-id guard ----------

G1 = {"1": {"content": "a", "pass": True}, "2": {"content": "b", "pass": False}}
P1 = {"FT-01": {"content": "x", "pass": True}}


def test_fingerprint_deterministic_and_sensitive():
    fp = scoring.match_cache_fingerprint(G1, P1)
    assert fp == scoring.match_cache_fingerprint(dict(reversed(list(G1.items()))), P1)  # order-free
    g_flip = {"1": {"content": "a", "pass": True}, "2": {"content": "b", "pass": True}}
    assert fp != scoring.match_cache_fingerprint(g_flip, P1)          # gold pass flip
    p_more = {**P1, "EX-01": {"content": "y", "pass": False}}
    assert fp != scoring.match_cache_fingerprint(G1, p_more)          # preds change


def test_cache_reuse_requires_matching_fingerprint():
    fp = scoring.match_cache_fingerprint(G1, P1)
    legacy = {"source": "result", "votes": 3, "matches": [["FT-01", "1"]]}
    assert scoring.cache_reusable(legacy, "result", 3, fp) is False   # legacy: no fp -> rematch
    fresh = {**legacy, "fingerprint": fp}
    assert scoring.cache_reusable(fresh, "result", 3, fp) is True
    assert scoring.cache_reusable(fresh, "result", 1, fp) is False    # votes mismatch
    assert scoring.cache_reusable(fresh, "checklist", 3, fp) is False # source mismatch
    assert scoring.cache_reusable({**legacy, "fingerprint": "other"}, "result", 3, fp) is False


class _P:
    pass


def test_compute_metrics_survives_stale_pred_ids():
    gold = {"1": {"content": "g", "pass": False, "class": "functionality"}}
    # cached match references a pred id that no longer exists -> no KeyError,
    # gold bug counts as uncovered (FN), not as a phantom pass/fail
    metric, _ = scoring.ScoringPipeline._compute_metrics(
        _P(), [("GHOST", "1")], gold, {})
    assert metric["recall"] == 0.0
    # mixed: one stale + one real FAIL pred -> the real one decides (TP)
    preds = {"FT-01": {"content": "p", "pass": False}}
    metric2, _ = scoring.ScoringPipeline._compute_metrics(
        _P(), [("GHOST", "1"), ("FT-01", "1")], gold, preds)
    assert metric2["recall"] == 1.0


# ---------- P0-3: duplicate-injection detection ----------

def _mk_mutant(root, k, rel, content):
    d = root / f"m{k}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "patch_meta.json").write_text(json.dumps({"file": rel}))
    (d / "new_file.txt").write_text(content)
    (d / "injected.json").write_text(json.dumps({"description": f"m{k}"}))


def test_find_duplicate_mutant(tmp_path):
    app = tmp_path / "WebTestBench_0009"
    _mk_mutant(app, 0, "src/A.tsx", "const x = 1 < 2")
    _mk_mutant(app, 1, "src/A.tsx", "const x = 1 < 2")   # byte-identical to m0
    _mk_mutant(app, 2, "src/A.tsx", "const x = 1 <= 2")  # different content
    _mk_mutant(app, 3, "src/B.tsx", "const x = 1 < 2")   # same content, different file
    assert ml.find_duplicate_mutant(app, 0) is None
    assert ml.find_duplicate_mutant(app, 1) == 0
    assert ml.find_duplicate_mutant(app, 2) is None
    assert ml.find_duplicate_mutant(app, 3) is None
    assert ml.find_duplicate_mutant(app, 9) is None      # missing dir -> None


# ---------- P0-4: cached verdict reuse on resume ----------

def test_injection_sha_and_cached_result_roundtrip(tmp_path):
    app = tmp_path / "WebTestBench_0042"
    _mk_mutant(app, 0, "src/A.tsx", "patched content")
    mdir = app / "m0"
    sha = ml.injection_sha(mdir)
    assert sha == ml.injection_sha(mdir)                  # deterministic
    # matching sha -> verdict reused
    (mdir / "result.json").write_text(json.dumps(
        {"app": "WebTestBench_0042", "k": 0, "caught": True, "validity": "valid",
         "votes": [], "injection_sha": sha}))
    cached = ml.cached_result_ok(mdir)
    assert cached is not None and cached["caught"] is True
    # injection changed after the verdict -> stale, must NOT be reused
    (mdir / "new_file.txt").write_text("DIFFERENT patch")
    assert ml.cached_result_ok(mdir) is None
    # legacy result without injection_sha -> reused (re-deriving would re-roll votes)
    (mdir / "result.json").write_text(json.dumps(
        {"app": "WebTestBench_0042", "k": 0, "caught": False, "validity": "valid", "votes": []}))
    legacy = ml.cached_result_ok(mdir)
    assert legacy is not None and legacy["caught"] is False
    # no result.json -> None
    (mdir / "result.json").unlink()
    assert ml.cached_result_ok(mdir) is None
