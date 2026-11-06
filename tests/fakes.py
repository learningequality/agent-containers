import os
import subprocess
import threading

from agent_containers.instance import Served
from agent_containers.runtime import LABEL_INSTANCE
from agent_containers.runtime import LABEL_WORKTREE
from agent_containers.runtime import Resource
from agent_containers.runtime import Runtime


class FakeInstance:
    def __init__(self, target):
        self.target = target
        self.calls = []
        self.killed = threading.Event()

    def run(self, argv, *, cwd="", stdout=None, stderr=None, group=None, ensure=True):
        self.calls.append(("run", argv, cwd))
        if argv == ["block"]:
            self.killed.wait(10)
            return 143
        os.write(stdout.fileno(), b"out:" + " ".join(argv).encode() + b"\n")
        os.write(stderr.fileno(), b"err-line\n")
        return int(argv[-1]) if argv[-1].isdigit() else 0

    def up(self):
        self.calls.append(("up",))

    def kill(self, group):
        self.calls.append(("kill", group))
        self.killed.set()

    def serve(self):
        self.calls.append(("serve",))
        return Served(urls={8000: "http://127.0.0.1:30000"}, log=".agent-containers/web/serve.log")

    def stop(self, *, purge=False):
        self.calls.append(("stop", purge))

    def status(self):
        return [Resource("ac-x-web", {"agent-containers.target": "web", "agent-containers.role": "target"}, "running")]


class ListedRuntime(Runtime):
    def __init__(self, worktrees):
        super().__init__("podman")
        self.worktrees = worktrees
        self.removed = []

    def labelled(self, kind, labels):
        return [
            Resource(f"ac-ns-{name}", {LABEL_INSTANCE: name, LABEL_WORKTREE: str(path)})
            for name, path in self.worktrees.items()
        ]

    def remove(self, labels, *, volumes):
        self.removed.append(labels[LABEL_INSTANCE])


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
