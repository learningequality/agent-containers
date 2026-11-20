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


SIDECAR_PORTS = """
services:
  app:
    build: .
    x-agent:
      ports:
        "8000": {env: APP_PORT}
        "gcs:4443": {env: GCS_PUBLIC_PORT}
  gcs:
    image: fsouza/fake-gcs-server:1.55.1
"""


class _LabelledRuntime(runtime.Runtime):
    def __init__(self, labels):
        super().__init__("podman")
        self.target_labels = labels

    def labelled(self, kind, labels):
        return [runtime.Resource("ac-x-app", self.target_labels, "running")]


def test_a_running_targets_sidecar_ports_are_reused_and_served(tmp_path):
    given = {8000: 30000, "gcs:4443": 31000}
    spec = instance.make_spec(runtime.Runtime("podman"), parse(SIDECAR_PORTS), "app", tmp_path, "img", host_ports=given)
    rt = _LabelledRuntime(spec.labels("target"))
    found = instance.make_spec(rt, parse(SIDECAR_PORTS), "app", tmp_path, "img")
    assert found.host_ports == given
    assert instance.Instance(rt, found)._reusable()
    assert instance.Instance(rt, found).urls() == {8000: "http://127.0.0.1:30000", "gcs:4443": "http://127.0.0.1:31000"}


def test_spec_rejects_given_ports_that_miss_a_sidecar_port(tmp_path):
    declaration = parse(SIDECAR_PORTS)
    with pytest.raises(ToolError, match="gcs:4443"):
        instance.make_spec(runtime.Runtime("podman"), declaration, "app", tmp_path, "img", host_ports={8000: 1})


@pytest.mark.parametrize("given", [{8000: 3128, "gcs:4443": 31000}, {8000: 30000, "gcs:4443": 3128}])
def test_spec_rejects_a_host_port_the_gateway_proxy_listens_on(tmp_path, given):
    declaration = parse(SIDECAR_PORTS)
    with pytest.raises(ToolError, match="3128 is the gateway's proxy"):
        instance.make_spec(runtime.Runtime("podman"), declaration, "app", tmp_path, "img", host_ports=given)


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
        self.calls = []
        self.removed_volumes = []

    def labelled(self, kind, labels):
        return [runtime.Resource(name, {}) for name in self.existing] if kind == "volume" else []

    def exists(self, kind, name):
        return name in self.existing

    def remove(self, labels, *, volumes):
        pass

    def remove_volumes(self, names):
        self.removed_volumes.extend(names)

    def run(self, args, **kwargs):
        self.calls.append(args)
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
        self.claimed[volume] = (image, path)


def _claims(tmp_path, monkeypatch, existing_names=()):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    names = runtime.Names("default", "claims")
    rt = _Claiming([names.volume(name) for name in existing_names])
    spec = instance.make_spec(rt, parse(VOLUMES), "app", tmp_path, "img", instance="claims", host_ports={})
    instance.Instance(rt, spec).up(fresh=True)
    return rt.claimed, names


def test_up_hands_only_the_targets_new_volumes_to_the_user(tmp_path, monkeypatch):
    claimed, names = _claims(tmp_path, monkeypatch)
    assert claimed == {names.volume("cache"): ("img", "/cache"), names.state: ("img", "/state")}


def test_up_leaves_a_volume_that_already_exists_alone(tmp_path, monkeypatch):
    claimed, names = _claims(tmp_path, monkeypatch, existing_names=("state",))
    assert claimed == {names.volume("cache"): ("img", "/cache")}


SHARED = """
services:
  db:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]
  lod:
    image: kolibri:web
    volumes: [lodhome:/lod]
  app:
    build: .
    x-agent:
      cache: /template
      worktree_sidecars: [lod]
volumes:
  pgdata:
  lodhome:
"""


def _shared(tmp_path, monkeypatch, rt, **kwargs):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    spec = instance.make_spec(
        rt, parse(SHARED), "app", tmp_path, "img", instance="shared", host_ports={}, repo="org/repo", **kwargs
    )
    return instance.Instance(rt, spec)


def test_up_creates_the_cache_unlabelled_by_instance_and_hands_it_and_worktree_sidecar_volumes_over(
    tmp_path, monkeypatch
):
    rt = _Claiming()
    inst = _shared(tmp_path, monkeypatch, rt)
    inst.up(fresh=True)
    cache = inst.spec.cache
    created = next(call for call in rt.calls if call[:2] == ["volume", "create"] and call[-1] == cache)
    assert runtime.flags("--label", inst.spec.cache_labels()) == created[2:-1]
    lodhome = inst.spec.names.volume("lodhome")
    assert rt.claimed == {cache: ("img", "/template"), lodhome: ("docker.io/library/kolibri:web", "/lod")}


def test_an_existing_cache_is_neither_created_nor_handed_over(tmp_path, monkeypatch):
    rt = _Claiming()
    inst = _shared(tmp_path, monkeypatch, rt)
    rt.existing = (inst.spec.cache,)
    inst.up(fresh=True)
    assert not [call for call in rt.calls if call[:2] == ["volume", "create"] and call[-1] == inst.spec.cache]
    assert inst.spec.cache not in rt.claimed


class _FailingTarget(_Claiming):
    def start_target(self, spec, addresses):
        raise ToolError("target broke")


def test_a_failed_up_removes_the_volumes_it_created_but_never_the_cache(tmp_path, monkeypatch):
    rt = _FailingTarget()
    inst = _shared(tmp_path, monkeypatch, rt)
    with pytest.raises(ToolError, match="target broke"):
        inst.up(fresh=True)
    assert inst.spec.names.volume("lodhome") in rt.removed_volumes
    assert inst.spec.cache not in rt.removed_volumes


SCOPED = """
services:
  web:
    build: .
    depends_on: {lod: {condition: service_started}}
    x-agent: {}
  server:
    build: .
    x-agent: {}
  lod:
    image: kolibri:web
    volumes: [lodhome:/lod]
  db:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]
volumes:
  lodhome:
  pgdata:
"""


def test_a_target_starts_and_names_only_the_sidecars_it_depends_on(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    rt = _Claiming()
    started = []
    monkeypatch.setattr(rt, "start_sidecar", lambda spec, sidecar, addresses: started.append(sidecar.name))
    spec = instance.make_spec(rt, parse(SCOPED), "web", tmp_path, "img", host_ports={})
    instance.Instance(rt, spec).up(fresh=True)

    assert started == ["lod"]
    assert set(runtime.layout("10.89.0.0/24", spec).services) == {"web", "lod"}
    assert spec.environment("10.89.0.2")["NO_PROXY"] == "localhost,127.0.0.1,web,lod"
    created = [call[-1] for call in rt.calls if call[:2] == ["volume", "create"]]
    assert created == [spec.names.volume("lodhome")]
    server = instance.make_spec(rt, parse(SCOPED), "server", tmp_path, "img", host_ports={})
    assert [s.name for s in server.sidecars] == ["lod", "db"]


def test_a_cache_needs_a_repo_to_be_named_by(tmp_path):
    with pytest.raises(ToolError, match="cache"):
        instance.make_spec(runtime.Runtime("podman"), parse(SHARED), "app", tmp_path, "img", host_ports={})


def test_a_local_instance_keys_its_cache_on_the_worktree(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text(SHARED)
    monkeypatch.setattr(instance, "_build_local", lambda *args: None)
    built = instance.prepare(tmp_path, "app", runtime=_RunningRuntime("podman"), local=True)
    assert built.spec.repo == f"local:{tmp_path.resolve()}"


def test_status_names_the_cache_volume_of_a_running_target(tmp_path):
    spec = instance.make_spec(
        runtime.Runtime("podman"), parse(SHARED), "app", tmp_path, "img", host_ports={}, repo="org/repo"
    )
    target = runtime.Resource("ac-x-app", spec.labels("target"), "running")
    assert instance.status_lines([target]) == ["app\ttarget\trunning\tac-x-app", f"app\tcache\tvolume\t{spec.cache}"]


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
