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
    assert ml.should_regenerate(mdir, regen=False) is True   # no injected.json yet
    (mdir / "injected.json").write_text("{}")
    assert ml.should_regenerate(mdir, regen=False) is False  # cached -> reuse
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
