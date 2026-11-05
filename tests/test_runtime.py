import ipaddress
import socket
import subprocess
from pathlib import Path

import pytest

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
    assert "127.0.0.1:28000:8000" in _values(argv, "--publish")
    assert "127.0.0.1:24100:4100" in _values(argv, "--publish")
    assert "8000:10.89.0.3:8000" in argv
    assert "4100:10.89.0.3:4100" in argv
    assert f"{PROXY_IP}:{runtime.PROXY_PORT}" in argv


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


def test_reserved_ranges_are_consecutive_and_disjoint():
    ports = parse((FIXTURES / "kdp-compose.yml").read_text()).targets["web"].ports
    reserved = runtime.reserve_host_ports(ports)
    single, block = reserved[8000], reserved[4000]
    assert single not in range(block, block + 101)
    sockets = [socket.socket() for _ in range(101)]
    try:
        for offset, sock in enumerate(sockets):
            sock.bind(("127.0.0.1", block + offset))
    finally:
        for sock in sockets:
            sock.close()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_linked_worktree_mounts_its_git_file_and_common_dir_read_only(tmp_path):
    bare = tmp_path / "project" / ".bare"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _git(seed, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    worktree = tmp_path / "project" / "feature"
    _git(bare, "worktree", "add", "-q", str(worktree), "main")

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
    first, second = tmp_path / "one.git", tmp_path / "two.git"
    _git(tmp_path, "init", "-q", "--bare", str(first))
    _git(tmp_path, "init", "-q", "--bare", str(second))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {first}\n")
    recorded = runtime.git_dirs(worktree)
    (worktree / ".git").write_text(f"gitdir: {second}\n")

    sources = {source for source, _ in runtime.git_mounts(worktree, "/work", recorded)}

    assert str(second) not in sources
    assert str(first) in sources


@pytest.mark.parametrize("name", ["podman", "docker"])
def test_the_worktree_mount_is_shared_between_instances(tmp_path, monkeypatch, name):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    spec = _spec(tmp_path)
    volumes = _values(runtime.Runtime(name).target_args(spec, _addresses(spec)), "--volume")
    worktree = next(v for v in volumes if v.startswith(f"{tmp_path}:"))
    assert worktree.endswith(":z")


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


def test_global_ipv6_networks_widen_to_the_site():
    site = ipaddress.ip_network("2a01:4f8:1234:5600::/56")
    assert runtime._site(ipaddress.ip_network("2a01:4f8:1234:5678::/64")) == site
    assert runtime._site(ipaddress.ip_network("10.0.0.0/24")) == ipaddress.ip_network("10.0.0.0/24")
