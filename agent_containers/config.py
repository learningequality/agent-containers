from __future__ import annotations

import json
import os
from pathlib import Path

from agent_containers.errors import ToolError


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path(os.environ.get("HOME") or Path.home()) / ".config")
    return Path(base) / "agent-containers" / "config.json"


def load_roots() -> list[Path]:
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ToolError(f"{path} is unreadable: {exc}") from exc
    roots = data.get("roots", []) if isinstance(data, dict) else None
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise ToolError(f"{path}: roots must be a list of paths")
    return [Path(root) for root in roots]


def add_roots(paths: list[str]) -> list[Path]:
    roots = load_roots()
    for path in paths:
        resolved = _resolve(path)
        if not resolved.is_dir():
            raise ToolError(f"{path} is not a directory")
        if resolved not in roots:
            roots.append(resolved)
    _save(roots)
    return roots


def remove_roots(paths: list[str]) -> list[Path]:
    roots = load_roots()
    for path in paths:
        resolved = _resolve(path)
        if resolved not in roots:
            raise ToolError(f"{path} is not a root (roots: {', '.join(map(str, roots)) or 'none'})")
        roots.remove(resolved)
    _save(roots)
    return roots


def _resolve(path: str) -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    expanded = home + path[1:] if path == "~" or path.startswith("~/") else path
    return Path(expanded).resolve()


def _save(roots: list[Path]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"roots": [str(root) for root in roots]}, indent=2) + "\n")
    temporary.replace(path)
