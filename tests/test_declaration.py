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
