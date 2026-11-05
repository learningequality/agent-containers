from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from agent_containers import client
from agent_containers import config
from agent_containers import service
from agent_containers.daemon import Daemon
from agent_containers.declaration import parse
from agent_containers.errors import EXIT_TOOL_ERROR
from agent_containers.errors import ToolError
from agent_containers.images import COMPOSE_FILE
from agent_containers.images import select
from agent_containers.instance import Instance
from agent_containers.instance import prepare
from agent_containers.instance import reap
from agent_containers.runtime import LABEL_NAMESPACE
from agent_containers.runtime import LABEL_ROLE
from agent_containers.runtime import LABEL_TARGET
from agent_containers.runtime import LABEL_WORKTREE
from agent_containers.runtime import Runtime
from agent_containers.runtime import detect
from agent_containers.runtime import namespace

BROKERED = ("run", "serve", "stop", "status")
BROKER_FIXED = ("local", "repo", "runtime", "instance", "fresh", "purge")


def build_plan(compose_file: Path, target: str, paths: list[str]) -> dict[str, object]:
    try:
        declaration = parse(compose_file.read_text())
    except (OSError, ValueError) as exc:
        raise ToolError(f"{compose_file}: {exc}") from exc
    build = declaration.targets[select(declaration, target, str(compose_file))].build
    context = _relative(compose_file.parent / build.context)
    dockerfile = _relative(compose_file.parent / build.context / build.dockerfile)
    return {
        "context": context,
        "dockerfile": dockerfile,
        "stage": build.stage,
        "watched": sorted({dockerfile, _relative(compose_file), *paths}),
    }


def _relative(path: Path) -> str:
    return os.path.relpath(os.path.normpath(path.absolute()), Path.cwd())


def worktree_and_cwd() -> tuple[Path, str]:
    here = Path.cwd().resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True, errors="replace"
    )
    root = Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else here
    relative = os.path.relpath(here, root)
    return root, "" if relative == "." else relative


def runtime_usable(name: str | None) -> bool:
    try:
        return detect(name).run(["info"], what="runtime info", check=False, timeout=30).returncode == 0
    except ToolError:
        return False


def cancel_on_termination() -> None:
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-containers", description="Run commands in your repo's containers.")
    commands = parser.add_subparsers(dest="command", required=True)

    def instance_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("target", nargs="?")
        sub.add_argument("--repo", help="owner/name; default: the worktree's upstream, then origin remote")
        sub.add_argument("--local", action="store_true", help="read the worktree's compose file and build locally")
        sub.add_argument("--instance", help="instance id; default: one per worktree and target")
        sub.add_argument("--fresh", action="store_true", help="recreate the containers, keeping volumes")

    run = commands.add_parser("run", help="run a command in a target: run [<target>] -- <command…>")
    instance_options(run)
    serve = commands.add_parser("serve", help="start a target's serve command and print its URLs")
    instance_options(serve)
    stop = commands.add_parser("stop", help="stop this worktree's containers")
    stop.add_argument("target", nargs="?")
    stop.add_argument("--purge", action="store_true", help="also remove volumes")
    commands.add_parser("status", help="list this worktree's containers")
    commands.add_parser("reap", help="remove this namespace's instances whose worktree is gone")
    install = commands.add_parser("install", help="choose the folders agents may run worktrees from; start the service")
    install.add_argument("--root", action="append", default=[], help="a folder to allow (repeatable); skips the prompt")
    commands.add_parser("uninstall", help="stop and remove the service")
    roots = commands.add_parser("roots", help="list, add or remove the folders agents may run worktrees from")
    roots.add_argument("action", nargs="?", choices=("add", "remove"))
    roots.add_argument("paths", nargs="*")
    commands.add_parser("daemon", help="serve every worktree under the roots (what the service runs)")
    plan = commands.add_parser("build-plan", help="print how CI builds a target, as JSON")
    plan.add_argument("target")
    plan.add_argument("--compose-file", default=COMPOSE_FILE)
    plan.add_argument("--paths", default="", help="extra watched globs, space-separated")
    for sub in (run, serve, stop, commands.choices["status"], commands.choices["reap"]):
        sub.add_argument("--runtime", choices=("podman", "docker"))
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    try:
        args = _parser().parse_args(argv)
        return _dispatch(args, command)
    except ToolError as exc:
        print(f"agent-containers: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace, command: list[str]) -> int:
    if args.command == "build-plan":
        print(json.dumps(build_plan(Path(args.compose_file), args.target, args.paths.split())))
        return 0
    if args.command in ("install", "uninstall", "roots", "daemon"):
        return _manage(args)
    worktree, cwd = worktree_and_cwd()
    if args.command in BROKERED and (client.broker_alive(worktree) or not runtime_usable(args.runtime)):
        return _through_broker(args, worktree, cwd, command)
    runtime = detect(args.runtime)
    scope = {LABEL_NAMESPACE: namespace(), LABEL_WORKTREE: str(worktree)}
    if args.command == "run":
        if not command:
            raise ToolError("run needs a command after --")
        instance = _instance(args, worktree, runtime)
        if args.fresh:
            instance.up(fresh=True)
        return instance.run(command, cwd=cwd, tty=sys.stdin.isatty())
    if args.command == "serve":
        instance = _instance(args, worktree, runtime)
        if args.fresh:
            instance.up(fresh=True)
        served = instance.serve()
        for port, url in served.urls.items():
            print(f"{port} {url}")
        print(f"log {served.log}")
        return 0
    if args.command == "stop":
        if args.target:
            scope[LABEL_TARGET] = args.target
        runtime.remove(scope, volumes=args.purge)
        return 0
    if args.command == "status":
        for resource in runtime.labelled("container", scope):
            labels = resource.labels
            print(f"{labels.get(LABEL_TARGET, '?')}\t{labels.get(LABEL_ROLE, '?')}\t{resource.state}\t{resource.name}")
        return 0
    if args.command == "reap":
        for reaped in reap(runtime, namespace()):
            print(reaped)
        return 0
    raise ToolError(f"unknown command {args.command}")


def _through_broker(args: argparse.Namespace, worktree: Path, cwd: str, command: list[str]) -> int:
    fixed = [f"--{name}" for name in BROKER_FIXED if getattr(args, name, None)]
    if fixed:
        raise ToolError(f"the agent-containers service fixes {', '.join(fixed)} for this worktree; drop them")
    if args.command == "run" and not command:
        raise ToolError("run needs a command after --")
    cancel_on_termination()
    return client.request(
        worktree,
        args.command,
        getattr(args, "target", None),
        command,
        cwd,
        out=sys.stdout.buffer,
        err=sys.stderr.buffer,
    )


def _manage(args: argparse.Namespace) -> int:
    if args.command == "install":
        roots = args.root or _prompt_roots()
        if not roots and not config.load_roots():
            raise ToolError("no folders to allow; pass --root or answer the prompt")
        for root in config.add_roots(roots):
            print(f"root {root}")
        print(f"service {service.install()}")
        return 0
    if args.command == "uninstall":
        service.uninstall()
        return 0
    if args.command == "daemon":
        serving = Daemon()
        print("agent-containers daemon serving the configured roots", file=sys.stderr, flush=True)
        try:
            serving.serve_forever()
        finally:
            serving.close()
        return 0
    if args.action == "add":
        roots = config.add_roots(args.paths)
    elif args.action == "remove":
        roots = config.remove_roots(args.paths)
    else:
        roots = config.load_roots()
    for root in roots:
        print(root)
    return 0


def _prompt_roots() -> list[str]:
    if not sys.stdin.isatty():
        return []
    current = config.load_roots()
    if current:
        print("Agents may already run worktrees under:\n" + "\n".join(f"  {root}" for root in current))
    print("Folders where agents may run worktrees, one per line (e.g. ~/github); a blank line finishes:")
    answers = []
    while answer := input("> ").strip():
        answers.append(answer)
    return answers


def _instance(args: argparse.Namespace, worktree: Path, runtime: Runtime) -> Instance:
    if args.local and args.repo:
        raise ToolError("--local reads the worktree's compose file; it takes no --repo")
    return prepare(
        worktree,
        args.target,
        runtime=runtime,
        repo=args.repo,
        local=args.local,
        instance=args.instance,
        fresh=args.fresh,
    )


if __name__ == "__main__":
    sys.exit(main())
