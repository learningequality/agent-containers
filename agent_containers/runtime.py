from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import random
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from agent_containers import gateway
from agent_containers.declaration import PROXY_PORT
from agent_containers.declaration import Port
from agent_containers.declaration import Sidecar
from agent_containers.declaration import Target
from agent_containers.errors import ToolError

GATEWAY_IMAGE = (
    "docker.io/library/python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc"
)
READY_FILE = "/tmp/.agent-containers-ready"
DEFAULT_WORKDIR = "/workspace"
NAMESPACE_ENV = "AGENT_CONTAINERS_NAMESPACE"
DEFAULT_NAMESPACE = "default"
LABEL_NAMESPACE = "agent-containers.namespace"
LABEL_INSTANCE = "agent-containers.instance"
LABEL_WORKTREE = "agent-containers.worktree"
LABEL_ROLE = "agent-containers.role"
LABEL_TARGET = "agent-containers.target"
LABEL_PORTS = "agent-containers.ports"
QUICK_TIMEOUT = 60
SUBNET_ATTEMPTS = 20
SUBNET_POOLS = tuple(ipaddress.IPv4Network(n) for n in ("10.128.0.0/9", "172.16.0.0/12", "192.168.0.0/16"))
GATEWAY_START_TIMEOUT = 60
LOG_TAIL = 2000
# RFC 6177 / RIPE-690: a site is delegated a /48 to /56, so another /64 inside it is another of its LANs.
SITE_PREFIX = 56
# Not `is_private`: before Python 3.13 it checks only the end addresses, so 0.0.0.0/1 counts as private.
PRIVATE_IPV4 = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def namespace() -> str:
    return os.environ.get(NAMESPACE_ENV) or DEFAULT_NAMESPACE


@dataclass(frozen=True)
class Names:
    namespace: str
    instance: str

    @property
    def prefix(self) -> str:
        return f"ac-{self.namespace}-{self.instance}"

    def labels(self) -> dict[str, str]:
        return {LABEL_NAMESPACE: self.namespace, LABEL_INSTANCE: self.instance}

    @property
    def network(self) -> str:
        return f"{self.prefix}-net"

    @property
    def gateway(self) -> str:
        return f"{self.prefix}-gateway"

    def container(self, service: str) -> str:
        return f"{self.prefix}-{service}"

    def volume(self, name: str) -> str:
        return f"{self.prefix}-{name}"

    @property
    def state(self) -> str:
        return self.volume("state")


@dataclass(frozen=True)
class Spec:
    names: Names
    worktree: Path
    mount: str
    target: Target
    image: str
    sidecars: tuple[Sidecar, ...] = ()
    host_ports: dict[int, int] = field(default_factory=dict)
    denied: tuple[Network, ...] = ()
    git_mounts: tuple[tuple[str, str], ...] = ()
    ports_given: bool = True

    def labels(self, role: str) -> dict[str, str]:
        labels = {
            **self.names.labels(),
            LABEL_WORKTREE: str(self.worktree),
            LABEL_TARGET: self.target.name,
            LABEL_ROLE: role,
        }
        if role == "target":
            labels[LABEL_PORTS] = json.dumps({str(k): v for k, v in sorted(self.host_ports.items())})
        return labels

    def environment(self, proxy_ip: str) -> dict[str, str]:
        proxy = f"http://{proxy_ip}:{PROXY_PORT}"
        no_proxy = ",".join(["localhost", "127.0.0.1", self.target.name, *(s.name for s in self.sidecars)])
        env = dict(self.target.environment)
        for port in self.target.ports:
            env[port.env] = str(self.host_ports[port.start])
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
        return env

    def forwards(self) -> list[tuple[int, int]]:
        return [
            (self.host_ports[port.start] + offset, port.start + offset)
            for port in self.target.ports
            for offset in range(port.count)
        ]


def reserve_host_ports(ports: tuple[Port, ...], *, attempts: int = 50) -> dict[int, int]:
    reserved: dict[int, int] = {}
    held: list[socket.socket] = []
    try:
        for port in ports:
            for _ in range(attempts):
                block = _bind_block(port.count)
                if block:
                    held.extend(block)
                    reserved[port.start] = block[0].getsockname()[1]
                    break
            else:
                raise ToolError(f"no {port.count} consecutive free loopback ports for {port.start}-{port.end}")
        return reserved
    finally:
        for sock in held:
            sock.close()


def _bind_block(count: int) -> list[socket.socket]:
    first = socket.socket()
    first.bind(("127.0.0.1", 0))
    block = [first]
    base = first.getsockname()[1]
    try:
        for offset in range(1, count):
            sock = socket.socket()
            block.append(sock)
            sock.bind(("127.0.0.1", base + offset))
    except OSError:
        for sock in block:
            sock.close()
        return []
    return block


def git_dirs(worktree: Path) -> tuple[Path, ...]:
    root = worktree.resolve()
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISLNK(os.lstat(root / ".git").st_mode):
            raise ToolError(f"{root / '.git'} is a symlink; refusing to mount what it points at")

    def rev_parse(flag: str) -> Path | None:
        result = subprocess.run(["git", "rev-parse", flag], cwd=root, capture_output=True, text=True, errors="replace")
        printed = result.stdout.strip()
        return (root / printed).resolve() if result.returncode == 0 and printed else None

    own = rev_parse("--absolute-git-dir")
    if own is not None and not own.is_relative_to(root):
        try:
            points_back = (own / "gitdir").read_text().strip()
        except OSError:
            points_back = ""
        if not points_back or (own / points_back).resolve() != root / ".git":
            raise ToolError(f"{root / '.git'} names {own}, which does not belong to this worktree")
    found: list[Path] = []
    for directory in (rev_parse("--git-common-dir"), own):
        if directory is None or directory.is_relative_to(root):
            continue
        if not any(directory.is_relative_to(kept) for kept in found):
            found.append(directory)
    return tuple(found)


def git_mounts(worktree: Path, mount: str, dirs: tuple[Path, ...] | None = None) -> tuple[tuple[str, str], ...]:
    dirs = git_dirs(worktree) if dirs is None else dirs
    dot_git = worktree.resolve() / ".git"
    mounts: list[tuple[str, str]] = []
    try:
        info = os.lstat(dot_git)
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise ToolError(f"{dot_git} is a symlink; refusing to mount what it points at")
        mounts.append((str(dot_git), f"{mount}/.git"))
    mounts.extend((str(directory), str(directory)) for directory in dirs)
    return tuple(mounts)


def host_networks() -> tuple[Network, ...]:
    addresses: list[str] = []
    if shutil.which("ip"):
        for link in _ip_json("addr"):
            for addr in link.get("addr_info", ()):
                if "local" in addr:
                    addresses.append(f"{addr['local']}/{addr['prefixlen']}")
        addresses += _routed_networks(_ip_json("route"))
    elif shutil.which("ifconfig"):
        shown = subprocess.run(["ifconfig"], capture_output=True, text=True, errors="replace")
        addresses = _ifconfig_addresses(shown.stdout)
    networks = []
    for address in addresses:
        try:
            networks.append(_site(ipaddress.ip_interface(address).network))
        except ValueError:
            continue
    return tuple(dict.fromkeys(networks))


def _ip_json(kind: str) -> list[dict[str, Any]]:
    shown = subprocess.run(["ip", "-j", kind, "show"], capture_output=True, text=True, errors="replace")
    return json.loads(shown.stdout or "[]") if shown.returncode == 0 else []


def _routed_networks(routes: list[dict[str, Any]]) -> list[str]:
    found = []
    for route in routes:
        try:
            network = ipaddress.ip_network(route.get("dst", ""))
        except ValueError:
            continue
        if network.version == 4 and any(network.subnet_of(private) for private in PRIVATE_IPV4):
            found.append(route["dst"])
    return found


def _ifconfig_addresses(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "inet":
            mask = words[words.index("netmask") + 1] if "netmask" in words else "0xffffffff"
            prefix = bin(int(mask, 16)).count("1") if mask.startswith("0x") else mask
            found.append(f"{words[1]}/{prefix}")
        elif len(words) >= 2 and words[0] == "inet6":
            prefix = words[words.index("prefixlen") + 1] if "prefixlen" in words else "128"
            found.append(f"{words[1].split('%')[0]}/{prefix}")
    return found


def _site(network: Network) -> Network:
    if network.version == 6 and network.is_global and network.prefixlen > SITE_PREFIX:
        return network.supernet(new_prefix=SITE_PREFIX)
    return network


def detect(choice: str | None = None) -> Runtime:
    if choice:
        if choice not in ("podman", "docker"):
            raise ToolError(f"--runtime must be podman or docker, not {choice!r}")
        if not shutil.which(choice):
            raise ToolError(f"{choice} is not on PATH")
        return Runtime(choice)
    for name in ("podman", "docker"):
        if shutil.which(name):
            return Runtime(name)
    raise ToolError("neither podman nor docker is on PATH")


def qualified(image: str) -> str:
    first, slash, rest = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return image
    return f"docker.io/{image}" if slash else f"docker.io/library/{image}"


def flags(flag: str, values: dict[str, str]) -> list[str]:
    return [item for key, value in values.items() for item in (flag, f"{key}={value}")]


def _health_flags(sidecar: Sidecar) -> list[str]:
    check = sidecar.healthcheck
    if check is None or not check.test or check.test[0] == "NONE":
        return []
    kind, *rest = check.test
    if kind == "CMD-SHELL":
        command = " ".join(rest)
    elif kind == "CMD":
        command = shlex.join(rest)
    else:
        command = " ".join(check.test)
    flags = ["--health-cmd", command]
    for flag, value in (
        ("--health-interval", check.interval),
        ("--health-timeout", check.timeout),
        ("--health-start-period", check.start_period),
        ("--health-retries", str(check.retries) if check.retries else ""),
    ):
        if value:
            flags += [flag, value]
    return flags


class Runtime:
    def __init__(self, name: str):
        self.name = name
        self._rootless: bool | None = None

    @property
    def default_network(self) -> str:
        return "podman" if self.name == "podman" else "bridge"

    def user_args(self) -> list[str]:
        if self.name == "podman":
            return ["--userns=keep-id"]
        if self._rootless is None:
            try:
                info = self.run(["info", "--format", "{{json .SecurityOptions}}"], what="docker info", check=False)
                self._rootless = "name=rootless" in info.stdout
            except ToolError:
                self._rootless = False
        return [] if self._rootless else ["--user", f"{os.getuid()}:{os.getgid()}"]

    def claim_volume(self, image: str, volume: str, path: str) -> None:
        if self.name == "podman" or not self.user_args():
            return
        argv = [
            "run",
            "--rm",
            "--user",
            "0",
            "--cap-drop=ALL",
            *(f"--cap-add={cap}" for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER")),
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none",
            "--volume",
            f"{volume}:{path}",
            "--entrypoint",
            "chown",
            image,
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            path,
        ]
        self.run(argv, what=f"hand {volume} to you", timeout=600)

    def run(
        self,
        args: list[str],
        *,
        what: str,
        timeout: int = QUICK_TIMEOUT,
        input: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [self.name, *args]
        try:
            result = subprocess.run(
                argv, input=input, capture_output=True, text=True, errors="replace", timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"{what} timed out after {timeout}s") from exc
        except OSError as exc:
            raise ToolError(f"{what} could not start {self.name}: {exc}") from exc
        if check and result.returncode != 0:
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            raise ToolError(f"{what} failed: {output[-LOG_TAIL:]}")
        return result

    def network_args(self, spec: Spec, subnet: str) -> list[str]:
        no_dns = ["--disable-dns"] if self.name == "podman" else []
        return [
            "network",
            "create",
            "--internal",
            *no_dns,
            "--subnet",
            subnet,
            *flags("--label", spec.labels("network")),
            spec.names.network,
        ]

    def gateway_args(self, spec: Spec, addresses: Addresses) -> list[str]:
        publishes = [item for host, port in spec.forwards() for item in ("--publish", f"127.0.0.1:{host}:{port}")]
        allow = [item for host in spec.target.hosts for item in ("--allow", host)]
        deny = [item for network in spec.denied for item in ("--deny", str(network))]
        target_ip = addresses.services[spec.target.name]
        forward = [item for _, port in spec.forwards() for item in ("--forward", f"{port}:{target_ip}:{port}")]
        return [
            "create",
            "--name",
            spec.names.gateway,
            *flags("--label", spec.labels("gateway")),
            "--network",
            self.default_network,
            *publishes,
            "--cap-drop=ALL",
            "--cap-add=NET_BIND_SERVICE",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            GATEWAY_IMAGE,
            "python3",
            "-u",
            "-c",
            Path(gateway.__file__).read_text(),
            *allow,
            *deny,
            "--proxy",
            f"{addresses.gateway}:{PROXY_PORT}",
            *forward,
        ]

    def sidecar_args(self, spec: Spec, sidecar: Sidecar, addresses: Addresses) -> list[str]:
        return [
            "run",
            "-d",
            "--name",
            spec.names.container(sidecar.name),
            *flags("--label", spec.labels("sidecar")),
            "--network",
            spec.names.network,
            "--ip",
            addresses.services[sidecar.name],
            "--hostname",
            sidecar.name,
            *addresses.host_flags(),
            "--security-opt",
            "no-new-privileges",
            *flags("--env", sidecar.environment),
            *[item for v in sidecar.volumes for item in ("--volume", f"{spec.names.volume(v.name)}:{v.path}")],
            *_health_flags(sidecar),
            qualified(sidecar.image),
            *sidecar.command,
        ]

    def target_args(self, spec: Spec, addresses: Addresses) -> list[str]:
        target = spec.target
        relabel = self.name == "podman" or sys.platform == "linux"
        volumes = ["--volume", f"{spec.worktree}:{spec.mount}{':z' if relabel else ''}"]
        for source, destination in spec.git_mounts:
            volumes += ["--volume", f"{source}:{destination}:ro{',z' if relabel else ''}"]
        if target.state:
            volumes += ["--volume", f"{spec.names.state}:{target.state}"]
        for volume in target.volumes:
            volumes += ["--volume", f"{spec.names.volume(volume.name)}:{volume.path}"]
        devices = ["--device", "/dev/kvm"] if "kvm" in target.devices and sys.platform == "linux" else []
        return [
            "run",
            "-d",
            "--name",
            spec.names.container(target.name),
            *flags("--label", spec.labels("target")),
            "--network",
            spec.names.network,
            "--ip",
            addresses.services[target.name],
            "--hostname",
            target.name,
            *addresses.host_flags(),
            "--init",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            *self.user_args(),
            *volumes,
            *devices,
            *flags("--env", spec.environment(addresses.gateway)),
            "--workdir",
            spec.mount,
            spec.image,
            "sh",
            "-c",
            f"touch {READY_FILE} && exec sleep infinity",
        ]

    def exec_args(
        self, container: str, argv: list[str], *, workdir: str, tty: bool = False, interactive: bool = True
    ) -> list[str]:
        return [
            "exec",
            *(["-i"] if interactive else []),
            *(["-t"] if tty else []),
            "--workdir",
            workdir,
            container,
            *argv,
        ]

    def create_network(self, spec: Spec) -> Addresses:
        denied = [network for network in spec.denied if network.version == 4]
        pools = [pool for pool in SUBNET_POOLS if not any(pool.subnet_of(network) for network in denied)]
        if not pools:
            covering = ", ".join(map(str, denied))
            raise ToolError(
                f"could not create {spec.names.network}: host networks {covering} cover every private range"
            )
        failures = []
        for pool in pools:
            for _ in range(SUBNET_ATTEMPTS):
                offset = 256 * random.randrange(pool.num_addresses // 256)
                subnet = ipaddress.IPv4Network((int(pool.network_address) + offset, 24))
                if any(subnet.overlaps(network) for network in denied):
                    failures.append(f"{subnet} overlaps a host network")
                    continue
                result = self.run(self.network_args(spec, str(subnet)), what="create network", check=False)
                if result.returncode == 0:
                    return layout(str(subnet), spec)
                failures.append(result.stderr.strip())
        raise ToolError(f"could not create {spec.names.network} on any free subnet: {failures[-1]}")

    def start_gateway(self, spec: Spec, addresses: Addresses) -> None:
        self.run(self.gateway_args(spec, addresses), what="create gateway")
        connect = ["network", "connect", "--ip", addresses.gateway, spec.names.network, spec.names.gateway]
        self.run(connect, what="connect gateway")
        self.run(["start", spec.names.gateway], what="start gateway")
        deadline = time.monotonic() + GATEWAY_START_TIMEOUT
        while time.monotonic() < deadline:
            logs = self.logs(spec.names.gateway)
            if gateway.READY in logs:
                return
            if not self.running(spec.names.gateway):
                raise ToolError(f"gateway exited: {logs[-LOG_TAIL:]}")
            time.sleep(0.2)
        logs = self.logs(spec.names.gateway)[-LOG_TAIL:]
        raise ToolError(f"gateway not ready after {GATEWAY_START_TIMEOUT}s: {logs}")

    def start_sidecar(self, spec: Spec, sidecar: Sidecar, addresses: Addresses) -> None:
        self.run(self.sidecar_args(spec, sidecar, addresses), what=f"start {sidecar.name}", timeout=600)

    def start_target(self, spec: Spec, addresses: Addresses) -> None:
        self.run(self.target_args(spec, addresses), what=f"start {spec.target.name}", timeout=600)

    def logs(self, container: str) -> str:
        result = self.run(["logs", container], what=f"logs of {container}", check=False)
        return (result.stdout or "") + (result.stderr or "")

    def running(self, container: str) -> bool:
        result = self.run(
            ["inspect", "--format", "{{.State.Running}}", container], what=f"inspect {container}", check=False
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def health(self, container: str) -> str:
        result = self.run(
            ["inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", container],
            what=f"inspect {container}",
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def exists(self, kind: str, name: str) -> bool:
        return self.run([kind, "inspect", name], what=f"inspect {name}", check=False).returncode == 0

    def labelled(self, kind: str, labels: dict[str, str]) -> list[Resource]:
        if kind == "container":
            args = ["ps", "-a", "--format", "json", *_filters(labels)]
        elif kind == "volume":
            args = ["volume", "ls", "--format", "json", *_filters(labels)]
        else:
            args = ["network", "ls", "--format", "json", *_filters(labels)]
        result = self.run(args, what=f"list {kind}s")
        resources = []
        for row in _json_rows(result.stdout):
            name = _row_name(row)
            found = row.get("Labels") if "Labels" in row else row.get("labels")
            if not isinstance(found, dict):
                found = self._inspect_labels(kind, name)
            state = str(row.get("State") or row.get("Status") or "")
            resources.append(Resource(name=name, labels=found, state=state))
        return resources

    def _inspect_labels(self, kind: str, name: str) -> dict[str, str]:
        template = "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}"
        prefix = [] if kind == "container" else [kind]
        result = self.run([*prefix, "inspect", "--format", template, name], what=f"inspect {name}", check=False)
        try:
            return json.loads(result.stdout or "null") or {}
        except json.JSONDecodeError:
            return {}

    def remove(self, labels: dict[str, str], *, volumes: bool) -> None:
        containers = [resource.name for resource in self.labelled("container", labels)]
        if containers:
            no_wait = ["-t", "0"] if self.name == "podman" else []
            self.run(["rm", "-f", *no_wait, *containers], what="remove containers", check=False)
        networks = [resource.name for resource in self.labelled("network", labels)]
        if networks:
            self.run(["network", "rm", *networks], what="remove networks", check=False)
        if volumes:
            self.remove_volumes([resource.name for resource in self.labelled("volume", labels)])

    def remove_volumes(self, names: list[str]) -> None:
        if names:
            self.run(["volume", "rm", "-f", *names], what="remove volumes", check=False)


@dataclass(frozen=True)
class Resource:
    name: str
    labels: dict[str, str]
    state: str = ""


def _filters(labels: dict[str, str]) -> list[str]:
    return [item for key, value in labels.items() for item in ("--filter", f"label={key}={value}")]


def _json_rows(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _row_name(row: dict) -> str:
    names = row.get("Names") or row.get("Name") or row.get("name")
    if isinstance(names, list):
        return names[0]
    return str(names)


@dataclass(frozen=True)
class Addresses:
    subnet: str
    gateway: str
    services: dict[str, str]

    def host_flags(self) -> list[str]:
        return [item for name, ip in self.services.items() for item in ("--add-host", f"{name}:{ip}")]


def layout(subnet: str, spec: Spec) -> Addresses:
    hosts = ipaddress.ip_network(subnet).hosts()
    next(hosts)
    gateway_ip = str(next(hosts))
    names = [spec.target.name, *(sidecar.name for sidecar in spec.sidecars)]
    return Addresses(subnet=subnet, gateway=gateway_ip, services={name: str(next(hosts)) for name in names})
