import http.server
import subprocess
import threading

import pytest
from fakes import ListedRuntime

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


VOLUMES = """
services:
  db:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]
  app:
    build: .
    volumes: [cache:/cache]
    x-agent:
      state: /state
volumes:
  pgdata:
  cache:
"""


class _Claiming(runtime.Runtime):
    def __init__(self, existing=()):
        super().__init__("docker")
        self.existing = existing
        self.claimed = {}

    def labelled(self, kind, labels):
        return [runtime.Resource(name, {}) for name in self.existing] if kind == "volume" else []

    def remove(self, labels, *, volumes):
        pass

    def run(self, args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    def create_network(self, spec):
        return runtime.layout("10.89.0.0/24", spec)

    def start_gateway(self, spec, addresses):
        pass

    def start_sidecar(self, spec, sidecar, addresses):
        pass

    def start_target(self, spec, addresses):
        pass

    def claim_volume(self, image, volume, path):
        self.claimed[volume] = path


def _claims(tmp_path, monkeypatch, existing_names=()):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    names = runtime.Names("default", "claims")
    rt = _Claiming([names.volume(name) for name in existing_names])
    spec = instance.make_spec(rt, parse(VOLUMES), "app", tmp_path, "img", instance="claims", host_ports={})
    instance.Instance(rt, spec).up(fresh=True)
    return rt.claimed, names


def test_up_hands_only_the_targets_new_volumes_to_the_user(tmp_path, monkeypatch):
    claimed, names = _claims(tmp_path, monkeypatch)
    assert claimed == {names.volume("cache"): "/cache", names.state: "/state"}


def test_up_leaves_a_volume_that_already_exists_alone(tmp_path, monkeypatch):
    claimed, names = _claims(tmp_path, monkeypatch, existing_names=("state",))
    assert claimed == {names.volume("cache"): "/cache"}


SERVING = """
services:
  app:
    build: .
    x-agent:
      commands: {serve: make serve}
      ready: {command: probe, timeout: 30}
"""


def _serving(tmp_path, recorded):
    rt = runtime.Runtime("podman")
    rt.run = lambda args, **kwargs: recorded.append(args) or subprocess.CompletedProcess(args, 0, "", "")
    spec = instance.make_spec(rt, parse(SERVING), "app", tmp_path, "img", host_ports={})
    made = instance.Instance(rt, spec)
    made.up = lambda **kwargs: None
    return made


def test_serve_clears_the_last_exit_code_before_it_launches(tmp_path, monkeypatch):
    recorded = []
    made = _serving(tmp_path, recorded)
    probes = iter([(False, "down"), (True, "up")])
    monkeypatch.setattr(made, "_probe", lambda: next(probes))
    monkeypatch.setattr(made, "_wait_served", lambda: None)

    made.serve()

    launch = next(i for i, args in enumerate(recorded) if "-d" in args)
    clears = [i for i, args in enumerate(recorded) if "rm" in " ".join(args) and made.rc in " ".join(args)]
    assert clears and clears[0] < launch
    assert made.rc not in " ".join(recorded[launch]).split("rm -f")[-1].split("&&")[0]


def test_a_ready_command_that_times_out_is_a_failed_probe(tmp_path, monkeypatch):
    made = _serving(tmp_path, [])

    def hang(*args, **kwargs):
        raise ToolError("ready command timed out after 60s")

    monkeypatch.setattr(made, "_exec", hang)

    assert made._probe() == (False, "ready command timed out after 60s")


def test_reap_skips_a_worktree_whose_parent_is_also_missing(tmp_path):
    (tmp_path / "github").mkdir()
    listed = ListedRuntime(
        {
            "deleted": tmp_path / "github" / "deleted",
            "unmounted": tmp_path / "usb-drive" / "repo",
            "present": tmp_path,
        }
    )

    assert instance.reap(listed, "ns") == ["deleted"]
    assert listed.removed == ["deleted"]
