import subprocess
import urllib.request

import pytest

from agent_containers import cli
from agent_containers import runtime

pytestmark = pytest.mark.live

COMPOSE = """
services:
  app:
    build: {context: ., dockerfile: Containerfile}
    working_dir: /workspace
    x-agent:
      commands: {serve: "python3 -m http.server 8000"}
      ports: {"8000": {env: APP_PORT}}
      ready: {path: /, port: 8000, timeout: 60}
"""


@pytest.fixture
def repo(tmp_path, monkeypatch, namespace):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / "Containerfile").write_text(f"FROM {runtime.GATEWAY_IMAGE}\nWORKDIR /workspace\n")
    (tmp_path / "platforms" / "android").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(runtime.NAMESPACE_ENV, namespace)
    return tmp_path


def test_run_exits_with_the_commands_code(rt, repo):
    assert cli.main(["run", "--local", "--runtime", rt.name, "--", "sh", "-c", "exit 7"]) == 7


def test_run_from_a_subdirectory_runs_in_the_same_one_inside(rt, repo, monkeypatch, capfd):
    monkeypatch.chdir(repo / "platforms" / "android")
    assert cli.main(["run", "--local", "--runtime", rt.name, "--", "pwd"]) == 0
    assert capfd.readouterr().out.strip().endswith("/workspace/platforms/android")


def test_serve_prints_a_url_that_answers_and_a_second_serve_reattaches(rt, repo, capfd):
    assert cli.main(["serve", "--local", "--runtime", rt.name]) == 0
    first = capfd.readouterr().out
    url = next(line for line in first.split() if line.startswith("http://"))
    assert urllib.request.urlopen(url, timeout=10).status == 200
    assert ".agent-containers/app/serve.log" in first

    assert cli.main(["serve", "--local", "--runtime", rt.name]) == 0
    assert url in capfd.readouterr().out


def test_stop_then_status_shows_nothing_running(rt, repo, capfd):
    assert cli.main(["run", "--local", "--runtime", rt.name, "--", "true"]) == 0
    assert cli.main(["status", "--runtime", rt.name]) == 0
    assert "app" in capfd.readouterr().out
    assert cli.main(["stop", "--runtime", rt.name, "--purge"]) == 0
    assert cli.main(["status", "--runtime", rt.name]) == 0
    assert "app" not in capfd.readouterr().out
