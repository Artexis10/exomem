"""Hash a restored vault and compare it with a reference, for the Cloud restore tools.

`infra/scripts/cloud_restore.sh` runs this file in two places: inside a pod on
the cell image, passed with `python3 -c`, and on the node. So it uses only the
standard library and imports nothing from this repository.

A hash list holds one JSON array `[digest, relative path]` per line, so a file
name with a newline or a byte that is not UTF-8 survives the round trip. The
lists name vault files, so the restore tools keep them in memory-backed
storage and print only counts.

`compare` writes the differing names to OUT_DIR, prints the counts and exits 0
only when both lists hold the same non-empty set of files with equal digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass


def tree_digests(root: str) -> Iterator[tuple[str, str]]:
    """Yield (digest, relative path) for every entry under `root` but directories.

    A regular file is hashed by content, read through lstat and O_NOFOLLOW. A
    symlink is hashed by its target text and never followed, so a link cannot
    read outside the tree or pass for the file it points at. Anything else is
    recorded by its type. A walk error stops the hash, so an unreadable
    directory can never drop out of both sides and still compare equal.
    """

    if not os.path.isdir(root):
        raise FileNotFoundError("hash root missing")

    def fail(error: OSError) -> None:
        raise error

    noatime = getattr(os, "O_NOATIME", 0)
    flags = os.O_RDONLY | noatime | getattr(os, "O_NOFOLLOW", 0)
    for base, dirs, files in os.walk(root, onerror=fail):
        dirs.sort()
        # os.walk lists a symlink to a directory among `dirs` and does not descend it.
        names = sorted(files + [d for d in dirs if os.path.islink(os.path.join(base, d))])
        for name in names:
            path = os.path.join(base, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                digest = "link:" + hashlib.sha256(target).hexdigest()
            elif stat.S_ISREG(mode):
                try:
                    fd = os.open(path, flags)
                except PermissionError:
                    # O_NOATIME needs file ownership; root reading a tenant file falls back.
                    fd = os.open(path, flags & ~noatime)
                content = hashlib.sha256()
                with os.fdopen(fd, "rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        content.update(block)
                digest = content.hexdigest()
            else:
                digest = "special:" + oct(stat.S_IFMT(mode))
            yield digest, os.path.relpath(path, root)


def load(path: str) -> dict[str, str]:
    """Read a hash list into {relative path: digest}."""

    digests: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            digest, name = json.loads(line)
            digests[name] = digest
    return digests


@dataclass(frozen=True)
class Comparison:
    restored: int
    reference: int
    identical: int
    differ: list[str]
    only_restored: list[str]
    only_reference: list[str]

    @property
    def passed(self) -> bool:
        # An empty restore never passes, even against an empty reference.
        return self.restored == self.reference == self.identical > 0

    def summary(self) -> str:
        return (
            f"restored={self.restored} reference={self.reference} identical={self.identical} "
            f"differ={len(self.differ)} only_restored={len(self.only_restored)} "
            f"only_reference={len(self.only_reference)}"
        )


def compare(restored: dict[str, str], reference: dict[str, str]) -> Comparison:
    return Comparison(
        restored=len(restored),
        reference=len(reference),
        identical=sum(1 for name, digest in restored.items() if reference.get(name) == digest),
        differ=sorted(name for name in restored if name in reference and reference[name] != restored[name]),
        only_restored=sorted(set(restored) - set(reference)),
        only_reference=sorted(set(reference) - set(restored)),
    )


def _write_names(path: str, names: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(name) + "\n" for name in names)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    try:
        if command == "hash" and len(argv) == 2:
            for digest, name in tree_digests(argv[1]):
                sys.stdout.write(json.dumps([digest, name]) + "\n")
            return 0
        if command == "compare" and len(argv) == 4:
            result = compare(load(argv[1]), load(argv[2]))
            for label in ("differ", "only_restored", "only_reference"):
                _write_names(os.path.join(argv[3], label + ".txt"), getattr(result, label))
            print(result.summary())
            return 0 if result.passed else 3
    except Exception as error:  # noqa: BLE001 - a traceback would name vault paths in the run log
        print(f"{command} failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print("usage: hash ROOT | compare RESTORED REFERENCE OUT_DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
