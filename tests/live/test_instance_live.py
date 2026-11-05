import shutil
import threading
import time
import urllib.request

import pytest

from agent_containers import instance as instances
from agent_containers import runtime
from agent_containers.declaration import parse
from agent_containers.errors import ToolError

pytestmark = pytest.mark.live

DB = f"""
  db:
    image: {runtime.GATEWAY_IMAGE}
    command: ["python3", "-c", "import time, pathlib; time.sleep(3); pathlib.Path('/tmp/up').touch(); time.sleep(1e9)"]
    healthcheck:
      test: ["CMD-SHELL", "test -f /tmp/up"]
      interval: 1s
      retries: 60
"""

FIVE_HUNDRED = """import http.server as h
class H(h.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(500); self.end_headers(); self.wfile.write(b'boom-body')
h.HTTPServer(('', 8000), H).serve_forever()
"""


def _compose(serve="python3 -m http.server 8000", ready="{path: /, port: 8000, timeout: 60}", env="", db=DB):
    return f"""
services:{db}
  app:
    build: .
    environment: {{{env}}}
    x-agent:
      commands:
        serve: {serve!r}
      ports: {{"8000": {{env: APP_PORT}}}}
      state: /state
      ready: {ready}
"""


def _instance(rt, image, namespace, worktree, compose=None, name=None):
    worktree.mkdir(parents=True, exist_ok=True)
    declaration = parse(compose or _compose())
    spec = instances.make_spec(rt, declaration, "app", worktree, image, instance=name)
    spec = runtime.Spec(**{**spec.__dict__, "names": runtime.Names(namespace, spec.names.instance)})
    return instances.Instance(rt, spec)


def _out(inst, *argv):
    result = inst.runtime.run(["exec", inst.spec.names.container("app"), *argv], what="exec", check=False, timeout=60)
    return result.returncode, result.stdout + result.stderr


def test_entrypoint_install_runs_before_the_first_run(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt")
    inst.up()
    assert inst.run(["test", "-f", "/tmp/installed"]) == 0


def test_run_exits_with_the_commands_code_in_the_requested_directory(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt")
    (tmp_path / "wt" / "platforms" / "android").mkdir(parents=True)
    inst.up()
    assert inst.run(["sh", "-c", "exit 7"]) == 7
    assert inst.run(["sh", "-c", 'test "$PWD" = /workspace/platforms/android'], cwd="platforms/android") == 0


def test_a_failing_entrypoint_fails_up_and_leaves_no_volume_it_created(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(env="FAIL_ENTRYPOINT: nope"))
    with pytest.raises(ToolError, match="entrypoint broke: nope"):
        inst.up()
    assert not rt.exists("volume", inst.spec.names.state)
    assert rt.labelled("container", {runtime.LABEL_NAMESPACE: namespace}) == []


def test_the_target_waits_for_every_sidecar_healthcheck(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt")
    inst.up()
    db = rt.run(["exec", inst.spec.names.container("db"), "test", "-f", "/tmp/up"], what="db", check=False)
    assert db.returncode == 0


def test_a_readiness_timeout_raises_with_the_serve_logs_tail(rt, test_image, namespace, tmp_path):
    compose = _compose(serve="echo serving-marker; sleep 1000", ready="{path: /, port: 8000, timeout: 3}", db="")
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    with pytest.raises(ToolError, match="serving-marker"):
        inst.serve()


def test_a_serve_command_that_changes_directory_and_exits_fails_at_once(rt, test_image, namespace, tmp_path):
    compose = _compose(serve="cd /tmp && exit 3", ready="{path: /, port: 8000, timeout: 60}", db="")
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    inst.up()
    started = time.monotonic()
    with pytest.raises(ToolError, match="rc=3"):
        inst.serve()
    assert time.monotonic() - started < 20


def test_a_ready_command_is_polled_until_it_exits_zero(rt, test_image, namespace, tmp_path):
    compose = _compose(
        serve="sleep 2 && touch /tmp/ready-now && sleep 1000",
        ready="{command: 'test -f /tmp/ready-now', timeout: 30}",
        db="",
    )
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    inst.up()
    started = time.monotonic()
    inst.serve()
    assert time.monotonic() - started >= 2
    assert _out(inst, "test", "-f", "/tmp/ready-now")[0] == 0


def test_serve_answers_on_the_printed_url(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(db=""))
    served = inst.serve()
    assert urllib.request.urlopen(served.urls[8000], timeout=10).status == 200
    assert served.log == ".agent-containers/app/serve.log"
    assert inst.serve().urls == served.urls


def test_a_5xx_body_lands_in_the_serve_log(rt, test_image, namespace, tmp_path):
    compose = _compose(serve="python3 server500.py", ready="{path: /, port: 8000, timeout: 8}", db="")
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    (tmp_path / "wt" / "server500.py").write_text(FIVE_HUNDRED)
    with pytest.raises(ToolError):
        inst.serve()
    assert "boom-body" in _out(inst, "cat", "/workspace/.agent-containers/app/serve.log")[1]


def test_two_concurrent_ups_on_a_cold_instance_create_it_once(rt, test_image, namespace, tmp_path):
    first = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(db=""))
    second = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(db=""))
    assert first.spec.host_ports != second.spec.host_ports
    errors = []

    def up(inst):
        try:
            inst.up()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=up, args=(inst,)) for inst in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300)
    assert errors == []
    targets = rt.labelled("container", {runtime.LABEL_NAMESPACE: namespace, runtime.LABEL_ROLE: "target"})
    assert [t.state for t in targets] == ["running"]
    assert len(targets) == 1
    assert len((tmp_path / "wt" / ".installs").read_text().splitlines()) == 1


def test_two_concurrent_serves_start_the_command_once(rt, test_image, namespace, tmp_path):
    compose = _compose(serve="echo started >> starts; python3 -m http.server 8000", db="")
    first = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    second = _instance(rt, test_image, namespace, tmp_path / "wt", compose)
    first.up()
    errors = []

    def serve(inst):
        try:
            inst.serve()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=serve, args=(inst,)) for inst in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert errors == []
    assert (tmp_path / "wt" / "starts").read_text().splitlines() == ["started"]


def test_up_reuses_a_running_instance_and_fresh_recreates_it_keeping_volumes(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(db=""))
    inst.up()
    inst.run(["sh", "-c", "echo kept > /state/marker"])
    installed = _out(inst, "cat", "/tmp/installed")[1]
    inst.up()
    assert _out(inst, "cat", "/tmp/installed")[1] == installed
    inst.up(fresh=True)
    assert _out(inst, "cat", "/tmp/installed")[1] != installed
    assert _out(inst, "cat", "/state/marker")[1].strip() == "kept"


def test_stop_keeps_the_state_volume_and_purge_drops_it(rt, test_image, namespace, tmp_path):
    inst = _instance(rt, test_image, namespace, tmp_path / "wt", _compose(db=""))
    inst.up()
    inst.stop()
    assert rt.labelled("container", {runtime.LABEL_NAMESPACE: namespace}) == []
    assert rt.exists("volume", inst.spec.names.state)
    inst.stop(purge=True)
    assert not rt.exists("volume", inst.spec.names.state)


def test_reap_removes_only_this_namespaces_instances_whose_worktree_is_gone(rt, test_image, namespace, tmp_path):
    gone = _instance(rt, test_image, namespace, tmp_path / "gone", _compose(db=""), name="gone")
    kept = _instance(rt, test_image, namespace, tmp_path / "kept", _compose(db=""), name="kept")
    other_ns = f"{namespace}x"
    other = _instance(rt, test_image, other_ns, tmp_path / "other", _compose(db=""), name="other")
    try:
        for inst in (gone, kept, other):
            inst.up()
        shutil.rmtree(tmp_path / "gone")
        shutil.rmtree(tmp_path / "other")

        reaped = instances.reap(rt, namespace)

        assert reaped == ["gone"]
        assert not rt.exists("volume", gone.spec.names.state)
        assert rt.running(kept.spec.names.container("app"))
        assert rt.running(other.spec.names.container("app"))
    finally:
        rt.remove({runtime.LABEL_NAMESPACE: other_ns}, volumes=True)


def test_a_symlink_planted_at_the_serve_log_leaves_its_target_untouched(rt, test_image, namespace, tmp_path):
    secret = tmp_path / "outside" / "secret"
    secret.parent.mkdir()
    secret.write_text("original")
    worktree = tmp_path / "wt"
    (worktree / ".agent-containers" / "app").mkdir(parents=True)
    (worktree / ".agent-containers" / "app" / "serve.log").symlink_to(secret)
    inst = _instance(rt, test_image, namespace, worktree, _compose(db=""))

    inst.serve()

    assert secret.read_text() == "original"
