from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_containers.declaration import Declaration
from agent_containers.declaration import parse
from agent_containers.errors import ToolError

REGISTRY = "ghcr.io"
COMPOSE_FILE = "docker-compose.yml"
TAG = "default"
REFRESH_HINT = "gh auth refresh -s read:packages"
REGISTRY_TOKEN_ENV = "AGENT_CONTAINERS_REGISTRY_TOKEN"
GENERIC_REPO = "learningequality/agent-containers"
GENERIC_TARGET = "generic"

_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
_REFUSED = re.compile(r"denied|unauthorized|forbidden|manifest unknown|\b40[134]\b", re.IGNORECASE)


def _run(argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=input, cwd=cwd, capture_output=True, text=True, errors="replace")


def repo_of(worktree: Path) -> str:
    for remote in ("upstream", "origin"):
        result = _run(["git", "remote", "get-url", remote], cwd=worktree)
        match = _REMOTE.search(result.stdout.strip()) if result.returncode == 0 else None
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    raise ToolError(f"{worktree}: neither upstream nor origin is a GitHub remote; pass --repo owner/name")


def image_ref(repo: str, target: str) -> str:
    return f"{REGISTRY}/{repo.lower()}/{target.lower()}:{TAG}"


def fetch_declaration(repo: str) -> Declaration | None:
    result = _run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{COMPOSE_FILE}"])
    if result.returncode != 0:
        if "(HTTP 404)" in result.stderr:
            visible = _run(["gh", "api", f"repos/{repo}"])
            if visible.returncode == 0:
                return None
            raise ToolError(
                f"the gh token cannot see {repo} ({visible.stderr.strip()}); a private repo answers 404 to a token "
                f"without access, so its declaration cannot be told apart from none"
            )
        raise ToolError(f"cannot read {repo}'s {COMPOSE_FILE} from its default branch: {result.stderr.strip()}")
    try:
        return parse(result.stdout)
    except ValueError as exc:
        raise ToolError(f"{repo}'s {COMPOSE_FILE}: {exc}") from exc


@dataclass(frozen=True)
class Resolved:
    declaration: Declaration
    target: str
    image: str


def resolve(repo: str, target: str | None) -> Resolved:
    declaration = fetch_declaration(repo)
    if declaration is None or not declaration.targets:
        repo, declaration = GENERIC_REPO, fetch_declaration(GENERIC_REPO)
        if declaration is None or GENERIC_TARGET not in declaration.targets:
            raise ToolError(f"{GENERIC_REPO} declares no {GENERIC_TARGET} target on its default branch")
    chosen = select(declaration, target, repo)
    return Resolved(declaration, chosen, image_ref(repo, chosen))


def select(declaration: Declaration, target: str | None, repo: str) -> str:
    declared = sorted(declaration.targets)
    if target is None and len(declared) == 1:
        return declared[0]
    if target is None:
        raise ToolError(f"{repo} declares several targets; name one of: {', '.join(declared)}")
    if target not in declaration.targets:
        raise ToolError(f"{repo} declares no target {target!r} (declared: {', '.join(declared)})")
    return target


def pull(runtime: str, ref: str) -> None:
    token = _token()
    if token is None:
        _check_pull(_run([runtime, "pull", ref]), ref)
        return
    auth_dir = Path(tempfile.mkdtemp(prefix="agent-containers-auth-"))
    try:
        if runtime == "docker":
            authfile = auth_dir / "config.json"
            pull_argv = ["docker", "--config", str(auth_dir), "pull", ref]
        else:
            authfile = auth_dir / "auth.json"
            pull_argv = [runtime, "pull", "--authfile", str(authfile), ref]
        user = "agent-containers" if os.environ.get(REGISTRY_TOKEN_ENV) else _login()
        encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
        descriptor = os.open(authfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as written:
            json.dump({"auths": {REGISTRY: {"auth": encoded}}}, written)
        _check_pull(_run(pull_argv), ref)
    finally:
        shutil.rmtree(auth_dir, ignore_errors=True)


def _token() -> str | None:
    if os.environ.get(REGISTRY_TOKEN_ENV):
        return os.environ[REGISTRY_TOKEN_ENV]
    result = _run(["gh", "auth", "token"])
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _login() -> str:
    result = _run(["gh", "api", "user", "--jq", ".login"])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "agent-containers"


def _check_pull(result: subprocess.CompletedProcess[str], ref: str) -> None:
    if result.returncode == 0:
        return
    error = (result.stderr or result.stdout).strip()
    if _REFUSED.search(error):
        raise ToolError(
            f"cannot pull {ref}: {error}\nIf the image exists, give your token the scope: `{REFRESH_HINT}`, "
            f"or set {REGISTRY_TOKEN_ENV} to a classic token with read:packages"
        )
    raise ToolError(f"cannot pull {ref}: {error}")
