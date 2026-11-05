import pytest

from agent_containers import config
from agent_containers.errors import ToolError


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "home" / "github" / "kolibri").mkdir(parents=True)
    (tmp_path / "home" / "github" / "studio").mkdir(parents=True)
    return tmp_path / "home"


def test_the_config_lives_under_xdg_config_home(tmp_path):
    assert config.config_path() == tmp_path / "xdg" / "agent-containers" / "config.json"


def test_roots_start_empty():
    assert config.load_roots() == []


def test_added_roots_are_expanded_resolved_and_deduplicated(home):
    config.add_roots(["~/github/kolibri", str(home / "github" / "kolibri" / ".")])
    roots = config.add_roots(["~/github/studio"])
    assert roots == [home / "github" / "kolibri", home / "github" / "studio"]
    assert config.load_roots() == roots


def test_a_root_that_does_not_exist_is_refused():
    with pytest.raises(ToolError, match="nope"):
        config.add_roots(["~/nope"])


def test_removing_a_root_keeps_the_others(home):
    config.add_roots(["~/github/kolibri", "~/github/studio"])
    assert config.remove_roots(["~/github/kolibri"]) == [home / "github" / "studio"]


def test_removing_a_root_that_was_never_added_is_refused():
    config.add_roots(["~/github/kolibri"])
    with pytest.raises(ToolError, match="studio"):
        config.remove_roots(["~/github/studio"])


def test_a_corrupt_config_is_an_error_not_an_empty_list():
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text("{not json")
    with pytest.raises(ToolError, match="config.json"):
        config.load_roots()
