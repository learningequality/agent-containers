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
from agent_containers.config import home
from agent_containers.declaration import SERVE
from agent_containers.declaration import Build
from agent_containers.declaration import Declaration
from agent_containers.declaration import Sidecar
from agent_containers.declaration import load
from agent_containers.errors import ToolError
from agent_containers.images import COMPOSE_FILE
from agent_containers.images import select
from agent_containers.layout import IGNORE_ALL
from agent_containers.layout import STATE_DIR
from agent_containers.runtime import DEFAULT_WORKDIR
from agent_containers.runtime import LABEL_INSTANCE
from agent_containers.runtime import LABEL_NAMESPACE
from agent_containers.runtime import LABEL_PORTS
from agent_containers.runtime import LABEL_ROLE
from agent_containers.runtime import LABEL_TARGET
from agent_containers.runtime import LABEL_WORKTREE
from agent_containers.runtime import LOG_TAIL
from agent_containers.runtime import READY_FILE
from agent_containers.runtime import Names
from agent_containers.runtime import Resource
from agent_containers.runtime import Runtime
from agent_containers.runtime import Spec
from agent_containers.runtime import flags
from agent_containers.runtime import git_mounts
from agent_containers.runtime import host_networks
from agent_containers.runtime import namespace as current_namespace
from agent_containers.runtime import reserve_host_ports

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


def served_lines(served: Served) -> list[str]:
    return [*(f"{port} {url}" for port, url in served.urls.items()), f"log {served.log}"]


def status_lines(containers: list[Resource]) -> list[str]:
    return [
        f"{c.labels.get(LABEL_TARGET, '?')}\t{c.labels.get(LABEL_ROLE, '?')}\t{c.state}\t{c.name}" for c in containers
    ]


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
        declaration = load(worktree / COMPOSE_FILE)
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


def _target_container(runtime: Runtime, names: Names) -> Resource | None:
    found = runtime.labelled("container", {**names.labels(), LABEL_ROLE: "target"})
    return found[0] if found else None


def _ports_of(container: Resource | None) -> dict[int, int]:
    try:
        return {int(k): int(v) for k, v in json.loads(container.labels.get(LABEL_PORTS, "{}")).items()}
    except (AttributeError, ValueError):
        return {}


def _running_ports(runtime: Runtime, names: Names, declared: tuple) -> dict[int, int] | None:
    ports = _ports_of(_target_container(runtime, names))
    return ports if ports and set(ports) == {port.start for port in declared} else None


def lock_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        directory = Path(runtime_dir) / "agent-containers"
    else:
        directory = home() / ".cache" / "agent-containers" / "locks"
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
        return self.spec.names.labels()

    @property
    def container(self) -> str:
        return self.spec.names.container(self.spec.target.name)

    @property
    def log(self) -> str:
        return f"{STATE_DIR}/{self.spec.target.name}/serve.log"

    @property
    def rc(self) -> str:
        return f"{STATE_DIR}/{self.spec.target.name}/serve.rc"

    def up(self, *, fresh: bool = False) -> None:
        with self._lock():
            if not self.spec.ports_given:
                running = _running_ports(self.runtime, self.spec.names, self.spec.target.ports)
                if running:
                    self.spec = dataclasses.replace(self.spec, host_ports=running)
            if not fresh and self._reusable():
                return
            self.runtime.remove(self.labels, volumes=False)
            created = self._ensure_volumes()
            try:
                for volume, path in self._target_volumes().items():
                    if volume in created:
                        self.runtime.claim_volume(self.spec.image, volume, path)
                addresses = self.runtime.create_network(self.spec)
                self.runtime.start_gateway(self.spec, addresses)
                for sidecar in self.spec.sidecars:
                    self.runtime.start_sidecar(self.spec, sidecar, addresses)
                for sidecar in self.spec.sidecars:
                    self._wait_healthy(sidecar)
                self.runtime.start_target(self.spec, addresses)
                self._wait_target()
            except BaseException:
                self.runtime.remove(self.labels, volumes=False)
                self.runtime.remove_volumes(created)
                raise

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = "",
        tty: bool = False,
        stdout=None,
        stderr=None,
        group: str | None = None,
        ensure: bool = True,
    ) -> int:
        if ensure:
            self.up()
        workdir = f"{self.spec.mount}/{cwd}".rstrip("/") if cwd else self.spec.mount
        stdin = None
        if group:
            argv = ["setsid", "-w", "sh", "-c", f'echo $$ > {_pidfile(group)}; exec "$@"', "sh", *argv]
            stdin = subprocess.DEVNULL
        args = self.runtime.exec_args(self.container, argv, workdir=workdir, tty=tty)
        return subprocess.run([self.runtime.name, *args], stdin=stdin, stdout=stdout, stderr=stderr).returncode

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
            log, rc = f"{mount}/{self.log}", f"{mount}/{self.rc}"
            prepare = (
                f"mkdir -p {STATE_DIR}/{self.spec.target.name}"
                f" && printf %s {shlex.quote(IGNORE_ALL)} > {STATE_DIR}/.gitignore && rm -f {log} {rc}"
            )
            cleared = self._exec(["sh", "-c", prepare], what="prepare serve")
            if cleared.returncode != 0:
                raise ToolError(f"cannot prepare {self.log}: {cleared.stderr.strip()[-LOG_TAIL:]}")
            script = f"cd {mount} && ( {self.spec.target.commands[SERVE]}\n) > {log} 2>&1; echo $? > {rc}"
            self.runtime.run(["exec", "-d", self.container, "sh", "-c", script], what="start serve")
            self._wait_served()
        return served

    def urls(self) -> dict[int, str]:
        return {port.start: f"http://127.0.0.1:{self.spec.host_ports[port.start]}" for port in self.spec.target.ports}

    def stop(self, *, purge: bool = False) -> None:
        with self._lock():
            self.runtime.remove(self.labels, volumes=purge)

    def status(self) -> list[Resource]:
        return self.runtime.labelled("container", self.labels)

    def _reusable(self) -> bool:
        current = _target_container(self.runtime, self.spec.names)
        return current is not None and current.state == "running" and _ports_of(current) == self.spec.host_ports

    def _started(self) -> bool:
        return self._exec(["test", "-f", READY_FILE], what="probe").returncode == 0

    def _exec(self, argv: list[str], *, what: str, **kwargs) -> subprocess.CompletedProcess[str]:
        exec_args = self.runtime.exec_args(self.container, argv, workdir=self.spec.mount, interactive="input" in kwargs)
        return self.runtime.run(exec_args, what=what, check=False, **kwargs)

    def _target_volumes(self) -> dict[str, str]:
        mounted = {self.spec.names.volume(v.name): v.path for v in self.spec.target.volumes}
        if self.spec.target.state:
            mounted[self.spec.names.state] = self.spec.target.state
        return mounted

    def _ensure_volumes(self) -> list[str]:
        wanted = {volume.name for sidecar in self.spec.sidecars for volume in sidecar.volumes}
        wanted |= {volume.name for volume in self.spec.target.volumes}
        names = [self.spec.names.volume(name) for name in sorted(wanted)]
        if self.spec.target.state:
            names.append(self.spec.names.state)
        existing = {volume.name for volume in self.runtime.labelled("volume", self.labels)}
        labels = flags("--label", self.spec.labels("volume"))
        created = []
        for name in names:
            if name in existing:
                continue
            made = self.runtime.run(["volume", "create", *labels, name], what="create volume", check=False)
            if made.returncode == 0:
                created.append(name)
            elif not self.runtime.exists("volume", name):
                raise ToolError(f"create volume {name} failed: {made.stderr.strip()[-LOG_TAIL:]}")
        return created

    def _wait_healthy(self, sidecar: Sidecar) -> None:
        if sidecar.healthcheck is None:
            return
        container = self.spec.names.container(sidecar.name)
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
            if self._started():
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
            rc = self._exec(["sh", "-c", f"cat {self.rc} 2>/dev/null"], what="read").stdout.strip()
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
            try:
                result = self._exec(["sh", "-c", ready.command], what="ready command", timeout=60)
            except ToolError as exc:
                return False, str(exc)
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
            body = f"\n--- HTTP {exc.code} body ---\n{body}\n"
            self._exec(["sh", "-c", f"cat >> {self.log}"], what="keep error body", input=body)
        except OSError:
            pass

    def _tail(self) -> str:
        return self._exec(["tail", "-c", str(LOG_TAIL), self.log], what="read serve log").stdout

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


def reap(runtime: Runtime, ns: str) -> list[str]:
    reaped: set[str] = set()
    for kind in ("container", "volume", "network"):
        for resource in runtime.labelled(kind, {LABEL_NAMESPACE: ns}):
            worktree = Path(resource.labels.get(LABEL_WORKTREE) or "")
            deleted = worktree.is_absolute() and not worktree.exists() and worktree.parent.is_dir()
            if deleted and resource.labels.get(LABEL_INSTANCE):
                reaped.add(resource.labels[LABEL_INSTANCE])
    for instance in sorted(reaped):
        runtime.remove(Names(ns, instance).labels(), volumes=True)
    return sorted(reaped)
