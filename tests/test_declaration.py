import re
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_containers.declaration import Build
from agent_containers.declaration import Healthcheck
from agent_containers.declaration import Port
from agent_containers.declaration import Ready
from agent_containers.declaration import Volume
from agent_containers.declaration import parse

FIXTURES = Path(__file__).parent / "fixtures"

OWNED_FIELDS = """
    privileged: true
    cap_add: [SYS_ADMIN]
    devices: ["/dev/sda:/dev/sda"]
    network_mode: host
    pid: host
    ipc: host
    security_opt: ["seccomp=unconfined"]
    userns_mode: host
    extra_hosts: ["metadata:169.254.169.254"]
    ports: ["127.0.0.1:9999:80"]
    volumes:
      - /etc:/host-etc
      - ./src:/src
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
      - keep-me:/data
"""

OWNED_SUBSTRINGS = ["SYS_ADMIN", "/dev/sda", "seccomp", "169.254.169.254", "9999", "/etc", "./src", "docker.sock"]
OWNED_EXACT = ["host", "privileged", "network_mode", "pid", "ipc", "userns_mode", "extra_hosts", "cap_add"]


def _values(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _values(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _values(item)
    else:
        yield str(obj)


def test_tool_owned_fields_never_reach_the_model():
    text = f"""
services:
  app:
    build: .
{OWNED_FIELDS}
    x-agent:
      commands: {{test: make test}}
  side:
    image: redis:7
{OWNED_FIELDS}
volumes:
  keep-me:
"""
    declaration = parse(text)
    rendered = list(_values(asdict(declaration)))
    leaked = [value for value in OWNED_SUBSTRINGS if any(value in item for item in rendered)]
    leaked += [value for value in OWNED_EXACT if value in rendered]
    assert leaked == []
    assert declaration.targets["app"].volumes == (Volume("keep-me", "/data"),)
    assert declaration.sidecars["side"].volumes == (Volume("keep-me", "/data"),)


def test_interpolation_takes_defaults_never_the_environment(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "ghp_leaked")
    declaration = parse(
        """
services:
  db:
    image: "postgres:${PG_VERSION:-16}"
    environment:
      TOKEN: "${SECRET_TOKEN:-none}"
      BARE: "${SECRET_TOKEN}"
      COST: "$$5"
"""
    )
    sidecar = declaration.sidecars["db"]
    assert sidecar.image == "postgres:16"
    assert sidecar.environment == {"TOKEN": "none", "BARE": "", "COST": "$5"}


def test_list_form_environment_and_string_command_are_normalised():
    declaration = parse(
        """
services:
  db:
    image: postgres:16
    environment: ["A=1", "B=two=2"]
    command: postgres -c fsync=off
"""
    )
    sidecar = declaration.sidecars["db"]
    assert sidecar.environment == {"A": "1", "B": "two=2"}
    assert sidecar.command == ("postgres", "-c", "fsync=off")


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("services: [unclosed", "malformed"),
        ("volumes: [data]\nservices: {}\n", "volumes"),
        ("services:\n  db:\n    image: x\n    healthcheck: pg_isready\n", "'db': healthcheck"),
        ("services:\n  db:\n    image: x\n    healthcheck: {test: 5}\n", "'db': healthcheck: test"),
        (
            "services:\n  db:\n    image: x\n    healthcheck: {test: [CMD, x], retries: [1]}\n",
            "'db': healthcheck: retries",
        ),
        ("services:\n  db:\n    image: x\n    environment: 5\n", "'db': environment"),
        ("services:\n  db:\n    image: x\n    volumes: 5\n", "'db': volumes"),
        ("services:\n  db:\n    image: x\n    command: 5\n", "'db': command"),
        ("services:\n  app:\n    build: .\n    x-agent:\n      commands: [make test]\n", "'app': commands"),
        ("services:\n  app:\n    build: .\n    x-agent:\n      ports: [8000]\n", "'app': ports"),
        ("services:\n  app:\n    build: .\n    x-agent:\n      hosts: 5\n", "'app': hosts"),
        ("services:\n  app:\n    build: .\n    x-agent:\n      devices: 3\n", "'app': devices"),
        (
            "services:\n  app:\n    build: .\n    x-agent:\n      ports: {'8000': {env: P}}\n"
            "      ready: {path: /, port: 8000, timeout: soon}\n",
            "timeout",
        ),
        ("services:\n  app:\n    build: .\n    x-agent:\n      host: [pypi.org]\n", "unknown keys host ("),
        (
            "services:\n  app:\n    build: .\n    x-agent:\n      ready: {command: 'true', timout: 30}\n",
            "unknown keys timout (",
        ),
    ],
)
def test_a_malformed_declaration_is_a_value_error_naming_the_problem(text, named):
    with pytest.raises(ValueError, match=re.escape(named)):
        parse(text)


def test_unknown_device_fails():
    with pytest.raises(ValueError, match="device"):
        parse(
            """
services:
  app:
    build: .
    x-agent:
      devices: [gpu]
"""
        )


def test_commands_are_optional():
    declaration = parse(
        """
services:
  app:
    build: .
    x-agent: {}
"""
    )
    assert declaration.targets["app"].commands == {}


def test_serve_needs_ready():
    with pytest.raises(ValueError, match="ready"):
        parse(
            """
services:
  app:
    build: .
    x-agent:
      commands: {serve: make serve}
"""
        )


@pytest.mark.parametrize(
    "ready",
    [
        "{path: /, port: 8000, command: 'true'}",
        "{timeout: 10}",
        "{path: /, port: 9000}",
    ],
)
def test_ready_is_a_declared_http_probe_or_a_command(ready):
    with pytest.raises(ValueError, match="ready"):
        parse(
            f"""
services:
  app:
    build: .
    x-agent:
      ports: {{"8000": {{env: APP_PORT}}}}
      ready: {ready}
"""
        )


def test_ready_command_form():
    declaration = parse(
        """
services:
  android:
    build: {context: platforms/android, dockerfile: Containerfile}
    x-agent:
      commands: {serve: make emulator}
      devices: [kvm]
      ports: {"5037": {env: ADB_SERVER_PORT}}
      ready: {command: "adb shell getprop sys.boot_completed | grep -q 1", timeout: 3600}
"""
    )
    target = declaration.targets["android"]
    assert target.ready == Ready(timeout=3600, command="adb shell getprop sys.boot_completed | grep -q 1")
    assert target.devices == ("kvm",)
    assert target.build == Build(context="platforms/android", dockerfile="Containerfile")


@pytest.mark.parametrize("host", ["https://pypi.org", "pypi.org:443", "*", "*.", "pypi..org", ""])
def test_malformed_host_fails(host):
    with pytest.raises(ValueError, match="host"):
        parse(
            f"""
services:
  app:
    build: .
    x-agent:
      hosts: ["{host}"]
"""
        )


@pytest.mark.parametrize("port", ["0", "70000", "4100-4000", "http", "1-2-3", "3128", "3000-3200"])
def test_malformed_port_fails(port):
    with pytest.raises(ValueError, match="port"):
        parse(
            f"""
services:
  app:
    build: .
    x-agent:
      ports: {{"{port}": {{env: P}}}}
"""
        )


SIDECAR_PORTS = """
services:
  app:
    build: .
    x-agent:
      ports:
        "8000": {{env: APP_PORT}}
        "{key}": {{env: SIDE_PORT}}
  other:
    build: .
    x-agent: {{}}
  gcs:
    image: fsouza/fake-gcs-server:1.55.1
"""


@pytest.mark.parametrize(
    ("key", "port"),
    [("gcs:4443", Port(4443, 4443, "SIDE_PORT", "gcs")), ("gcs:4443-4445", Port(4443, 4445, "SIDE_PORT", "gcs"))],
)
def test_a_port_may_name_a_sidecar_declared_after_the_target(key, port):
    ports = parse(SIDECAR_PORTS.format(key=key)).targets["app"].ports
    assert ports == (Port(8000, 8000, "APP_PORT"), port)
    assert port.key == key.partition("-")[0]
    assert ports[0].key == 8000


@pytest.mark.parametrize(
    ("key", "named"),
    [
        ("cache:6379", "target 'app': port 'cache:6379' names no sidecar (sidecars: gcs)"),
        ("other:9000", "target 'app': port 'other:9000' names no sidecar (sidecars: gcs)"),
        ("app:9000", "target 'app': port 'app:9000' names the target itself; write '9000'"),
        ("gcs:", "port 'gcs:' is neither a port nor a range"),
        (":4443", "port ':4443' is neither a port nor a range"),
    ],
)
def test_a_port_naming_anything_but_a_sidecar_fails(key, named):
    with pytest.raises(ValueError, match=re.escape(named)):
        parse(SIDECAR_PORTS.format(key=key))


def test_ready_probes_only_the_targets_own_ports():
    text = SIDECAR_PORTS.format(key="gcs:4443").replace(
        "x-agent:\n      ports:", "x-agent:\n      ready: {path: /, port: 4443}\n      ports:", 1
    )
    with pytest.raises(ValueError, match="ready's port 4443"):
        parse(text)


WORKTREE_SIDECARS = """
services:
  app:
    build: .
    x-agent:
      worktree_sidecars: {names}
      cache: {cache}
  other:
    build: .
    x-agent: {{}}
  lod:
    image: ghcr.io/learningequality/kolibri/web:default
  db:
    image: postgres:16
"""


def test_worktree_sidecars_and_cache_parse():
    target = parse(WORKTREE_SIDECARS.format(names="[lod]", cache="/cache")).targets["app"]
    assert (target.worktree_sidecars, target.cache) == (("lod",), "/cache")


@pytest.mark.parametrize(
    ("names", "cache", "named"),
    [
        ("[web]", "/cache", "target 'app': worktree_sidecars 'web' names no sidecar (sidecars: db, lod)"),
        ("[other]", "/cache", "target 'app': worktree_sidecars 'other' names no sidecar (sidecars: db, lod)"),
        ("[app]", "/cache", "target 'app': worktree_sidecars 'app' names the target itself"),
        ("lod", "/cache", "target 'app': worktree_sidecars is not a list"),
        ("[lod]", "cache", "target 'app': cache 'cache' is not an absolute path"),
    ],
)
def test_a_bad_worktree_sidecar_or_cache_fails(names, cache, named):
    with pytest.raises(ValueError, match=re.escape(named)):
        parse(WORKTREE_SIDECARS.format(names=names, cache=cache))


DEPENDS_ON = """
services:
  web:
    build: .
    {depends_on}
    x-agent:
      {agent}
  server:
    build: .
    x-agent: {{}}
  lod:
    image: kolibri:web
  db:
    image: postgres:16
  redis:
    image: redis:7
"""


@pytest.mark.parametrize(
    ("depends_on", "scoped"),
    [
        ("depends_on: [lod]", ("lod",)),
        ("depends_on: {lod: {condition: service_started}, db: {condition: service_healthy}}", ("lod", "db")),
        ("depends_on: []", ()),
        ("depends_on: [server]", ("server",)),
        ("", None),
    ],
)
def test_depends_on_takes_the_list_or_mapping_form_and_absent_means_every_sidecar(depends_on, scoped):
    declaration = parse(DEPENDS_ON.format(depends_on=depends_on, agent=""))
    assert declaration.targets["web"].depends_on == scoped
    assert declaration.targets["server"].depends_on is None
    expected = ("lod", "db", "redis") if scoped is None else tuple(n for n in ("lod", "db", "redis") if n in scoped)
    assert tuple(s.name for s in declaration.sidecars_of("web")) == expected
    assert tuple(s.name for s in declaration.sidecars_of("server")) == ("lod", "db", "redis")


@pytest.mark.parametrize(
    ("depends_on", "agent", "named"),
    [
        ("depends_on: [nginx]", "", "target 'web': depends_on 'nginx' names no service"),
        ("depends_on: [web]", "", "target 'web': depends_on 'web' names the target itself"),
        (
            "depends_on: [server, lod]",
            'ports: {"server:9000": {env: SERVER_PORT}}',
            "target 'web': port 'server:9000' names no sidecar in depends_on (depends_on: lod)",
        ),
        ("depends_on: lod", "", "target 'web': depends_on is neither a list nor a mapping"),
        (
            "depends_on: [lod]",
            "worktree_sidecars: [db]",
            "target 'web': worktree_sidecars 'db' names no sidecar in depends_on (depends_on: lod)",
        ),
        (
            "depends_on: [lod]",
            'ports: {"redis:6379": {env: REDIS_PORT}}',
            "target 'web': port 'redis:6379' names no sidecar in depends_on (depends_on: lod)",
        ),
    ],
)
def test_depends_on_names_sidecars_and_bounds_ports_and_worktree_sidecars(depends_on, agent, named):
    with pytest.raises(ValueError, match=re.escape(named)):
        parse(DEPENDS_ON.format(depends_on=depends_on, agent=agent))


def test_a_compose_without_targets_ignores_services_it_cannot_run():
    undeclared = (FIXTURES / "kdp-compose.yml").read_text().split("    x-agent:")[0]
    declaration = parse(undeclared + "volumes:\n  kdp-postgres:\n")
    assert declaration.targets == {}


def test_a_declaring_compose_names_a_build_only_service_it_cannot_run():
    with pytest.raises(ValueError, match="'worker'.*image or an x-agent"):
        parse("services:\n  worker:\n    build: .\n  app:\n    build: .\n    x-agent: {}\n")


def test_target_without_build_fails():
    with pytest.raises(ValueError, match="build"):
        parse(
            """
services:
  app:
    image: ghcr.io/someone/else:latest
    x-agent: {}
"""
        )


def test_kdp_compose_parses_to_its_target_and_sidecars():
    declaration = parse((FIXTURES / "kdp-compose.yml").read_text())

    assert set(declaration.targets) == {"web"}
    assert set(declaration.sidecars) == {"db", "gcs"}

    web = declaration.targets["web"]
    assert web.build == Build(context=".", dockerfile="Dockerfile", stage="dev")
    assert web.environment == {"STORAGE_EMULATOR_HOST": "http://gcs:4443"}
    assert web.volumes == ()
    assert web.commands["serve"] == "pnpm run devserver"
    assert web.hosts == ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
    assert web.ports == (Port(8000, 8000, "KDP_PORT"), Port(4000, 4100, "WEBPACK_DEV_PUBLIC_PORT"))
    assert web.state == "/kolibri"
    assert web.ready == Ready(timeout=900, path="/portal/", port=8000)

    db = declaration.sidecars["db"]
    assert db.image == "postgres:16"
    assert db.volumes == (Volume("kdp-postgres", "/var/lib/postgresql/data"),)
    assert db.healthcheck == Healthcheck(test=("CMD-SHELL", "pg_isready -U kdp_postgres"), interval="2s", retries=30)

    gcs = declaration.sidecars["gcs"]
    assert gcs.volumes == ()
    assert gcs.command[:2] == ("-scheme", "http")
