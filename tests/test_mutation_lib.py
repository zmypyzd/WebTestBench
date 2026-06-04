from prompt import USER_PROMPT


def test_mutation_prompts_registered_and_substitutable():
    gen = USER_PROMPT["mutation_gen"].substitute(
        instruction="x", source="y", fault_class="CS"
    )
    assert "fault class `CS`" in gen
    catch = USER_PROMPT["mutation_catch"].substitute(injected="a", result="b")
    assert "CAUGHT" in catch
