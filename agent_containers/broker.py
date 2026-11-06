from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_containers import images
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.errors import ToolError
from agent_containers.instance import prepare
from agent_containers.instance import served_lines
from agent_containers.instance import status_lines
from agent_containers.layout import IGNORE_ALL
from agent_containers.layout import STATE_DIR
from agent_containers.runtime import detect
from agent_containers.runtime import git_dirs

BROKER = "broker"
BROKER_DIR = f"{STATE_DIR}/{BROKER}"
ALIVE = "alive"
REQUESTS = "requests"
RESULTS = "results"
REQUEST = "request.json"
CANCEL = ".cancel"
REQUEST_LIMIT = 64 * 1024
COMMANDS = ("run", "serve", "stop", "status")
FIELDS = {"command", "target", "argv", "cwd"}
POLL_INTERVAL = 0.2
EXIT_CANCELLED = 130
HEARTBEAT_INTERVAL = 1.0
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Refused(Exception):
    pass


def _open_dir(parent: int, name: str, *, create: bool) -> int:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o755, dir_fd=parent)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _write_new(directory: int, name: str, data: bytes = b"") -> int:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except BaseException:
        os.close(fd)
        raise
    return fd


def _replace(directory: int, name: str, data: bytes) -> None:
    temporary = f".{name}.{threading.get_ident()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temporary, dir_fd=directory)
    os.close(_write_new(directory, temporary, data))
    os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)


def _read_capped(directory: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{name} is not a regular file")
        chunks, left = [], limit + 1
        while left and (chunk := os.read(fd, left)):
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise Refused(f"request over {limit} bytes")
        return data
    finally:
        os.close(fd)


def validate(payload: Any, targets: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise Refused("request is not a JSON object")
    extra = set(payload) - FIELDS
    if extra:
        raise Refused(f"request fields the broker fixes at start: {', '.join(sorted(extra))}")
    command = payload.get("command")
    if command not in COMMANDS:
        raise Refused(f"unknown command {command!r}")
    argv = payload.get("argv", [])
    if not isinstance(argv, list) or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise Refused("argv must be a list of strings")
    if command == "run" and not argv:
        raise Refused("run needs a command")
    cwd = payload.get("cwd", "")
    if not isinstance(cwd, str) or "\0" in cwd:
        raise Refused("cwd must be a string")
    normal = posixpath.normpath(cwd) if cwd else "."
    if posixpath.isabs(cwd) or normal == ".." or normal.startswith("../"):
        raise Refused(f"cwd {cwd!r} leaves the worktree")
    target = payload.get("target")
    if target is None and len(targets) == 1:
        target = targets[0]
    if target not in targets:
        raise Refused(f"unknown target {target!r} (declared: {', '.join(targets)})")
    return {"command": command, "target": target, "argv": argv, "cwd": "" if normal == "." else normal}


def _claim(requests: int, results: int, request_id: str) -> int | None:
    try:
        os.mkdir(request_id, 0o755, dir_fd=results)
        result = os.open(request_id, _DIR_FLAGS, dir_fd=results)
    except OSError:
        return None
    try:
        os.rename(f"{request_id}.json", REQUEST, src_dir_fd=requests, dst_dir_fd=result)
    except OSError:
        os.close(result)
        with contextlib.suppress(OSError):
            os.rmdir(request_id, dir_fd=results)
        return None
    return result


def _refuse(result: int, message: str) -> None:
    try:
        os.close(_write_new(result, "stdout"))
        os.close(_write_new(result, "stderr", message.encode()))
        _replace(result, "exit", f"{EXIT_TOOL_ERROR}\n".encode())
    except OSError:
        pass
    finally:
        os.close(result)


def _queued(requests: int) -> list[str]:
    ids = (name[:-5] for name in sorted(os.listdir(requests)) if name.endswith(".json"))
    return [request_id for request_id in ids if _REQUEST_ID.match(request_id)]


def refuse_pending(worktree: Path, message: str) -> None:
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(worktree, os.O_RDONLY | os.O_DIRECTORY))
        for name in (STATE_DIR, BROKER):
            descriptors.append(_open_dir(descriptors[-1], name, create=False))
        broker = descriptors[-1]
        requests = _open_dir(broker, REQUESTS, create=False)
        descriptors.append(requests)
        results = _open_dir(broker, RESULTS, create=True)
        descriptors.append(results)
        for request_id in _queued(requests):
            result = _claim(requests, results, request_id)
            if result is not None:
                _refuse(result, f"agent-containers: {message}\n")
    except OSError:
        return
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class Broker:
    def __init__(
        self,
        worktree: Path,
        repo: str | None,
        namespace: str,
        instance_id: str | None,
        host_ports: dict[str, dict[int, int]] | None = None,
        *,
        runtime: str | None = None,
        factory: Callable[[str], Any] | None = None,
        targets: tuple[str, ...] | None = None,
    ):
        self.worktree = Path(worktree).resolve()
        self.repo = repo
        self.namespace = namespace
        self.instance_id = instance_id
        self.host_ports = host_ports or {}
        self.runtime = runtime
        self._factory = factory
        self._targets = targets or ()
        self._instances: dict[str, Any] = {}
        self._creating: dict[str, threading.Lock] = {}
        self._running: dict[str, Any] = {}
        self._cancelled: set[str] = set()
        self._mutex = threading.Lock()
        self._closed = threading.Event()
        self._broker_fd: int | None = None

    def start(self) -> None:
        if self._factory is None:
            self._factory, self._targets = self._real_factory()
        root = os.open(self.worktree, os.O_RDONLY | os.O_DIRECTORY)
        try:
            state = _open_dir(root, STATE_DIR, create=True)
        except OSError as exc:
            raise ToolError(f"{self.worktree / STATE_DIR} is not a directory the broker may use: {exc}") from exc
        finally:
            os.close(root)
        try:
            _replace(state, ".gitignore", IGNORE_ALL.encode())
            self._broker_fd = _open_dir(state, BROKER, create=True)
            for name in (REQUESTS, RESULTS):
                os.close(_open_dir(self._broker_fd, name, create=True))
        except OSError as exc:
            raise ToolError(f"{self.worktree / BROKER_DIR} is not a directory the broker may use: {exc}") from exc
        finally:
            os.close(state)
        self._beat()
        threading.Thread(target=self._heartbeat, name="agent-containers-heartbeat", daemon=True).start()

    def serve_forever(self) -> None:
        while not self._closed.wait(POLL_INTERVAL):
            self.poll_once()

    def close(self) -> None:
        self._closed.set()
        if self._broker_fd is not None:
            with contextlib.suppress(OSError):
                _replace(self._broker_fd, ALIVE, b"stopped\n")
                os.utime(ALIVE, (0, 0), dir_fd=self._broker_fd, follow_symlinks=False)
            os.close(self._broker_fd)
            self._broker_fd = None

    def poll_once(self) -> None:
        if self._broker_fd is None:
            return
        try:
            requests = _open_dir(self._broker_fd, REQUESTS, create=False)
        except OSError:
            return
        try:
            for request_id in _queued(requests):
                self._accept(requests, request_id)
            for name in sorted(os.listdir(requests)):
                if name.endswith(CANCEL):
                    self._cancel(requests, name)
        finally:
            os.close(requests)

    def _cancel(self, requests: int, name: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=requests)
        request_id = name[: -len(CANCEL)]
        with self._mutex:
            if request_id not in self._running:
                return
            self._cancelled.add(request_id)
            instance = self._running[request_id]
        if instance is not None:
            threading.Thread(target=instance.kill, args=(request_id,), daemon=True).start()

    def _accept(self, requests: int, request_id: str) -> None:
        try:
            results = _open_dir(self._broker_fd, RESULTS, create=False)
        except OSError:
            return
        try:
            result = _claim(requests, results, request_id)
        finally:
            os.close(results)
        if result is None:
            return
        try:
            try:
                payload = _read_capped(result, REQUEST, REQUEST_LIMIT)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(REQUEST, dir_fd=result)
            request = validate(json.loads(payload), self._targets)
        except (Refused, OSError, ValueError, RecursionError) as exc:
            _refuse(result, f"agent-containers broker: refused: {exc}\n")
            return
        with self._mutex:
            self._running[request_id] = None
            if f"{request_id}{CANCEL}" in os.listdir(requests):
                self._cancelled.add(request_id)
        threading.Thread(target=self._handle, args=(request_id, request, result), daemon=True).start()

    def _handle(self, request_id: str, request: dict[str, Any], result: int) -> None:
        code = EXIT_TOOL_ERROR
        try:
            with (
                os.fdopen(_write_new(result, "stdout"), "wb", buffering=0) as out,
                os.fdopen(_write_new(result, "stderr"), "wb", buffering=0) as err,
            ):
                code = self._execute(request_id, request, out, err)
        except OSError:
            pass
        finally:
            with self._mutex:
                self._running.pop(request_id, None)
                self._cancelled.discard(request_id)
            try:
                _replace(result, "exit", f"{code}\n".encode())
            except OSError:
                pass
            finally:
                os.close(result)

    def _execute(self, request_id: str, request: dict[str, Any], out, err) -> int:
        code = EXIT_TOOL_ERROR
        try:
            instance = self._instance(request["target"])
            if request["command"] == "run":
                instance.up()
                with self._mutex:
                    cancelled = request_id in self._cancelled
                    self._running[request_id] = instance
                if cancelled:
                    code = EXIT_CANCELLED
                else:
                    code = instance.run(
                        request["argv"], cwd=request["cwd"], stdout=out, stderr=err, group=request_id, ensure=False
                    )
            else:
                if request["command"] == "serve":
                    lines = served_lines(instance.serve())
                elif request["command"] == "stop":
                    instance.stop()
                    lines = []
                else:
                    lines = status_lines(instance.status())
                out.write("".join(f"{line}\n" for line in lines).encode())
                code = 0
        except ToolError as exc:
            err.write(f"agent-containers: {exc}\n".encode())
        except Exception as exc:
            err.write(f"agent-containers broker: {type(exc).__name__}: {exc}\n".encode())
        return code

    def _instance(self, target: str) -> Any:
        with self._mutex:
            creating = self._creating.setdefault(target, threading.Lock())
        with creating:
            if target not in self._instances:
                self._instances[target] = self._factory(target)
            return self._instances[target]

    def _beat(self) -> None:
        if self._broker_fd is None:
            return
        try:
            _replace(self._broker_fd, ALIVE, f"{time.time()}\n".encode())
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(ALIVE, dir_fd=self._broker_fd)

    def _heartbeat(self) -> None:
        while not self._closed.wait(HEARTBEAT_INTERVAL):
            self._beat()

    def _real_factory(self) -> tuple[Callable[[str], Any], tuple[str, ...]]:
        runtime = detect(self.runtime)
        repo = self.repo or images.repo_of(self.worktree)
        recorded = git_dirs(self.worktree)
        declaration = images.fetch_declaration(repo)
        if declaration is None or not declaration.targets:
            targets: tuple[str, ...] = (images.GENERIC_TARGET,)
        else:
            targets = tuple(sorted(declaration.targets))

        def factory(target: str) -> Any:
            instance = f"{self.instance_id}-{target}" if self.instance_id else None
            return prepare(
                self.worktree,
                target,
                runtime=runtime,
                repo=repo,
                instance=instance,
                host_ports=self.host_ports.get(target),
                namespace=self.namespace,
                git_dirs=recorded,
            )

        return factory, targets
