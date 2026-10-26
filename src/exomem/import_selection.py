"""Which files of a folder an import takes, and why it leaves the others behind.

`exomem import` and `exomem cloud import` both start here, so one folder gives the same
selection and the same report on either route. The walk never follows a link, and it never
reads a file's content. An entry is counted once, under the first reason that applies.
"""
from __future__ import annotations

import argparse
import enum
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from .cli_progress import files_phrase, human_bytes

# A .gitignore larger than this is not a rules file; reading it whole could exhaust memory.
_GITIGNORE_LIMIT = 4 * 1024 * 1024
_SAMPLE_PATHS = 5
# The flags that open a file for reading without following a final link or waiting on a pipe.
# `getattr` because Windows has no O_NOFOLLOW or O_NONBLOCK.
_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)


class Reason(enum.StrEnum):
    """Why an entry stays behind; the value is the word a person reads in the report.

    A closed set that the walk below implements, one branch each. It is not a vocabulary a vault defines.
    """

    HIDDEN = "hidden"
    GITIGNORE = "gitignore"
    SYMLINK = "symlink"
    SPECIAL = "special file"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Kept:
    rel: str
    size: int
    mtime_ns: int
    identity: tuple[int, int]  # (st_dev, st_ino) at enumeration, checked again at open


@dataclass(frozen=True, slots=True)
class Skipped:
    """A file, or a whole folder, that stays behind; `files` and `bytes` count what it holds."""

    rel: str
    reason: Reason
    is_dir: bool
    files: int
    bytes: int


@dataclass
class Selection:
    kept: list[Kept] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def kept_bytes(self) -> int:
        return sum(item.size for item in self.kept)

    def tally(self) -> dict[Reason, tuple[int, int]]:
        """`(files, bytes)` left behind under each reason that occurs."""
        totals: dict[Reason, list[int]] = {}
        for item in self.skipped:
            row = totals.setdefault(item.reason, [0, 0])
            row[0] += item.files
            row[1] += item.bytes
        # Enum order, so the report reads the same on every run.
        return {reason: (totals[reason][0], totals[reason][1]) for reason in Reason if reason in totals}

    def summary(self) -> dict:
        return {
            "selected": {"files": len(self.kept), "bytes": self.kept_bytes},
            "left_out": {
                reason.name: {"files": files, "bytes": size}
                for reason, (files, size) in self.tally().items()
            },
        }

    def report_lines(self) -> list[str]:
        """What the import takes and what it leaves behind, for a person to read before it starts."""
        lines = [f"Selected {files_phrase(len(self.kept))}, {human_bytes(self.kept_bytes)}."]
        tally = self.tally()
        if tally:
            lines.append("Left behind (each entry counts under the first reason that applies):")
            for reason, (files, size) in tally.items():
                lines.append(f"  {reason.value}: {files_phrase(files)}, {human_bytes(size)}")
                if reason in (Reason.SYMLINK, Reason.SPECIAL, Reason.UNREADABLE):
                    paths = [item.rel for item in self.skipped if item.reason is reason]
                    shown = ", ".join(paths[:_SAMPLE_PATHS])
                    more = f" and {len(paths) - _SAMPLE_PATHS} more" if len(paths) > _SAMPLE_PATHS else ""
                    lines.append(f"    {shown}{more}")
            lines.append("  To take them, pass --include-hidden or --no-gitignore.")
        lines.extend(self.notes)
        return lines


def add_selection_arguments(command: argparse.ArgumentParser) -> None:
    """The two switches that both import commands share."""
    command.add_argument("--include-hidden", action="store_true",
                         help="also take files and folders whose name starts with a dot")
    command.add_argument("--no-gitignore", action="store_true",
                         help="ignore the .gitignore files inside the folder")


def open_for_read(path: str | Path) -> int:
    """A read-only descriptor for a file; a link as the last name is refused, never followed."""
    return os.open(path, _READ_FLAGS)


def _tally_tree(directory: str, notes: list[str], rel: str) -> tuple[int, int]:
    """Count the entries and regular-file bytes under a folder that stays behind, by `lstat` alone."""
    files = size = 0
    pending = [directory]
    partial = False
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                        continue
                    files += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        partial = True
                        continue
                    if stat.S_ISREG(info.st_mode):
                        size += info.st_size
        except OSError:
            partial = True
    if partial:
        notes.append(f"The counts for {rel}/ are partial: part of it could not be read.")
    return files, size


def _read_gitignore(path: str, rel: str, notes: list[str]) -> pathspec.GitIgnoreSpec | None:
    try:
        descriptor = open_for_read(path)
    except OSError:
        notes.append(f"{rel} could not be read, so its rules are not applied.")
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            notes.append(f"{rel} is not a regular file, so its rules are not applied.")
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_GITIGNORE_LIMIT + 1)
    except OSError:
        notes.append(f"{rel} could not be read, so its rules are not applied.")
        return None
    finally:
        os.close(descriptor)
    if len(raw) > _GITIGNORE_LIMIT:
        notes.append(f"{rel} is too large to be a rules file, so its rules are not applied.")
        return None
    try:
        return pathspec.GitIgnoreSpec.from_lines(raw.decode("utf-8", errors="replace").splitlines())
    except ValueError:
        notes.append(f"{rel} holds a pattern that cannot be read, so its rules are not applied.")
        return None


def _is_ignored(rules: tuple[tuple[str, pathspec.GitIgnoreSpec], ...], rel: str, is_dir: bool) -> bool:
    """Git's precedence: the deepest .gitignore with an opinion on the path decides."""
    for base, spec in reversed(rules):
        inner = rel[len(base):] if base else rel
        verdict = spec.check_file(inner + "/" if is_dir else inner).include
        if verdict is not None:
            return verdict
    return False


def select(folder: Path, *, include_hidden: bool = False, use_gitignore: bool = True) -> Selection:
    """Walk `folder` once and sort every entry into the files to take or the reason to leave it."""
    result = Selection()
    # Each pending folder carries the .gitignore files above it as (prefix, spec); the prefix is
    # the folder's path from the root, with a closing slash, so a rule reads relative to its file.
    pending: list[tuple[str, str, tuple[tuple[str, pathspec.GitIgnoreSpec], ...]]] = [
        (str(folder), "", ())
    ]
    while pending:
        directory, prefix, rules = pending.pop()
        try:
            with os.scandir(directory) as scanned:
                entries = sorted(scanned, key=lambda item: item.name)
        except OSError:
            where = prefix.rstrip("/") or "."
            result.skipped.append(Skipped(where, Reason.UNREADABLE, True, 0, 0))
            result.notes.append(f"{where} could not be listed, so what it holds is not counted.")
            continue
        if use_gitignore:
            ruleset = next((item for item in entries if item.name == ".gitignore"), None)
            if ruleset is not None and ruleset.is_file(follow_symlinks=False):
                spec = _read_gitignore(ruleset.path, prefix + ruleset.name, result.notes)
                if spec is not None:
                    rules = (*rules, (prefix, spec))
        descend = []
        for entry in entries:
            rel = prefix + entry.name
            is_dir = entry.is_dir(follow_symlinks=False)
            reason: Reason | None = None
            if entry.name.startswith(".") and not include_hidden:
                reason = Reason.HIDDEN
            elif rules and _is_ignored(rules, rel, is_dir):
                reason = Reason.GITIGNORE
            elif entry.is_symlink():
                reason = Reason.SYMLINK
            if reason is not None:
                if is_dir:
                    files, size = _tally_tree(entry.path, result.notes, rel)
                else:
                    files, size = 1, 0
                    try:
                        info = entry.stat(follow_symlinks=False)
                        size = info.st_size if stat.S_ISREG(info.st_mode) else 0
                    except OSError:
                        pass
                result.skipped.append(Skipped(rel, reason, is_dir, files, size))
                continue
            if is_dir:
                descend.append((entry.path, rel + "/", rules))
                continue
            try:
                info = os.lstat(entry.path)
            except OSError:
                result.skipped.append(Skipped(rel, Reason.UNREADABLE, False, 1, 0))
                continue
            if not stat.S_ISREG(info.st_mode):
                result.skipped.append(Skipped(rel, Reason.SPECIAL, False, 1, 0))
                continue
            try:
                os.close(open_for_read(entry.path))
            except OSError:
                result.skipped.append(Skipped(rel, Reason.UNREADABLE, False, 1, info.st_size))
                continue
            result.kept.append(Kept(rel, info.st_size, info.st_mtime_ns, (info.st_dev, info.st_ino)))
        # Reverse so the pop order matches the sorted order.
        pending.extend(reversed(descend))
    result.kept.sort(key=lambda item: item.rel)
    return result
