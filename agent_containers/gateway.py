from __future__ import annotations

import argparse
import contextlib
import ipaddress
import itertools
import socket
import socketserver
import sys
import threading
import urllib.parse
from dataclasses import dataclass

HEAD_LIMIT = 64 * 1024
PORTS = (80, 443)
ADDRESS_TIMEOUT = 5
READY = "agent-containers gateway ready"
_CHUNK = 65536

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Policy:
    hosts: tuple[str, ...]
    denied: tuple[Network, ...] = ()

    def allows_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for pattern in self.hosts:
            pattern = pattern.lower()
            if pattern.startswith("*.") and host.endswith(pattern[1:]) or host == pattern:
                return True
        return False


@dataclass(frozen=True)
class Config:
    policy: Policy
    proxy: tuple[str, int]
    forwards: tuple[tuple[int, str, int], ...] = ()


def parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(prog="gateway")
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--deny", action="append", default=[], type=ipaddress.ip_network)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--forward", action="append", default=[])
    args = parser.parse_args(argv)
    proxy_host, _, proxy_port = args.proxy.rpartition(":")
    forwards = []
    for spec in args.forward:
        listen, host, port = spec.split(":")
        forwards.append((int(listen), host, int(port)))
    return Config(
        policy=Policy(hosts=tuple(args.allow), denied=tuple(args.deny)),
        proxy=(proxy_host, int(proxy_port)),
        forwards=tuple(forwards),
    )


class Refused(Exception):
    def __init__(self, status: str, line: str):
        super().__init__(line)
        self.status = status


def _reply(status: str) -> bytes:
    return f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


def _resolve(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


# _admitted, _interleaved, _connect, _read_head and _target are copied from coding-agent's
# github_agent/egress_guard.py (v26.11.04.10); recheck there when changing them.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


def _admitted(address: str, denied: tuple[Network, ...]) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _IPV4_COMPATIBLE or ip in _SITE_LOCAL:
            return False
        if ip in _NAT64 or ip in _IPV4_TRANSLATED:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    return ip.is_global and not any(ip in network for network in denied)


def _interleaved(addresses: list[str]) -> list[str]:
    v6 = [address for address in addresses if ":" in address]
    v4 = [address for address in addresses if ":" not in address]
    first, second = (v6, v4) if addresses and ":" in addresses[0] else (v4, v6)
    paired = itertools.zip_longest(first, second)
    return [address for pair in paired for address in pair if address is not None]


def _connect(host: str, port: int, denied: tuple[Network, ...]) -> socket.socket:
    try:
        addresses = _resolve(host, port)
    except (OSError, UnicodeError) as e:
        raise Refused("502 Bad Gateway", f"{host}:{port} did not resolve: {e}") from e
    admitted = [address for address in addresses if _admitted(address, denied)]
    if not admitted:
        raise Refused("403 Forbidden", f"refused {host}:{port}, resolved to {', '.join(addresses)}")
    failures = []
    for address in _interleaved(admitted):
        try:
            upstream = socket.create_connection((address, port), timeout=ADDRESS_TIMEOUT)
        except OSError as e:
            failures.append(f"{address}: {e}")
            continue
        upstream.settimeout(None)
        return upstream
    raise Refused("502 Bad Gateway", f"{host}:{port} unreachable at {'; '.join(failures)}")


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    buffered = b""
    while b"\r\n\r\n" not in buffered:
        if len(buffered) > HEAD_LIMIT or not (chunk := sock.recv(_CHUNK)):
            raise ValueError("no complete request head")
        buffered += chunk
    head, _, rest = buffered.partition(b"\r\n\r\n")
    return head, rest


def _target(head: bytes) -> tuple[str, int, bytes | None]:
    request_line, *headers = head.split(b"\r\n")
    method, uri, version = request_line.decode("latin-1").split(" ")
    if method == "CONNECT":
        parts = urllib.parse.urlsplit(f"//{uri}")
        if not parts.hostname or parts.port is None:
            raise ValueError(f"CONNECT {uri} names no host and port")
        return parts.hostname, parts.port, None
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"{method} {uri} is not an absolute http URI")
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    kept = [header for header in headers if not header.lower().startswith((b"proxy-", b"connection:"))]
    forward = b"\r\n".join([f"{method} {path} {version}".encode("latin-1"), *kept, b"Connection: close"]) + b"\r\n\r\n"
    return parts.hostname, parts.port or 80, forward


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(_CHUNK):
            dst.sendall(data)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def _splice(client: socket.socket, upstream: socket.socket) -> None:
    back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    back.start()
    _pipe(client, upstream)
    back.join()


def serve(client: socket.socket, policy: Policy) -> None:
    try:
        head, rest = _read_head(client)
        host, port, forwarded = _target(head)
    except (OSError, ValueError, UnicodeError):
        client.sendall(_reply("400 Bad Request"))
        return
    try:
        if port not in PORTS or not policy.allows_host(host):
            raise Refused("403 Forbidden", f"refused {host}:{port}: not a declared host on port 80 or 443")
        upstream = _connect(host, port, policy.denied)
    except Refused as e:
        print(f"gateway: {e}", file=sys.stderr, flush=True)
        client.sendall(_reply(e.status))
        return
    with upstream:
        if forwarded is None:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            upstream.sendall(forwarded)
        if rest:
            upstream.sendall(rest)
        _splice(client, upstream)


def forward(client: socket.socket, host: str, port: int) -> None:
    try:
        upstream = socket.create_connection((host, port), timeout=ADDRESS_TIMEOUT)
    except OSError as e:
        print(f"gateway: cannot reach {host}:{port}: {e}", file=sys.stderr, flush=True)
        return
    upstream.settimeout(None)
    with upstream:
        outbound = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        outbound.start()
        _pipe(upstream, client)
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        outbound.join()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = socket.SOMAXCONN


def _listen(address: tuple[str, int], handle) -> _Server:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            handle(self.request)

    server = _Server(address, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str]) -> int:
    config = parse_args(argv)
    _listen(config.proxy, lambda client: serve(client, config.policy))
    for listen, host, port in config.forwards:
        _listen(("0.0.0.0", listen), lambda client, host=host, port=port: forward(client, host, port))
    print(READY, flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
