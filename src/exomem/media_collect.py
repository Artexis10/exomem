"""`exomem cell-media-collect`: condemn the released names that no retained backup can reach.

The backup Job runs this after its backup and its prune have both succeeded (collect-released-media
D5). It reads the media manifest of every snapshot that restic retains. The candidates are the
released names of this hold's own manifest, less every name that any retained manifest references
or protects. A missing, malformed or unknown manifest condemns nothing for the cell. No clock, age
or timestamp enters the decision: only which snapshots restic retains.

Each candidate is condemned through the media edge with the cell's collector credential. The edge
hides a name only while it is still visible, so a repeated hold hides nothing twice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from .media_edge_protocol import CONDEMN_ACTION, OBJECTS_PATH
from .media_edge_store import URL_ENV
from .media_manifest import Manifest, parse

COLLECTOR_CREDENTIAL_FILE_ENV = "EXOMEM_MEDIA_EDGE_COLLECTOR_CREDENTIAL_FILE"
# restic's snapshot ID format: 64 lowercase hexadecimal characters.
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
# Each restic call reads the repository index; one stuck call must not take the Job's whole deadline.
RESTIC_SECONDS = 60


class CollectStopped(Exception):
    """The pass cannot go on. Nothing that it has not condemned yet is condemned."""


def candidates(newest: str, manifests: Mapping[str, Manifest | None]) -> frozenset[str] | None:
    """The names to condemn, or None when any retained snapshot cannot say what it reaches."""
    if newest not in manifests or any(manifest is None for manifest in manifests.values()):
        return None
    reachable = frozenset().union(*(manifest.reachable() for manifest in manifests.values() if manifest is not None))
    newest_manifest = manifests[newest]
    assert newest_manifest is not None
    return newest_manifest.released - reachable


def _restic(*args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["restic", *args], capture_output=True, check=False, timeout=RESTIC_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollectStopped(f"restic {args[0]} failed: {type(error).__name__}") from error


def retained(manifest_path: str) -> dict[str, Manifest | None]:
    """Every retained snapshot's manifest, None for one that is missing, malformed or unknown."""
    listed = _restic("snapshots", "--json")
    if listed.returncode:
        raise CollectStopped("restic cannot list the retained snapshots")
    try:
        snapshots = json.loads(listed.stdout)
        identifiers = [snapshot["id"] for snapshot in snapshots]
    except (KeyError, TypeError, ValueError) as error:
        raise CollectStopped("restic listed the snapshots in an unknown shape") from error
    if not all(isinstance(identifier, str) and _SNAPSHOT_ID.fullmatch(identifier) for identifier in identifiers):
        raise CollectStopped("restic listed a snapshot without a valid ID")
    manifests: dict[str, Manifest | None] = {}
    for identifier in identifiers:
        dumped = _restic("dump", identifier, manifest_path)
        manifests[identifier] = None if dumped.returncode else parse(dumped.stdout)
    return manifests


def edge_condemner(url: str, credential_file: Path, *, timeout: float = 30.0) -> Callable[[str], bool]:
    """A condemn call through the media edge. True when the edge hid the name."""
    try:
        credential = credential_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise CollectStopped("the collector credential cannot be read") from error
    if not credential:
        raise CollectStopped("the collector credential is empty")

    def condemn(name: str) -> bool:
        try:
            # An in-cluster literal address needs neither proxy nor DNS configuration.
            response = httpx.post(f"{url.rstrip('/')}{OBJECTS_PATH}{name}/{CONDEMN_ACTION}", timeout=timeout, trust_env=False,
                                  headers={"authorization": f"Bearer {credential}"})
        except httpx.HTTPError as error:
            raise CollectStopped(f"the media edge condemn failed: {type(error).__name__}") from error
        if response.status_code != 200:
            raise CollectStopped(f"the media edge answered {response.status_code}")
        try:
            hidden = response.json()["hidden"]
        except (KeyError, TypeError, ValueError) as error:
            raise CollectStopped("the media edge answered in an unknown shape") from error
        if type(hidden) is not bool:
            raise CollectStopped("the media edge answered in an unknown shape")
        return hidden

    return condemn


def collect(newest: str, manifests: Mapping[str, Manifest | None], condemn: Callable[[str], bool]) -> tuple[int, int]:
    """Condemn every candidate, in name order. Returns (hidden now, candidates)."""
    chosen = candidates(newest, manifests)
    if chosen is None:
        raise CollectStopped("a retained snapshot has no complete manifest, so nothing is condemned")
    return sum(condemn(name) for name in sorted(chosen)), len(chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-collect")
    parser.add_argument("--snapshot", required=True, help="the snapshot that this hold's backup produced")
    parser.add_argument("--manifest-path", required=True, help="where each snapshot holds its media manifest")
    args = parser.parse_args(argv)
    url, credential = os.environ.get(URL_ENV), os.environ.get(COLLECTOR_CREDENTIAL_FILE_ENV)
    try:
        if not url or not credential:
            raise CollectStopped("the media edge or the collector credential is not configured")
        condemn = edge_condemner(url, Path(credential))
        manifests = retained(args.manifest_path)
        hidden, total = collect(args.snapshot, manifests, condemn)
    except CollectStopped as stopped:
        print(f"media collection stopped: {stopped}", file=sys.stderr)
        return 1
    print(f"media collection: {hidden} hidden of {total} candidates across {len(manifests)} retained snapshots",
          file=sys.stderr)
    return 0
