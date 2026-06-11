import asyncio
import json
import types

from prompt import USER_PROMPT
import mutation_lib as ml


def test_mutation_prompts_registered_and_substitutable():
    gen = USER_PROMPT["mutation_gen"].substitute(
        instruction="x", source="y", fault_class="CS"
    )
    assert "fault class `CS`" in gen
    catch = USER_PROMPT["mutation_catch"].substitute(injected="a", result="b")
    assert "CAUGHT" in catch
    assert "$" not in gen
    assert "$" not in catch


def test_majority_caught():
    assert ml.majority_caught([True, True, False]) is True
    assert ml.majority_caught([True, False, False]) is False
    assert ml.majority_caught([True]) is True
    assert ml.majority_caught([]) is False


def test_classify_validity():
    assert ml.classify_validity(deploy_ok=False, reachable=True) == "invalid"
    assert ml.classify_validity(deploy_ok=True, reachable=False) == "suspect"
    assert ml.classify_validity(deploy_ok=True, reachable=True) == "valid"


def test_should_regenerate(tmp_path):
    mdir = tmp_path / "m0"
    mdir.mkdir()
    assert ml.should_regenerate(mdir, regen=False) is True   # nothing cached yet
    (mdir / "injected.json").write_text("{}")
    assert ml.should_regenerate(mdir, regen=False) is True   # partial cache -> regenerate
    (mdir / "patch_meta.json").write_text("{}")
    (mdir / "new_file.txt").write_text("x")
    assert ml.should_regenerate(mdir, regen=False) is False  # full cache -> reuse
    assert ml.should_regenerate(mdir, regen=True) is True     # forced


def test_aggregate_denominator_excludes_invalid_and_by_class():
    records = [
        {"fault_class": "CS", "validity": "valid",   "caught": True},
        {"fault_class": "CS", "validity": "valid",   "caught": False},
        {"fault_class": "IX", "validity": "valid",   "caught": True},
        {"fault_class": "FT", "validity": "invalid", "caught": False},  # excluded
        {"fault_class": "FT", "validity": "suspect", "caught": True},   # excluded
    ]
    agg = ml.aggregate(records)
    assert agg["valid"] == 3 and agg["invalid"] == 1 and agg["suspect"] == 1
    assert agg["catch_rate"] == round(2 / 3, 3)          # 2 caught of 3 valid
    assert agg["by_class"]["CS"]["catch_rate"] == 0.5    # 1 of 2
    assert agg["by_class"]["IX"]["catch_rate"] == 1.0    # 1 of 1
    assert "FT" not in agg["by_class"]                   # no valid FT mutants


def test_aggregate_all_invalid_returns_none_rate():
    agg = ml.aggregate([{"fault_class": "CS", "validity": "invalid", "caught": False}])
    assert agg["valid"] == 0 and agg["catch_rate"] is None and agg["by_class"] == {}


def test_copy_app_sources_clones_node_modules_write_isolated(tmp_path):
    # D2: node_modules must be a REAL (cloned/copied) dir, NOT a symlink, so the
    # mutant copy's `npm install` writes copy-on-write/isolated pages that never
    # mutate the shared source tree. On a non-APFS box the real-copy fallback
    # satisfies the identical postconditions.
    src = tmp_path / "app"
    (src / "src").mkdir(parents=True)
    (src / "src" / "App.tsx").write_text("export default 1")
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / "node_modules" / "dep" / "index.js").write_text("// big dep")

    dst = tmp_path / "copy"
    ml.copy_app_sources(src, dst)

    assert (dst / "src" / "App.tsx").read_text() == "export default 1"
    nm = dst / "node_modules"
    assert nm.is_dir() and not nm.is_symlink()                 # real copy, not symlink
    assert (nm / "dep" / "index.js").read_text() == "// big dep"  # same content resolves

    # write-isolation: mutating the dst clone must NOT touch the source tree
    (nm / "dep" / "index.js").write_text("// MUTATED in copy")
    (nm / "newly_installed.js").write_text("// added by npm install")
    assert (src / "node_modules" / "dep" / "index.js").read_text() == "// big dep"
    assert not (src / "node_modules" / "newly_installed.js").exists()


def test_copy_app_sources_falls_back_to_real_copy_on_clonefile_unsupported(tmp_path, monkeypatch):
    # D2 item 9/10: simulate a non-APFS target where `cp -c` returns NON-ZERO (not
    # an exception). The fallback must be a REAL recursive copy (never a symlink),
    # still write-isolated. On an APFS box this branch is otherwise never hit.
    src = tmp_path / "app"
    (src / "src").mkdir(parents=True)
    (src / "src" / "App.tsx").write_text("export default 1")
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / "node_modules" / "dep" / "index.js").write_text("// big dep")
    # a relative self-contained symlink inside node_modules must be preserved
    (src / "node_modules" / ".bin").mkdir()
    try:
        (src / "node_modules" / ".bin" / "tool").symlink_to("../dep/index.js")
    except OSError:
        pass  # platforms without symlink perms: still validate the rest

    real_run = ml.subprocess.run

    def fake_run(cmd, *a, **kw):
        # Only force the clonefile `cp -c -R ...` to "fail"; pass through any others.
        if isinstance(cmd, list) and cmd[:3] == ["cp", "-c", "-R"]:
            return types.SimpleNamespace(returncode=1, stderr="clonefile not supported", stdout="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(ml.subprocess, "run", fake_run)

    dst = tmp_path / "copy"
    ml.copy_app_sources(src, dst)

    nm = dst / "node_modules"
    assert nm.is_dir() and not nm.is_symlink()                  # real copy, not a symlink
    assert (nm / "dep" / "index.js").read_text() == "// big dep"
    # write-isolation still holds via the real-copy fallback
    (nm / "dep" / "index.js").write_text("// MUTATED")
    assert (src / "node_modules" / "dep" / "index.js").read_text() == "// big dep"


def test_copy_app_sources_no_node_modules_skips(tmp_path):
    # apps without node_modules: copy succeeds, dst simply has no node_modules
    # (base_agent's unconditional `npm install` creates it). `cp` of a missing
    # source returns exit 1 which must NOT be misread as clonefile-unsupported.
    src = tmp_path / "app"
    (src / "src").mkdir(parents=True)
    (src / "src" / "App.tsx").write_text("export default 1")

    dst = tmp_path / "copy"
    ml.copy_app_sources(src, dst)
    assert (dst / "src" / "App.tsx").read_text() == "export default 1"
    assert not (dst / "node_modules").exists()


# ---------------------------------------------------------------------------
# D1: independent HTTP judge (MiniMax-M3) — routing + aggregation, fully mocked
# ---------------------------------------------------------------------------

class _Cfg:
    """APIConfig-shaped stand-in (.base_url / .api_key / .model)."""
    def __init__(self, base_url=None, api_key=None, model="MiniMax-M3"):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


def test_use_http_judge_truth_table():
    # None cfg -> False (no AttributeError)
    assert ml.use_http_judge(None) is False
    # cfg with empty/None base_url -> False (key off base_url ONLY)
    assert ml.use_http_judge(_Cfg(base_url=None)) is False
    assert ml.use_http_judge(_Cfg(base_url="")) is False
    # only --judge_model set (default non-None) -> still False
    assert ml.use_http_judge(_Cfg(base_url=None, model="MiniMax-M3")) is False
    # base_url set -> True
    assert ml.use_http_judge(_Cfg(base_url="https://api.example/v1/chat")) is True
    # dict-shaped cfg also supported (defensive)
    assert ml.use_http_judge({"base_url": "https://x"}) is True
    assert ml.use_http_judge({"base_url": None}) is False


def _fake_resp(content_str, status=200):
    class R:
        status_code = status
        def json(self):
            return {"choices": [{"message": {"content": content_str}}]}
    return R()


def test_judge_catch_http_aggregates_majority(monkeypatch):
    # 2 caught=true ballots, 1 caught=false -> majority True
    contents = [
        '```json\n{"caught": true, "matched_item": "EX-01", "reason": "same bug"}\n```',
        '```json\n{"caught": true, "matched_item": "EX-02", "reason": "same bug"}\n```',
        '```json\n{"caught": false, "matched_item": null, "reason": "different"}\n```',
    ]
    seq = iter(contents)

    def fake_post(*a, **kw):
        return _fake_resp(next(seq))

    monkeypatch.setattr(ml.requests, "post", fake_post)
    cfg = _Cfg(base_url="https://api.example/v1/chat", api_key="k", model="MiniMax-M3")
    verdict = asyncio.run(ml.judge_catch_http({"file": "src/x.tsx"}, "# Test Result\nFAIL", cfg))

    assert {"caught", "votes"} <= set(verdict.keys())  # + discarded_unparseable (P0-1)
    assert verdict["caught"] is True
    assert isinstance(verdict["caught"], bool)
    assert len(verdict["votes"]) == 3
    for v in verdict["votes"]:
        assert isinstance(v, dict) and "caught" in v   # parse_catch dicts, never raw strings
    assert sum(1 for v in verdict["votes"] if v["caught"]) == 2


def test_judge_catch_http_minority_not_caught(monkeypatch):
    contents = [
        '{"caught": true, "reason": "x"}',
        '{"caught": false, "reason": "y"}',
        '{"caught": false, "reason": "z"}',
    ]
    seq = iter(contents)
    monkeypatch.setattr(ml.requests, "post", lambda *a, **kw: _fake_resp(next(seq)))
    cfg = _Cfg(base_url="https://api.example/v1/chat", api_key="k")
    verdict = asyncio.run(ml.judge_catch_http({"file": "f"}, "result", cfg))
    assert verdict["caught"] is False           # 1/3 is not a strict majority
    assert len(verdict["votes"]) == 3           # all ballots valid -> all seated
    assert verdict["discarded_unparseable"] == 0


def test_judge_catch_http_malformed_ballot_reballoted_then_discarded(monkeypatch):
    # P0-1: a malformed ballot is RE-CAST; if it stays garbage it is DISCARDED,
    # never seated as a fake 'no' vote (the 0074 m3 false-miss scar).
    contents = [
        '{"caught": true, "reason": "x"}',
        'totally not json',
        '{"caught": true, "reason": "z"}',
        'still not json',     # re-ballot 1 for seat 2
        'nope, garbage',      # re-ballot 2 for seat 2 -> discarded
    ]
    seq = iter(contents)
    monkeypatch.setattr(ml.requests, "post", lambda *a, **kw: _fake_resp(next(seq)))
    cfg = _Cfg(base_url="https://api.example/v1/chat", api_key="k")
    verdict = asyncio.run(ml.judge_catch_http({"file": "f"}, "result", cfg))
    assert verdict["caught"] is True            # majority over 2 VALID seats
    assert len(verdict["votes"]) == 2           # discarded seat does not vote
    assert verdict["discarded_unparseable"] == 1
    assert all(v["caught"] is True for v in verdict["votes"])


def test_judge_catch_http_total_failure_counts_as_no_catch(monkeypatch):
    # every vote's HTTP fully fails (post raises) -> after retries each ballot is a
    # conservative caught=False; len(votes) MUST still equal vote count (no phantom).
    def boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(ml.requests, "post", boom)
    # keep retries fast: patch time.sleep used inside judge_catch_http
    monkeypatch.setattr(ml.time, "sleep", lambda *a, **kw: None)
    cfg = _Cfg(base_url="https://api.example/v1/chat", api_key="k")
    verdict = asyncio.run(ml.judge_catch_http({"file": "f"}, "result", cfg))
    assert verdict["caught"] is False           # conservative: zero valid seats
    assert verdict["votes"] == []               # P0-1: dead ballots never seated
    assert verdict["discarded_unparseable"] == 3


def test_judge_catch_http_non200_counts_as_no_catch(monkeypatch):
    # HTTP 500 / missing choices -> retries exhausted -> conservative no-catch ballot
    def http_500(*a, **kw):
        class R:
            status_code = 500
            def json(self):
                return {"error": "server"}
        return R()

    monkeypatch.setattr(ml.requests, "post", http_500)
    monkeypatch.setattr(ml.time, "sleep", lambda *a, **kw: None)
    cfg = _Cfg(base_url="https://api.example/v1/chat", api_key="k")
    verdict = asyncio.run(ml.judge_catch_http({"file": "f"}, "result", cfg))
    assert verdict["caught"] is False
    assert verdict["votes"] == []               # P0-1: dead ballots never seated
    assert verdict["discarded_unparseable"] == 3


def test_parse_injection_extracts_record_and_file():
    md = '''Here you go:
```json
{"description": "total wrong", "file": "src/Cart.tsx", "fault_class": "CS", "repro_steps": "1. add item"}
```
```file:src/Cart.tsx
export const x = 2
```
'''
    rec, path, content = ml.parse_injection(md)
    assert rec["fault_class"] == "CS"
    assert rec["file"] == "src/Cart.tsx"
    assert path == "src/Cart.tsx"
    assert content.strip() == "export const x = 2"


def test_parse_catch_reads_verdict():
    md = 'verdict:\n```json\n{"caught": true, "matched_item": "EX-01", "reason": "same bug"}\n```'
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "EX-01"


def test_parse_catch_defaults_false_on_garbage():
    assert ml.parse_catch("no json here").get("caught") is False


def test_parse_catch_defaults_false_on_broken_json():
    # a "{...}" containing "caught" is matched but is not valid JSON -> fail safe
    v = ml.parse_catch('{"caught": true, "reason": broken}')
    assert v["caught"] is False


def test_parse_catch_handles_none_and_empty():
    # judge_catch_http feeds '' (or None) on total HTTP failure -> must not raise
    assert ml.parse_catch("").get("caught") is False
    assert ml.parse_catch(None).get("caught") is False


def test_parse_catch_fenced_block_with_braces_in_reason():
    # MiniMax-M3 reasoning verdicts may put braces in the reason prose. The bare
    # brace regex `{[^{}]*"caught"[^{}]*}` would fail on the inner braces; a fenced
    # ```json block must be tried first so a genuine catch is not dropped.
    md = (
        'verdict:\n```json\n'
        '{"caught": true, "matched_item": "EX-01", "reason": "fails when {count} shown"}\n'
        '```'
    )
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "EX-01"


def test_parse_catch_unfenced_verdict_with_braces_in_reason():
    # THE actual 0070-m1 failure: MiniMax-M3 with reasoning OFF emits a RAW JSON
    # verdict with NO ```fence``` and braces in the reason prose. The bare-brace
    # regex cannot span the inner braces, so a genuine catch was dropped as
    # "unparseable verdict". A balanced-brace scan must recover it.
    md = '{"caught": true, "matched_item": "CS-04", "reason": "fires when {votes} increments"}'
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "CS-04"


def test_parse_catch_unfenced_verdict_with_unbalanced_brace_in_string():
    # A naive brace-depth counter breaks if a string literal holds a lone brace.
    # The scanner must ignore braces INSIDE JSON string values.
    md = '{"caught": true, "matched_item": "IX-02", "reason": "use a } to close it"}'
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "IX-02"


def test_parse_catch_unfenced_verdict_with_leading_prose():
    # Leading prose before a raw (unfenced) JSON verdict must still parse.
    md = 'My verdict:\n{"caught": true, "matched_item": "FT-01", "reason": "matches {x}"}'
    v = ml.parse_catch(md)
    assert v["caught"] is True
    assert v["matched_item"] == "FT-01"


def test_is_rate_limited_detects_429_status():
    # Shape taken from a real claude_agent_sdk event log line (dd3r incident
    # 2026-06-11: 22 mutants starved by CLI-quota 429s were mislabeled invalid).
    log = '"errors": null,\n      "api_error_status": 429,\n      "uuid": "e5e7"'
    assert ml.is_rate_limited(log) is True


def test_is_rate_limited_ignores_clean_and_empty_logs():
    assert ml.is_rate_limited('"api_error_status": null') is False
    assert ml.is_rate_limited("plain text mentioning 429 elsewhere") is False
    assert ml.is_rate_limited("") is False
    assert ml.is_rate_limited(None) is False
