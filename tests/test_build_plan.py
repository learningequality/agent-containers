import json

import pytest

from agent_containers.cli import build_plan
from agent_containers.cli import main
from agent_containers.errors import ToolError


def _compose(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_context_and_dockerfile_resolve_relative_to_the_compose_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compose(
        tmp_path / "deploy" / "docker-compose.yml",
        "services:\n  android:\n    build:\n      context: ../platforms/android\n"
        "      dockerfile: ci/Containerfile\n      target: sdk\n    x-agent: {}\n",
    )

    plan = build_plan(tmp_path / "deploy" / "docker-compose.yml", "android", [])

    assert plan["context"] == "platforms/android"
    assert plan["dockerfile"] == "platforms/android/ci/Containerfile"
    assert plan["stage"] == "sdk"


def test_watched_paths_cover_the_dockerfile_compose_and_extra_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compose(
        tmp_path / "docker-compose.yml", "services:\n  web:\n    build: {context: ., target: dev}\n    x-agent: {}\n"
    )

    plan = build_plan(tmp_path / "docker-compose.yml", "web", ["uv.lock", "pnpm-lock.yaml"])

    assert plan["context"] == "."
    assert set(plan["watched"]) == {"Dockerfile", "docker-compose.yml", "uv.lock", "pnpm-lock.yaml"}


def test_unknown_target_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compose(tmp_path / "docker-compose.yml", "services:\n  web:\n    build: .\n    x-agent: {}\n")
    with pytest.raises(ToolError, match="web"):
        build_plan(tmp_path / "docker-compose.yml", "android", [])


def test_cli_prints_the_plan_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _compose(
        tmp_path / "docker-compose.yml",
        "services:\n  generic:\n    build: {context: images/generic}\n    x-agent: {}\n",
    )

    assert main(["build-plan", "generic", "--paths", "a b"]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["dockerfile"] == "images/generic/Dockerfile"
    assert {"a", "b"} <= set(plan["watched"])
