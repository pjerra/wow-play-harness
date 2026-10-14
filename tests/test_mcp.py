from wowharness.client import ClientNotRunning
from wowharness.mcp import config_path_from_argv, guarded


def test_config_path_from_argv_separate_value():
    assert config_path_from_argv(["--config", "C:\\a.json"]) == "C:\\a.json"


def test_config_path_from_argv_equals_form():
    assert config_path_from_argv(["--config=C:\\b.json"]) == "C:\\b.json"


def test_config_path_from_argv_absent():
    assert config_path_from_argv([]) is None


def test_guarded_turns_dead_client_into_error():
    @guarded
    def tool():
        raise ClientNotRunning("client not running (exit code 1)")
    assert tool() == {"ok": False, "error": "client not running (exit code 1)"}


def test_guarded_passes_results():
    @guarded
    def tool():
        return {"x": 1}
    assert tool() == {"ok": True, "x": 1}
