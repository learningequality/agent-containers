import os
import signal
import subprocess

import pytest
from fakes import git as _git

from agent_containers import cli
from agent_containers import images
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.errors import ToolError
from agent_containers.runtime import Runtime


def _failing_gh(argv, *, input=None, cwd=None):
    if argv[0] == "git":
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return subprocess.CompletedProcess(argv, 1, "", "gh: Bad credentials (HTTP 401)")


def test_a_tool_failure_exits_125_with_its_message(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/learningequality/kolibri.git")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(images, "_run", _failing_gh)
    monkeypatch.setattr(cli, "detect", lambda name: Runtime("podman"))

    code = cli.main(["run", "--", "true"])

    assert code == EXIT_TOOL_ERROR
    assert "401" in capsys.readouterr().err


def test_relative_cwd_is_measured_from_the_worktree_root(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "platforms" / "android").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "platforms" / "android")
    assert cli.worktree_and_cwd() == (tmp_path.resolve(), "platforms/android")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_termination_signal_cancels_a_brokered_command_like_ctrl_c(signum):
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        cli.cancel_on_termination()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signum)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def _direct_mode_marker(*args, **kwargs):
    raise ToolError("ran directly")


@pytest.mark.parametrize(
    ("alive", "usable", "mode"),
    [(True, True, "service"), (False, True, "direct"), (False, False, "service")],
)
def test_the_cli_uses_the_service_when_it_answers_or_no_runtime_works(
    tmp_path, monkeypatch, capsys, alive, usable, mode
):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.client, "broker_alive", lambda worktree: alive)
    monkeypatch.setattr(cli, "runtime_usable", lambda name: usable)
    monkeypatch.setattr(cli, "detect", lambda name: Runtime("podman"))
    monkeypatch.setattr(cli, "prepare", _direct_mode_marker)
    monkeypatch.setattr(cli.client, "request", lambda *args, **kwargs: 42)

    code = cli.main(["run", "--", "true"])

    assert (code, "ran directly" in capsys.readouterr().err) == ((42, False) if mode == "service" else (125, True))


def test_with_no_runtime_and_no_service_the_cli_names_the_install(tmp_path, monkeypatch, capsys):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "runtime_usable", lambda name: False)
    monkeypatch.setattr(cli.client, "PICKUP_WAIT", 0.5)

    assert cli.main(["run", "--", "true"]) == EXIT_TOOL_ERROR
    assert "agent-containers install" in capsys.readouterr().err


def test_run_needs_a_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == EXIT_TOOL_ERROR
