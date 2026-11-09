"""`exomem cell-media-collect`: condemn the released names that no retained backup can reach.

The backup Job runs this after its backup and its prune have both succeeded (collect-released-media
D5). It reads the media manifest of every snapshot that restic retains. The candidates are the
released names of this hold's own manifest, less every name that any retained manifest references
or protects. A missing, malformed or unknown manifest condemns nothing for the cell. No clock, age
or timestamp enters the decision: only which snapshots restic retains.

Each candidate is condemned through the media edge with the cell's collector credential. The edge
hides a name only while it is still visible, so a repeated hold hides nothing twice.

The command prints one result line on stdout, which the Job appends to its termination message for
cellctl: "<outcome> <reason> <hidden> <candidates> <skipped>", with "?" for a count it does not know.
It names no object, so cellctl can keep it in its log after the Job's pod is gone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


# The result line's closed outcomes, which cellctl's parser fixes (backup_source.parse_media_collection).
COMPLETE, STOPPED, UNKNOWN_MANIFEST = "complete", "stopped", "unknown-manifest"
NO_REASON = "-"


class CollectStopped(Exception):
    """The pass cannot go on. Nothing that it has not condemned yet is condemned.

    `reason` is a lowercase token for the result line; the message, for the Job's log, says more.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Result:
    """What one pass did. None is a count that the pass does not know, never 0."""

    outcome: str
    reason: str = NO_REASON
    hidden: int | None = 0
    candidates: int | None = None
    skipped: int | None = 0

    def line(self) -> str:
        counts = ("?" if count is None else str(count) for count in (self.hidden, self.candidates, self.skipped))
        return " ".join((self.outcome, self.reason, *counts))


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
        raise CollectStopped("restic", f"restic {args[0]} failed: {type(error).__name__}") from error


def retained(manifest_path: str) -> dict[str, Manifest | None]:
    """Every retained snapshot's manifest, None for one that is missing, malformed or unknown."""
    listed = _restic("snapshots", "--json")
    if listed.returncode:
        raise CollectStopped("restic", "restic cannot list the retained snapshots")
    try:
        snapshots = json.loads(listed.stdout)
        identifiers = [snapshot["id"] for snapshot in snapshots]
    except (KeyError, TypeError, ValueError) as error:
        raise CollectStopped("restic", "restic listed the snapshots in an unknown shape") from error
    if not all(isinstance(identifier, str) and _SNAPSHOT_ID.fullmatch(identifier) for identifier in identifiers):
        raise CollectStopped("restic", "restic listed a snapshot without a valid ID")
    manifests: dict[str, Manifest | None] = {}
    for identifier in identifiers:
        dumped = _restic("dump", identifier, manifest_path)
        manifests[identifier] = None if dumped.returncode else parse(dumped.stdout)
    return manifests


def edge_condemner(url: str, credential_file: Path, *, timeout: float = 30.0) -> Callable[[str], bool | None]:
    """A condemn call through the media edge. True when the edge hid the name, None when it refused this name alone.

    An outage, a refused credential or a missing route stops the pass: every later name would get the same answer. A
    name that the edge holds busy leaves the rest of the pass going, and the next hold tries it again.
    """
    try:
        credential = credential_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise CollectStopped("credential", "the collector credential cannot be read") from error
    if not credential:
        raise CollectStopped("credential", "the collector credential is empty")

    def condemn(name: str) -> bool:
        try:
            # An in-cluster literal address needs neither proxy nor DNS configuration.
            response = httpx.post(f"{url.rstrip('/')}{OBJECTS_PATH}{name}/{CONDEMN_ACTION}", timeout=timeout, trust_env=False,
                                  headers={"authorization": f"Bearer {credential}"})
        except httpx.HTTPError as error:
            raise CollectStopped("edge-outage", f"the media edge condemn failed: {type(error).__name__}") from error
        # HTTP's own statuses: only 409, the edge's serial-write hold, answers for one name. A current edge answers a
        # missing name with 200 and hides nothing, so a 404 means the route itself is gone (an edge rolled back).
        if response.status_code == 409:
            return None
        if response.status_code != 200:
            reason = ("edge-refused" if response.status_code in (401, 403)
                      else "edge-route" if response.status_code == 404
                      else "edge-outage" if response.status_code >= 500 else "edge-answer")
            raise CollectStopped(reason, f"the media edge answered {response.status_code}")
        try:
            hidden = response.json()["hidden"]
        except (KeyError, TypeError, ValueError) as error:
            raise CollectStopped("edge-answer", "the media edge answered in an unknown shape") from error
        if type(hidden) is not bool:
            raise CollectStopped("edge-answer", "the media edge answered in an unknown shape")
        return hidden

    return condemn


def collect(newest: str, manifests: Mapping[str, Manifest | None], condemn: Callable[[str], bool | None]) -> Result:
    """Condemn every candidate, in name order, until the pass stops."""
    chosen = candidates(newest, manifests)
    if chosen is None:
        return Result(UNKNOWN_MANIFEST)
    hidden = skipped = 0
    for name in sorted(chosen):
        try:
            answer = condemn(name)
        except CollectStopped as stopped:
            return Result(STOPPED, stopped.reason, hidden, len(chosen), skipped)
        hidden += answer is True
        skipped += answer is None
    return Result(COMPLETE, NO_REASON, hidden, len(chosen), skipped)


def _deadline(_signal: int, _frame: object) -> None:
    raise CollectStopped("deadline", "the Job's collection deadline passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-collect")
    parser.add_argument("--snapshot", required=True, help="the snapshot that this hold's backup produced")
    parser.add_argument("--manifest-path", required=True, help="where each snapshot holds its media manifest")
    args = parser.parse_args(argv)
    url, credential = os.environ.get(URL_ENV), os.environ.get(COLLECTOR_CREDENTIAL_FILE_ENV)
    # The Job's `timeout` ends the pass with SIGTERM; the pass then reports what it did up to there.
    previous = signal.signal(signal.SIGTERM, _deadline)
    try:
        if not url or not credential:
            raise CollectStopped("unconfigured", "the media edge or the collector credential is not configured")
        condemn = edge_condemner(url, Path(credential))
        manifests = retained(args.manifest_path)
        result = collect(args.snapshot, manifests, condemn)
        missing = sum(manifest is None for manifest in manifests.values())
        detail = f"{len(manifests)} retained snapshots, {missing} of them without a complete manifest"
    except CollectStopped as stopped:
        result, detail = Result(STOPPED, stopped.reason), str(stopped)
    except Exception as error:  # noqa: BLE001 -- a fault still reports a result line, with what it does not know
        result, detail = Result(STOPPED, "fault", None, None, None), f"the pass failed with {type(error).__name__}"
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(result.line())
    print(f"media collection {result.line()}: {detail}", file=sys.stderr)
    return 0 if result.outcome == COMPLETE else 1
