"""The arm's own HTTP MCP server on a loopback port, for the cases whose agent must send a file's bytes.

Only an HTTP MCP session with a verified sign-in can hand a file's bytes to Exomem (``upload_files``, then
``exomem attach --handoff-file``). A stdio session cannot, and a loopback server that skips sign-in
refuses the handoff ("no verified HTTP session"). So the unit's server runs the product's own sign-in mode:
a disposable key, a placeholder identity provider that nothing contacts, and one owner session that the
arm's own session authority issues (``arm_side.py mint``). No product guard is relaxed. The server binds
127.0.0.1 only, serves the unit's own vault and state, and stops when the session ends.
"""

from __future__ import annotations

import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..isolation import child_env, server_pins

READY_SECONDS = 120
STOP_SECONDS = 30


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def lay_exomem_command(root: Path, arm: Path) -> Path:
    """A directory to put first on the actor's PATH: its ``exomem`` runs the arm's own CLI under the unit's pins."""
    directory = root / "bin"
    directory.mkdir()
    pins = " ".join(f"{key}={shlex.quote(value)}" for key, value in server_pins(root, arm).items())
    command = directory / "exomem"
    command.write_text(f'#!/bin/sh\nexec env {pins} {shlex.quote(sys.executable)} -m exomem "$@"\n')
    command.chmod(0o755)
    return directory


def _await_ready(base_url: str, server: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(
                f"the loopback server exited ({server.returncode}): {log.read_text()[-400:]}"
            )
        try:
            with urllib.request.urlopen(base_url + "/health/ready", timeout=2) as reply:  # noqa: S310 - literal loopback http
                if reply.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"the loopback server was not ready in {READY_SECONDS} s: {log.read_text()[-400:]}"
    )


def _stop(server: subprocess.Popen) -> None:
    """End the server, then anything of its process group that outlived it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(server.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            try:
                server.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    server.wait()


@contextmanager
def loopback_mcp(root: Path, arm: Path, mint: Callable[[dict], str]) -> Iterator[dict]:
    """Serve the arm's product on 127.0.0.1 and yield the session's ``--mcp-config`` entry.

    ``mint`` turns the session fields into the owner's sign-in token (``arm_side.py mint``). The server is
    stopped when the block ends, whatever the block raises.
    """
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    signing_root = secrets.token_hex(16)
    home = root / "auth-home"
    # Placeholders: the loopback server never signs a person in through GitHub, and only the arm's own
    # session authority issues the token below.
    login, user_id = "incident-owner", 1
    env = {
        **child_env(root, arm),
        "FASTMCP_HOME": str(home),
        "EXOMEM_BASE_URL": base_url,
        "GITHUB_CLIENT_ID": "incident-loopback",
        "GITHUB_CLIENT_SECRET": secrets.token_hex(16),
        "EXOMEM_GITHUB_USERNAME": login,
        "EXOMEM_GITHUB_USER_ID": str(user_id),
        "EXOMEM_JWT_SIGNING_KEY": signing_root,
        # The production upload configuration, on a fresh token per unit: `transfer_artifact`
        # must not answer UPLOAD_DISABLED. The value never enters the run record.
        "EXOMEM_UPLOAD_TOKEN": secrets.token_urlsafe(32),
    }
    token = mint(
        {
            "base_url": base_url,
            "home": str(home),
            "signing_root": signing_root,
            "user_id": user_id,
            "login": login,
        }
    )
    log = root / "server.log"
    with log.open("w") as sink:
        server = subprocess.Popen(  # noqa: S603 - fixed argv; the interpreter and arm are the runner's own
            [
                sys.executable,
                "-m",
                "exomem",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _await_ready(base_url, server, log)
            yield {
                "type": "http",
                "url": base_url + "/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        finally:
            _stop(server)
