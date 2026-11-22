"""Store-mode arms for the planted suite: the same built vault, served from the collection store.

``migrated`` cuts a store-mode arm from a file arm's pristine ``built`` vault: it enrols the
collection store in the copy and moves the copy into store mode, both through the product's own
owner commands. ``enrolled`` then gives the file arm the same enrolment, so the two arms hold the
same pages and differ only in the mode. An arm's machine-local state is keyed by its vault path, so
each arm is enrolled where it is served and a copy of an enrolled arm would find no store.
``lease_coordinator`` runs the writer-lease coordinator those commands and every store-mode
server need, on loopback and under a database of its own. Its ``Rig.arm_env(arm)`` goes to
``answer.answer_all`` and to ``isolation.mcp_server``; ``GRAPH_OFF`` is the setting of a graph-off arm.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..isolation import child_env, copy_state

#: The one collection a vault owner enrols before a first migration: a summary collection of its own.
#: It adds one page to the arm, to the file arm as well, so the two arms hold the same pages.
ENROLMENT_PATH = "Knowledge Base/Records/Store Mode/Enrolment/_collection.md"
ENROLMENT_MANIFEST = """---
type: collection
exomem_id: 6b2f9d1e-3c47-4a58-8e0d-71c4a9b5f326
title: Store mode enrolment
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: summary
storage:
  strategy: sqlite
  source: Items
  format_version: 1
item_schema:
  natural_key: [item_id]
  fields:
    item_id: {type: string, required: true}
---
"""
#: The graph lane's own switch: the setting of the graph-off arms (``Eg`` and ``Esg``).
GRAPH_OFF = {"EXOMEM_DISABLE_GRAPH_INDEX": "1"}
#: A short lease term, so that each owner command waits only a few seconds for the one before it to expire.
OWNER_LEASE_TTL = "4"
COMMAND_SECONDS = 600


@dataclass(frozen=True)
class Rig:
    """The coordinator's address and credentials. Each arm holds its own vault id there, so arms never contend for a lease."""

    url: str
    token: str
    operator: str

    def arm_env(self, arm: str) -> dict[str, str]:
        """The settings that every server and owner command of ``arm`` needs for the writer lease."""
        return {
            "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
            "EXOMEM_WRITER_LEASE_URL": self.url,
            "EXOMEM_WRITER_LEASE_VAULT_ID": f"planted-{arm}",
            "EXOMEM_WRITER_LEASE_REPLICA_ID": f"planted-{arm}",
            "EXOMEM_WRITER_LEASE_TOKEN": self.token,
        }


@contextmanager
def lease_coordinator(run_dir: Path, python: Path = Path(sys.executable)) -> Iterator[Rig]:
    """A loopback lease coordinator of this run's own, stopped on exit. Never reaches a running service."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token, operator = secrets.token_hex(16), secrets.token_hex(16)
    env = {
        **child_env(run_dir),
        "EXOMEM_LEASE_COORDINATOR_TOKEN": token,
        "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN": operator,
    }
    argv = [
        str(python),
        "-m",
        "exomem.lease_coordinator",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    log = (run_dir / "coordinator.log").open("w")
    process = subprocess.Popen(  # noqa: S603 - fixed argv
        [*argv, "--database", str(run_dir / "coordinator.sqlite")],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(
                        f"the lease coordinator did not start; see {log.name}"
                    ) from None
                time.sleep(0.5)
        yield Rig(url=f"http://127.0.0.1:{port}", token=token, operator=operator)
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def owner(arm_dir: Path, rig: Rig, python: Path, *args: str, apply: bool = False) -> dict:
    """One owner command on ``arm_dir``'s vault (the pinned ``EXOMEM_VAULT_PATH``); its last stdout line is the JSON result."""
    # An owner apply refuses with this fixed code until the previous command's lease expires.
    held, deadline = (
        "COLLECTION_STORE_SERVICE_ACTIVE",
        time.monotonic() + 3 * float(OWNER_LEASE_TTL) + 30,
    )
    env = child_env(
        arm_dir, arm_env={**rig.arm_env(arm_dir.name), "EXOMEM_WRITER_LEASE_TTL": OWNER_LEASE_TTL}
    )
    if apply:
        env["EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"] = rig.operator
    while True:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [str(python), "-m", "exomem", *args],
            env=env, capture_output=True, text=True, timeout=COMMAND_SECONDS, cwd=arm_dir,
        )  # fmt: skip
        if not (held in done.stderr and time.monotonic() < deadline):
            break
        time.sleep(1)
    if done.returncode != 0:
        raise RuntimeError(
            f"exomem {' '.join(args)} exited {done.returncode}: {done.stderr[-600:]}"
        )
    return json.loads(done.stdout.strip().splitlines()[-1])


def enrolled(arm_dir: Path, rig: Rig, python: Path = Path(sys.executable)) -> None:
    """Enrol the collection store in ``arm_dir``'s built vault, build its sidecars, and freeze ``built`` again."""
    copy_state(arm_dir / "built", arm_dir)
    manifest = arm_dir / "store-enrolment.md"
    manifest.write_text(ENROLMENT_MANIFEST, encoding="utf-8")
    made = owner(
        arm_dir, rig, python, "collections", "create", "--manifest-path", ENROLMENT_PATH,
        "--manifest-file", str(manifest), "--why", "enrol the planted arm's collection store",
        apply=True,
    )  # fmt: skip
    if made.get("status") != "marker_admitted":
        raise RuntimeError(f"enrolment did not admit the store: {made}")
    if not owner(arm_dir, rig, python, "maintain", "--reconcile", "--json").get("success"):
        raise RuntimeError("maintain --reconcile did not succeed")
    copy_state(arm_dir, arm_dir / "built")


def migrated(source: Path, arm_dir: Path, rig: Rig, python: Path = Path(sys.executable)) -> dict:
    """Cut store-mode arm ``arm_dir`` from ``source``'s pristine ``built`` vault, which must not be enrolled yet.

    Call it before ``enrolled(source)``: an enrolled vault's store sits under a state key that
    names its own path, so only a copy of the pristine vault can be enrolled where it is served.
    """
    copy_state(source / "built", arm_dir / "built")
    enrolled(arm_dir, rig, python)
    owner(arm_dir, rig, python, "store-mode", "migrate", "--dry-run")
    done = owner(
        arm_dir, rig, python, "store-mode", "migrate", "--apply", "--why",
        "serve a planted arm from the collection store", apply=True,
    )  # fmt: skip
    status = owner(arm_dir, rig, python, "store-mode", "status")
    if done.get("status") != "migrated" or status.get("mode") != "store":
        raise RuntimeError(f"the arm is not in store mode: {done} {status}")
    copy_state(arm_dir, arm_dir / "built")
    return done
