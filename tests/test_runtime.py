import ipaddress
import os
import subprocess
from pathlib import Path

import pytest
from fakes import git as _git

from agent_containers import runtime
from agent_containers.declaration import parse

FIXTURES = Path(__file__).parent / "fixtures"
PROXY_IP = "10.89.0.2"


def _spec(tmp_path, compose=None, target="web", **overrides):
    declaration = parse(compose or (FIXTURES / "kdp-compose.yml").read_text())
    chosen = declaration.targets[target]
    fields = dict(
        names=runtime.Names("default", "abc123"),
        worktree=tmp_path,
        mount="/kolibri-data-portal-plugin",
        target=chosen,
        image="ghcr.io/learningequality/kolibri-data-portal/web:default",
        sidecars=tuple(declaration.sidecars.values()),
        host_ports={port.start: 20000 + port.start for port in chosen.ports},
        git_mounts=((str(tmp_path / ".git"), "/kolibri-data-portal-plugin/.git"),),
    )
    fields.update(overrides)
    return runtime.Spec(**fields)


def _addresses(spec):
    return runtime.layout("10.89.0.0/24", spec)


def _all_argv(rt, spec):
    addresses = _addresses(spec)
    yield "network", rt.network_args(spec, addresses.subnet)
    yield "gateway", rt.gateway_args(spec, addresses)
    yield "target", rt.target_args(spec, addresses)
    for sidecar in spec.sidecars:
        yield sidecar.name, rt.sidecar_args(spec, sidecar, addresses)


def _values(argv, flag):
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_no_container_gets_privileges_or_host_namespaces(tmp_path, name):
    rt = runtime.Runtime(name)
    for role, argv in _all_argv(rt, _spec(tmp_path)):
        assert "--privileged" not in argv, role
        for flag in ("--network", "--net", "--pid", "--ipc", "--uts", "--userns"):
            assert "host" not in _values(argv, flag), role
        assert not [a for a in argv if a.startswith("--net=host")], role
        caps = [a for a in argv if a.startswith("--cap-add")]
        assert caps in ([], ["--cap-add=NET_BIND_SERVICE"]), role
        if role == "gateway":
            assert caps == ["--cap-add=NET_BIND_SERVICE"]


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_target_and_sidecars_mount_nothing_of_the_host_but_the_worktree_and_git_dirs(tmp_path, name):
    rt = runtime.Runtime(name)
    spec = _spec(tmp_path)
    allowed = {str(tmp_path), *(source for source, _ in spec.git_mounts)}
    for role, argv in _all_argv(rt, spec):
        if role in ("network", "gateway"):
            continue
        sources = [v.split(":")[0] for v in _values(argv, "--volume") + _values(argv, "-v")]
        host_sources = [s for s in sources if s.startswith(("/", ".", "~"))]
        assert set(host_sources) <= allowed, role
    target = rt.target_args(spec, _addresses(spec))
    git = [v for v in _values(target, "--volume") if v.startswith(str(tmp_path / ".git"))]
    assert git and all(":ro" in v for v in git)


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_only_the_gateway_publishes_and_only_on_loopback(tmp_path, name):
    rt = runtime.Runtime(name)
    spec = _spec(tmp_path)
    for role, argv in _all_argv(rt, spec):
        published = _values(argv, "--publish") + _values(argv, "-p")
        if role == "gateway":
            assert len(published) == 1 + 101
            assert all(p.startswith("127.0.0.1:") for p in published)
        else:
            assert published == [], role


def test_gateway_forwards_each_declared_port_to_the_target(tmp_path):
    spec = _spec(tmp_path)
    argv = runtime.Runtime("podman").gateway_args(spec, _addresses(spec))
    assert "127.0.0.1:28000:28000" in _values(argv, "--publish")
    assert "127.0.0.1:24100:24100" in _values(argv, "--publish")
    assert "28000:10.89.0.3:8000" in _values(argv, "--forward")
    assert "24100:10.89.0.3:4100" in _values(argv, "--forward")
    assert f"{PROXY_IP}:{runtime.PROXY_PORT}" in argv


SIDECAR_PORTS = """
services:
  app:
    build: .
    x-agent:
      ports:
        "4443": {env: APP_PORT}
        "gcs:4443-4444": {env: GCS_PUBLIC_PORT}
  gcs:
    image: fsouza/fake-gcs-server:1.55.1
"""


def _sidecar_spec(tmp_path):
    return _spec(tmp_path, compose=SIDECAR_PORTS, target="app", host_ports={4443: 30000, "gcs:4443": 31000})


def test_gateway_forwards_a_sidecar_port_to_the_sidecar_beside_the_same_target_port(tmp_path):
    spec = _sidecar_spec(tmp_path)
    addresses = _addresses(spec)
    argv = runtime.Runtime("podman").gateway_args(spec, addresses)
    app, gcs = addresses.services["app"], addresses.services["gcs"]
    assert _values(argv, "--publish") == ["127.0.0.1:30000:30000", "127.0.0.1:31000:31000", "127.0.0.1:31001:31001"]
    assert _values(argv, "--forward") == [f"30000:{app}:4443", f"31000:{gcs}:4443", f"31001:{gcs}:4444"]


def test_target_environment_carries_sidecar_host_ports(tmp_path):
    env = _sidecar_spec(tmp_path).environment(PROXY_IP)
    assert (env["APP_PORT"], env["GCS_PUBLIC_PORT"]) == ("30000", "31000")


def test_reserved_ports_are_keyed_apart_for_the_same_port_on_two_services():
    ports = parse(SIDECAR_PORTS).targets["app"].ports
    reserved = runtime.reserve_host_ports(ports)
    assert set(reserved) == {4443, "gcs:4443"}
    assert reserved[4443] not in (reserved["gcs:4443"], reserved["gcs:4443"] + 1)


def test_every_service_gets_a_fixed_address_and_names_its_peers(tmp_path):
    spec = _spec(tmp_path)
    addresses = _addresses(spec)
    assert addresses.gateway == PROXY_IP
    assert len(set(addresses.services.values()) | {addresses.gateway}) == 1 + 1 + len(spec.sidecars)
    rt = runtime.Runtime("docker")
    target = rt.target_args(spec, addresses)
    assert f"db:{addresses.services['db']}" in _values(target, "--add-host")
    sidecar = rt.sidecar_args(spec, spec.sidecars[0], addresses)
    assert f"web:{addresses.services['web']}" in _values(sidecar, "--add-host")


ANDROID = """
services:
  android:
    build: .
    x-agent:
      devices: [kvm]
  web:
    build: .
    x-agent: {}
"""


@pytest.mark.parametrize(
    ("target", "platform", "expected"),
    [("android", "linux", True), ("android", "darwin", False), ("web", "linux", False)],
)
def test_kvm_only_for_a_declaring_target_on_linux(tmp_path, monkeypatch, target, platform, expected):
    monkeypatch.setattr(runtime.sys, "platform", platform)
    spec = _spec(tmp_path, compose=ANDROID, target=target, sidecars=(), host_ports={})
    argv = runtime.Runtime("podman").target_args(spec, _addresses(spec))
    assert ("/dev/kvm" in _values(argv, "--device")) is expected


def test_target_environment_carries_host_ports_and_the_proxy(tmp_path):
    env = _spec(tmp_path).environment(PROXY_IP)
    assert env["KDP_PORT"] == "28000"
    assert env["WEBPACK_DEV_PUBLIC_PORT"] == "24000"
    assert env["HTTPS_PROXY"] == f"http://{PROXY_IP}:3128"
    assert set(env["NO_PROXY"].split(",")) >= {"db", "gcs", "localhost"}
    assert env["STORAGE_EMULATOR_HOST"] == "http://gcs:4443"


def test_reserved_ranges_do_not_overlap():
    ports = parse((FIXTURES / "kdp-compose.yml").read_text()).targets["web"].ports
    reserved = runtime.reserve_host_ports(ports)
    spans = sorted((reserved[port.start], reserved[port.start] + port.count - 1) for port in ports)
    assert all(earlier[1] < later[0] for earlier, later in zip(spans, spans[1:], strict=False))


def test_a_bound_block_is_consecutive():
    block = []
    while not block:
        block = runtime._bind_block(5)
    try:
        ports = [sock.getsockname()[1] for sock in block]
    finally:
        for sock in block:
            sock.close()
    assert ports == list(range(ports[0], ports[0] + 5))


def _linked_worktree(parent, name):
    bare = parent / name / ".bare"
    _git(parent, "init", "-q", "--bare", str(bare))
    seed = parent / f"{name}-seed"
    _git(parent, "clone", "-q", str(bare), str(seed))
    _git(seed, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    worktree = parent / name / "feature"
    _git(bare, "worktree", "add", "-q", str(worktree), "main")
    return bare, worktree


def test_linked_worktree_mounts_its_git_file_and_common_dir_read_only(tmp_path):
    bare, worktree = _linked_worktree(tmp_path, "project")

    mounts = dict(runtime.git_mounts(worktree, "/work"))

    assert mounts[str(worktree / ".git")] == "/work/.git"
    assert mounts[str(bare.resolve())] == str(bare.resolve())
    assert len(mounts) == 2


def test_a_symlinked_dot_git_is_refused(tmp_path):
    outside = tmp_path / "secrets"
    outside.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").symlink_to(outside)
    with pytest.raises(runtime.ToolError, match=".git"):
        runtime.git_mounts(worktree, "/work")


def test_git_mounts_take_the_dirs_recorded_earlier_over_a_rewritten_dot_git(tmp_path):
    first, worktree = _linked_worktree(tmp_path, "one")
    second, _ = _linked_worktree(tmp_path, "two")
    recorded = runtime.git_dirs(worktree)
    (worktree / ".git").write_text(f"gitdir: {second}/worktrees/feature\n")

    sources = {source for source, _ in runtime.git_mounts(worktree, "/work", recorded)}

    assert str(second.resolve()) not in sources
    assert str(first.resolve()) in sources


@pytest.mark.parametrize("planted", ["{other}", "{other}/worktrees/feature"])
def test_a_dot_git_pointing_at_a_repo_that_does_not_point_back_is_refused(tmp_path, planted):
    _, worktree = _linked_worktree(tmp_path, "mine")
    other, _ = _linked_worktree(tmp_path, "other")
    (worktree / ".git").write_text(f"gitdir: {planted.format(other=other)}\n")
    with pytest.raises(runtime.ToolError, match="does not belong"):
        runtime.git_dirs(worktree)


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_the_worktree_mount_is_shared_between_instances(tmp_path, monkeypatch, name):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    spec = _spec(tmp_path)
    volumes = _values(runtime.Runtime(name).target_args(spec, _addresses(spec)), "--volume")
    worktree = next(v for v in volumes if v.startswith(f"{tmp_path}:"))
    assert worktree.endswith(":z")


WORKTREE_SIDECAR = """
services:
  web:
    build: .
    working_dir: /kolibri
    x-agent:
      worktree_sidecars: [lod]
      cache: /cache
      ports:
        "lod:8081": {env: LOD_PUBLIC_PORT}
  lod:
    image: ghcr.io/learningequality/kolibri/web:default
    environment: {KOLIBRI_HOME: /lod}
    command: kolibri start --port 8081
    volumes: [lodhome:/lod]
  db:
    image: postgres:16
volumes:
  lodhome:
"""


def _worktree_spec(tmp_path, **overrides):
    fields = dict(
        compose=WORKTREE_SIDECAR,
        mount="/kolibri",
        host_ports={"lod:8081": 30000},
        git_mounts=((str(tmp_path / ".git"), "/kolibri/.git"),),
        repo="learningequality/kolibri",
    )
    return _spec(tmp_path, **{**fields, **overrides})


def _sidecar(spec, name):
    return next(sidecar for sidecar in spec.sidecars if sidecar.name == name)


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_a_worktree_sidecar_is_mounted_and_run_as_the_target_is(tmp_path, monkeypatch, name):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    rt = runtime.Runtime(name)
    rt._rootless = False
    spec = _worktree_spec(tmp_path)
    addresses = _addresses(spec)
    target = rt.target_args(spec, addresses)
    lod = rt.sidecar_args(spec, _sidecar(spec, "lod"), addresses)
    target_volumes = set(_values(target, "--volume"))
    lod_volumes = set(_values(lod, "--volume"))

    assert {f"{tmp_path}:/kolibri:z", f"{tmp_path}/.git:/kolibri/.git:ro,z"} <= target_volumes & lod_volumes
    assert "ac-default-abc123-lodhome:/lod" in lod_volumes
    assert not {v for v in lod_volumes if v.endswith(":/cache")}
    assert rt.user_args() and set(rt.user_args()) <= set(lod)
    assert {"--cap-drop=ALL", "--init"} <= set(lod)
    assert _values(lod, "--workdir") == ["/kolibri"]
    assert lod[-5:] == ["ghcr.io/learningequality/kolibri/web:default", "kolibri", "start", "--port", "8081"]
    assert _values(lod, "--network") == [spec.names.network]
    assert "HTTPS_PROXY" not in " ".join(_values(lod, "--env"))


def test_a_plain_sidecar_gets_neither_the_worktree_nor_the_invoking_user(tmp_path):
    rt = runtime.Runtime("podman")
    spec = _worktree_spec(tmp_path)
    db = rt.sidecar_args(spec, _sidecar(spec, "db"), _addresses(spec))
    assert _values(db, "--volume") == []
    assert not {"--userns=keep-id", "--workdir", "--cap-drop=ALL"} & set(db)


def test_a_worktree_sidecars_port_is_forwarded_to_it(tmp_path):
    spec = _worktree_spec(tmp_path)
    addresses = _addresses(spec)
    argv = runtime.Runtime("podman").gateway_args(spec, addresses)
    assert _values(argv, "--forward") == [f"30000:{addresses.services['lod']}:8081"]
    assert spec.environment(PROXY_IP)["LOD_PUBLIC_PORT"] == "30000"


def test_the_cache_is_mounted_in_the_target_from_one_volume_per_repo_and_target(tmp_path):
    spec = _worktree_spec(tmp_path)
    other_instance = _worktree_spec(tmp_path, names=runtime.Names("default", "def456"))
    volumes = _values(runtime.Runtime("podman").target_args(spec, _addresses(spec)), "--volume")

    assert f"{spec.cache}:/cache" in volumes
    assert spec.cache == other_instance.cache
    assert spec.cache.startswith("ac-default-cache-kolibri-web-")
    assert spec.cache == _worktree_spec(tmp_path, repo="LearningEquality/Kolibri").cache
    assert spec.cache != _worktree_spec(tmp_path, names=runtime.Names("other", "abc123")).cache
    assert _worktree_spec(tmp_path, repo="a-b/c").cache != _worktree_spec(tmp_path, repo="a/b-c").cache
    assert spec.labels("target")[runtime.LABEL_CACHE] == spec.cache


def test_cache_labels_name_no_instance_or_worktree(tmp_path):
    labels = _worktree_spec(tmp_path).cache_labels()
    assert labels == {
        runtime.LABEL_NAMESPACE: "default",
        runtime.LABEL_REPO: "learningequality/kolibri",
        runtime.LABEL_TARGET: "web",
        runtime.LABEL_ROLE: "cache",
    }


def test_a_target_without_a_cache_mounts_none(tmp_path):
    spec = _spec(tmp_path)
    assert spec.cache == ""
    assert runtime.LABEL_CACHE not in spec.labels("target")


class _Recorded:
    def __init__(self, stdout=""):
        self.calls = []
        self.stdout = stdout

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(args, 0, '{"Names": ["ac-x"], "Labels": {}}\n', "")
        return subprocess.CompletedProcess(args, 0, self.stdout, "")


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_removal_uses_only_flags_the_runtime_accepts(monkeypatch, name):
    rt = runtime.Runtime(name)
    recorded = _Recorded()
    monkeypatch.setattr(rt, "run", recorded)
    rt.remove({"k": "v"}, volumes=False)
    removal = next(call for call in recorded.calls if call[0] == "rm")
    assert ("-t" in removal) is (name == "podman")


def test_docker_without_an_answering_daemon_counts_as_rootful(monkeypatch):
    rt = runtime.Runtime("docker")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker")))
    assert rt.user_args()[:1] == ["--user"]


@pytest.mark.parametrize(
    ("security", "expected"),
    [('["name=seccomp,profile=default","name=rootless"]', []), ('["name=seccomp,profile=default"]', ["--user"])],
)
def test_rootless_docker_runs_as_the_containers_root(monkeypatch, security, expected):
    rt = runtime.Runtime("docker")
    monkeypatch.setattr(rt, "run", _Recorded(security))
    assert rt.user_args()[:1] == expected


@pytest.mark.parametrize(
    ("image", "qualified"),
    [
        ("postgres:16", "docker.io/library/postgres:16"),
        ("fsouza/fake-gcs-server:1.55.1", "docker.io/fsouza/fake-gcs-server:1.55.1"),
        ("ghcr.io/learningequality/x:1", "ghcr.io/learningequality/x:1"),
        ("localhost/agent:dev", "localhost/agent:dev"),
        ("registry.local:5000/a/b", "registry.local:5000/a/b"),
        ("redis@sha256:abc", "docker.io/library/redis@sha256:abc"),
    ],
)
def test_short_sidecar_images_mean_docker_hub_as_compose_does(tmp_path, image, qualified):
    compose = f"services:\n  side:\n    image: {image}\n  app:\n    build: .\n    x-agent: {{}}\n"
    spec = _spec(tmp_path, compose=compose, target="app", host_ports={}, git_mounts=())
    argv = runtime.Runtime("podman").sidecar_args(spec, spec.sidecars[0], _addresses(spec))
    assert qualified in argv


@pytest.mark.parametrize(("name", "inhibited"), [("docker", True), ("podman", False)])
def test_only_docker_is_told_to_keep_the_host_off_the_bridge(tmp_path, name, inhibited):
    argv = runtime.Runtime(name).network_args(_spec(tmp_path), "10.89.0.0/24")

    assert ("com.docker.network.bridge.inhibit_ipv4=true" in _values(argv, "--opt")) is inhibited


def test_the_internal_subnet_never_overlaps_a_host_network(tmp_path, monkeypatch):
    spec = _spec(tmp_path, denied=(ipaddress.ip_network("10.200.0.0/16"),))
    picks = iter([(200 - 128) * 256 + 7, (201 - 128) * 256 + 7])
    monkeypatch.setattr(runtime.random, "randrange", lambda stop: next(picks))
    rt = runtime.Runtime("podman")
    recorded = _Recorded()
    monkeypatch.setattr(rt, "run", recorded)

    addresses = rt.create_network(spec)

    assert addresses.subnet == "10.201.7.0/24"
    assert [call for call in recorded.calls if "10.200.7.0/24" in call] == []


@pytest.mark.parametrize(
    ("denied", "pool"),
    [(("10.0.0.0/8",), "172.16.0.0/12"), (("10.0.0.0/8", "172.16.0.0/12"), "192.168.0.0/16")],
)
def test_a_vpn_route_over_a_whole_pool_moves_the_subnet_to_the_next(tmp_path, monkeypatch, denied, pool):
    spec = _spec(tmp_path, denied=tuple(ipaddress.ip_network(d) for d in denied))
    rt = runtime.Runtime("podman")
    monkeypatch.setattr(rt, "run", _Recorded())

    addresses = rt.create_network(spec)

    assert ipaddress.ip_network(addresses.subnet).subnet_of(ipaddress.ip_network(pool))


def test_no_free_pool_fails_without_creating_a_network(tmp_path, monkeypatch):
    denied = tuple(ipaddress.ip_network(d) for d in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    rt = runtime.Runtime("podman")
    recorded = _Recorded()
    monkeypatch.setattr(rt, "run", recorded)

    with pytest.raises(runtime.ToolError, match="10.0.0.0/8"):
        rt.create_network(_spec(tmp_path, denied=denied))
    assert recorded.calls == []


@pytest.mark.parametrize(
    ("name", "rootless", "claims"), [("docker", False, True), ("docker", True, False), ("podman", False, False)]
)
def test_only_rootful_docker_hands_new_target_volumes_to_the_user(monkeypatch, name, rootless, claims):
    rt = runtime.Runtime(name)
    rt._rootless = rootless
    recorded = _Recorded()
    monkeypatch.setattr(rt, "run", recorded)

    rt.claim_volume("img:default", "ac-x-state", "/state")

    helper = [call for call in recorded.calls if "chown" in call]
    assert bool(helper) is claims
    if claims:
        argv = helper[0]
        assert argv[-4:] == ["img:default", "-R", f"{os.getuid()}:{os.getgid()}", "/state"]
        assert "--privileged" not in argv and _values(argv, "--network") == ["none"]
        assert "ac-x-state:/state" in _values(argv, "--volume")


@pytest.mark.parametrize(("installed", "chosen"), [({"podman", "docker"}, "podman"), ({"docker"}, "docker")])
def test_podman_is_preferred_when_both_are_installed(monkeypatch, installed, chosen):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)
    assert runtime.detect().name == chosen


def test_normal_checkout_mounts_its_git_dir_read_only(tmp_path):
    _git(tmp_path, "init", "-q")
    assert runtime.git_mounts(tmp_path, "/work") == ((str(tmp_path / ".git"), "/work/.git"),)


def test_macos_ifconfig_addresses_become_networks():
    text = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
	inet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet6 fe80::1c2b:3a4d:5e6f:7a8b%en0 prefixlen 64 secured scopeid 0x6
	inet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
	inet6 2001:db8:1234:5600:aaaa::1 prefixlen 64 autoconf secured
"""
    addresses = runtime._ifconfig_addresses(text)
    networks = {runtime._site(ipaddress.ip_interface(a).network) for a in addresses}
    assert ipaddress.ip_network("192.168.1.0/24") in networks
    assert ipaddress.ip_network("127.0.0.0/8") in networks


def test_private_routes_become_networks_but_default_and_public_routes_do_not():
    routes = [
        {"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0"},
        {"dst": "0.0.0.0/1", "dev": "tun0"},
        {"dst": "128.0.0.0/1", "dev": "tun0"},
        {"dst": "10.0.0.0/8", "dev": "tun0"},
        {"dst": "172.16.4.7", "dev": "wg0"},
        {"dst": "192.168.1.0/24", "dev": "wlan0", "scope": "link"},
    ]

    assert runtime._routed_networks(routes) == ["10.0.0.0/8", "172.16.4.7", "192.168.1.0/24"]


def test_global_ipv6_networks_widen_to_the_site():
    site = ipaddress.ip_network("2a01:4f8:1234:5600::/56")
    assert runtime._site(ipaddress.ip_network("2a01:4f8:1234:5678::/64")) == site
    assert runtime._site(ipaddress.ip_network("10.0.0.0/24")) == ipaddress.ip_network("10.0.0.0/24")
