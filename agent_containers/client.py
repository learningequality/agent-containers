from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from agent_containers.broker import ALIVE
from agent_containers.broker import BROKER_DIR
from agent_containers.broker import CANCEL
from agent_containers.broker import REQUESTS
from agent_containers.broker import RESULTS
from agent_containers.errors import EXIT_TOOL_ERROR

HEARTBEAT_STALE = 5.0
POLL_INTERVAL = 0.1
CANCEL_WAIT = 30.0
START_HINT = (
    f"start `agent-containers broker` in this worktree outside your sandbox, or delete {BROKER_DIR} to run without one"
)
_interrupted = threading.Event()


def broker_alive(worktree: Path) -> bool:
    try:
        info = os.lstat(worktree / BROKER_DIR / ALIVE)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and time.time() - info.st_mtime < HEARTBEAT_STALE


def broker_known(worktree: Path) -> bool:
    return os.path.lexists(worktree / BROKER_DIR / ALIVE)


def interrupt_current() -> None:
    _interrupted.set()


def request(
    worktree: Path,
    command: str,
    target: str | None,
    argv: list[str],
    cwd: str,
    *,
    out: BinaryIO,
    err: BinaryIO,
) -> int:
    _interrupted.clear()
    if not broker_alive(worktree):
        err.write(f"agent-containers: no broker answers in {worktree}; {START_HINT}\n".encode())
        return EXIT_TOOL_ERROR
    broker = worktree / BROKER_DIR
    request_id = uuid.uuid4().hex
    payload = json.dumps({"command": command, "target": target, "argv": argv, "cwd": cwd}).encode()
    temporary = broker / REQUESTS / f".{request_id}.tmp"
    temporary.write_bytes(payload)
    temporary.rename(broker / REQUESTS / f"{request_id}.json")
    result = broker / RESULTS / request_id
    streams = {"stdout": (out, [None, 0]), "stderr": (err, [None, 0])}
    cancelled_at = None
    try:
        while True:
            try:
                done = _exit_code(result)
                _drain(result, streams)
                if done is not None:
                    _drain(result, streams)
                    return done
                if _interrupted.is_set() and cancelled_at is None:
                    (broker / REQUESTS / f"{request_id}{CANCEL}").write_bytes(b"")
                    cancelled_at = time.monotonic()
                if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_WAIT:
                    return 130
                if not broker_alive(worktree):
                    err.write(f"agent-containers: the broker stopped answering; {START_HINT}\n".encode())
                    return EXIT_TOOL_ERROR
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                _interrupted.set()
    finally:
        for _, (_, state) in streams.items():
            if state[0] is not None:
                os.close(state[0])


def _exit_code(result: Path) -> int | None:
    try:
        fd = os.open(result / "exit", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        text = os.read(fd, 64).decode(errors="replace").strip()
    finally:
        os.close(fd)
    return int(text) if text.lstrip("-").isdigit() else EXIT_TOOL_ERROR


def _drain(result: Path, streams: dict[str, tuple[BinaryIO, list]]) -> None:
    for name, (sink, state) in streams.items():
        if state[0] is None:
            with contextlib.suppress(OSError):
                state[0] = os.open(result / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if state[0] is None:
            continue
        while chunk := os.read(state[0], 65536):
            sink.write(chunk)
        sink.flush()
