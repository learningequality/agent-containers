from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

import yaml

from agent_containers.errors import ToolError

DEVICES = ("kvm",)
SERVE = "serve"
PROXY_PORT = 3128
X_AGENT_KEYS = frozenset({"commands", "hosts", "ports", "state", "ready", "devices"})
READY_KEYS = frozenset({"timeout", "path", "port", "command"})

_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERPOLATION = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Build:
    context: str = "."
    dockerfile: str = "Dockerfile"
    stage: str = ""


@dataclass(frozen=True)
class Port:
    start: int
    end: int
    env: str

    def __post_init__(self) -> None:
        if not (0 < self.start <= self.end < 65536):
            raise ValueError(f"port range {self.start}-{self.end} is not within 1-65535 in order")
        if self.covers(PROXY_PORT):
            raise ValueError(f"port {PROXY_PORT} is the gateway's proxy; declare another port")
        if not _ENV_NAME.match(self.env):
            raise ValueError(f"port {self.start}: env {self.env!r} is not a variable name")

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    def covers(self, port: int) -> bool:
        return self.start <= port <= self.end


@dataclass(frozen=True)
class Ready:
    timeout: int = 600
    path: str = ""
    port: int = 0
    command: str = ""

    def __post_init__(self) -> None:
        for name, kind in (("timeout", int), ("port", int), ("path", str), ("command", str)):
            value = getattr(self, name)
            if not isinstance(value, kind) or isinstance(value, bool):
                raise ValueError(f"ready's {name} must be {'a number' if kind is int else 'text'}, not {value!r}")
        probe = bool(self.path or self.port)
        if probe == bool(self.command):
            raise ValueError("ready needs either path and port, or command")
        if probe and not (self.path.startswith("/") and self.port):
            raise ValueError("ready's probe needs a path starting with / and a port")
        if self.timeout <= 0:
            raise ValueError("ready's timeout must be positive")


@dataclass(frozen=True)
class Volume:
    name: str
    path: str


@dataclass(frozen=True)
class Healthcheck:
    test: tuple[str, ...]
    interval: str = ""
    timeout: str = ""
    retries: int = 0
    start_period: str = ""


@dataclass(frozen=True)
class Target:
    name: str
    build: Build
    environment: dict[str, str] = field(default_factory=dict)
    working_dir: str = ""
    volumes: tuple[Volume, ...] = ()
    commands: dict[str, str] = field(default_factory=dict)
    hosts: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    state: str = ""
    ready: Ready | None = None
    devices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for device in self.devices:
            if device not in DEVICES:
                raise ValueError(f"target {self.name!r}: device {device!r} is not one of {DEVICES}")
        for host in self.hosts:
            _check_host(host)
        if SERVE in self.commands and self.ready is None:
            raise ValueError(f"target {self.name!r}: {SERVE} needs ready")
        if self.ready and self.ready.port and not any(p.covers(self.ready.port) for p in self.ports):
            raise ValueError(f"target {self.name!r}: ready's port {self.ready.port} is not a declared port")
        if self.state and not self.state.startswith("/"):
            raise ValueError(f"target {self.name!r}: state {self.state!r} is not an absolute path")

    @property
    def serves(self) -> bool:
        return SERVE in self.commands


@dataclass(frozen=True)
class Sidecar:
    name: str
    image: str
    environment: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    healthcheck: Healthcheck | None = None
    volumes: tuple[Volume, ...] = ()

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError(f"sidecar {self.name!r} has no image")


@dataclass(frozen=True)
class Declaration:
    targets: dict[str, Target] = field(default_factory=dict)
    sidecars: dict[str, Sidecar] = field(default_factory=dict)


def load(path: Path) -> Declaration:
    try:
        return parse(path.read_text())
    except (OSError, ValueError) as exc:
        raise ToolError(f"{path}: {exc}") from exc


def parse(text: str) -> Declaration:
    try:
        return _parse(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed compose file: {exc}") from exc


def _parse(text: str) -> Declaration:
    document = _mapping("the compose file", _interpolate(yaml.safe_load(text)))
    services = _mapping("services", document.get("services"))
    named = set(_mapping("volumes", document.get("volumes")))
    targets: dict[str, Target] = {}
    sidecars: dict[str, Sidecar] = {}
    unrunnable: list[str] = []
    for name, service in services.items():
        service = _mapping(f"service {name!r}", service)
        if "x-agent" in service:
            targets[name] = _target(name, service, named)
        elif service.get("image"):
            sidecars[name] = _sidecar(name, service, named)
        else:
            unrunnable.append(name)
    if targets and unrunnable:
        names = ", ".join(repr(name) for name in unrunnable)
        raise ValueError(f"service {names} needs an image or an x-agent block: the tool builds only targets")
    return Declaration(targets=targets, sidecars=sidecars)


def _target(name: str, service: dict[str, Any], named: set[str]) -> Target:
    where = f"target {name!r}"
    agent = _mapping(f"{where}: x-agent", service["x-agent"])
    if "build" not in service:
        raise ValueError(f"{where} has no build: the tool builds every target's image")
    _known_keys(f"{where}: x-agent", agent, X_AGENT_KEYS)
    ready = agent.get("ready")
    if ready is not None:
        _known_keys(f"{where}: ready", ready, READY_KEYS)
    return Target(
        name=name,
        build=_build(service["build"]),
        environment=_environment(f"{where}: environment", service.get("environment")),
        working_dir=str(service.get("working_dir") or ""),
        volumes=_volumes(f"{where}: volumes", service.get("volumes"), named),
        commands={str(k): str(v) for k, v in _mapping(f"{where}: commands", agent.get("commands")).items()},
        hosts=tuple(str(h) for h in _sequence(f"{where}: hosts", agent.get("hosts"))),
        ports=tuple(_port(str(k), v) for k, v in _mapping(f"{where}: ports", agent.get("ports")).items()),
        state=str(agent.get("state") or ""),
        ready=Ready(**{k: ready[k] for k in ("timeout", "path", "port", "command") if k in ready}) if ready else None,
        devices=tuple(str(d) for d in _sequence(f"{where}: devices", agent.get("devices"))),
    )


def _mapping(where: str, value: Any) -> dict[Any, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} is not a mapping")
    return value


def _sequence(where: str, value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where} is not a list")
    return value


def _known_keys(where: str, value: Any, known: frozenset[str]) -> None:
    unknown = sorted(set(map(str, _mapping(where, value))) - known)
    if unknown:
        raise ValueError(f"{where} has unknown keys {', '.join(unknown)} (known: {', '.join(sorted(known))})")


def _sidecar(name: str, service: dict[str, Any], named: set[str]) -> Sidecar:
    where = f"service {name!r}"
    command = service.get("command")
    return Sidecar(
        name=name,
        image=str(service.get("image") or ""),
        environment=_environment(f"{where}: environment", service.get("environment")),
        command=(
            tuple(shlex.split(command))
            if isinstance(command, str)
            else tuple(str(c) for c in _sequence(f"{where}: command", command))
        ),
        healthcheck=_healthcheck(f"{where}: healthcheck", service.get("healthcheck")),
        volumes=_volumes(f"{where}: volumes", service.get("volumes"), named),
    )


def _build(value: Any) -> Build:
    if isinstance(value, str):
        return Build(context=value)
    if not isinstance(value, dict):
        raise ValueError("build is neither a path nor a mapping")
    return Build(
        context=str(value.get("context") or "."),
        dockerfile=str(value.get("dockerfile") or "Dockerfile"),
        stage=str(value.get("target") or ""),
    )


def _environment(where: str, value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    pairs = (str(item).partition("=") for item in _sequence(where, value))
    return {key: val for key, _, val in pairs}


def _volumes(where: str, value: Any, named: set[str]) -> tuple[Volume, ...]:
    kept = []
    for entry in _sequence(where, value):
        if isinstance(entry, dict):
            if entry.get("type", "volume") != "volume":
                continue
            source, path = str(entry.get("source") or ""), str(entry.get("target") or "")
        else:
            source, _, rest = str(entry).partition(":")
            path = rest.partition(":")[0]
        if source in named and path:
            kept.append(Volume(source, path))
    return tuple(kept)


def _healthcheck(where: str, value: Any) -> Healthcheck | None:
    value = _mapping(where, value)
    if not value or value.get("disable"):
        return None
    test = value.get("test")
    retries = value.get("retries") or 0
    if not isinstance(retries, (int, str)) or isinstance(retries, bool) or not str(retries).isdigit():
        raise ValueError(f"{where}: retries must be a number, not {retries!r}")
    return Healthcheck(
        test=(test,) if isinstance(test, str) else tuple(str(t) for t in _sequence(f"{where}: test", test)),
        interval=str(value.get("interval") or ""),
        timeout=str(value.get("timeout") or ""),
        retries=int(retries),
        start_period=str(value.get("start_period") or ""),
    )


def _port(key: str, value: Any) -> Port:
    start, sep, end = key.partition("-")
    if not (start.isdigit() and (not sep or end.isdigit())):
        raise ValueError(f"port {key!r} is neither a port nor a range")
    if not isinstance(value, dict) or "env" not in value:
        raise ValueError(f"port {key!r} needs {{env: VAR}}")
    return Port(int(start), int(end or start), str(value["env"]))


def _check_host(host: str) -> None:
    name = host[2:] if host.startswith("*.") else host
    labels = name.split(".")
    if not name or not all(_HOST_LABEL.match(label) for label in labels):
        raise ValueError(f"host {host!r} is not a hostname or *.suffix")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _INTERPOLATION.sub(_substitute, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _substitute(match: re.Match[str]) -> str:
    if match.group(0) == "$$":
        return "$"
    operator, default = match.group(2), match.group(3)
    return default if operator in ("-", ":-") else ""
