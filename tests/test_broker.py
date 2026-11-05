import io
import json
import os
import subprocess
import threading
import time

import pytest

from agent_containers import broker as brokers
from agent_containers import client
from agent_containers.declaration import parse
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.instance import Served


class FakeInstance:
    def __init__(self, target):
        self.target = target
        self.calls = []
        self.killed = threading.Event()

    def run(self, argv, *, cwd="", stdout=None, stderr=None, group=None):
        self.calls.append(("run", argv, cwd))
        if argv == ["block"]:
            self.killed.wait(10)
            return 143
        os.write(stdout.fileno(), b"out:" + " ".join(argv).encode() + b"\n")
        os.write(stderr.fileno(), b"err-line\n")
        return int(argv[-1]) if argv[-1].isdigit() else 0

    def up(self):
        self.calls.append(("up",))

    def kill(self, group):
        self.calls.append(("kill", group))
        self.killed.set()

    def serve(self):
        self.calls.append(("serve",))
        return Served(urls={8000: "http://127.0.0.1:30000"}, log=".agent-containers/web/serve.log")

    def stop(self, *, purge=False):
        self.calls.append(("stop", purge))

    def status(self):
        return {"ac-x-web": "target running"}


@pytest.fixture
def made(tmp_path):
    instances = {}

    def factory(target):
        return instances.setdefault(target, FakeInstance(target))

    broker = brokers.Broker(tmp_path, None, "test", None, factory=factory, targets=("web", "android"))
    broker.start()
    yield broker, instances
    broker.close()


def _dir(tmp_path, name):
    return tmp_path / ".agent-containers" / "broker" / name


def _submit(tmp_path, request_id, payload):
    path = _dir(tmp_path, "requests") / f"{request_id}.json"
    path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def _exit(tmp_path, request_id, timeout=5):
    path = _dir(tmp_path, "results") / request_id / "exit"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"no exit for {request_id}")


def test_start_writes_a_gitignore_and_a_heartbeat(made, tmp_path):
    assert (tmp_path / ".agent-containers" / ".gitignore").read_text() == "*\n"
    assert client.broker_alive(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "../outside"},
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "/etc"},
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "a/../../b"},
        {"command": "run", "target": "nope", "argv": ["ls"], "cwd": ""},
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "", "local": True},
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "", "repo": "evil/repo"},
        {"command": "run", "target": "web", "argv": ["ls"], "cwd": "", "runtime": "docker"},
        {"command": "rm", "target": "web", "argv": [], "cwd": ""},
        {"command": "run", "target": "web", "argv": "ls", "cwd": ""},
        b"x" * (brokers.REQUEST_LIMIT + 1),
        b"not json",
    ],
)
def test_bad_requests_are_refused_and_reach_no_instance(made, tmp_path, payload):
    broker, instances = made
    _submit(tmp_path, "r1", payload)
    broker.poll_once()
    assert _exit(tmp_path, "r1") == EXIT_TOOL_ERROR
    assert all(not inst.calls for inst in instances.values())
    assert sorted(p.name for p in (_dir(tmp_path, "results") / "r1").iterdir()) == ["exit", "stderr", "stdout"]


@pytest.mark.parametrize("linked", ["requests", "results"])
def test_a_symlinked_directory_writes_nothing(made, tmp_path, linked):
    broker, instances = made
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "r1.json").write_text(json.dumps({"command": "run", "target": "web", "argv": ["ls"], "cwd": ""}))
    if linked == "results":
        _submit(tmp_path, "r1", {"command": "run", "target": "web", "argv": ["ls"], "cwd": ""})
    _dir(tmp_path, linked).rmdir()
    _dir(tmp_path, linked).symlink_to(outside)

    broker.poll_once()
    time.sleep(0.3)

    assert sorted(p.name for p in outside.iterdir()) == ["r1.json"]
    assert all(not inst.calls for inst in instances.values())


def test_a_symlinked_request_file_is_refused(made, tmp_path):
    broker, instances = made
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"command": "run", "target": "web", "argv": ["ls"], "cwd": ""}))
    (_dir(tmp_path, "requests") / "r1.json").symlink_to(secret)
    broker.poll_once()
    assert _exit(tmp_path, "r1") == EXIT_TOOL_ERROR
    assert all(not inst.calls for inst in instances.values())


def test_the_client_streams_output_and_exits_with_the_commands_code(made, tmp_path):
    broker, instances = made
    out, err = io.BytesIO(), io.BytesIO()
    pump = threading.Thread(target=lambda: [broker.poll_once() or time.sleep(0.05) for _ in range(60)], daemon=True)
    pump.start()

    code = client.request(tmp_path, "run", "web", ["make", "test", "3"], "sub/dir", out=out, err=err)

    assert code == 3
    assert out.getvalue() == b"out:make test 3\n"
    assert err.getvalue() == b"err-line\n"
    assert instances["web"].calls == [("up",), ("run", ["make", "test", "3"], "sub/dir")]


def test_the_client_prints_serve_urls(made, tmp_path):
    broker, _ = made
    out, err = io.BytesIO(), io.BytesIO()
    pump = threading.Thread(target=lambda: [broker.poll_once() or time.sleep(0.05) for _ in range(60)], daemon=True)
    pump.start()
    assert client.request(tmp_path, "serve", "web", [], "", out=out, err=err) == 0
    assert b"http://127.0.0.1:30000" in out.getvalue()


def test_interrupting_the_client_kills_the_command(made, tmp_path, monkeypatch):
    broker, instances = made
    out, err = io.BytesIO(), io.BytesIO()
    pump = threading.Thread(target=lambda: [broker.poll_once() or time.sleep(0.05) for _ in range(100)], daemon=True)
    pump.start()
    interrupted = threading.Timer(0.5, lambda: client.interrupt_current())
    interrupted.start()

    code = client.request(tmp_path, "run", "web", ["block"], "", out=out, err=err)

    assert [call[0] for call in instances["web"].calls] == ["up", "run", "kill"]
    assert code == 143


def test_a_cancel_during_start_up_stops_the_command_ever_running(tmp_path):
    started = threading.Event()
    release = threading.Event()
    instances = []

    def slow_factory(target):
        started.set()
        release.wait(10)
        instances.append(FakeInstance(target))
        return instances[-1]

    made = brokers.Broker(tmp_path, None, "test", None, factory=slow_factory, targets=("web",))
    made.start()
    try:
        _submit(tmp_path, "r1", {"command": "run", "target": "web", "argv": ["make", "test"], "cwd": ""})
        made.poll_once()
        assert started.wait(5)
        (_dir(tmp_path, "requests") / f"r1{brokers.CANCEL}").write_bytes(b"")
        made.poll_once()
        release.set()
        assert _exit(tmp_path, "r1") == 130
    finally:
        made.close()
    assert [call for call in instances[0].calls if call[0] == "run"] == []


def test_a_slow_start_up_blocks_no_other_request(tmp_path):
    release = threading.Event()

    def factory(target):
        if target == "web":
            release.wait(10)
        return FakeInstance(target)

    made = brokers.Broker(tmp_path, None, "test", None, factory=factory, targets=("web", "android"))
    made.start()
    try:
        _submit(tmp_path, "slow", {"command": "run", "target": "web", "argv": ["true"], "cwd": ""})
        made.poll_once()
        time.sleep(0.2)
        _submit(tmp_path, "quick", {"command": "status", "target": "android", "argv": [], "cwd": ""})
        started = time.monotonic()
        made.poll_once()
        assert _exit(tmp_path, "quick", timeout=3) == 0
        assert time.monotonic() - started < 3
    finally:
        release.set()
        made.close()


def _config(worktree, key, value):
    subprocess.run(["git", "config", key, value], cwd=worktree, check=True)


def test_the_repo_and_git_dirs_are_fixed_when_the_broker_starts(tmp_path, monkeypatch):
    for bare in ("one.git", "two.git"):
        subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / bare)], check=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {tmp_path / 'one.git'}\n")
    _config(worktree, "remote.origin.url", "git@github.com:learningequality/kdp.git")
    captured = {}
    declaration = parse("services:\n  app:\n    build: .\n    x-agent: {}\n")
    monkeypatch.setattr(brokers.images, "fetch_declaration", lambda repo: declaration)
    monkeypatch.setattr(brokers, "detect", lambda name: None)
    monkeypatch.setattr(brokers, "prepare", lambda *args, **kwargs: captured.update(kwargs) or FakeInstance("app"))
    made = brokers.Broker(worktree, None, "test", "task1")
    made.start()
    try:
        (worktree / ".git").write_text(f"gitdir: {tmp_path / 'two.git'}\n")
        _config(worktree, "remote.upstream.url", "https://github.com/attacker/repo")
        _submit(worktree, "r1", {"command": "status", "target": "app", "argv": [], "cwd": ""})
        made.poll_once()
        assert _exit(worktree, "r1") == 0
    finally:
        made.close()
    assert captured["repo"] == "learningequality/kdp"
    assert str(tmp_path / "two.git") not in {str(d) for d in captured["git_dirs"]}


def test_a_closed_broker_leaves_a_heartbeat_that_reads_as_stale(tmp_path):
    made = brokers.Broker(tmp_path, None, "test", None, factory=FakeInstance, targets=("web",))
    made.start()
    made.close()
    assert client.broker_known(tmp_path)
    assert not client.broker_alive(tmp_path)


def test_a_stale_heartbeat_tells_the_client_to_start_the_broker(tmp_path):
    alive = _dir(tmp_path, "alive")
    alive.parent.mkdir(parents=True)
    alive.write_text("0")
    old = time.time() - 60
    os.utime(alive, (old, old))
    assert not client.broker_alive(tmp_path)
    err = io.BytesIO()
    assert client.request(tmp_path, "run", "web", ["ls"], "", out=io.BytesIO(), err=err) == EXIT_TOOL_ERROR
    assert b"agent-containers broker" in err.getvalue()
