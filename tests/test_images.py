import base64
import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from agent_containers import images
from agent_containers.errors import ToolError

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "ghp_not_a_real_token"


class FakeRun:
    def __init__(
        self,
        *,
        compose: str = "",
        token: str | None = TOKEN,
        pull_error: str = "",
        api_error: str = "",
        composes: dict[str, str] | None = None,
        readable: set[str] | None = None,
    ):
        self.compose = compose
        self.composes = composes or {}
        self.readable = readable or set()
        self.token = token
        self.pull_error = pull_error
        self.api_error = api_error
        self.calls: list[tuple[list[str], str | None]] = []
        self.auth_dirs_during: list[bool] = []
        self.auth_at_pull: dict | None = None

    def __call__(self, argv, *, input=None, cwd=None):
        self.calls.append((list(argv), input))
        auth = _auth_dir(argv)
        if auth is not None:
            self.auth_dirs_during.append(auth.exists())
            files = list(auth.glob("*.json"))
            if "pull" in argv and files:
                self.auth_at_pull = json.loads(files[0].read_text())
        if argv[:3] == ["gh", "auth", "token"]:
            return _done(argv, 0 if self.token else 1, self.token or "", "" if self.token else "not logged in")
        if argv[:3] == ["gh", "api", "user"]:
            return _done(argv, 0, "rtibblesbot\n")
        if argv[:2] == ["gh", "api"] and argv[-1].count("/") == 2:
            readable = argv[-1].removeprefix("repos/") in self.readable
            if readable:
                return _done(argv, 0, "{}")
            return _done(argv, 1, "", "gh: Not Found (HTTP 404)")
        if argv[:2] == ["gh", "api"]:
            for repo, compose in self.composes.items():
                if f"repos/{repo}/" in argv[-1]:
                    if compose.startswith("gh: "):
                        return _done(argv, 1, "", compose)
                    return _done(argv, 0, compose)
            if self.api_error:
                return _done(argv, 1, "", self.api_error)
            return _done(argv, 0, self.compose)
        if "pull" in argv and self.pull_error:
            return _done(argv, 125, "", self.pull_error)
        return _done(argv, 0, "")

    def runtime_calls(self):
        return [(argv, stdin) for argv, stdin in self.calls if argv[0] != "gh"]


def _done(argv, code, out, err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


def _auth_dir(argv):
    for flag in ("--config", "--authfile"):
        if flag in argv:
            path = Path(argv[argv.index(flag) + 1])
            return path if flag == "--config" else path.parent
    return None


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repo_prefers_upstream_then_origin(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "git@github.com:rtibblesbot/kolibri-data-portal.git")
    assert images.repo_of(tmp_path) == "rtibblesbot/kolibri-data-portal"
    _git(tmp_path, "remote", "add", "upstream", "https://github.com/learningequality/kolibri-data-portal")
    assert images.repo_of(tmp_path) == "learningequality/kolibri-data-portal"


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:learningequality/kolibri.git",
        "https://github.com/learningequality/kolibri.git",
        "ssh://git@github.com/learningequality/kolibri",
        "https://github.com/learningequality/kolibri/",
    ],
)
def test_repo_parses_each_remote_form(tmp_path, url):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", url)
    assert images.repo_of(tmp_path) == "learningequality/kolibri"


def test_repo_without_a_github_remote_fails(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://gitlab.com/someone/thing.git")
    with pytest.raises(ToolError, match="--repo"):
        images.repo_of(tmp_path)


def test_declaration_comes_from_the_default_branch_not_the_worktree(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n    x-agent:\n      hosts: [evil.example]\n"
    )
    monkeypatch.chdir(tmp_path)
    fake = FakeRun(compose=(FIXTURES / "kdp-compose.yml").read_text())
    monkeypatch.setattr(images, "_run", fake)

    declaration = images.resolve("learningequality/kolibri-data-portal", "web").declaration

    assert "evil.example" not in declaration.targets["web"].hosts
    assert "registry.npmjs.org" in declaration.targets["web"].hosts


def test_reference_derives_only_from_repo_and_target(monkeypatch):
    compose = "services:\n  web:\n    image: evil.example/web:latest\n    build: .\n    x-agent: {}\n"
    monkeypatch.setattr(images, "_run", FakeRun(compose=compose))

    ref = images.resolve("LearningEquality/Kolibri-Data-Portal", "web").image

    assert ref == "ghcr.io/learningequality/kolibri-data-portal/web:default"


def test_unknown_target_names_the_declared_ones(monkeypatch):
    monkeypatch.setattr(images, "_run", FakeRun(compose=(FIXTURES / "kdp-compose.yml").read_text()))
    with pytest.raises(ToolError, match="web"):
        images.resolve("learningequality/kolibri-data-portal", "android")


def test_unreadable_declaration_fails(monkeypatch):
    monkeypatch.setattr(images, "_run", FakeRun(api_error="gh: Bad credentials (HTTP 401)"))
    with pytest.raises(ToolError, match="401"):
        images.resolve("learningequality/kolibri-data-portal", "web")


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_the_token_reaches_the_runtime_only_through_a_temporary_auth_file(monkeypatch, runtime):
    monkeypatch.delenv(images.REGISTRY_TOKEN_ENV, raising=False)
    fake = FakeRun()
    monkeypatch.setattr(images, "_run", fake)

    images.pull(runtime, "ghcr.io/learningequality/kolibri-data-portal/web:default")

    calls = fake.runtime_calls()
    assert all(TOKEN not in " ".join(argv) and stdin != TOKEN for argv, stdin in calls)
    assert [argv for argv, _ in calls if "login" in argv] == []
    assert fake.auth_at_pull is not None
    encoded = fake.auth_at_pull["auths"]["ghcr.io"]["auth"]
    assert base64.b64decode(encoded).decode().endswith(f":{TOKEN}")


def test_a_registry_token_in_the_environment_takes_over_from_gh(monkeypatch):
    monkeypatch.setenv(images.REGISTRY_TOKEN_ENV, "ghp_packages_only")
    fake = FakeRun()
    monkeypatch.setattr(images, "_run", fake)

    images.pull("podman", "ghcr.io/learningequality/kolibri-data-portal/web:default")

    assert ["gh", "auth", "token"] not in [argv for argv, _ in fake.calls]
    encoded = fake.auth_at_pull["auths"]["ghcr.io"]["auth"]
    assert base64.b64decode(encoded).decode().endswith(":ghp_packages_only")


@pytest.mark.parametrize("runtime", ["podman", "docker"])
@pytest.mark.parametrize("pull_error", ["", "Error: reading manifest default: denied"])
def test_temporary_auth_config_is_gone_after_a_pull(monkeypatch, runtime, pull_error):
    monkeypatch.delenv(images.REGISTRY_TOKEN_ENV, raising=False)
    fake = FakeRun(pull_error=pull_error)
    monkeypatch.setattr(images, "_run", fake)

    with contextlib.suppress(ToolError):
        images.pull(runtime, "ghcr.io/learningequality/kolibri-data-portal/web:default")

    dirs = {_auth_dir(argv) for argv, _ in fake.runtime_calls()}
    assert len(dirs) == 1 and None not in dirs
    assert all(fake.auth_dirs_during)
    assert not dirs.pop().exists()


@pytest.mark.parametrize(
    "error",
    [
        "Error: initializing source docker://ghcr.io/x/y/web:default: reading manifest default: denied",
        'Error response from daemon: Head "https://ghcr.io/v2/x/y/web/manifests/default": unauthorized',
        "Error: reading manifest default in ghcr.io/x/y/web: manifest unknown",
        "failed: 403 Forbidden",
    ],
)
def test_refused_pull_names_the_scope_refresh(monkeypatch, error):
    monkeypatch.setattr(images, "_run", FakeRun(pull_error=error))
    with pytest.raises(ToolError, match="gh auth refresh -s read:packages"):
        images.pull("podman", "ghcr.io/x/y/web:default")


def test_other_pull_failures_keep_their_own_message(monkeypatch):
    monkeypatch.setattr(images, "_run", FakeRun(pull_error="Error: no space left on device"))
    with pytest.raises(ToolError) as raised:
        images.pull("podman", "ghcr.io/x/y/web:default")
    assert "no space left" in str(raised.value)
    assert "read:packages" not in str(raised.value)


def test_no_gh_token_pulls_anonymously(monkeypatch):
    monkeypatch.delenv(images.REGISTRY_TOKEN_ENV, raising=False)
    fake = FakeRun(token=None)
    monkeypatch.setattr(images, "_run", fake)

    images.pull("podman", "ghcr.io/learningequality/agent-containers/generic:default")

    calls = fake.runtime_calls()
    assert [argv for argv, _ in calls if "login" in argv] == []
    assert any("pull" in argv for argv, _ in calls)


GENERIC_COMPOSE = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
NO_TARGETS = "services:\n  db:\n    image: postgres:16\n"


@pytest.mark.parametrize("compose", ["gh: Not Found (HTTP 404)", NO_TARGETS])
@pytest.mark.parametrize("target", [None, "generic"])
def test_a_repo_declaring_no_target_resolves_to_generic(monkeypatch, compose, target):
    fake = FakeRun(
        composes={"learningequality/ricecooker": compose, images.GENERIC_REPO: GENERIC_COMPOSE},
        readable={"learningequality/ricecooker"},
    )
    monkeypatch.setattr(images, "_run", fake)

    resolved = images.resolve("learningequality/ricecooker", target)

    assert resolved.image == "ghcr.io/learningequality/agent-containers/generic:default"
    assert resolved.target == "generic"
    assert set(resolved.declaration.targets) == {"generic"}
    assert "pypi.org" in resolved.declaration.targets["generic"].hosts


@pytest.mark.parametrize(
    "error",
    [
        "gh: Resource not accessible by personal access token (HTTP 403)",
        "gh: Server Error (HTTP 502)",
        "gh: error connecting to api.github.com",
    ],
)
def test_a_failed_read_never_falls_back_to_generic(monkeypatch, error):
    fake = FakeRun(composes={"learningequality/ricecooker": error, images.GENERIC_REPO: GENERIC_COMPOSE})
    monkeypatch.setattr(images, "_run", fake)

    with pytest.raises(ToolError):
        images.resolve("learningequality/ricecooker", None)


def test_a_404_from_a_repo_the_token_cannot_see_is_an_error_not_generic(monkeypatch):
    fake = FakeRun(
        composes={
            "learningequality/kolibri-data-portal": "gh: Not Found (HTTP 404)",
            images.GENERIC_REPO: GENERIC_COMPOSE,
        }
    )
    monkeypatch.setattr(images, "_run", fake)

    with pytest.raises(ToolError, match="cannot see learningequality/kolibri-data-portal"):
        images.resolve("learningequality/kolibri-data-portal", None)


def test_a_declaring_repo_has_no_generic(monkeypatch):
    fake = FakeRun(composes={"learningequality/kolibri-data-portal": (FIXTURES / "kdp-compose.yml").read_text()})
    monkeypatch.setattr(images, "_run", fake)

    with pytest.raises(ToolError, match="web"):
        images.resolve("learningequality/kolibri-data-portal", "generic")


def test_target_may_be_omitted_only_when_one_is_declared(monkeypatch):
    two = "services:\n  web:\n    build: .\n    x-agent: {}\n  android:\n    build: .\n    x-agent: {}\n"
    fake = FakeRun(
        composes={
            "learningequality/kolibri-data-portal": (FIXTURES / "kdp-compose.yml").read_text(),
            "learningequality/kolibri": two,
        }
    )
    monkeypatch.setattr(images, "_run", fake)

    resolved = images.resolve("learningequality/kolibri-data-portal", None)
    assert resolved.target == "web"
    assert resolved.image.endswith("/web:default")
    with pytest.raises(ToolError, match="android, web"):
        images.resolve("learningequality/kolibri", None)
