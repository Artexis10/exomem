#!/usr/bin/env python3
"""Minimal stand-in cell image entrypoint for the 3.10 K3s integration test
(tests/test_k3s_integration.py), used only because lane A's real cell image
was not yet available when this test was written (see the delivery report).

It implements just enough of the D5 contract manifests.py assumes to
exercise cellctl end-to-end against a real cluster:

- `exomem cell-init --vault <path>`: creates the vault directory (what the
  real cell-init container is assumed to do) and exits 0.
- `exomem --transport http --host <h> --port <p>`: serves /health and
  /health/ready (used by the StatefulSet's liveness/readiness probes) plus
  two test-only endpoints, POST /write and GET /read, that round-trip an
  owner-only (0600) file under the vault directory so the integration test
  can prove data survives a pod kill and StatefulSet recreation.

This is not a general-purpose runtime and implements nothing beyond that.
The Dockerfile in this directory copies this file to /usr/local/bin/exomem
(no .py suffix, matching the `exomem` command manifests.py's StatefulSet
renders) so it can be built as a real image without a real MCP runtime.
"""
from __future__ import annotations

import http.server
import os
import socketserver
import sys

VAULT_DEFAULT = "/data/vault"
HOST_DEFAULT = "/data/host"


def _mkdir_owner_only(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def cell_init() -> None:
    args = sys.argv[2:]
    vault = VAULT_DEFAULT
    if "--vault" in args:
        vault = args[args.index("--vault") + 1]
    _mkdir_owner_only(vault)
    # D2: /data/host is the unmanaged host-scope directory the backup Job
    # also backs up (manifests.py's render_backup_job). --json is accepted
    # and ignored, matching D3's exact contract command; this stand-in has
    # no structured output of its own to emit.
    _mkdir_owner_only(HOST_DEFAULT)
    sys.exit(0)


class Handler(http.server.BaseHTTPRequestHandler):
    vault_path = VAULT_DEFAULT
    host_path = HOST_DEFAULT

    def _send(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib override
        if self.path == "/health":
            self._send(200, b"ok")
        elif self.path == "/health/ready":
            self._send(200, b"ready")
        elif self.path == "/read":
            marker = os.path.join(self.vault_path, "canary.txt")
            if os.path.exists(marker):
                with open(marker, "rb") as handle:
                    self._send(200, handle.read())
            else:
                self._send(404, b"missing")
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib override
        if self.path == "/write":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            for directory in (self.vault_path, self.host_path):
                marker = os.path.join(directory, "canary.txt")
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
            self._send(200, b"ok")
        else:
            self._send(404, b"not found")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True


def serve() -> None:
    args = sys.argv[1:]
    host = "0.0.0.0"
    port = 8765
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    with ReusableServer((host, port), Handler) as httpd:
        httpd.serve_forever()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "cell-init":
        cell_init()
    else:
        serve()


if __name__ == "__main__":
    main()
