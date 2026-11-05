import subprocess

import pytest
import yaml
from fakes import git as _git

from agent_containers import cli
from agent_containers import init
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.errors import ToolError

COMPOSE = """
services:
  db:
    image: postgres:16
  web:
    build: .
    x-agent: {}
  android:
    build: platforms/android
    x-agent: {}
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "develop")
    _git(tmp_path, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
    _git(tmp_path, "remote", "add", "origin", str(tmp_path))
    _git(tmp_path, "fetch", "-q", "origin")
    _git(tmp_path, "remote", "set-head", "origin", "develop")
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "platforms" / "android").mkdir(parents=True)
    (tmp_path / "platforms" / "android" / "build.gradle").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _workflow(repo):
    return yaml.safe_load((repo / init.WORKFLOW).read_text())


def test_the_default_branch_comes_from_the_remotes_head(repo):
    assert init.default_branch(repo) == "develop"


def test_github_names_the_default_branch_over_a_stale_remote_head(repo, monkeypatch):
    _git(repo, "remote", "set-url", "origin", "https://github.com/learningequality/kolibri-data-portal.git")
    calls = []

    def fake_run(argv, *, input=None, cwd=None):
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "release-v1\n", "")

    monkeypatch.setattr(init.images, "_run", fake_run)

    assert init.default_branch(repo) == "release-v1"
    assert any("repos/learningequality/kolibri-data-portal" in " ".join(argv) for argv in calls)


def test_dependency_files_present_in_the_repo_are_found(repo):
    assert init.dependency_files(repo) == ["pnpm-lock.yaml", "uv.lock", "platforms/android/build.gradle"]


def test_init_writes_a_publish_job_for_every_declared_target(repo, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.main(["init"]) == 0

    workflow = _workflow(repo)
    assert workflow[True]["push"]["branches"] == ["develop"]
    job = workflow["jobs"]["publish"]
    assert job["strategy"]["matrix"]["target"] == ["android", "web"]
    assert job["with"]["paths"] == "pnpm-lock.yaml uv.lock platforms/android/build.gradle"
    assert job["uses"].startswith("learningequality/agent-containers/.github/workflows/publish.yml@")
    assert job["permissions"] == {"contents": "read", "packages": "write"}


def test_init_asks_to_confirm_or_change_the_branch_and_paths(repo, monkeypatch):
    answers = iter(["main", "uv.lock"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli.main(["init"]) == 0

    job = _workflow(repo)["jobs"]["publish"]
    assert _workflow(repo)[True]["push"]["branches"] == ["main"]
    assert job["with"]["paths"] == "uv.lock"


def test_an_empty_answer_keeps_the_detected_value(repo, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli.main(["init"]) == 0

    assert _workflow(repo)[True]["push"]["branches"] == ["develop"]


def test_init_never_overwrites_without_force(repo, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / init.WORKFLOW).write_text("mine\n")

    assert cli.main(["init"]) == EXIT_TOOL_ERROR
    assert (repo / init.WORKFLOW).read_text() == "mine\n"
    assert cli.main(["init", "--force"]) == 0


def test_a_compose_with_no_target_points_at_declaring_one(repo):
    (repo / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")
    with pytest.raises(ToolError, match="Declaring targets"):
        init.write(repo, "develop", [])
