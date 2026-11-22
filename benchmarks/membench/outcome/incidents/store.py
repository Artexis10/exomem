"""A store vault for one unit: the arm's collection store enrolled and served as the personal service has it.

The personal service keeps Records in a collection store that a writer-lease coordinator guards. A fresh vault
has no store, and the arm refuses to enrol one without that coordinator and the owner's operator credential. So
a unit with ``"store": true`` gets what the product's own tests give a store vault (``tests/
test_collection_store_service.py``): a coordinator on loopback that nothing else uses, the lease settings in the
environment of every process that serves or reads the vault, and the owner's offline ``exomem collections create``
under the operator credential, which enrols the store with its first collection. The case's own seeded collections
stay files, as the product's tests keep theirs beside the store.

The operator credential reaches the enrolment command alone, never the server or the agent. Everything lives under
the unit's own state root, and the coordinator stops when the unit ends.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..isolation import child_env
from .server import _stop, free_port

READY_SECONDS = 60
ENROL_SECONDS = 300
OPERATOR_ENV = "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"
#: The unit's first store collection. The arm's store exists from its first collection on, and the arm creates
#: that one only offline. It is a plain collection of a kind no case is about, as the personal service's others
#: are, and never counts as a collection the session made (``seed_unit`` reads the collections after it).
ENROLMENT_PATH = "Knowledge Base/Records/Library loans/_collection.md"
ENROLMENT_MANIFEST = """---
type: collection
exomem_id: 6f1c2d01-5c0f-4f8e-9a51-4a438d0b6b4a
title: Library loans
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
  natural_key: [title]
  fields:
    title: {type: string, required: true}
    borrowed: {type: string}
---
"""


@dataclass(frozen=True)
class StoreUnit:
    """The unit's coordinator: what the arm needs to serve its store, and the one credential that enrols it."""

    root: Path
    arm: Path
    #: The lease settings for every process of the unit that serves or reads the vault, applied as ``arm_env``.
    settings: Mapping[str, str]
    operator: str

    def environment(self, arm_env: Mapping[str, str] | None) -> dict[str, str]:
        """The arm's own settings with the unit's lease settings added."""
        return {**(arm_env or {}), **self.settings}

    def enrol(self, vault: Path, arm_env: Mapping[str, str] | None = None) -> dict:
        """Enrol the vault's store through the owner's offline create; a refusal raises, so the unit is unknown.

        Runs with the service stopped, as the arm requires: no server of the unit exists yet.
        """
        manifest = self.root / "enrolment.md"
        manifest.write_text(ENROLMENT_MANIFEST, encoding="utf-8")
        started = time.monotonic()
        done = subprocess.run(  # noqa: S603 - fixed argv; the interpreter and arm are the runner's own
            [
                sys.executable,
                "-m",
                "exomem",
                "collections",
                "create",
                "--vault",
                str(vault),
                "--manifest-path",
                ENROLMENT_PATH,
                "--manifest-file",
                str(manifest),
                "--why",
                "enrol the unit's collection store",
            ],
            env={
                **child_env(self.root, self.arm, self.environment(arm_env)),
                OPERATOR_ENV: self.operator,
            },
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=ENROL_SECONDS,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"enrolling the collection store failed ({done.returncode}): {done.stderr.strip()[-300:]}"
            )
        return {"collection": ENROLMENT_PATH, "seconds": round(time.monotonic() - started, 1)}


def _await_listening(port: int, coordinator: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if coordinator.poll() is not None:
            raise RuntimeError(
                f"the lease coordinator exited ({coordinator.returncode}): {log.read_text()[-400:]}"
            )
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(
        f"the lease coordinator was not listening in {READY_SECONDS} s: {log.read_text()[-400:]}"
    )


@contextmanager
def store_unit(
    root: Path, arm: Path, arm_env: Mapping[str, str] | None = None
) -> Iterator[StoreUnit]:
    """Run the unit's lease coordinator on loopback and yield the unit's store settings; stop it when the block ends.

    The writer credential and the operator credential are disposable, differ (the coordinator refuses equal ones)
    and die with the run. The coordinator keeps its database under the unit's state root, which the unit's pins name.
    """
    root.mkdir(parents=True, exist_ok=True)
    port = free_port()
    writer, operator = secrets.token_hex(16), secrets.token_hex(16)
    env = child_env(root, arm, arm_env)
    # The database the pins name lives under the state root, which the vault's first command makes later.
    Path(env["EXOMEM_LEASE_COORDINATOR_DB"]).parent.mkdir(parents=True, exist_ok=True)
    log = root / "coordinator.log"
    with log.open("w") as sink:
        coordinator = subprocess.Popen(  # noqa: S603 - fixed argv; the interpreter and arm are the runner's own
            [
                sys.executable,
                "-m",
                "exomem.lease_coordinator",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env={
                **env,
                "EXOMEM_LEASE_COORDINATOR_TOKEN": writer,
                OPERATOR_ENV: operator,
            },
            # The unit root holds no `.env`; the caller's directory may, and the coordinator loads it over `env`.
            cwd=root,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _await_listening(port, coordinator, log)
            yield StoreUnit(
                root=root,
                arm=arm,
                settings={
                    "EXOMEM_WRITER_LEASE_URL": f"http://127.0.0.1:{port}",
                    # A coordinator on this host's loopback that only this unit uses: no other replica exists.
                    "EXOMEM_WRITER_LEASE_TOPOLOGY": "single-host",
                    "EXOMEM_WRITER_LEASE_VAULT_ID": "incident",
                    "EXOMEM_WRITER_LEASE_REPLICA_ID": "incident-1",
                    "EXOMEM_WRITER_LEASE_TOKEN": writer,
                    # The unit's server takes the lease at its start, as the personal service does.
                    "EXOMEM_WRITER_LEASE_PREFERRED": "1",
                },
                operator=operator,
            )
        finally:
            _stop(coordinator)
