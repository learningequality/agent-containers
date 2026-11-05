import ipaddress
import socket
import threading

import pytest

from agent_containers import gateway

GLOBAL = "93.184.215.14"


def _exchange(policy, request: bytes, upstream_reply: bytes = b"", resolved=(GLOBAL,), monkeypatch=None):
    client, served = socket.socketpair()
    upstream_ours, upstream_theirs = socket.socketpair()
    dialled = []

    def fake_resolve(host, port):
        return list(resolved)

    def fake_connect(address, timeout=None):
        dialled.append(address)
        return upstream_theirs

    monkeypatch.setattr(gateway, "_resolve", fake_resolve)
    monkeypatch.setattr(gateway.socket, "create_connection", fake_connect)

    def handle():
        with served:
            gateway.serve(served, policy)

    thread = threading.Thread(target=handle, daemon=True)
    thread.start()
    client.sendall(request)
    seen_upstream = b""
    if upstream_reply:
        upstream_ours.settimeout(2)
        seen_upstream = upstream_ours.recv(65536)
        upstream_ours.sendall(upstream_reply)
        upstream_ours.shutdown(socket.SHUT_WR)
    client.shutdown(socket.SHUT_WR)
    client.settimeout(2)
    reply = b""
    while chunk := client.recv(65536):
        reply += chunk
    thread.join(2)
    return reply, dialled, seen_upstream


POLICY = gateway.Policy(hosts=("pypi.org", "*.pythonhosted.org"), denied=(ipaddress.ip_network("203.0.113.0/24"),))


def test_listed_host_is_tunnelled(monkeypatch):
    reply, dialled, seen = _exchange(
        POLICY, b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\nhello", b"world", monkeypatch=monkeypatch
    )
    assert reply.startswith(b"HTTP/1.1 200")
    assert reply.endswith(b"world")
    assert dialled == [(GLOBAL, 443)]
    assert seen == b"hello"


def test_plain_http_is_forwarded_with_an_origin_form_path(monkeypatch):
    reply, _, seen = _exchange(
        POLICY,
        b"GET http://pypi.org/simple/ HTTP/1.1\r\nHost: pypi.org\r\nProxy-Authorization: x\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n\r\n",
        monkeypatch=monkeypatch,
    )
    assert seen.startswith(b"GET /simple/ HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in seen
    assert reply.startswith(b"HTTP/1.1 200 OK")


@pytest.mark.parametrize(
    "request_line",
    [
        b"CONNECT evil.example:443 HTTP/1.1",
        b"CONNECT pythonhosted.org:443 HTTP/1.1",
        b"CONNECT pypi.org:22 HTTP/1.1",
        b"CONNECT pypi.org.evil.example:443 HTTP/1.1",
        b"GET http://pypi.org:8080/ HTTP/1.1",
    ],
)
def test_unlisted_host_or_port_is_refused_without_dialling(monkeypatch, request_line):
    reply, dialled, _ = _exchange(POLICY, request_line + b"\r\n\r\n", monkeypatch=monkeypatch)
    assert reply.startswith(b"HTTP/1.1 403")
    assert dialled == []


@pytest.mark.parametrize("resolved", [("10.0.0.5",), ("127.0.0.1",), ("169.254.169.254",), ("203.0.113.7",), ("::1",)])
def test_listed_host_resolving_to_a_refused_address_is_refused(monkeypatch, resolved):
    reply, dialled, _ = _exchange(
        POLICY, b"CONNECT files.pythonhosted.org:443 HTTP/1.1\r\n\r\n", resolved=resolved, monkeypatch=monkeypatch
    )
    assert reply.startswith(b"HTTP/1.1 403")
    assert dialled == []


def test_only_admitted_addresses_are_dialled(monkeypatch):
    _, dialled, _ = _exchange(
        POLICY,
        b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\nhi",
        b"x",
        resolved=("10.0.0.5", GLOBAL),
        monkeypatch=monkeypatch,
    )
    assert dialled == [(GLOBAL, 443)]


def test_oversized_head_is_refused(monkeypatch):
    request = b"CONNECT pypi.org:443 HTTP/1.1\r\nX: " + b"a" * (gateway.HEAD_LIMIT + 10)
    reply, dialled, _ = _exchange(POLICY, request, monkeypatch=monkeypatch)
    assert reply.startswith(b"HTTP/1.1 400")
    assert dialled == []


@pytest.mark.parametrize(
    ("pattern", "host", "allowed"),
    [
        ("*.pypi.org", "files.pypi.org", True),
        ("*.pypi.org", "a.b.pypi.org", True),
        ("*.pypi.org", "pypi.org", False),
        ("*.pypi.org", "evilpypi.org", False),
        ("pypi.org", "PyPI.org", True),
        ("pypi.org", "pypi.org.", True),
    ],
)
def test_suffix_patterns_match_subdomains_only(pattern, host, allowed):
    assert gateway.Policy(hosts=(pattern,)).allows_host(host) is allowed


def test_forwarder_pipes_both_ways_and_closes_both_sides(monkeypatch):
    outside, served = socket.socketpair()
    target_ours, target_theirs = socket.socketpair()
    monkeypatch.setattr(gateway.socket, "create_connection", lambda address, timeout=None: target_theirs)

    thread = threading.Thread(target=gateway.forward, args=(served, "web", 8000), daemon=True)
    thread.start()
    outside.sendall(b"ping")
    target_ours.settimeout(2)
    assert target_ours.recv(4) == b"ping"
    target_ours.sendall(b"pong")
    outside.settimeout(2)
    assert outside.recv(4) == b"pong"

    target_ours.close()
    assert outside.recv(4) == b""
    thread.join(2)
    assert not thread.is_alive()


def test_main_parses_its_arguments():
    config = gateway.parse_args(
        [
            "--allow",
            "pypi.org",
            "--allow",
            "*.npmjs.org",
            "--deny",
            "192.168.1.0/24",
            "--proxy",
            "10.89.0.2:3128",
            "--forward",
            "8000:web:8000",
            "--forward",
            "4000:web:4000",
        ]
    )
    assert config.policy == gateway.Policy(
        hosts=("pypi.org", "*.npmjs.org"), denied=(ipaddress.ip_network("192.168.1.0/24"),)
    )
    assert config.proxy == ("10.89.0.2", 3128)
    assert config.forwards == ((8000, "web", 8000), (4000, "web", 4000))
