from __future__ import annotations

import subprocess
from pathlib import Path

from agent_containers import images
from agent_containers.declaration import Declaration
from agent_containers.declaration import parse
from agent_containers.errors import ToolError
from agent_containers.images import COMPOSE_FILE

WORKFLOW = ".github/workflows/agent-containers.yml"
TOOL_REF = "main"
DEPENDENCY_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements*.txt",
    "build.gradle",
    "settings.gradle",
    "gradle.properties",
    "go.sum",
    "Cargo.lock",
    "Gemfile.lock",
)
DECLARE_FIRST = "the compose file declares no target; add an x-agent block first (README: Declaring targets)"


def default_branch(worktree: Path) -> str | None:
    try:
        repo = images.repo_of(worktree)
    except ToolError:
        repo = None
    if repo:
        answer = images._run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
        if answer.returncode == 0 and answer.stdout.strip():
            return answer.stdout.strip()
    for remote in ("upstream", "origin"):
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip().startswith(f"{remote}/"):
            return result.stdout.strip()[len(remote) + 1 :]
    return None


def dependency_files(worktree: Path) -> list[str]:
    declaration = _declaration(worktree)
    contexts = [worktree, *((worktree / t.build.context).resolve() for t in declaration.targets.values())]
    found: list[str] = []
    for context in dict.fromkeys(contexts):
        matches = {path for pattern in DEPENDENCY_FILES for path in context.glob(pattern) if path.is_file()}
        found.extend(str(path.relative_to(worktree.resolve())) for path in sorted(matches))
    return list(dict.fromkeys(found))


def render(branch: str, targets: list[str], paths: list[str]) -> str:
    return f"""name: Publish agent-containers targets

on:
  push:
    branches: [{branch}]
  workflow_dispatch:

jobs:
  publish:
    strategy:
      matrix:
        target: [{", ".join(targets)}]
    uses: learningequality/agent-containers/.github/workflows/publish.yml@{TOOL_REF}
    with:
      target: ${{{{ matrix.target }}}}
      paths: "{" ".join(paths)}"
      tool-ref: {TOOL_REF}
    permissions:
      contents: read
      packages: write
"""


def write(worktree: Path, branch: str, paths: list[str], *, force: bool = False) -> Path:
    targets = sorted(_declaration(worktree).targets)
    if not targets:
        raise ToolError(DECLARE_FIRST)
    path = worktree / WORKFLOW
    if path.exists() and not force:
        raise ToolError(f"{WORKFLOW} exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(branch, targets, paths))
    return path


def _declaration(worktree: Path) -> Declaration:
    try:
        return parse((worktree / COMPOSE_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise ToolError(f"{worktree / COMPOSE_FILE}: {exc}") from exc
