import plistlib
import subprocess

import pytest

from agent_containers import cli
from agent_containers import config
from agent_containers import service


class Recorded:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/opt/tools/bin/agent-containers")
    (tmp_path / "home" / "github" / "kolibri").mkdir(parents=True)
    return tmp_path / "home"


def test_install_on_linux_writes_a_user_unit_and_starts_it(home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    run = Recorded()

    where = service.install(run)

    unit = (home / ".config" / "systemd" / "user" / "agent-containers.service").read_text()
    assert where == str(home / ".config" / "systemd" / "user" / "agent-containers.service")
    assert 'ExecStart="/opt/tools/bin/agent-containers" daemon' in unit
    assert 'Environment="PATH=/opt/tools/bin:/usr/bin"' in unit
    assert ["systemctl", "--user", "enable", "--now", "agent-containers.service"] in run.calls


def test_a_path_with_spaces_percent_or_quotes_survives_systemd_parsing(home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("PATH", '/opt/my tools/bin:/x%y:/q"z\\w')

    service.install(Recorded())

    unit = (home / ".config" / "systemd" / "user" / "agent-containers.service").read_text()
    assert 'Environment="PATH=/opt/my tools/bin:/x%%y:/q\\"z\\\\w"' in unit


def test_install_on_macos_writes_a_launch_agent_and_loads_it(home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    run = Recorded()

    service.install(run)

    path = home / "Library" / "LaunchAgents" / "org.learningequality.agent-containers.plist"
    agent = plistlib.loads(path.read_bytes())
    assert agent["ProgramArguments"] == ["/opt/tools/bin/agent-containers", "daemon"]
    assert agent["EnvironmentVariables"]["PATH"] == "/opt/tools/bin:/usr/bin"
    assert agent["KeepAlive"] is True
    assert ["launchctl", "bootstrap", "gui/501", str(path)] in run.calls


def test_uninstall_on_linux_stops_and_removes_the_unit(home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    service.install(Recorded())
    run = Recorded()

    service.uninstall(run)

    assert ["systemctl", "--user", "disable", "--now", "agent-containers.service"] in run.calls
    assert not (home / ".config" / "systemd" / "user" / "agent-containers.service").exists()


def test_install_prompts_for_roots_until_a_blank_line(home, monkeypatch, capsys):
    answers = iter(["~/github/kolibri", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.service, "install", lambda: "unit")

    assert cli.main(["install"]) == 0

    assert config.load_roots() == [home / "github" / "kolibri"]


def test_install_takes_roots_without_prompting(home, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
    monkeypatch.setattr(cli.service, "install", lambda: "unit")

    assert cli.main(["install", "--root", "~/github/kolibri"]) == 0

    assert config.load_roots() == [home / "github" / "kolibri"]


def test_install_with_no_roots_and_no_terminal_is_refused(home, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.service, "install", lambda: pytest.fail("installed"))

    assert cli.main(["install"]) == 125


def test_roots_add_list_and_remove(home, capsys):
    assert cli.main(["roots", "add", "~/github"]) == 0
    assert cli.main(["roots"]) == 0
    assert capsys.readouterr().out.strip().endswith(str(home / "github"))
    assert cli.main(["roots", "remove", "~/github"]) == 0
    assert config.load_roots() == []
