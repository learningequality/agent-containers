from __future__ import annotations

import contextlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from agent_containers.broker import ALIVE
from agent_containers.broker import BROKER_DIR
from agent_containers.broker import CANCEL
from agent_containers.broker import EXIT_CANCELLED
from agent_containers.broker import REQUESTS
from agent_containers.broker import RESULTS
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.layout import IGNORE_ALL

HEARTBEAT_STALE = 5.0
POLL_INTERVAL = 0.1
CANCEL_WAIT = 30.0
PICKUP_WAIT = 10.0
STREAMS = ("stdout", "stderr")


def _unanswered(worktree: Path) -> str:
    return (
        f"agent-containers: nothing answered for {worktree}. Outside your sandbox, run `agent-containers install`; "
        f"if it is installed, add a folder holding this worktree with `agent-containers roots add <folder>`\n"
    )


def broker_alive(worktree: Path) -> bool:
    try:
        info = os.lstat(worktree / BROKER_DIR / ALIVE)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and time.time() - info.st_mtime < HEARTBEAT_STALE


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
    broker = worktree / BROKER_DIR
    (broker / REQUESTS).mkdir(parents=True, exist_ok=True)
    ignore = broker.parent / ".gitignore"
    if not os.path.lexists(ignore):
        ignore.write_text(IGNORE_ALL)
    request_id = uuid.uuid4().hex
    payload = json.dumps({"command": command, "target": target, "argv": argv, "cwd": cwd}).encode()
    temporary = broker / REQUESTS / f".{request_id}.tmp"
    queued = broker / REQUESTS / f"{request_id}.json"
    temporary.write_bytes(payload)
    temporary.rename(queued)
    asked_at = time.monotonic()
    result = broker / RESULTS / request_id
    sinks = dict(zip(STREAMS, (out, err), strict=True))
    fds: dict[str, int] = {}
    claimed = False
    cancelled_at = None
    try:
        while True:
            try:
                done = _exit_code(result)
                _drain(result, sinks, fds)
                if done is not None:
                    return done
                if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_WAIT:
                    return EXIT_CANCELLED
                if not claimed and time.monotonic() - asked_at > PICKUP_WAIT:
                    try:
                        queued.unlink()
                    except FileNotFoundError:
                        claimed = True
                    else:
                        err.write(_unanswered(worktree).encode())
                        return EXIT_TOOL_ERROR
                if claimed and not broker_alive(worktree):
                    err.write(_unanswered(worktree).encode())
                    return EXIT_TOOL_ERROR
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                if not claimed:
                    try:
                        queued.unlink()
                    except FileNotFoundError:
                        claimed = True
                    else:
                        return EXIT_CANCELLED
                if cancelled_at is None:
                    (broker / REQUESTS / f"{request_id}{CANCEL}").write_bytes(b"")
                    cancelled_at = time.monotonic()
    finally:
        for fd in fds.values():
            os.close(fd)


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


def _drain(result: Path, sinks: dict[str, BinaryIO], fds: dict[str, int]) -> None:
    for name, sink in sinks.items():
        if name not in fds:
            with contextlib.suppress(OSError):
                fds[name] = os.open(result / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if name not in fds:
            continue
        while chunk := os.read(fds[name], 65536):
            sink.write(chunk)
        sink.flush()
