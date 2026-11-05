import os
import subprocess
import uuid

import pytest

from agent_containers import runtime

ENTRYPOINT = """#!/bin/sh
set -e
if [ -n "$FAIL_ENTRYPOINT" ]; then echo "entrypoint broke: $FAIL_ENTRYPOINT" >&2; exit 3; fi
date +%s%N >> /tmp/installed
date +%s%N >> "$PWD/.installs" 2>/dev/null || true
exec "$@"
"""


def usable(name):
    chosen = os.environ.get("AGENT_CONTAINERS_TEST_RUNTIME")
    if chosen and chosen != name:
        return False
    try:
        return subprocess.run([name, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


RUNTIMES = [
    pytest.param(name, marks=pytest.mark.skipif(not usable(name), reason=f"{name} unusable"))
    for name in ("podman", "docker")
]


@pytest.fixture(params=RUNTIMES)
def rt(request):
    return runtime.Runtime(request.param)


@pytest.fixture
def namespace(rt):
    chosen = f"livetest{uuid.uuid4().hex[:8]}"
    yield chosen
    rt.remove({runtime.LABEL_NAMESPACE: chosen}, volumes=True)


@pytest.fixture
def test_image(rt, tmp_path_factory):
    context = tmp_path_factory.mktemp("image")
    (context / "entrypoint.sh").write_text(ENTRYPOINT)
    (context / "Containerfile").write_text(
        f"FROM {runtime.GATEWAY_IMAGE}\nCOPY --chmod=755 entrypoint.sh /entrypoint.sh\n"
        'ENTRYPOINT ["/entrypoint.sh"]\nWORKDIR /workspace\n'
    )
    tag = "localhost/agent-containers-test:lifecycle"
    rt.run(["build", "-t", tag, "-f", str(context / "Containerfile"), str(context)], what="build", timeout=900)
    return tag
