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
PICKUP_WAIT = 10.0
_interrupted = threading.Event()


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
    broker = worktree / BROKER_DIR
    (broker / REQUESTS).mkdir(parents=True, exist_ok=True)
    ignore = broker.parent / ".gitignore"
    if not os.path.lexists(ignore):
        ignore.write_text("*\n")
    request_id = uuid.uuid4().hex
    payload = json.dumps({"command": command, "target": target, "argv": argv, "cwd": cwd}).encode()
    temporary = broker / REQUESTS / f".{request_id}.tmp"
    queued = broker / REQUESTS / f"{request_id}.json"
    temporary.write_bytes(payload)
    temporary.rename(queued)
    asked_at = time.monotonic()
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
                picked = os.path.lexists(result)
                if not picked and time.monotonic() - asked_at > PICKUP_WAIT:
                    with contextlib.suppress(FileNotFoundError):
                        queued.unlink()
                    if not os.path.lexists(result):
                        err.write(_unanswered(worktree).encode())
                        return EXIT_TOOL_ERROR
                if picked and not broker_alive(worktree):
                    err.write(_unanswered(worktree).encode())
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
