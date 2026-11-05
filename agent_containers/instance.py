from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agent_containers import images
from agent_containers.declaration import SERVE
from agent_containers.declaration import Build
from agent_containers.declaration import Declaration
from agent_containers.declaration import parse
from agent_containers.errors import ToolError
from agent_containers.images import COMPOSE_FILE
from agent_containers.images import select
from agent_containers.runtime import DEFAULT_WORKDIR
from agent_containers.runtime import LABEL_INSTANCE
from agent_containers.runtime import LABEL_NAMESPACE
from agent_containers.runtime import LABEL_PORTS
from agent_containers.runtime import LABEL_ROLE
from agent_containers.runtime import LABEL_WORKTREE
from agent_containers.runtime import LOG_TAIL
from agent_containers.runtime import READY_FILE
from agent_containers.runtime import Names
from agent_containers.runtime import Runtime
from agent_containers.runtime import Spec
from agent_containers.runtime import git_mounts
from agent_containers.runtime import host_networks
from agent_containers.runtime import namespace as current_namespace
from agent_containers.runtime import reserve_host_ports

STATE_DIR = ".agent-containers"
UP_TIMEOUT = 900
SIDECAR_TIMEOUT = 300
POLL_INTERVAL = 1.0
ERROR_BODY_LIMIT = 4096
KILL_GRACE = 3
PIDFILE_WAIT = 10
_UNSAFE = re.compile(r"[^a-z0-9]+")


class _Unredirected(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECTS = urllib.request.build_opener(_Unredirected, urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class Served:
    urls: dict[int, str]
    log: str


def instance_id(worktree: Path, target: str) -> str:
    return hashlib.sha256(f"{worktree.resolve()}\0{target}".encode()).hexdigest()[:12]


def _safe(name: str) -> str:
    return _UNSAFE.sub("-", name.lower()).strip("-") or "x"


def make_spec(
    runtime: Runtime,
    declaration: Declaration,
    target: str,
    worktree: Path,
    image: str,
    *,
    instance: str | None = None,
    host_ports: dict[int, int] | None = None,
    namespace: str | None = None,
    git_dirs: tuple[Path, ...] | None = None,
) -> Spec:
    chosen = declaration.targets[target]
    names = Names(_safe(namespace or current_namespace()), _safe(instance or instance_id(worktree, target)))
    ports_given = host_ports is not None
    if host_ports is None:
        host_ports = _running_ports(runtime, names, chosen.ports) or reserve_host_ports(chosen.ports)
    missing = [port.start for port in chosen.ports if port.start not in host_ports]
    if missing:
        raise ToolError(f"no host port given for {', '.join(map(str, missing))}")
    mount = chosen.working_dir or DEFAULT_WORKDIR
    return Spec(
        names=names,
        worktree=worktree.resolve(),
        mount=mount,
        target=chosen,
        image=image,
        sidecars=tuple(declaration.sidecars.values()),
        host_ports=dict(host_ports),
        denied=host_networks(),
        git_mounts=git_mounts(worktree, mount, git_dirs),
        ports_given=ports_given,
    )


def prepare(
    worktree: Path,
    target: str | None,
    *,
    runtime: Runtime,
    repo: str | None = None,
    local: bool = False,
    instance: str | None = None,
    host_ports: dict[int, int] | None = None,
    namespace: str | None = None,
    git_dirs: tuple[Path, ...] | None = None,
    fresh: bool = False,
) -> Instance:
    if local:
        try:
            declaration = parse((worktree / COMPOSE_FILE).read_text())
        except (OSError, ValueError) as exc:
            raise ToolError(f"{worktree / COMPOSE_FILE}: {exc}") from exc
        chosen = select(declaration, target, str(worktree))
        image = f"localhost/agent-containers-local/{_safe(worktree.name)}-{chosen}:latest"
    else:
        resolved = images.resolve(repo or images.repo_of(worktree), target)
        declaration, chosen, image = resolved.declaration, resolved.target, resolved.image
    spec = make_spec(
        runtime,
        declaration,
        chosen,
        worktree,
        image,
        instance=instance,
        host_ports=host_ports,
        namespace=namespace,
        git_dirs=git_dirs,
    )
    built = Instance(runtime, spec)
    if fresh or not runtime.running(built.container):
        if local:
            _build_local(runtime, worktree, declaration.targets[chosen].build, image)
        else:
            images.pull(runtime.name, image)
    return built


def _build_local(runtime: Runtime, worktree: Path, build: Build, image: str) -> None:
    context = worktree / build.context
    stage = ["--target", build.stage] if build.stage else []
    argv = [runtime.name, "build", "-t", image, "-f", str(context / build.dockerfile), *stage, str(context)]
    if subprocess.run(argv, stdout=sys.stderr).returncode != 0:
        raise ToolError(f"building {image} from {context} failed")


def _running_ports(runtime: Runtime, names: Names, declared: tuple) -> dict[int, int] | None:
    labels = {LABEL_NAMESPACE: names.namespace, LABEL_INSTANCE: names.instance, LABEL_ROLE: "target"}
    for resource in runtime.labelled("container", labels):
        try:
            ports = {int(k): int(v) for k, v in json.loads(resource.labels.get(LABEL_PORTS, "{}")).items()}
        except (ValueError, AttributeError):
            continue
        if set(ports) == {port.start for port in declared}:
            return ports
    return None


def lock_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        directory = Path(runtime_dir) / "agent-containers"
    else:
        directory = Path(os.environ.get("HOME") or Path.home()) / ".cache" / "agent-containers" / "locks"
    directory.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ToolError(f"{directory} is not a lock directory only you own; remove it")
    return directory


def probe_url(spec: Spec) -> str:
    ready = spec.target.ready
    assert ready is not None and ready.port
    port = next(p for p in spec.target.ports if p.covers(ready.port))
    return f"http://127.0.0.1:{spec.host_ports[port.start] + ready.port - port.start}{ready.path}"


class Instance:
    def __init__(self, runtime: Runtime, spec: Spec):
        self.runtime = runtime
        self.spec = spec

    @property
    def labels(self) -> dict[str, str]:
        return {LABEL_NAMESPACE: self.spec.names.namespace, LABEL_INSTANCE: self.spec.names.instance}

    @property
    def container(self) -> str:
        return self.spec.names.container(self.spec.target.name)

    @property
    def log(self) -> str:
        return f"{STATE_DIR}/{self.spec.target.name}/serve.log"

    def up(self, *, fresh: bool = False) -> None:
        with self._lock():
            if not self.spec.ports_given:
                running = _running_ports(self.runtime, self.spec.names, self.spec.target.ports)
                if running:
                    self.spec = dataclasses.replace(self.spec, host_ports=running)
            if not fresh and self._ready():
                return
            self.runtime.remove(self.labels, volumes=False)
            created = self._ensure_volumes()
            try:
                addresses = self.runtime.create_network(self.spec)
                self.runtime.start_gateway(self.spec, addresses)
                for sidecar in self.spec.sidecars:
                    self.runtime.start_sidecar(self.spec, sidecar, addresses)
                for sidecar in self.spec.sidecars:
                    self._wait_healthy(self.spec.names.container(sidecar.name))
                self.runtime.start_target(self.spec, addresses)
                self._wait_target()
            except BaseException:
                self.runtime.remove(self.labels, volumes=False)
                for volume in created:
                    self.runtime.run(["volume", "rm", "-f", volume], what="remove volume", check=False)
                raise

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = "",
        tty: bool = False,
        stdin=None,
        stdout=None,
        stderr=None,
        group: str | None = None,
    ) -> int:
        self.up()
        workdir = f"{self.spec.mount}/{cwd}".rstrip("/") if cwd else self.spec.mount
        if group:
            argv = ["setsid", "-w", "sh", "-c", f'echo $$ > {_pidfile(group)}; exec "$@"', "sh", *argv]
            stdin = stdin or subprocess.DEVNULL
        args = self.runtime.exec_args(self.container, argv, workdir=workdir, tty=tty)
        return _passthrough([self.runtime.name, *args], stdin=stdin, stdout=stdout, stderr=stderr)

    def kill(self, group: str) -> None:
        pidfile = _pidfile(group)
        script = (
            f"for i in $(seq {PIDFILE_WAIT * 2}); do [ -s {pidfile} ] && break; sleep 0.5; done; "
            f"pgid=$(cat {pidfile}) || exit 0; kill -TERM -$pgid 2>/dev/null; "
            f"sleep {KILL_GRACE}; kill -KILL -$pgid 2>/dev/null; true"
        )
        kill = ["exec", self.container, "sh", "-c", script]
        self.runtime.run(kill, what="kill", check=False, timeout=PIDFILE_WAIT + KILL_GRACE + 30)

    def serve(self) -> Served:
        if not self.spec.target.serves:
            raise ToolError(f"target {self.spec.target.name!r} declares no {SERVE} command")
        self.up()
        served = Served(urls=self.urls(), log=self.log)
        with self._lock("serve"):
            if self._probe()[0]:
                return served
            mount = shlex.quote(self.spec.mount)
            log, rc = f"{mount}/{self.log}", f"{mount}/{self.log[:-4]}.rc"
            script = (
                f"cd {mount} && mkdir -p {STATE_DIR}/{self.spec.target.name} && printf '*\\n' > {STATE_DIR}/.gitignore"
                f" && rm -f {log} {rc} && ( {self.spec.target.commands[SERVE]}\n) > {log} 2>&1; echo $? > {rc}"
            )
            self.runtime.run(["exec", "-d", self.container, "sh", "-c", script], what="start serve")
            self._wait_served()
        return served

    def urls(self) -> dict[int, str]:
        return {port.start: f"http://127.0.0.1:{self.spec.host_ports[port.start]}" for port in self.spec.target.ports}

    def stop(self, *, purge: bool = False) -> None:
        with self._lock():
            self.runtime.remove(self.labels, volumes=purge)

    def status(self) -> dict[str, str]:
        rows = self.runtime.labelled("container", self.labels)
        return {r.name: f"{r.labels.get(LABEL_ROLE, '?')} {r.state}" for r in rows}

    def _ready(self) -> bool:
        if not self.runtime.running(self.container):
            return False
        current = self.runtime.labelled("container", {**self.labels, LABEL_ROLE: "target"})
        if not current or current[0].labels.get(LABEL_PORTS) != self.spec.labels("target")[LABEL_PORTS]:
            return False
        probe = self.runtime.run(["exec", self.container, "test", "-f", READY_FILE], what="probe", check=False)
        return probe.returncode == 0

    def _ensure_volumes(self) -> list[str]:
        wanted = {volume.name for sidecar in self.spec.sidecars for volume in sidecar.volumes}
        wanted |= {volume.name for volume in self.spec.target.volumes}
        names = [self.spec.names.volume(name) for name in sorted(wanted)]
        if self.spec.target.state:
            names.append(self.spec.names.state)
        created = []
        for name in names:
            if self.runtime.exists("volume", name):
                continue
            labels = [item for k, v in self.spec.labels("volume").items() for item in ("--label", f"{k}={v}")]
            self.runtime.run(["volume", "create", *labels, name], what="create volume")
            created.append(name)
        return created

    def _wait_healthy(self, container: str) -> None:
        sidecar = next(s for s in self.spec.sidecars if self.spec.names.container(s.name) == container)
        if sidecar.healthcheck is None:
            return
        deadline = time.monotonic() + SIDECAR_TIMEOUT
        while time.monotonic() < deadline:
            if not self.runtime.running(container):
                raise ToolError(f"sidecar {sidecar.name} exited: {self.runtime.logs(container)[-LOG_TAIL:]}")
            if self.runtime.name == "podman":
                healthy = self.runtime.run(["healthcheck", "run", container], what="healthcheck", check=False)
                if healthy.returncode == 0:
                    return
            elif self.runtime.health(container) == "healthy":
                return
            time.sleep(POLL_INTERVAL)
        raise ToolError(f"sidecar {sidecar.name} not healthy after {SIDECAR_TIMEOUT}s")

    def _wait_target(self) -> None:
        timeout = self.spec.target.ready.timeout if self.spec.target.ready else UP_TIMEOUT
        deadline = time.monotonic() + max(timeout, UP_TIMEOUT)
        while time.monotonic() < deadline:
            if self._ready():
                return
            if not self.runtime.running(self.container):
                logs = self.runtime.logs(self.container)[-LOG_TAIL:]
                raise ToolError(f"{self.spec.target.name} exited during start-up: {logs}")
            time.sleep(POLL_INTERVAL / 2)
        raise ToolError(f"{self.spec.target.name} did not start: {self.runtime.logs(self.container)[-LOG_TAIL:]}")

    def _wait_served(self) -> None:
        ready = self.spec.target.ready
        assert ready is not None
        deadline = time.monotonic() + ready.timeout
        outcome = "no probe ran"
        while time.monotonic() < deadline:
            rc = self._read(f"{self.log[:-4]}.rc").strip()
            if rc:
                raise ToolError(f"{SERVE} exited early (rc={rc}):\n{self._tail()}")
            ok, outcome = self._probe()
            if ok:
                return
            if outcome.startswith("HTTP 5"):
                self._keep_error_body()
            time.sleep(POLL_INTERVAL)
        raise ToolError(f"{SERVE} not ready within {ready.timeout}s (last probe: {outcome}):\n{self._tail()}")

    def _probe(self) -> tuple[bool, str]:
        ready = self.spec.target.ready
        assert ready is not None
        if ready.command:
            result = self.runtime.run(
                ["exec", "--workdir", self.spec.mount, self.container, "sh", "-c", ready.command],
                what="ready command",
                check=False,
                timeout=60,
            )
            return result.returncode == 0, f"ready command exited {result.returncode}"
        try:
            with _NO_REDIRECTS.open(probe_url(self.spec), timeout=5) as response:
                return response.status < 500, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code < 500, f"HTTP {exc.code}"
        except OSError as exc:
            return False, str(getattr(exc, "reason", exc))

    def _keep_error_body(self) -> None:
        try:
            _NO_REDIRECTS.open(probe_url(self.spec), timeout=5).close()
        except urllib.error.HTTPError as exc:
            with exc:
                body = exc.read(ERROR_BODY_LIMIT).decode(errors="replace")
            self.runtime.run(
                ["exec", "-i", "--workdir", self.spec.mount, self.container, "sh", "-c", f"cat >> {self.log}"],
                what="keep error body",
                input=f"\n--- HTTP {exc.code} body ---\n{body}\n",
                check=False,
            )
        except OSError:
            pass

    def _read(self, relative: str) -> str:
        result = self.runtime.run(
            ["exec", "--workdir", self.spec.mount, self.container, "sh", "-c", f"cat {relative} 2>/dev/null"],
            what="read",
            check=False,
        )
        return result.stdout

    def _tail(self) -> str:
        result = self.runtime.run(
            ["exec", "--workdir", self.spec.mount, self.container, "tail", "-c", str(LOG_TAIL), self.log],
            what="read serve log",
            check=False,
        )
        return result.stdout

    @contextlib.contextmanager
    def _lock(self, purpose: str = "up") -> Iterator[None]:
        name = f"{self.spec.names.prefix}.{purpose}.lock"
        fd = os.open(lock_dir() / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _pidfile(group: str) -> str:
    return f"/tmp/.agent-containers-{_safe(group)}.pid"


def _passthrough(argv: list[str], *, stdin=None, stdout=None, stderr=None) -> int:
    return subprocess.run(argv, stdin=stdin, stdout=stdout, stderr=stderr).returncode


def reap(runtime: Runtime, ns: str) -> list[str]:
    reaped: dict[str, str] = {}
    for kind in ("container", "volume", "network"):
        for resource in runtime.labelled(kind, {LABEL_NAMESPACE: ns}):
            worktree = resource.labels.get(LABEL_WORKTREE)
            if worktree and resource.labels.get(LABEL_INSTANCE) and not Path(worktree).exists():
                reaped[resource.labels[LABEL_INSTANCE]] = worktree
    for instance in reaped:
        runtime.remove({LABEL_NAMESPACE: ns, LABEL_INSTANCE: instance}, volumes=True)
    return sorted(reaped)
