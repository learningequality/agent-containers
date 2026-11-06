import ipaddress
import os
import socket
import time
import urllib.request
import uuid

import pytest

from agent_containers import runtime
from agent_containers.declaration import parse

pytestmark = pytest.mark.live

IMAGE = runtime.GATEWAY_IMAGE

COMPOSE = f"""
services:
  db:
    image: {IMAGE}
    command: ["python3", "-m", "http.server", "5432"]
  app:
    build: .
    x-agent:
      hosts: [pypi.org]
      ports:
        "8000": {{env: APP_PORT}}
        "4000-4100": {{env: APP_RANGE}}
"""


def bring_up(rt, spec):
    addresses = rt.create_network(spec)
    rt.start_gateway(spec, addresses)
    for sidecar in spec.sidecars:
        rt.start_sidecar(spec, sidecar, addresses)
    rt.start_target(spec, addresses)
    container = spec.names.container(spec.target.name)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        probe = rt.run(["exec", container, "test", "-f", runtime.READY_FILE], what="probe", check=False)
        if probe.returncode == 0:
            return container, addresses
        time.sleep(0.5)
    raise AssertionError(rt.logs(container))


@pytest.fixture
def instance(rt, tmp_path, namespace):
    rt.run(["pull", IMAGE], what="pull", timeout=600)
    declaration = parse(COMPOSE)
    target = declaration.targets["app"]
    spec = runtime.Spec(
        names=runtime.Names(namespace, uuid.uuid4().hex[:10]),
        worktree=tmp_path,
        mount="/workspace",
        target=target,
        image=IMAGE,
        sidecars=tuple(declaration.sidecars.values()),
        host_ports=runtime.reserve_host_ports(target.ports),
        denied=runtime.host_networks(),
    )
    container, addresses = bring_up(rt, spec)
    return rt, spec, container, addresses


def _exec(rt, container, code):
    return rt.run(["exec", container, "python3", "-c", code], what="exec", check=False, timeout=60)


def _connect(rt, container, address, port):
    code = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(({address!r}, {port}), timeout=5)\n"
        "    print('connected')\n"
        "except OSError as exc:\n"
        "    print('refused', exc)\n"
    )
    result = _exec(rt, container, code)
    assert result.stdout.startswith(("connected", "refused")), result.stderr
    return result.stdout


def test_allowlisted_host_succeeds_through_the_proxy(instance):
    rt, _, container, _ = instance
    code = "import urllib.request; print(urllib.request.urlopen('https://pypi.org/simple/pip/', timeout=20).status)"
    result = _exec(rt, container, code)
    assert result.stdout.strip() == "200", result.stderr


def test_unlisted_host_is_refused_by_the_proxy(instance):
    rt, _, container, _ = instance
    result = _exec(rt, container, "import urllib.request; urllib.request.urlopen('https://example.com/', timeout=20)")
    assert result.returncode != 0
    assert "403" in result.stderr


def test_a_direct_connection_bypassing_the_proxy_fails(instance):
    rt, spec, container, _ = instance
    outside = socket.gethostbyname("pypi.org")
    assert _connect(rt, spec.names.gateway, outside, 443).startswith("connected")
    assert _connect(rt, container, outside, 443).startswith("refused")


def test_the_host_is_unreachable_from_the_target(instance):
    rt, _, container, addresses = instance
    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen()
    port = listener.getsockname()[1]
    bridge = str(next(ipaddress.ip_network(addresses.subnet).hosts()))
    try:
        candidates = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        candidates |= {bridge, "10.0.2.2", "host.containers.internal", "host.docker.internal"}
        for address in candidates:
            assert _connect(rt, container, address, port).startswith("refused"), address
    finally:
        listener.close()


def test_external_names_do_not_resolve(instance):
    rt, _, container, _ = instance
    result = _exec(rt, container, "import socket; print(socket.gethostbyname('example.com'))")
    assert result.returncode != 0


def test_a_sidecar_resolves_by_its_compose_name(instance):
    rt, _, container, _ = instance
    deadline = time.monotonic() + 30
    while True:
        code = "import urllib.request; print(urllib.request.urlopen('http://db:5432/', timeout=5).status)"
        result = _exec(rt, container, code)
        if result.stdout.strip() == "200" or time.monotonic() > deadline:
            break
        time.sleep(1)
    assert result.stdout.strip() == "200", result.stderr


def test_the_published_host_port_reaches_the_serve_port(instance):
    rt, spec, container, _ = instance
    rt.run(["exec", "-d", container, "python3", "-m", "http.server", "8000"], what="serve")
    url = f"http://127.0.0.1:{spec.host_ports[8000]}/"
    deadline = time.monotonic() + 30
    while True:
        try:
            assert urllib.request.urlopen(url, timeout=5).status == 200
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def test_the_target_is_told_its_host_ports(instance):
    rt, spec, container, _ = instance
    result = rt.run(["exec", container, "sh", "-c", "echo $APP_PORT $APP_RANGE"], what="echo")
    assert result.stdout.split() == [str(spec.host_ports[8000]), str(spec.host_ports[4000])]


def test_orphaned_processes_are_reaped(instance):
    rt, _, container, _ = instance
    rt.run(["exec", container, "sh", "-c", "sleep 0.2 & exit 0"], what="orphan")
    time.sleep(2)
    code = (
        "import glob\n"
        "print(sum(open(p).read().rsplit(')', 1)[1].split()[0] == 'Z' for p in glob.glob('/proc/[0-9]*/stat')))"
    )
    assert _exec(rt, container, code).stdout.strip() == "0"


def test_files_the_target_writes_belong_to_the_invoking_user(instance):
    rt, spec, container, _ = instance
    rt.run(["exec", container, "sh", "-c", "echo hi > /workspace/written"], what="write")
    stat = (spec.worktree / "written").stat()
    assert (stat.st_uid, stat.st_gid) == (os.getuid(), os.getgid())
