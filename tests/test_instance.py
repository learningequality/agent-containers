import http.server
import threading

import pytest

from agent_containers import instance
from agent_containers import runtime
from agent_containers.declaration import parse
from agent_containers.errors import ToolError

COMPOSE = """
services:
  app:
    build: .
    working_dir: /src
    x-agent:
      ports:
        "8000": {env: APP_PORT}
        "4000-4100": {env: RANGE}
      ready: {path: /health, port: 4005}
"""


def test_instance_id_is_stable_per_worktree_and_target(tmp_path):
    first = instance.instance_id(tmp_path / "a", "web")
    assert first == instance.instance_id(tmp_path / "a", "web")
    assert first != instance.instance_id(tmp_path / "b", "web")
    assert first != instance.instance_id(tmp_path / "a", "android")
    assert first.isalnum() and first == first.lower()


def test_spec_mounts_at_the_declared_working_dir_and_keeps_given_ports(tmp_path):
    declaration = parse(COMPOSE)
    spec = instance.make_spec(
        runtime.Runtime("podman"), declaration, "app", tmp_path, "img", host_ports={8000: 30000, 4000: 31000}
    )
    assert spec.mount == "/src"
    assert spec.host_ports == {8000: 30000, 4000: 31000}
    assert spec.names.instance == instance.instance_id(tmp_path, "app")


def test_probe_url_maps_a_port_inside_a_range(tmp_path):
    spec = instance.make_spec(
        runtime.Runtime("podman"), parse(COMPOSE), "app", tmp_path, "img", host_ports={8000: 30000, 4000: 31000}
    )
    assert instance.probe_url(spec) == "http://127.0.0.1:31005/health"


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        _Recorder.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class _Redirector(http.server.BaseHTTPRequestHandler):
    location = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", _Redirector.location)
        self.end_headers()

    def log_message(self, *args):
        pass


def _server(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_ready_probe_never_follows_a_redirect(tmp_path):
    recorder, redirector = _server(_Recorder), _server(_Redirector)
    _Redirector.location = f"http://127.0.0.1:{recorder.server_port}/secret"
    _Recorder.hits.clear()
    compose = (
        "services:\n  app:\n    build: .\n    x-agent:\n"
        "      ports: {'8000': {env: P}}\n      ready: {path: /, port: 8000}\n"
    )
    spec = instance.make_spec(
        runtime.Runtime("podman"), parse(compose), "app", tmp_path, "img", host_ports={8000: redirector.server_port}
    )
    try:
        ready, outcome = instance.Instance(runtime.Runtime("podman"), spec)._probe()
    finally:
        recorder.shutdown()
        redirector.shutdown()
    assert (ready, outcome) == (True, "HTTP 302")
    assert _Recorder.hits == []


def test_locks_live_in_the_runtime_dir_whatever_tmpdir_says(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
    assert instance.lock_dir() == tmp_path / "run" / "agent-containers"


def test_locks_fall_back_to_the_cache_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert instance.lock_dir() == tmp_path / ".cache" / "agent-containers" / "locks"


def test_a_planted_lock_dir_symlink_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "decoy").mkdir()
    (tmp_path / "agent-containers").symlink_to(tmp_path / "decoy")
    with pytest.raises(ToolError, match="lock"):
        instance.lock_dir()


class _RunningRuntime(runtime.Runtime):
    def running(self, container):
        return True

    def labelled(self, kind, labels):
        return []


@pytest.mark.parametrize(("fresh", "builds"), [(False, 0), (True, 1)])
def test_fresh_rebuilds_the_image_of_a_running_instance(tmp_path, monkeypatch, fresh, builds):
    (tmp_path / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n    x-agent: {}\n")
    built = []
    monkeypatch.setattr(instance, "_build_local", lambda *args: built.append(args))
    instance.prepare(tmp_path, None, runtime=_RunningRuntime("podman"), local=True, fresh=fresh)
    assert len(built) == builds


def test_spec_rejects_given_ports_that_miss_a_declared_one(tmp_path):
    with pytest.raises(ToolError, match="4000"):
        instance.make_spec(runtime.Runtime("podman"), parse(COMPOSE), "app", tmp_path, "img", host_ports={8000: 1})
