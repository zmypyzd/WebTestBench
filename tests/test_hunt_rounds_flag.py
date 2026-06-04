import tempfile
from agent.base_agent import APIConfig
from agent.claude_code import ClaudeCodeWebTester


def _make(**kw):
    with tempfile.TemporaryDirectory() as d:
        return ClaudeCodeWebTester(
            instruction="x",
            api_config=APIConfig(base_url="u", api_key="k", model="m"),
            server_url="http://localhost:6006",
            output_dir=d,
            **kw,
        )


def test_hunt_rounds_defaults_to_3():
    agent = _make()
    assert agent.hunt_rounds == 3


def test_hunt_rounds_override_zero():
    agent = _make(hunt_rounds=0)
    assert agent.hunt_rounds == 0


def test_bugs_path_is_under_output_dir():
    agent = _make()
    assert agent.bugs_path.name == "BUGS.md"
