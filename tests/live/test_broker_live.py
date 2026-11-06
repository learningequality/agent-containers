import _thread
import io
import subprocess
import threading
import time

import pytest

from agent_containers import broker as brokers
from agent_containers import client
from agent_containers import runtime
from agent_containers.declaration import parse
from agent_containers.instance import prepare

pytestmark = pytest.mark.live

COMPOSE = """
services:
  app:
    build: {context: ., dockerfile: Containerfile}
    x-agent:
      commands: {serve: "python3 -m http.server 8000"}
      ports: {"8000": {env: APP_PORT}}
      ready: {path: /, port: 8000, timeout: 60}
"""


@pytest.fixture
def worktree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    (tmp_path / "Containerfile").write_text(f"FROM {runtime.GATEWAY_IMAGE}\nWORKDIR /workspace\n")
    return tmp_path


def _broker(rt, worktree, namespace, ports):
    def factory(target):
        return prepare(
            worktree, target, runtime=rt, local=True, instance="task42", host_ports=ports, namespace=namespace
        )

    made = brokers.Broker(worktree, None, namespace, "task42", factory=factory, targets=("app",))
    made.start()
    threading.Thread(target=made.serve_forever, daemon=True).start()
    return made


def _ask(worktree, command, argv=(), cwd=""):
    out, err = io.BytesIO(), io.BytesIO()
    code = client.request(worktree, command, None, list(argv), cwd, out=out, err=err)
    return code, out.getvalue().decode(), err.getvalue().decode()


def test_a_sandboxed_client_runs_a_command_through_the_broker(rt, worktree, namespace):
    made = _broker(rt, worktree, namespace, None)
    try:
        code, out, _ = _ask(worktree, "run", ["sh", "-c", "echo hello; exit 4"])
        assert (code, out) == (4, "hello\n")
    finally:
        made.close()


def test_interrupting_the_client_kills_the_command_in_the_container(rt, worktree, namespace):
    made = _broker(rt, worktree, namespace, None)
    try:
        assert _ask(worktree, "run", ["true"])[0] == 0
        threading.Timer(3, _thread.interrupt_main).start()
        started = time.monotonic()
        code, _, _ = _ask(worktree, "run", ["sh", "-c", "sleep 300 & sleep 300"])
        assert time.monotonic() - started < 30
        assert code != 0
        container = f"ac-{namespace}-task42-app"
        left = rt.run(["exec", container, "sh", "-c", "ps -eo comm | grep -c '^sleep' || true"], what="ps", check=False)
        assert left.stdout.strip() in ("0", "")
    finally:
        made.close()


def test_a_reattached_instance_keeps_its_ports(rt, worktree, namespace):
    ports = runtime.reserve_host_ports(parse(COMPOSE).targets["app"].ports)
    first = _broker(rt, worktree, namespace, ports)
    try:
        code, out, err = _ask(worktree, "serve")
        assert code == 0, err
        container = f"ac-{namespace}-task42-app"
        before = rt.run(["inspect", "--format", "{{.Id}}", container], what="id").stdout
    finally:
        first.close()

    second = _broker(rt, worktree, namespace, ports)
    try:
        code, again, err = _ask(worktree, "serve")
        assert code == 0, err
        assert again == out
        assert f"http://127.0.0.1:{ports[8000]}" in again
        assert rt.run(["inspect", "--format", "{{.Id}}", container], what="id").stdout == before
    finally:
        second.close()
