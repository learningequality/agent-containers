from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agent_containers.config import config_home
from agent_containers.config import home
from agent_containers.errors import ToolError

UNIT = "agent-containers.service"
LABEL = "org.learningequality.agent-containers"

Runner = Callable[..., subprocess.CompletedProcess]


def _executable() -> str:
    found = shutil.which("agent-containers")
    if not found:
        raise ToolError("agent-containers is not on PATH; install it with `uv tool install` first")
    return found


def _unit_path() -> Path:
    return config_home() / "systemd" / "user" / UNIT


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _plist_path() -> Path:
    return home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(run: Runner = subprocess.run) -> str:
    executable, path = _executable(), os.environ.get("PATH", "")
    if sys.platform == "linux":
        unit = _unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=agent-containers: serve sandboxed agents' container requests\n\n"
            f"[Service]\nExecStart={_quoted(executable)} daemon\nEnvironment={_quoted(f'PATH={path}')}\n"
            "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"
        )
        _check(run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True))
        _check(run(["systemctl", "--user", "enable", "--now", UNIT], capture_output=True, text=True))
        return str(unit)
    if sys.platform == "darwin":
        agent = _plist_path()
        agent.parent.mkdir(parents=True, exist_ok=True)
        log = home() / "Library" / "Logs" / "agent-containers.log"
        agent.write_bytes(
            plistlib.dumps(
                {
                    "Label": LABEL,
                    "ProgramArguments": [executable, "daemon"],
                    "EnvironmentVariables": {"PATH": path},
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardErrorPath": str(log),
                    "StandardOutPath": str(log),
                }
            )
        )
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, text=True)
        _check(run(["launchctl", "bootstrap", domain, str(agent)], capture_output=True, text=True))
        return str(agent)
    raise ToolError(f"no service install for {sys.platform}; run `agent-containers daemon` yourself")


def uninstall(run: Runner = subprocess.run) -> None:
    if sys.platform == "linux":
        run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True, text=True)
        with contextlib.suppress(FileNotFoundError):
            _unit_path().unlink()
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    elif sys.platform == "darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        with contextlib.suppress(FileNotFoundError):
            _plist_path().unlink()


def _check(result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        raise ToolError(f"{' '.join(result.args)} failed: {(result.stderr or '').strip()}")
