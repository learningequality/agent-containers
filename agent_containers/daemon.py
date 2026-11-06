from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_containers.broker import BROKER_DIR
from agent_containers.broker import POLL_INTERVAL
from agent_containers.broker import REQUESTS
from agent_containers.broker import Broker
from agent_containers.broker import refuse_pending
from agent_containers.config import load_roots
from agent_containers.errors import ToolError
from agent_containers.instance import deleted_instances
from agent_containers.runtime import Names
from agent_containers.runtime import Runtime
from agent_containers.runtime import detect
from agent_containers.runtime import namespace

SCAN_INTERVAL = 2.0
REAP_INTERVAL = 3600.0
RETRY_AFTER = 30.0
DEPTH = 3
SKIPPED = {"node_modules", "__pycache__", "venv"}


def discover(roots: list[Path], depth: int = DEPTH) -> list[Path]:
    found: list[Path] = []

    def visit(directory: Path, level: int) -> None:
        if _queued(directory):
            found.append(directory)
        if level == depth:
            return
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.name in SKIPPED:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path), level + 1)

    for root in roots:
        visit(root, 0)
    return list(dict.fromkeys(found))


def _queued(directory: Path) -> bool:
    path = directory
    for part in (*BROKER_DIR.split("/"), REQUESTS):
        path = path / part
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return False
        except OSError:
            return False
    return True


def _real_broker(worktree: Path) -> Broker:
    return Broker(worktree, None, namespace(), None)


class Daemon:
    def __init__(
        self,
        broker_factory: Callable[[Path], Any] = _real_broker,
        roots: Callable[[], list[Path]] = load_roots,
        runtime: Callable[[], Runtime] = detect,
    ):
        self._broker_factory = broker_factory
        self._roots = roots
        self._runtime = runtime
        self._missing: set[str] = set()
        self._brokers: dict[Path, Any] = {}
        self._failed: dict[Path, tuple[float, str]] = {}
        self._closed = threading.Event()

    def scan_once(self) -> None:
        try:
            current = set(discover(self._roots()))
        except ToolError as exc:
            print(f"agent-containers daemon: {exc}", file=sys.stderr, flush=True)
            return
        for gone in set(self._brokers) - current:
            self._brokers.pop(gone).close()
        for worktree in current - set(self._brokers):
            if worktree in self._failed:
                failed_at, reason = self._failed[worktree]
                if time.monotonic() - failed_at < RETRY_AFTER:
                    refuse_pending(worktree, reason)
                    continue
            made = self._broker_factory(worktree)
            try:
                made.start()
            except (ToolError, OSError) as exc:
                self._failed[worktree] = (time.monotonic(), str(exc))
                refuse_pending(worktree, str(exc))
                continue
            self._failed.pop(worktree, None)
            self._brokers[worktree] = made

    def poll_once(self) -> None:
        for made in list(self._brokers.values()):
            made.poll_once()

    def reap_once(self) -> None:
        try:
            runtime, ns = self._runtime(), namespace()
            missing = deleted_instances(runtime, ns, self._roots())
            for instance in sorted(missing & self._missing):
                runtime.remove(Names(ns, instance).labels(), volumes=True)
            self._missing = missing
        except (ToolError, OSError) as exc:
            print(f"agent-containers daemon: {exc}", file=sys.stderr, flush=True)

    def _reap_forever(self) -> None:
        while not self._closed.wait(REAP_INTERVAL):
            self.reap_once()

    def serve_forever(self) -> None:
        threading.Thread(target=self._reap_forever, daemon=True).start()
        scanned = 0.0
        while not self._closed.wait(POLL_INTERVAL):
            if time.monotonic() - scanned >= SCAN_INTERVAL:
                self.scan_once()
                scanned = time.monotonic()
            self.poll_once()

    def close(self) -> None:
        self._closed.set()
        for made in self._brokers.values():
            made.close()
        self._brokers.clear()
