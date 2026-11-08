"""`exomem cell-media-manifest`: what one backup snapshot can still reach in the media store.

The backup Job runs this on the clone that restic backs up, and stores the output inside the
snapshot (collect-released-media D5). The manifest lists every stored reference, Trash included,
the names under a protection record and the names that the cell released. The collector reads
it from each retained snapshot. A manifest whose census is unknown condemns nothing, and so does
one that does not parse.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import media_allowance, originals
from .media_jobs import job_store_path

VERSION = 1
# The format's closed census values: only a complete census may release a name or decide its reach.
COMPLETE = "complete"
UNKNOWN = "unknown"


class ManifestUnknown(Exception):
    """The census cannot say what the snapshot reaches."""


@dataclass(frozen=True)
class Manifest:
    references: frozenset[tuple[str, str]]
    protected: frozenset[str]
    released: frozenset[str]

    def reachable(self) -> frozenset[str]:
        """Every name that a restore of this snapshot could need."""
        return frozenset(name for name, _version in self.references) | self.protected


def render(manifest: Manifest | None) -> bytes:
    """The stored form. None renders the unknown manifest."""
    if manifest is None:
        return json.dumps({"v": VERSION, "census": UNKNOWN}).encode()
    return json.dumps({
        "v": VERSION, "census": COMPLETE,
        "references": sorted([name, version] for name, version in manifest.references),
        "protected": sorted(manifest.protected), "released": sorted(manifest.released),
    }, separators=(",", ":")).encode()


def _names(value: object) -> frozenset[str] | None:
    if not isinstance(value, list) or not all(isinstance(name, str) and originals.is_sha256(name) for name in value):
        return None
    return frozenset(value)


def parse(raw: bytes) -> Manifest | None:
    """The manifest, or None when it is unknown or malformed. Either way it may decide nothing."""
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        return None
    # The closed version-1 fields; anything else is another format, which this reader cannot vouch for.
    if (not isinstance(value, dict) or set(value) != {"v", "census", "references", "protected", "released"}
            or type(value["v"]) is not int or value["v"] != VERSION or value["census"] != COMPLETE):
        return None
    references = value["references"]
    if not isinstance(references, list) or not all(
            isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and originals.is_sha256(pair[0])
            and isinstance(pair[1], str) and pair[1] for pair in references):
        return None
    protected, released = _names(value["protected"]), _names(value["released"])
    if protected is None or released is None:
        return None
    return Manifest(frozenset((name, version) for name, version in references), protected, released)


def _ledger(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """The protected and released names, from a copy of the ledger and its write-ahead log.

    The clone is read-only and a committed row can still sit in the log, which only a writable copy replays.
    """
    if not path.is_file():
        raise ManifestUnknown("the media ledger does not exist")
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / path.name
        try:
            shutil.copyfile(path, copy)
            log = path.with_name(path.name + "-wal")
            if log.exists():
                shutil.copyfile(log, copy.with_name(copy.name + "-wal"))
            with closing(sqlite3.connect(copy)) as conn:
                protected = frozenset(row[0] for row in conn.execute("SELECT object FROM media_protections"))
                released = frozenset(row[0] for row in conn.execute("SELECT object FROM media_releases"))
        except (OSError, sqlite3.Error) as error:
            raise ManifestUnknown(f"the media ledger cannot be read: {type(error).__name__}") from error
    return protected, released


def build(vault: Path) -> Manifest:
    """Read the vault's references to every store and its ledger. Raises `ManifestUnknown` when a read fails."""
    vault = Path(vault)
    references: set[tuple[str, str]] = set()
    unreadable: list[str] = []
    try:
        # Every store, not only the selected one: listing more references only protects more names.
        for _path, _binary, reference in media_allowance.bound_references(
                vault, None, unreadable=lambda path, _error: unreadable.append(path)):
            references.add((reference.object, reference.version))
    except media_allowance.UNREADABLE as error:
        raise ManifestUnknown(f"the Knowledge Base walk failed: {type(error).__name__}") from error
    if unreadable:  # A count only: the Job's log is no place for the owner's paths.
        raise ManifestUnknown(f"{len(unreadable)} companions could not be read")
    protected, released = _ledger(job_store_path(vault))
    return Manifest(frozenset(references), protected, released)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-media-manifest")
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    args = parser.parse_args(argv)
    try:
        manifest: Manifest | None = build(args.vault)
    except ManifestUnknown as unknown:
        print(f"the media manifest is unknown: {unknown}", file=sys.stderr)
        manifest = None
    sys.stdout.buffer.write(render(manifest))
    sys.stdout.buffer.flush()
    return 0
