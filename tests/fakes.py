import os
import threading

from agent_containers.instance import Served


class FakeInstance:
    def __init__(self, target):
        self.target = target
        self.calls = []
        self.killed = threading.Event()

    def run(self, argv, *, cwd="", stdout=None, stderr=None, group=None):
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
        return {"ac-x-web": "target running"}
