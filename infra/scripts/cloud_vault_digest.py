"""Hash a restored backup root and compare or judge it against a reference.

`infra/scripts/cloud_restore.sh` runs this file in two places: inside a pod on
the cell image, passed with `python3 -c`, and on the node. So it uses only the
standard library and imports nothing from this repository.

A hash list holds one JSON array `[digest, relative path]` per line, so a file
name with a newline or a byte that is not UTF-8 survives the round trip. The
lists name private files, so the restore tools keep them in private disk
storage and print only counts. `hash --ctime` adds the entry's change time in
nanoseconds as a third element; only the drill's reference capture uses it.

`compare` writes the differing names to OUT_DIR, prints the counts and exits 0
only when both lists hold the same non-empty set of files with equal digests.
`--allow-empty` also accepts two empty lists, for a successfully hashed host root.
The in-place restore uses `hash` and `compare`.

`judge` is the drill's verdict. It requires each reference entry that has not
changed since the snapshot's time (CUTOFF_NS) to be in the restore with the
same digest, and reports later changes and later removals without failing on
them. It writes the names to OUT_DIR, prints counts only and exits 0 on a pass,
3 on a judged mismatch, 4 when it judged no entry, and 1 when it cannot run.

`cutoff` reads a restic restore log on stdin and prints the snapshot's time,
truncated to the second, in nanoseconds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePath

# restic prints this line from Snapshot.String (internal/data/snapshot.go): a closed format that
# the pinned restic fixes, not text that a person or a model wrote.
RESTORING_LINE = re.compile(
    r"^restoring snapshot \S+ of \[.*\] at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? ([+-]\d{4}) ",
    re.MULTILINE,
)


def tree_entries(root: str) -> Iterator[tuple[str, str, int]]:
    """Yield (digest, relative path, ctime in ns) for every entry under `root` but directories.

    A regular file is hashed by content, read through lstat and O_NOFOLLOW. A
    symlink is hashed by its target text and never followed, so a link cannot
    read outside the tree or pass for the file it points at. Anything else is
    recorded by its type. A walk error stops the hash, so an unreadable
    directory can never drop out of both sides and still compare equal.

    A regular file's ctime comes from fstat on the open file after the read, so
    a write during the read shows in it. Any other entry's ctime is its lstat's.
    """

    # Normalize slash/dot spellings without resolving links or collapsing parent references.
    root = str(PurePath(root)) if root else root
    # A backup root must be a directory itself; following a root link reads another tree.
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise NotADirectoryError("hash root is not a directory")

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
            seen = os.lstat(path)
            ctime = seen.st_ctime_ns
            if stat.S_ISLNK(seen.st_mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                digest = "link:" + hashlib.sha256(target).hexdigest()
            elif stat.S_ISREG(seen.st_mode):
                try:
                    fd = os.open(path, flags)
                except PermissionError:
                    # O_NOATIME needs file ownership; root reading a tenant file falls back.
                    fd = os.open(path, flags & ~noatime)
                content = hashlib.sha256()
                with os.fdopen(fd, "rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        content.update(block)
                    ctime = os.fstat(handle.fileno()).st_ctime_ns
                digest = content.hexdigest()
            else:
                digest = "special:" + oct(stat.S_IFMT(seen.st_mode))
            yield digest, os.path.relpath(path, root), ctime


def _records(path: str) -> Iterator[list]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def load(path: str) -> dict[str, str]:
    """Read a hash list into {relative path: digest}."""

    return {name: digest for digest, name in _records(path)}


def load_reference(path: str) -> dict[str, tuple[str, int]]:
    """Read a `hash --ctime` list into {relative path: (digest, ctime in ns)}."""

    return {name: (digest, ctime) for digest, name, ctime in _records(path)}


@dataclass(frozen=True)
class Comparison:
    restored: int
    reference: int
    identical: int
    differ: list[str]
    only_restored: list[str]
    only_reference: list[str]
    allow_empty: bool = False

    @property
    def passed(self) -> bool:
        return self.restored == self.reference == self.identical and (self.restored > 0 or self.allow_empty)

    def summary(self) -> str:
        return (
            f"restored={self.restored} reference={self.reference} identical={self.identical} "
            f"differ={len(self.differ)} only_restored={len(self.only_restored)} "
            f"only_reference={len(self.only_reference)}"
        )


def compare(restored: dict[str, str], reference: dict[str, str], *, allow_empty: bool = False) -> Comparison:
    return Comparison(
        restored=len(restored),
        reference=len(reference),
        identical=sum(1 for name, digest in restored.items() if reference.get(name) == digest),
        differ=sorted(name for name in restored if name in reference and reference[name] != restored[name]),
        only_restored=sorted(set(restored) - set(reference)),
        only_reference=sorted(set(reference) - set(restored)),
        allow_empty=allow_empty,
    )


def _write_names(path: str, names: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(name) + "\n" for name in names)


def exclude_patterns_under(root: str, patterns: list[str]) -> list[list[str]]:
    """The backup's exclude patterns that fall under `root`, as path components relative to it.

    restic anchors a pattern that starts with `/` at the filesystem root. A
    pattern of another root does not apply here, so it is dropped.
    """

    prefix = root.rstrip("/") + "/"
    relative = [pattern[len(prefix):].split("/") for pattern in patterns if pattern.startswith(prefix)]
    if any("**" in parts for parts in relative):
        # restic's `**` crosses directories; fnmatch cannot say that, so a pattern that uses it cannot be judged.
        raise ValueError("an exclude pattern uses **")
    return relative


def is_excluded(name: str, patterns: list[list[str]]) -> bool:
    """Whether restic would exclude `name`: `*` stays inside one path component, and a pattern that
    matches a directory excludes everything below it."""

    parts = name.split("/")
    return any(
        len(parts) >= len(pattern) and all(map(fnmatch.fnmatchcase, parts, pattern)) for pattern in patterns
    )


@dataclass(frozen=True)
class Judgement:
    judged: int
    missing: list[str]
    differ: list[str]
    changed_since: list[str]
    removed_since: list[str]
    excluded: list[str]

    @property
    def exit_code(self) -> int:
        if self.missing or self.differ:
            return 3
        return 0 if self.judged else 4

    def summary(self) -> str:
        return (
            f"judged={self.judged} changed_since={len(self.changed_since)} "
            f"removed_since={len(self.removed_since)} excluded={len(self.excluded)} "
            f"missing={len(self.missing)} differ={len(self.differ)}"
        )


def judge(
    restored: dict[str, str], reference: dict[str, tuple[str, int]], cutoff_ns: int, excludes: list[list[str]]
) -> Judgement:
    """Judge the restore by what the reference has not changed since `cutoff_ns`.

    A reference entry with an earlier ctime must be in the restore with the same digest.
    A later ctime, and a restored entry the reference lacks (a deletion leaves no time to
    judge by), are reported. An entry that matches an exclude pattern is reported, never judged.
    """

    judged, missing, differ, changed_since, removed_since, excluded = 0, [], [], [], [], []
    for name in sorted(reference.keys() | restored.keys()):
        if is_excluded(name, excludes):
            excluded.append(name)
        elif name not in reference:
            removed_since.append(name)
        elif reference[name][1] >= cutoff_ns:
            changed_since.append(name)
        else:
            judged += 1
            if name not in restored:
                missing.append(name)
            elif restored[name] != reference[name][0]:
                differ.append(name)
    return Judgement(judged, missing, differ, changed_since, removed_since, excluded)


def snapshot_cutoff_ns(log: str) -> int:
    """The snapshot's time in ns, truncated down to the second, from a restic restore log."""

    found = RESTORING_LINE.search(log)
    if found is None:
        raise ValueError("no restoring line")
    moment = dt.datetime.strptime(f"{found[1]} {found[2]}", "%Y-%m-%d %H:%M:%S %z")
    return int(moment.timestamp()) * 1_000_000_000


def _judge(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="judge")
    parser.add_argument("restored")
    parser.add_argument("reference")
    parser.add_argument("cutoff_ns")
    parser.add_argument("out_dir")
    parser.add_argument("--root", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args(argv)
    result = judge(
        load(args.restored), load_reference(args.reference), int(args.cutoff_ns),
        exclude_patterns_under(args.root, args.exclude),
    )
    for label in ("missing", "differ", "changed_since", "removed_since", "excluded"):
        _write_names(os.path.join(args.out_dir, label + ".txt"), getattr(result, label))
    print(result.summary())
    return result.exit_code


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    try:
        if command == "hash" and (len(argv) == 2 or (len(argv) == 3 and argv[1] == "--ctime")):
            for digest, name, ctime in tree_entries(argv[-1]):
                sys.stdout.write(json.dumps([digest, name, ctime] if len(argv) == 3 else [digest, name]) + "\n")
            return 0
        if command == "compare" and (len(argv) == 4 or (len(argv) == 5 and argv[4] == "--allow-empty")):
            result = compare(load(argv[1]), load(argv[2]), allow_empty=len(argv) == 5)
            for label in ("differ", "only_restored", "only_reference"):
                _write_names(os.path.join(argv[3], label + ".txt"), getattr(result, label))
            print(result.summary())
            return 0 if result.passed else 3
        if command == "judge":
            return _judge(argv[1:])
        if command == "cutoff" and len(argv) == 1:
            print(snapshot_cutoff_ns(sys.stdin.buffer.read().decode("utf-8", "replace")))
            return 0
    except Exception as error:  # noqa: BLE001 - a traceback would name vault paths in the run log
        print(f"{command} failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        "usage: hash [--ctime] ROOT | compare RESTORED REFERENCE OUT_DIR [--allow-empty] | "
        "judge RESTORED REFERENCE CUTOFF_NS OUT_DIR --root ROOT [--exclude PATTERN]... | cutoff",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
