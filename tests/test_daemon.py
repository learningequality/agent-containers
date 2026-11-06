import contextlib
import io
import threading

import pytest
from fakes import FakeInstance

from agent_containers import broker as brokers
from agent_containers import client
from agent_containers import daemon
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.errors import ToolError


def _requests(worktree):
    path = worktree / brokers.BROKER_DIR / brokers.REQUESTS
    path.mkdir(parents=True)
    return path


def test_discovery_finds_queued_worktrees_under_roots_only(tmp_path):
    github = tmp_path / "github"
    found = [
        github / "kolibri",
        github / "studio" / "feature",
        github / "deep" / "a" / "b",
    ]
    hidden = [
        github / "node_modules" / "pkg",
        github / ".cache" / "x",
        github / "deep" / "a" / "b" / "c" / "d",
        tmp_path / "outside" / "repo",
    ]
    for worktree in found + hidden:
        _requests(worktree)
    (github / "studio" / ".bare").mkdir()

    assert sorted(daemon.discover([github])) == sorted(found)


def test_a_root_that_is_itself_a_worktree_is_found(tmp_path):
    _requests(tmp_path / "kolibri")
    assert daemon.discover([tmp_path / "kolibri"]) == [tmp_path / "kolibri"]


def _fake_broker(worktree):
    return brokers.Broker(worktree, None, "test", None, factory=FakeInstance, targets=("web",))


@contextlib.contextmanager
def _pumping(made):
    stop = threading.Event()

    def loop():
        while not stop.wait(0.05):
            made.scan_once()
            made.poll_once()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        made.close()


def test_a_client_in_a_worktree_under_a_root_is_served(tmp_path):
    worktree = tmp_path / "github" / "kolibri"
    worktree.mkdir(parents=True)
    made = daemon.Daemon(broker_factory=_fake_broker, roots=lambda: [tmp_path / "github"])
    out, err = io.BytesIO(), io.BytesIO()

    with _pumping(made):
        code = client.request(worktree, "run", "web", ["make", "test", "4"], "", out=out, err=err)

    assert code == 4, err.getvalue()
    assert out.getvalue() == b"out:make test 4\n"


def test_a_worktree_is_served_on_a_freshly_booted_host(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 5.0)
    worktree = tmp_path / "github" / "kolibri"
    _requests(worktree)
    made = daemon.Daemon(broker_factory=_fake_broker, roots=lambda: [tmp_path / "github"])

    made.scan_once()

    assert client.broker_alive(worktree)
    made.close()


def test_a_worktree_outside_every_root_is_never_served(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "PICKUP_WAIT", 1.0)
    worktree = tmp_path / "elsewhere"
    worktree.mkdir()
    made = daemon.Daemon(broker_factory=_fake_broker, roots=lambda: [tmp_path / "github"])
    err = io.BytesIO()

    with _pumping(made):
        code = client.request(worktree, "run", "web", ["true"], "", out=io.BytesIO(), err=err)

    assert code == EXIT_TOOL_ERROR
    assert b"agent-containers install" in err.getvalue()
    assert str(worktree).encode() in err.getvalue()


def test_removing_a_root_stops_serving_its_worktrees(tmp_path):
    worktree = tmp_path / "github" / "kolibri"
    _requests(worktree)
    roots = [tmp_path / "github"]
    made = daemon.Daemon(broker_factory=_fake_broker, roots=lambda: list(roots))
    made.scan_once()
    assert client.broker_alive(worktree)

    roots.clear()
    made.scan_once()

    assert not client.broker_alive(worktree)
    made.close()


@pytest.mark.parametrize(
    "error",
    [ToolError("neither upstream nor origin is a GitHub remote"), FileNotFoundError("no GitHub remote: worktree gone")],
)
def test_a_worktree_whose_broker_cannot_start_has_its_requests_refused_with_the_reason(tmp_path, error):
    worktree = tmp_path / "github" / "kolibri"
    requests = _requests(worktree)
    (requests / "r1.json").write_text('{"command": "run", "target": null, "argv": ["true"], "cwd": ""}')

    class Broken:
        def __init__(self, worktree):
            pass

        def start(self):
            raise error

    made = daemon.Daemon(broker_factory=Broken, roots=lambda: [tmp_path / "github"])
    made.scan_once()

    result = worktree / brokers.BROKER_DIR / brokers.RESULTS / "r1"
    assert (result / "exit").read_text().strip() == str(EXIT_TOOL_ERROR)
    assert "GitHub remote" in (result / "stderr").read_text()
    assert not (requests / "r1.json").exists()
