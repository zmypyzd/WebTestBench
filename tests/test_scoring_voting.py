"""Unit tests for matcher-voting aggregation (union tau=1).

These tests exercise the PURE, network-free function `aggregate_ballots`
imported top-level as `from scoring import aggregate_ballots` (tests/conftest.py
puts eval/ on sys.path). They must NOT construct ScoringPipeline, hit any API,
or read the dataset.

Contract under test (APPROVED design — Matcher Voting, union tau=1):
- A (pred -> gold) match counts if it appears in >= 1 of K ballots.
- None is ABSENCE of a vote, not a competing vote: a single non-None gold vote
  beats any number of None ballots for the same pred.
- If a pred matched DIFFERENT gold ids across ballots, keep the MOST FREQUENT
  (mode); ties broken by SMALLEST gold id (numeric-aware, deterministic).
- Exactly one (pred_id, gold_id|None) row per distinct pred_id, in predicted
  order (the pred order passed in, else first-appearance across ballots).
- gold_id is normalized to str before counting/comparison; None stays None.
- empty input -> []; K=1 single ballot -> identity.
"""

import json

import pytest

import scoring
from scoring import ScoringPipeline, aggregate_ballots


def _golds(rows):
    """Helper: map pred_id -> gold_id from a list of (pred, gold) rows."""
    return {p: g for p, g in rows}


def test_empty_input_returns_empty_list():
    assert aggregate_ballots([]) == []


def test_k1_identity_all_str_ids_including_none_row():
    """With a single ballot, aggregate == that ballot exactly (order + None rows)."""
    ballot = [("p1", "g1"), ("p2", None), ("p3", "g2")]
    out = aggregate_ballots([ballot])
    # modulo tuple/list: normalize to tuples for comparison
    assert [tuple(r) for r in out] == [("p1", "g1"), ("p2", None), ("p3", "g2")]


def test_k1_identity_numeric_str_ids():
    ballot = [("p1", "3"), ("p2", "10"), ("p3", None)]
    out = aggregate_ballots([ballot])
    assert [tuple(r) for r in out] == [("p1", "3"), ("p2", "10"), ("p3", None)]


def test_union_recovers_match_present_in_only_one_of_three_ballots():
    """THE load-bearing case: a 1-of-3 real match beats 2-of-3 None."""
    b1 = [("p1", None)]
    b2 = [("p1", "5")]
    b3 = [("p1", None)]
    out = aggregate_ballots([b1, b2, b3])
    assert _golds(out) == {"p1": "5"}


def test_gold_beats_two_nones_explicit():
    """[gold] (1 vote) wins over 2x None — None is not a competing vote."""
    ballots = [[("p1", "7")], [("p1", None)], [("p1", None)]]
    out = aggregate_ballots(ballots)
    assert [tuple(r) for r in out] == [("p1", "7")]


def test_pred_with_zero_nonnone_votes_emits_none():
    """A pred with ZERO non-None gold votes across all ballots -> (pred, None)."""
    ballots = [[("p1", None)], [("p1", None)], [("p1", None)]]
    out = aggregate_ballots(ballots)
    assert [tuple(r) for r in out] == [("p1", None)]


def test_mode_resolves_multi_gold_collision_to_one_row():
    """pred matched g1, g2, g1 across 3 ballots -> one row, gold=g1 (mode)."""
    b1 = [("p1", "1")]
    b2 = [("p1", "2")]
    b3 = [("p1", "1")]
    out = aggregate_ballots([b1, b2, b3])
    assert [tuple(r) for r in out] == [("p1", "1")]
    # exactly one row for the pred
    assert sum(1 for p, _ in out if p == "p1") == 1


def test_tie_breaks_to_smallest_numeric_gold_id_2_beats_10():
    """Tie -> smallest gold id, numeric-aware: 2 < 10 (NOT lexical '10' < '2')."""
    b1 = [("p1", "2")]
    b2 = [("p1", "10")]
    out = aggregate_ballots([b1, b2])
    assert _golds(out) == {"p1": "2"}


def test_tie_breaks_to_smallest_numeric_gold_id_5_beats_12():
    b1 = [("p1", "5")]
    b2 = [("p1", "12")]
    out = aggregate_ballots([b1, b2])
    assert _golds(out) == {"p1": "5"}


def test_tie_break_is_order_independent_under_ballot_shuffle():
    """Same output regardless of ballot order (deterministic tie-break)."""
    a = [[("p1", "2")], [("p1", "10")]]
    b = [[("p1", "10")], [("p1", "2")]]
    assert _golds(aggregate_ballots(a)) == _golds(aggregate_ballots(b)) == {"p1": "2"}


def test_string_gold_ids_tie_break_deterministic():
    """Non-numeric ids (EX-NN/FT-NN) still tie-break deterministically."""
    b1 = [("p1", "FT-02")]
    b2 = [("p1", "FT-01")]
    out = aggregate_ballots([b1, b2])
    # both appear once; deterministic smallest by the numeric-aware key
    # ('FT-01' < 'FT-02' lexically among non-digit ids)
    assert _golds(out) == {"p1": "FT-01"}


def test_mixed_int_and_str_gold_ids_collapse_to_one_vote():
    """int 3 and str '3' for the SAME pred must collapse to ONE vote (str-coerced)."""
    b1 = [("p1", 3)]
    b2 = [("p1", "3")]
    b3 = [("p1", "7")]
    out = aggregate_ballots([b1, b2, b3])
    # int 3 + str '3' = 2 votes for '3' > 1 vote for '7'
    assert _golds(out) == {"p1": "3"}
    # result gold id is the canonical str type
    assert isinstance(out[0][1], str)


def test_mixed_int_str_does_not_raise_typeerror_on_tiebreak():
    """min/sorted over mixed [int,str] candidates must not raise (str-coerced)."""
    b1 = [("p1", 2)]
    b2 = [("p1", "10")]
    out = aggregate_ballots([b1, b2])
    assert _golds(out) == {"p1": "2"}


def test_per_pred_uniqueness_g1_g2_g1_one_row():
    """g1/g2/g1 across 3 ballots -> exactly one row, gold=g1; never two rows."""
    ballots = [[("p1", "g1")], [("p1", "g2")], [("p1", "g1")]]
    out = aggregate_ballots(ballots)
    assert len([r for r in out if r[0] == "p1"]) == 1
    assert _golds(out) == {"p1": "g1"}


def test_predicted_order_preserved_from_pred_order_arg():
    """When pred order is supplied, rows follow it exactly (each pred once)."""
    ballots = [[("p3", "g3"), ("p1", "g1")], [("p2", None)]]
    out = aggregate_ballots(ballots, pred_order=["p1", "p2", "p3"])
    assert [p for p, _ in out] == ["p1", "p2", "p3"]
    assert _golds(out) == {"p1": "g1", "p2": None, "p3": "g3"}


def test_pred_order_arg_includes_pred_omitted_by_all_ballots():
    """A pred in pred_order but absent from every ballot -> (pred, None)."""
    ballots = [[("p1", "g1")]]
    out = aggregate_ballots(ballots, pred_order=["p1", "p2"])
    assert [tuple(r) for r in out] == [("p1", "g1"), ("p2", None)]


def test_predicted_order_first_appearance_when_no_pred_order():
    """Without pred_order, rows follow first-appearance across concatenated ballots."""
    ballots = [[("p2", "g2"), ("p1", "g1")], [("p3", "g3")]]
    out = aggregate_ballots(ballots)
    assert [p for p, _ in out] == ["p2", "p1", "p3"]


def test_all_ballots_empty_returns_empty_list():
    """Ballots that are all valid-empty [] -> aggregate returns []."""
    out = aggregate_ballots([[], [], []])
    assert out == []


def test_valid_empty_ballot_among_others_does_not_remove_preds():
    """A valid-empty [] ballot contributes nothing but does not veto matches."""
    ballots = [[("p1", "g1")], []]
    out = aggregate_ballots(ballots)
    assert _golds(out) == {"p1": "g1"}


def test_malformed_three_tuple_row_does_not_crash():
    """A stray 3-element row must not crash aggregation (filtered/defended)."""
    ballots = [[("p1", "g1", "extra"), ("p2", "g2")]]
    out = aggregate_ballots(ballots)
    # p2 survives; p1 (malformed) is dropped, never produces a 3-element output row
    assert _golds(out) == {"p2": "g2"}
    assert all(len(r) == 2 for r in out)


def test_output_rows_are_two_element_pairs():
    ballots = [[("p1", "g1"), ("p2", None)]]
    out = aggregate_ballots(ballots)
    assert all(len(tuple(r)) == 2 for r in out)


def test_intra_ballot_duplicate_pred_counts_once():
    """A pred appearing twice in ONE ballot must not outvote three other ballots.

    Per the one-to-one prompt rule a ballot maps a pred to at most one gold;
    if a flaky ballot repeats it, the single ballot must count once for that gold.
    """
    # One ballot says g1 twice; three ballots say g2 once each.
    b_dup = [("p1", "g1"), ("p1", "g1")]
    out = aggregate_ballots([b_dup, [("p1", "g2")], [("p1", "g2")], [("p1", "g2")]])
    # g2 has 3 votes, g1 has 1 (deduped) -> g2 wins
    assert _golds(out) == {"p1": "g2"}


def test_none_never_coerced_to_string_none():
    """None must stay the value None, never the literal string 'None'."""
    out = aggregate_ballots([[("p1", None)]])
    assert out[0][1] is None


# ---------------------------------------------------------------------------
# _get_matches integration tests (no network): a scripted _call_api drives
# _match_once; we assert the no-veto rule, all-fail -> None, votes-aware cache,
# and the empty-pred short-circuit. These build a bare pipeline via object.__new__
# so no dataset / APIConfig / network is touched.
# ---------------------------------------------------------------------------


def _bare_pipeline(match_votes=1):
    p = object.__new__(ScoringPipeline)
    p.match_votes = max(1, int(match_votes))
    p.canonicalize = True
    return p


class _ScriptedCalls:
    """Returns one (success, answer, message) per call from a fixed script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, prompt, retry=5):
        self.calls += 1
        if not self.script:
            return (False, None, None)
        return self.script.pop(0)


GOLD = {"1": {"content": "g one", "pass": False, "class": "FT"}}
PRED = {"p1": {"content": "pred one", "pass": False}}


def test_get_matches_all_ballots_fail_returns_none(tmp_path):
    """All K ballots fail -> _get_matches returns None (empty_match path)."""
    p = _bare_pipeline(match_votes=3)
    # Every _call_api fails; _match_once exhausts retry and returns None x3.
    p._call_api = _ScriptedCalls([(False, None, None)] * 99)
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=2,
    )
    assert out is None
    # No cache should be written on total failure.
    assert not (tmp_path / "score_match_ids.json").exists()


def test_get_matches_single_survivor_among_failures_no_veto(tmp_path):
    """One surviving ballot among K-1 failed ballots still yields its matches."""
    p = _bare_pipeline(match_votes=3)
    # Ballot 1: parse fails twice (retry budget=2) -> None ballot, DROPPED.
    # Ballot 2: succeeds with a real match.
    # Ballot 3: parse fails twice -> None ballot, DROPPED.
    p._call_api = _ScriptedCalls([
        (True, "garbage not a list", None),
        (True, "garbage not a list", None),
        (True, "[('p1', '1')]", None),
        (True, "garbage not a list", None),
        (True, "garbage not a list", None),
    ])
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=2,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]
    cache = json.loads((tmp_path / "score_match_ids.json").read_text())
    assert cache["votes"] == 3
    assert [tuple(r) for r in cache["matches"]] == [("p1", "1")]
    assert "ballots" in cache


def test_get_matches_union_recovers_one_of_three(tmp_path):
    """1-of-3 real match beats 2-of-3 None across surviving ballots."""
    p = _bare_pipeline(match_votes=3)
    p._call_api = _ScriptedCalls([
        (True, "[('p1', None)]", None),
        (True, "[('p1', '1')]", None),
        (True, "[('p1', None)]", None),
    ])
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]


def test_get_matches_valid_empty_ballot_kept_not_dropped(tmp_path):
    """With NON-empty preds, a valid-empty [] LLM reply is a SURVIVOR (not a
    parse failure): both ballots are cast and aggregate to a single (pred, None)
    row for the uncovered prediction. (Empty PREDS short-circuit before any
    ballot is cast — that path is covered separately.)"""
    p = _bare_pipeline(match_votes=2)
    p._call_api = _ScriptedCalls([
        (True, "[]", None),
        (True, "[]", None),
    ])
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    # Both ballots survived (valid-empty, not dropped); p1 is uncovered -> None.
    assert [tuple(r) for r in out] == [("p1", None)]
    cache = json.loads((tmp_path / "score_match_ids.json").read_text())
    assert [tuple(r) for r in cache["matches"]] == [("p1", None)]
    assert cache["votes"] == 2


def test_get_matches_cache_reused_when_source_and_votes_match(tmp_path):
    """Cache hit when stored source AND votes match -> no _call_api invoked."""
    cache = {
        "matches": [["p1", "1"]],
        "detailed_matches": [],
        "source": "result",
        "votes": 2,
    }
    (tmp_path / "score_match_ids.json").write_text(json.dumps(cache))
    p = _bare_pipeline(match_votes=2)
    sentinel = _ScriptedCalls([])  # any call would 0-script -> fail
    p._call_api = sentinel
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]
    assert sentinel.calls == 0  # cache short-circuit, no API calls


def test_get_matches_cache_invalidated_on_votes_mismatch(tmp_path):
    """Legacy cache (votes defaulting to 1) is invalidated under K>1 -> rematch."""
    legacy = {
        "matches": [["p1", "1"]],
        "detailed_matches": [],
        "source": "result",
        # NOTE: no 'votes' key -> defaults to 1
    }
    (tmp_path / "score_match_ids.json").write_text(json.dumps(legacy))
    p = _bare_pipeline(match_votes=3)
    scripted = _ScriptedCalls([
        (True, "[('p1', '1')]", None),
        (True, "[('p1', '1')]", None),
        (True, "[('p1', '1')]", None),
    ])
    p._call_api = scripted
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]
    assert scripted.calls == 3  # rematched, did NOT reuse legacy cache
    cache = json.loads((tmp_path / "score_match_ids.json").read_text())
    assert cache["votes"] == 3


def test_get_matches_legacy_cache_reused_at_k1(tmp_path):
    """Legacy cache lacking 'votes' is reused at default K=1 with zero recompute."""
    legacy = {
        "matches": [["p1", "1"]],
        "detailed_matches": [],
        "source": "result",
    }
    (tmp_path / "score_match_ids.json").write_text(json.dumps(legacy))
    p = _bare_pipeline(match_votes=1)
    sentinel = _ScriptedCalls([])
    p._call_api = sentinel
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]
    assert sentinel.calls == 0


def test_get_matches_empty_cache_matching_votes_and_empty_preds_short_circuits(tmp_path):
    """Empty matches cache + matching source/votes + empty preds -> return [] (no API)."""
    cache = {"matches": [], "detailed_matches": [], "source": "result", "votes": 2}
    (tmp_path / "score_match_ids.json").write_text(json.dumps(cache))
    p = _bare_pipeline(match_votes=2)
    sentinel = _ScriptedCalls([])
    p._call_api = sentinel
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items={},
        output_dir=tmp_path, source="result", retry=1,
    )
    assert out == []
    assert sentinel.calls == 0  # did NOT burn K calls on empty-pred record


def test_get_matches_empty_preds_no_cache_short_circuits_without_api(tmp_path):
    """Empty preds with NO cache -> return [] and write an empty cache WITHOUT
    burning any matcher calls. Each ballot is a real (~60-80s MiniMax) call that
    can only return []; the result-source empty-pred case is handled upstream in
    _process_record, so this guards the surviving checklist-fallback path
    (missing result + --use_checklist_fallback + checklist.md parsed to 0 items).
    """
    p = _bare_pipeline(match_votes=3)
    sentinel = _ScriptedCalls([])  # any call -> (False, None, None)
    p._call_api = sentinel
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items={},
        output_dir=tmp_path, source="result", retry=2,
    )
    assert out == []
    assert sentinel.calls == 0  # the optimization: zero API calls on empty preds
    # Empty artifact persisted + self-consistent with the empty-cache reuse path.
    cache = json.loads((tmp_path / "score_match_ids.json").read_text())
    assert cache["matches"] == []
    assert cache["votes"] == 3


def test_get_matches_k1_uses_one_match_once_path(tmp_path):
    """K=1 makes exactly one ballot path and aggregate is identity for it."""
    p = _bare_pipeline(match_votes=1)
    scripted = _ScriptedCalls([(True, "[('p1', '1')]", None)])
    p._call_api = scripted
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=3,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]
    assert scripted.calls == 1  # one successful call, no extra ballots


def test_get_matches_malformed_ballot_dropped_as_parse_failure(tmp_path):
    """A non-list reply is a parse failure -> ballot None -> dropped (no crash)."""
    p = _bare_pipeline(match_votes=2)
    p._call_api = _ScriptedCalls([
        (True, "{'p1': '1'}", None),   # dict, not list -> parse failure, retry
        (True, "{'p1': '1'}", None),
        (True, "[('p1', '1')]", None),  # second ballot succeeds
    ])
    out = p._get_matches(
        instruction="i", gold_items=GOLD, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=2,
    )
    assert [tuple(r) for r in out] == [("p1", "1")]


def test_detailed_matches_no_pred_under_two_gold_keys(tmp_path):
    """g1/g2/g1 across ballots -> pred under exactly one gold block, not two."""
    gold = {
        "1": {"content": "g one", "pass": False, "class": "FT"},
        "2": {"content": "g two", "pass": False, "class": "FT"},
    }
    p = _bare_pipeline(match_votes=3)
    p._call_api = _ScriptedCalls([
        (True, "[('p1', '1')]", None),
        (True, "[('p1', '2')]", None),
        (True, "[('p1', '1')]", None),
    ])
    p._get_matches(
        instruction="i", gold_items=gold, pred_items=PRED,
        output_dir=tmp_path, source="result", retry=1,
    )
    detailed = json.loads((tmp_path / "score_match_ids.json").read_text())["detailed_matches"]
    # Count gold blocks where p1 appears as a pred.
    blocks_with_p1 = 0
    for block in detailed:
        preds = block.get("pred") or []
        if any(pr["id"] == "p1" for pr in preds):
            blocks_with_p1 += 1
    assert blocks_with_p1 == 1
