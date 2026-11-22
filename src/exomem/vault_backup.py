"""Personal vault backup (OpenSpec change `add-personal-vault-backup`).

An owner config file outside the vault names one restic repository per vault. A service thread then keeps
dated snapshots of the vault and of the machine-local state that nothing can rebuild, applies the owner's keep
rules after a complete backup, checks a rotating part of the repository, and restores a sample to prove that
originals still hash to their admission SHA-256. One small JSON record under the vault's state directory holds
what ran. Losing it only makes status read unknown.

A cycle is single-flight under a file lock, never takes the vault mutation guard, and bounds every restic call.
Secrets are file paths: the repository, the password file and the backend credentials reach restic only in its
subprocess environment, so none shows in a process list, the record, the status or a log line.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import math
import os
import queue
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote_to_bytes

from filelock import FileLock, Timeout

from . import foreground_priority, originals, private_state, reserved_paths, state_paths

log = logging.getLogger(__name__)

CONFIG_ENV = "EXOMEM_VAULT_BACKUP_CONFIG"
STORE_DIRNAME = "vault-backup"
RECORD_STEM = "status"
#: The staged state's directory name. A snapshot lists this directory beside the vault, and a restore finds
#: each of the two by it, whatever path the machine that took the snapshot used.
STAGE_DIRNAME = "exomem-state-stage"
#: Names a sampled restore's temporary folder under the store.
SAMPLE_PREFIX = "sample-"
#: Marks this service's snapshots, so retention and restore never act on another tool's in the same repository.
TAG = "exomem-vault"
#: Starts the tag that names the vault root a snapshot was taken from. The root is a tag, not a snapshot path:
#: a backup without originals lists each file it keeps as a path, so the paths cannot name the root.
ROOT_TAG = "exomem-vault-root="
#: The first restic release with distinct exit codes for a missing repository (10) and a wrong password (12).
MIN_RESTIC = (0, 17, 1)
SCHEMA = 1
#: The record's activity keys, in the order a cycle runs them.
# nosemgrep: ep-word-set -- This module's own record protocol names its activities; the owner defines none.
_ACTIVITIES = ("backup", "check", "sample")

# State values are this status protocol's own, not vault vocabulary.
NOT_CONFIGURED = "not_configured"
INVALID_CONFIG = "invalid_config"
RESTIC_NOT_FOUND = "restic_not_found"
CONFIGURED = "configured"
UNKNOWN = "unknown"
NEVER = "never"
COMPLETE = "complete"
PARTIAL = "partial"
FAILED = "failed"
OK = "ok"
EMPTY = "empty"
DUE = "due"

# How restic reports a failure that the service names; the exit codes are restic's documented ones.
_RESTIC_EXIT_CODES = {10: "repository_missing", 11: "repository_locked", 12: "wrong_password"}
_BACKUP_PARTIAL_EXIT = 3  # a snapshot exists, but some source files could not be read

#: Seconds each kind of restic call may run. A backup or an owner restore of a very large vault has to finish in
#: one call, because restic resumes only by reusing the data it already moved. The others bound a hung connection;
#: a sampled restore is small, so a day of silence means the connection hung.
_TIMEOUT_SECONDS = {"backup": 7 * 86400, "check": 3 * 86400, "forget": 86400, "restore": 7 * 86400,
                    "sample": 86400, "query": 3600}
_RETRY_PAUSE_SECONDS = 30.0
#: How often a running restic process is checked against the stop signal and its timeout.
_POLL_PROCESS_SECONDS = 1.0
#: How long a terminated restic process may take to clean up before it is killed.
_TERMINATE_GRACE_SECONDS = 10.0
_IDEMPOTENT_RETRIES = 2
#: A failed activity waits this long before it is due again, so a broken repository is not hammered.
_RETRY_AFTER_FAILURE_SECONDS = 3600.0
_POLL_SECONDS = 900.0
_FINDINGS_LIMIT = 50
#: After this many files that fail alone, a sampled restore stops trying the rest one by one.
_FALLBACK_FAILURES = 10
_HASH_CHUNK = 1 << 20
_SQLITE_MAGIC = b"SQLite format 3\x00"
#: The SQLite sidecar suffixes, defined once in the registry (with the main file's empty suffix first).
_SQLITE_SIDECARS = reserved_paths._SQLITE_SUFFIXES[1:]
_POSIX = os.name == "posix"


class BackupError(Exception):
    """A refusal or failure that has a stable code, for the CLI, the record and the log."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ConfigError(BackupError):
    """The owner config file or its entry for this vault cannot be used."""


# --- configuration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    repository: str
    password_file: Path
    environment_file: Path | None
    interval_hours: float
    keep: dict[str, int]
    check_interval_days: float
    check_parts: int
    sample_interval_days: float
    sample_size: int
    sample_max_bytes: int
    include_originals: bool

    @property
    def fingerprint(self) -> str:
        """Names which repository this entry uses, without showing a string that may embed a credential."""
        return hashlib.sha256(self.repository.encode("utf-8")).hexdigest()[:8]


#: Restic's own keep policies, with the count each defaults to. Restic fixes the names; the owner sets the counts.
# nosemgrep: ep-word-set -- restic's forget policies are a closed set that restic defines, not vault vocabulary.
_KEEP_DEFAULTS = {"last": 0, "daily": 7, "weekly": 4, "monthly": 12}
#: The numeric settings of an entry: key -> (default, whole number, zero allowed).
# nosemgrep: ep-word-set -- this file format's own closed schema keys; the owner defines no key.
_SETTINGS = {
    "interval_hours": (24, False, False),
    "check_interval_days": (7, False, False),
    "check_subset_parts": (12, True, False),
    "sample_interval_days": (7, False, False),
    "sample_size": (20, True, False),
    "sample_max_mib": (64, True, False),
}
#: Every key an entry may hold. Any other key is a typo: it would silently fall back to a default, and a default
#: keep rule prunes history, so it is refused.
# nosemgrep: ep-word-set -- this file format's own closed schema; the owner defines no key.
_ENTRY_KEYS = frozenset((*_SETTINGS, *(f"keep_{name}" for name in _KEEP_DEFAULTS), "repository", "password_file",
                         "environment_file", "include_originals"))


def _number(entry: dict[str, Any], key: str, default: float, *, whole: bool = False, zero: bool = False) -> Any:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int if whole else (int, float)):
        raise ConfigError("invalid_config", key)
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ConfigError("invalid_config", key)
    return value


def _outside(vault: Path, path: Path, key: str) -> Path:
    """A vault copy must not carry the means to read the backup, so a secret path never lies inside the vault."""
    if path.resolve(strict=False).is_relative_to(vault):
        raise ConfigError("invalid_config", key)
    return path


def load_config(vault: Path) -> Config | None:
    """The vault's entry, or None when no backup is configured for it. Raises `ConfigError` for a bad one."""
    configured = os.environ.get(CONFIG_ENV)
    if not configured:
        return None
    vault = Path(vault).resolve()
    # The file names the credential files, so a copy of the vault that carried it would carry the way to the backup.
    _outside(vault, Path(configured), CONFIG_ENV)
    try:
        document = json.loads(Path(configured).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError("config_unreadable", type(error).__name__) from None
    if not isinstance(document, dict) or not isinstance(document.get("vaults"), dict):
        raise ConfigError("invalid_config", "vaults")
    entry = document["vaults"].get(str(vault))
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ConfigError("invalid_config", "entry")
    for key in sorted(entry):
        if key not in _ENTRY_KEYS:
            raise ConfigError("invalid_config", str(key))
    repository = entry.get("repository")
    password = entry.get("password_file")
    environment = entry.get("environment_file")
    if not isinstance(repository, str) or not repository.strip():
        raise ConfigError("invalid_config", "repository")
    if not isinstance(password, str) or not password:
        raise ConfigError("invalid_config", "password_file")
    if environment is not None and (not isinstance(environment, str) or not environment):
        raise ConfigError("invalid_config", "environment_file")
    if not isinstance(entry.get("include_originals", True), bool):
        raise ConfigError("invalid_config", "include_originals")
    # A repository named by an absolute path inside the vault would hold a snapshot of itself. Comparing two
    # paths reads no scheme: a remote location is never an absolute local path.
    if Path(repository).is_absolute():
        _outside(vault, Path(repository), "repository")
    keep = {name: _number(entry, f"keep_{name}", default, whole=True, zero=True)
            for name, default in _KEEP_DEFAULTS.items()}
    if not any(keep.values()):
        raise ConfigError("invalid_config", "keep")
    settings = {key: _number(entry, key, default, whole=whole, zero=zero)
                for key, (default, whole, zero) in _SETTINGS.items()}
    return Config(
        repository=repository,
        password_file=_outside(vault, Path(password).expanduser(), "password_file"),
        environment_file=_outside(vault, Path(environment).expanduser(), "environment_file") if environment else None,
        interval_hours=settings["interval_hours"],
        keep=keep,
        check_interval_days=settings["check_interval_days"],
        check_parts=settings["check_subset_parts"],
        sample_interval_days=settings["sample_interval_days"],
        sample_size=settings["sample_size"],
        sample_max_bytes=settings["sample_max_mib"] << 20,
        include_originals=entry.get("include_originals", True),
    )


def _credentials(config: Config) -> dict[str, str]:
    """The backend credentials in the environment file: `NAME=value` lines, blank lines and `#` comments allowed."""
    if config.environment_file is None:
        return {}
    try:
        lines = config.environment_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise BackupError("environment_file_unreadable", type(error).__name__) from None
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not name.isascii() or not name.isidentifier():
            raise BackupError("environment_file_invalid")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def _environment(config: Config, cache: Path) -> tuple[dict[str, str], list[str]]:
    """The restic subprocess environment, and the secret values that a log line must never carry."""
    credentials = _credentials(config)
    # Drop every inherited RESTIC_* variable: a stray one must not redirect the call to another repository.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")}
    environment.update(credentials)
    environment.update({
        "RESTIC_REPOSITORY": config.repository,
        "RESTIC_PASSWORD_FILE": str(config.password_file),
        "RESTIC_CACHE_DIR": str(cache),
        # One progress line a minute at most: a backup runs for hours and its output is read at the end.
        "RESTIC_PROGRESS_FPS": "0.0166",
    })
    secrets = [config.repository, *(value for value in credentials.values() if len(value) >= 4)]
    return environment, secrets


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


# --- the restic wrapper -----------------------------------------------------------------------


class _Restic:
    """Bounded restic calls for one configured vault."""

    def __init__(self, vault: Path, config: Config, stop: threading.Event | None = None) -> None:
        self.config = config
        self.vault = Path(vault)
        # The machine's name is recorded on each snapshot and used as a filter; it is one value for the whole call.
        self.host = socket.gethostname()
        self.stop = stop or threading.Event()
        cache = private_state.store(vault, STORE_DIRNAME) / "restic-cache"
        self.environment, self.secrets = _environment(config, cache)
        self.executable = shutil.which("restic")
        if self.executable is None:
            raise BackupError(RESTIC_NOT_FOUND)

    @property
    def tags(self) -> str:
        """The tags of this vault's snapshots, as restic's one comma-joined value. The root is percent-encoded,
        because restic splits a tag value at a comma."""
        return f"{TAG},{ROOT_TAG}{quote(os.fsencode(self.vault), safe='/')}"

    @property
    def own(self) -> list[str]:
        """restic's filters for this vault's snapshots made on this machine. The root tag keeps two vaults in one
        repository apart, and the host keeps two machines that use the same path apart, so a retention rule or a
        sample never reaches a snapshot that another vault or machine took. A machine that is renamed starts a
        new group: its old snapshots stay until the owner forgets them, which is the safe direction."""
        return ["--host", self.host, "--tag", self.tags]

    def require_version(self) -> None:
        done = self._once(["version"], timeout=_TIMEOUT_SECONDS["query"])
        found = re.match(rb"restic (\d+)\.(\d+)\.(\d+)", done.stdout)  # a version token's shape, not meaning
        if done.returncode or found is None:
            raise BackupError("restic_unusable", f"exit {done.returncode}")
        if tuple(int(part) for part in found.groups()) < MIN_RESTIC:
            raise BackupError("restic_too_old", ".".join(map(str, MIN_RESTIC)))

    def _once(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        """One restic process, ended at its timeout or when the service stops, whichever comes first."""
        deadline = time.monotonic() + timeout
        try:
            # An argument list and no shell: nothing the owner configured is ever read as a command.
            process = subprocess.Popen([self.executable, *args], env=self.environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            raise BackupError("restic_unusable", type(error).__name__) from None
        with process:
            while True:
                try:
                    out, err = process.communicate(timeout=_POLL_PROCESS_SECONDS)
                    return subprocess.CompletedProcess([self.executable, *args], process.returncode, out, err)
                except subprocess.TimeoutExpired:
                    if self.stop.is_set() or time.monotonic() >= deadline:
                        process.terminate()  # restic cleans up and releases its repository lock on SIGTERM
                        try:
                            process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate()
                        raise BackupError("stopped" if self.stop.is_set() else "timeout", args[0]) from None

    def run(self, args: list[str], *, kind: str, retries: int = 0, ok: tuple[int, ...] = (0,),
            retry_timeouts: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Run restic. Only a call that is safe to repeat passes `retries`, and only a timeout or a repository
        lock held by another restic process is retried: any other failure repeats the same way. A call that has
        written partial output by the time it times out (a restore) passes `retry_timeouts=False`: a lock
        failure happens before restic writes anything, a timeout does not."""
        attempt = 0
        while True:
            try:
                done = self._once(args, timeout=_TIMEOUT_SECONDS[kind])
            except BackupError as error:
                if error.code != "timeout" or not retry_timeouts:  # a stop or a missing executable is not repeated
                    raise
                failure = error
            else:
                if done.returncode in ok:
                    return done
                # Scrub the whole text, then cut: a secret that straddles the cut would otherwise leak its tail.
                log.warning("restic %s exited %d: %s", args[0], done.returncode,
                            _scrub(done.stderr.decode("utf-8", "replace"), self.secrets)[-300:])
                code = _RESTIC_EXIT_CODES.get(done.returncode)
                failure = BackupError(code or f"restic_exit_{done.returncode}", args[0])
                if code != "repository_locked":
                    raise failure
            attempt += 1
            if attempt > retries:
                raise failure
            if self.stop.wait(_RETRY_PAUSE_SECONDS):
                raise BackupError("stopped", args[0])


# --- the record -------------------------------------------------------------------------------


def _record_path(vault: Path) -> Path:
    return state_paths.vault_state_dir(Path(vault)) / STORE_DIRNAME / f"{RECORD_STEM}.json"


def _read_record(vault: Path) -> dict[str, Any] | None:
    """The record, or None when it is missing or unreadable: both read as unknown, never as an empty one."""
    try:
        record = json.loads(_record_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("schema") == SCHEMA else None


def _write_record(vault: Path, record: dict[str, Any]) -> None:
    store = private_state.store(vault, STORE_DIRNAME)
    private_state.write_record(store, RECORD_STEM, {**record, "schema": SCHEMA}, temp_suffix=".tmp")


# --- staging the state that cannot be rebuilt -------------------------------------------------


#: Copies the files that SQLite refused, in a child process. Closing any descriptor of a file drops every POSIX lock
#: the process that opened it holds on that file, and the service's own connections lock their databases: a
#: database whose header is damaged reads as no database while a connection here still holds it. A child process
#: holds no lock, so its reads and copies cannot drop one. It takes SQLite's header as hex in its argument, then
#: reads one JSON `[source, target]` line at a time and answers each with one result line: `ok`, `gone`,
#: `sqlite_header` (left uncopied) or `error`.
_PLAIN_COPIER = r"""
import json, os, shutil, sys
magic = bytes.fromhex(sys.argv[1])
for line in sys.stdin:
    source, target = json.loads(line)
    try:
        with open(source, "rb") as origin:
            if origin.read(len(magic)) == magic:
                result = "sqlite_header"
            else:
                origin.seek(0)
                try:
                    with open(target, "xb") as copy:
                        shutil.copyfileobj(origin, copy, 1 << 20)
                    os.chmod(target, 0o600)
                except OSError:
                    try:
                        os.unlink(target)  # a part copy must not reach the snapshot
                    except OSError:
                        pass
                    raise
                result = "ok"
    except FileNotFoundError:
        result = "gone"
    except OSError:
        result = "error"
    sys.stdout.write(result + "\n")
    sys.stdout.flush()
"""
#: The longest the child may take for one backup's plain files.
_PLAIN_COPY_TIMEOUT = 3600.0
#: How often a wait for the child looks at the stop request.
_PLAIN_COPY_POLL = 1.0


def _copy_plain(pending: list[tuple[Path, Path, str]], findings: list[dict[str, str]], stop: threading.Event) -> int:
    """Copy each `(source, target, name)` through one child process, one file at a time; the count copied.

    A failure becomes a finding. A stop request ends it between files or during a copy, and a stopped copy is
    removed. A child that stops answering, or runs past the timeout, leaves every file not yet copied as a finding.
    """
    if not pending:
        return 0
    try:
        # The protocol is ASCII: JSON escapes every other character of a path, and the answers are fixed tokens.
        helper = subprocess.Popen([sys.executable, "-I", "-c", _PLAIN_COPIER, _SQLITE_MAGIC.hex()], text=True,
                                  encoding="utf-8", errors="strict", stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        findings.extend({"kind": "state", "path": name, "code": "state_copy_failed"} for _, _, name in pending)
        return 0
    answers: queue.Queue[str] = queue.Queue()

    def read_answers() -> None:
        # A thread reads the answers, so a wait can look at the stop request on every platform. An empty answer
        # means the child stopped answering.
        for line in helper.stdout:
            answers.put(line.strip())
        answers.put("")

    threading.Thread(target=read_answers, daemon=True).start()
    deadline = time.monotonic() + _PLAIN_COPY_TIMEOUT
    copied = 0
    try:
        for index, (source, target, name) in enumerate(pending):
            if stop.is_set():
                raise BackupError("stopped", "staging")
            try:
                helper.stdin.write(json.dumps([str(source), str(target)]) + "\n")
                helper.stdin.flush()
            except OSError:
                result = ""
            else:
                result = None
            while result is None:
                if stop.is_set():
                    _end_copier(helper)
                    target.unlink(missing_ok=True)
                    raise BackupError("stopped", "staging")
                if time.monotonic() > deadline:
                    result = ""
                    break
                try:
                    result = answers.get(timeout=_PLAIN_COPY_POLL)
                except queue.Empty:
                    pass
            if result == "ok":
                copied += 1
            elif result == "":
                # The child stopped answering: this copy is not trusted, and nothing after it was copied.
                _end_copier(helper)
                target.unlink(missing_ok=True)
                findings.extend({"kind": "state", "path": rest, "code": "state_copy_failed"}
                                for _, _, rest in pending[index:])
                break
            elif result != "gone":  # a removal after the listing; the next backup sees the new file
                findings.append({"kind": "state", "path": name, "code": "state_copy_failed"})
    finally:
        if helper.poll() is None:
            with contextlib.suppress(OSError):
                helper.stdin.close()
            try:
                helper.wait(timeout=_PLAIN_COPY_POLL)
            except subprocess.TimeoutExpired:
                _end_copier(helper)
    return copied


def _end_copier(helper: subprocess.Popen) -> None:
    """Kill the child and wait until it is gone. Only then may a copy it was writing be removed: a child still
    running could write the file again after the removal."""
    helper.kill()
    helper.wait()


def _copy_sqlite(source: Path, target: Path) -> bool:
    """Copy a live database with SQLite's online backup API; whether the copy passes a quick integrity check.

    The copy is one step, so it reads one snapshot of the database: a stepped copy starts over whenever another
    connection writes, and never ends on a busy database. The source opens read-write but refuses writes
    (`query_only`): a read-only open leaves a `-wal` and a `-shm` file beside a database that is closed
    cleanly, and a read-write one removes them when it is the last connection to close.
    """
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=rw", uri=True, timeout=30)) as origin, \
            closing(sqlite3.connect(target)) as copy:
        origin.execute("PRAGMA query_only = ON")
        origin.backup(copy, pages=-1)
    os.chmod(target, 0o600)
    with closing(sqlite3.connect(target)) as copy:
        return copy.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)


def _stage_tree(source: Path, target: Path, findings: list[dict[str, str]], stop: threading.Event,
                plain: list[tuple[Path, Path, str]]) -> int:
    """Stage one state file or tree; the count of databases and empty files staged. A file that SQLite refuses goes
    to `plain`, for `_copy_plain`. A file it cannot stage becomes a finding. A stop ends it between files.

    This process opens a state file only through SQLite, which defers closing a file's descriptors while any of its
    connections holds a lock on that file, so staging never drops a lock that the service's connections hold (see
    `_PLAIN_COPIER`).
    """
    if stop.is_set():
        raise BackupError("stopped", "staging")
    if source.is_symlink():
        return 0
    if source.is_dir():
        target.mkdir(mode=0o700)
        return sum(_stage_tree(child, target / child.name, findings, stop, plain) for child in sorted(source.iterdir()))
    if not source.is_file():
        return 0
    name = source.name
    # A write-ahead-log, shared-memory or journal file is part of a live database: copying it raw is a torn copy.
    if any(name.endswith(suffix) and (source.parent / name[:-len(suffix)]).is_file() for suffix in _SQLITE_SIDECARS):
        return 0
    try:
        # SQLite reads a file of 0 or 1 bytes as an empty database (its unix layer reports a 1-byte file as empty) and
        # would stage a new one. A database holds at least one page, so such a file is no database: an empty one is
        # staged unopened, and a 1-byte one is copied whole.
        size = source.stat().st_size
        if size == 0:
            target.touch(mode=0o600, exist_ok=False)
            return 1
        if size == 1:
            plain.append((source, target, name))
            return 0
        try:
            intact = _copy_sqlite(source, target)
        except sqlite3.DatabaseError as error:
            target.unlink(missing_ok=True)
            if not source.exists():
                return 0  # removed after the listing, as below
            if getattr(error, "sqlite_errorname", None) != "SQLITE_NOTADB":
                raise
            plain.append((source, target, name))
            return 0
        if not intact:
            findings.append({"kind": "state", "path": name, "code": "state_integrity"})
    except FileNotFoundError:
        return 0  # an atomic replace or a removal between the listing and the copy; the next backup sees the new file
    except (OSError, sqlite3.Error):
        findings.append({"kind": "state", "path": name, "code": "state_copy_failed"})
        return 0
    return 1


def stage_state(vault: Path, stage: Path, stop: threading.Event | None = None) -> tuple[int, list[dict[str, str]]]:
    """Copy every non-rebuildable state family into `stage`, emptied first. Returns the file count and findings.

    The registry decides which families: `reserved_paths.non_rebuildable_state_descriptors` is the one place
    that declares it. This function holds no list of names of its own.
    """
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(mode=0o700)
    kept = {descriptor.id for descriptor in reserved_paths.non_rebuildable_state_descriptors()}
    findings: list[dict[str, str]] = []
    try:
        names = sorted(os.listdir(state_paths.vault_state_dir(vault)))
    except FileNotFoundError:
        return 0, findings
    staged = 0
    plain: list[tuple[Path, Path, str]] = []
    for name in names:
        if reserved_paths.classify_logical(name).descriptor_id in kept:
            staged += _stage_tree(state_paths.vault_state_dir(vault) / name, stage / name, findings,
                                  stop or threading.Event(), plain)
    return staged + _copy_plain(plain, findings, stop or threading.Event()), findings


# --- originals --------------------------------------------------------------------------------


def _literal(path: str) -> str:
    """A path as a restic glob pattern that matches only that path: each glob character is escaped."""
    return re.sub(r"([\\*?\[])", r"\\\1", path)  # restic's glob metacharacters, a closed set fixed by restic


def _bound_originals(vault: Path) -> Iterator[Any]:
    """Every sidecar-bound original, yielding to foreground requests. A binding with an unconfirmed retirement
    is left out, so its original stays in the snapshot."""
    from . import media_store

    return foreground_priority.yielding(media_store.bound_originals(vault, unconfirmed=lambda error: None))


def _write_targets(vault: Path, target: Path) -> int:
    """List what a backup without originals keeps, as NUL-separated paths for restic's `--files-from-raw`. Returns
    how many bound originals the walk named.

    An exclude pattern per original costs restic one comparison per file and pattern, so it grows as files times
    originals. A list costs one entry per file: a directory with no original below it is one entry, and a
    directory that holds one lists its other entries. The original pool and each bound original are left out,
    and so is the upload stage: a stage is an upload's transient copy that a committed upload renames into
    place, and its bytes may still be growing while restic reads them.
    """
    from . import original_pool, upload_stage

    omitted = {str(original_pool.pool_root(vault)), str(upload_stage.stage_root(vault))}
    count = 0
    for snapshot in _bound_originals(vault):
        count += 1
        for path in {snapshot.path, snapshot.local_path} - {None}:
            # A note is never an original that offload moves (originals.is_note_path), so it stays.
            if not originals.is_note_path(path.name):
                omitted.add(str(path))
    holders = {str(vault)}  # the directories with an omitted path somewhere below
    for place in omitted:
        parent = str(Path(place).parent)
        while parent not in holders and Path(parent).is_relative_to(vault):
            holders.add(parent)
            parent = str(Path(parent).parent)
    pending = [str(vault)]
    with target.open("wb") as sink:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if entry.path in omitted:
                        continue
                    if entry.path in holders and entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    else:
                        sink.write(os.fsencode(entry.path) + b"\0")
    return count


# --- the cycle's steps ------------------------------------------------------------------------


def _summary(done: subprocess.CompletedProcess[bytes]) -> dict[str, Any] | None:
    """restic's JSON summary line of a backup: the last line that parses to a summary."""
    for line in reversed(done.stdout.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            return message
    return None


def _stamp(entry: dict[str, Any], previous: dict[str, Any] | None, *, success: bool, wait: float) -> dict[str, Any]:
    """Finish an activity's entry: when it ran, when it last succeeded (kept from before when this run did not)
    and when it is due again."""
    entry["at"] = time.time()
    entry["ok_at"] = entry["at"] if success else (previous or {}).get("ok_at")
    entry["due_at"] = entry["at"] + wait
    return entry


def _backup(vault: Path, restic: _Restic, previous: dict[str, Any] | None) -> dict[str, Any]:
    store = private_state.store(vault, STORE_DIRNAME)
    entry: dict[str, Any] = {"findings": []}
    stage = store / STAGE_DIRNAME
    targets = store / "targets.list"
    try:
        try:
            entry["state_files"], entry["findings"] = stage_state(vault, stage, restic.stop)
        except (OSError, sqlite3.Error):
            entry["state_files"], entry["findings"] = None, [{"kind": "state", "code": "state_staging_failed"}]
        # Group by host and tags, not paths: a backup without originals lists changing paths, and restic must still
        # find this vault's previous snapshot as the parent, or it reads every listed file again.
        arguments = ["backup", "--json", "--quiet", "--host", restic.host, "--tag", restic.tags, "--group-by", "host,tags"]
        if restic.config.include_originals:
            arguments.append(str(vault))
        else:
            if not _POSIX:
                raise BackupError("unsupported_platform", "include_originals")
            entry["excluded_originals"] = _write_targets(vault, targets)
            arguments += ["--files-from-raw", str(targets)]
        done = restic.run([*arguments, str(stage)], kind="backup", ok=(0, _BACKUP_PARTIAL_EXIT))
    except BackupError as error:
        return _stamp({**entry, "state": FAILED, "code": error.code}, previous, success=False,
                      wait=_RETRY_AFTER_FAILURE_SECONDS)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        targets.unlink(missing_ok=True)
    summary = _summary(done)
    if summary is None or not isinstance(summary.get("snapshot_id"), str):
        return _stamp({**entry, "state": FAILED, "code": "backup_summary_missing"}, previous, success=False,
                      wait=_RETRY_AFTER_FAILURE_SECONDS)
    complete = done.returncode == 0 and not entry["findings"]
    code = None if complete else ("source_unreadable" if done.returncode else entry["findings"][0]["code"])
    # A partial snapshot exists, so the next backup waits the normal interval: the findings name what to fix.
    return _stamp(
        {**entry, "state": COMPLETE if complete else PARTIAL, "code": code, "snapshot_id": summary["snapshot_id"],
         "files": summary.get("total_files_processed"), "bytes": summary.get("total_bytes_processed")},
        previous, success=complete, wait=restic.config.interval_hours * 3600)


def _forget(restic: _Restic, previous: dict[str, Any] | None) -> dict[str, Any]:
    keep = [part for name, count in restic.config.keep.items() if count for part in (f"--keep-{name}", str(count))]
    try:
        # The filters leave this vault's snapshots on this machine, so one group by host is the whole set: a
        # backup without originals records a different path list each time, and grouping by path would keep every one.
        restic.run(["forget", "--prune", "--group-by", "host", *restic.own, *keep], kind="forget",
                   retries=_IDEMPOTENT_RETRIES)
    except BackupError as error:
        return _stamp({"state": FAILED, "code": error.code}, previous, success=False, wait=0)
    return _stamp({"state": OK}, previous, success=True, wait=0)


def _check(restic: _Restic, previous: dict[str, Any] | None) -> dict[str, Any]:
    """Check the repository's structure and read one rotating part of its data. A part that failed is read
    again next time, so the finding stays until that part passes."""
    parts = restic.config.check_parts
    part = ((previous or {}).get("next_part", 1) - 1) % parts + 1
    entry: dict[str, Any] = {"part": f"{part}/{parts}", "findings": []}
    try:
        restic.run(["check", f"--read-data-subset={part}/{parts}"], kind="check", retries=_IDEMPOTENT_RETRIES)
    except BackupError as error:
        entry["findings"] = [{"kind": "repository", "code": error.code, "part": entry["part"]}]
        return _stamp({**entry, "state": FAILED, "code": error.code, "next_part": part}, previous, success=False,
                      wait=_RETRY_AFTER_FAILURE_SECONDS)
    return _stamp({**entry, "state": OK, "next_part": part % parts + 1}, previous, success=True,
                  wait=restic.config.check_interval_days * 86400)


@dataclass(frozen=True)
class _Pick:
    kind: str  # "original" or "note"
    path: str  # vault-relative file that the snapshot should hold
    sidecar: str | None = None
    digest: str | None = None  # the live binding's hash, the fallback when the restored sidecar is unreadable


def _reservoir(items: Iterable[_Pick], size: int, rng: random.Random) -> list[_Pick]:
    """A uniform sample of `size` items in one pass and bounded memory, whatever the vault's size."""
    chosen: list[_Pick] = []
    for seen, item in enumerate(items):
        if len(chosen) < size:
            chosen.append(item)
        elif (slot := rng.randrange(seen + 1)) < size:
            chosen[slot] = item
    return chosen


def _unchanged_since(path: Path, cutoff: float) -> bool:
    """Whether nothing touched the file since the snapshot began. The inode change time moves on a rename too,
    so a file moved after the snapshot is not drawn from the path where the snapshot never saw it."""
    try:
        return path.stat().st_ctime <= cutoff
    except OSError:
        return False


def _draw(vault: Path, config: Config, cutoff: float, rng: random.Random) -> tuple[list[_Pick], list[_Pick]]:
    """A sample of the notes and of the originals that nothing has touched since the snapshot began."""
    from .backfill import iter_kb_files
    from .vault import kb_root

    def notes() -> Iterator[_Pick]:
        for path in foreground_priority.yielding(iter_kb_files(kb_root(vault))):
            if path.suffix.lower() == ".md" and _unchanged_since(path, cutoff):
                yield _Pick("note", path.relative_to(vault).as_posix())

    def bound() -> Iterator[_Pick]:
        for snapshot in _bound_originals(vault):
            physical = snapshot.local_path or snapshot.path
            if (snapshot.state in ("local", "cached") and snapshot.binary_sha256
                    and (snapshot.binary_size or 0) <= config.sample_max_bytes
                    and _unchanged_since(physical, cutoff) and _unchanged_since(snapshot.sidecar_path, cutoff)):
                yield _Pick("original", physical.relative_to(vault).as_posix(),
                            snapshot.sidecar_path.relative_to(vault).as_posix(), snapshot.binary_sha256)

    rng_notes = _reservoir(notes(), config.sample_size, rng)
    rng_originals = _reservoir(bound(), config.sample_size, rng) if config.include_originals else []
    return rng_notes, rng_originals


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _restored_digest(root: Path, pick: _Pick) -> str | None:
    """The admission hash that the snapshot's own sidecar records, else the live binding's."""
    from .vault import parse_frontmatter

    if pick.sidecar is not None:
        try:
            text = (root / pick.sidecar).read_text(encoding="utf-8")
            binding = originals.binary_binding(parse_frontmatter(text, custody=True)[0])
            if binding is not None:
                return binding.sha256
        except (OSError, ValueError):
            pass
    return pick.digest


def _restore(restic: _Restic, source: str, target: Path, paths: Iterable[str], *, kind: str) -> None:
    """Restore into an empty folder. A timed-out restore is never run again into that folder: `--overwrite never`
    would skip a file that the first run left truncated and still report success. A lock held by another restic
    process fails before restic writes anything, so only that is retried."""
    includes = [part for path in paths for part in ("--include", "/" + _literal(path))]
    arguments = ["restore", "--overwrite", "never", "--verify", "--target", str(target)]
    restic.run([*arguments, source, *includes], kind=kind, retries=_IDEMPOTENT_RETRIES, retry_timeouts=False)


def _snapshots(restic: _Restic, *, own: bool, wanted: str = "latest") -> list[dict[str, Any]]:
    """The snapshots to choose from: with `own`, this vault's on this machine, otherwise all of this service's.

    A snapshot of a backup without originals names every file it holds, so a listing never asks for all
    snapshots: it asks for the newest of each machine and vault, and an older one by its ID.
    """
    filters = restic.own if own else ["--tag", TAG]
    query = ["--group-by", "host,tags", "--latest", "1", *filters] if wanted == "latest" else [*filters, wanted]
    try:
        done = restic.run(["snapshots", "--json", *query], kind="query", retries=_IDEMPOTENT_RETRIES)
    except BackupError as error:
        if wanted != "latest" and error.code == "restic_exit_1":  # restic's one exit code for an ID it cannot match
            raise BackupError("snapshot_not_found", wanted) from None
        raise
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        raise BackupError("restic_unusable", "snapshots") from None
    found = [snap for item in listed if isinstance(item, dict)
             for snap in (item["snapshots"] if isinstance(item.get("snapshots"), list) else [item])]
    return [snap for snap in found if isinstance(snap, dict) and isinstance(snap.get("id"), str)
            and isinstance(snap.get("paths"), list)]


def _instant(text: str) -> float:
    """A restic timestamp (RFC 3339, nanoseconds, an offset) as epoch seconds; 0 when it does not parse."""
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _roots(snapshot: dict[str, Any]) -> tuple[str | None, str | None]:
    """The vault root a snapshot was taken from, named by its tag, and its staged-state root, found by the
    staging directory's name among the snapshot's paths."""
    staged = [path for path in snapshot["paths"] if PurePosixPath(path).name == STAGE_DIRNAME]
    roots = [os.fsdecode(unquote_to_bytes(tag[len(ROOT_TAG):])) for tag in snapshot.get("tags") or [] if tag.startswith(ROOT_TAG)]
    return (roots[0] if len(roots) == 1 else None), (staged[0] if len(staged) == 1 else None)


def _pick_snapshot(snapshots: list[dict[str, Any]], vault: Path, host: str, wanted: str = "latest") -> dict[str, Any]:
    """The named snapshot, or the latest of this vault. This machine's snapshots come first, then another
    machine's of the same vault (a replacement machine restores those). A repository that holds more than one
    vault or machine at that level needs a snapshot ID."""
    if wanted != "latest":
        match = [snap for snap in snapshots if snap["id"].startswith(wanted)]
        if len(match) != 1:
            raise BackupError("snapshot_not_found", wanted)
        return match[0]
    here = [snap for snap in snapshots if _roots(snap)[0] == str(vault)]
    pool = [snap for snap in here if snap.get("hostname") == host] or here or snapshots
    if not pool:
        raise BackupError("snapshot_not_found", "latest")
    if len({(_roots(snap)[0], snap.get("hostname")) for snap in pool}) != 1:
        raise BackupError("ambiguous_snapshot", "latest")
    return max(pool, key=lambda snap: _instant(str(snap.get("time", ""))))


def _finding(kind: str, path: str, code: str) -> dict[str, str]:
    return {"kind": kind, "path": path, "code": code}


def _kind_of(path: str) -> str:
    """Whether a sampled path is a note or an original; `.md` is the note extension that the vault format fixes."""
    return "note" if originals.is_note_path(path) else "original"


def _sample(vault: Path, restic: _Restic, previous: dict[str, Any] | None, stop: threading.Event | None
            ) -> dict[str, Any]:
    """Restore a random sample of the latest snapshot and prove it: each note byte for byte through restic's
    own verification, each original against the SHA-256 recorded at its admission."""
    entry: dict[str, Any] = {"findings": []}
    retry = _RETRY_AFTER_FAILURE_SECONDS
    if not _POSIX:
        return _stamp({**entry, "state": FAILED, "code": "unsupported_platform"}, previous, success=False, wait=retry)
    store = private_state.store(vault, STORE_DIRNAME)
    # This flight holds the lock, so a sample folder that exists is a killed flight's leftover.
    for stale in store.glob(f"{SAMPLE_PREFIX}*"):
        shutil.rmtree(stale, ignore_errors=True)
    scratch = store / f"{SAMPLE_PREFIX}{os.urandom(6).hex()}"
    try:
        snapshot = _pick_snapshot(_snapshots(restic, own=True), vault, restic.host)
        root = _roots(snapshot)[0]
        if root is None:
            raise BackupError("snapshot_layout", snapshot["id"][:8])
        notes, chosen = _draw(vault, restic.config, _instant(str(snapshot.get("time", ""))), random.Random())
        scratch.mkdir(mode=0o700)
        picks = [*notes, *chosen]
        wanted = [path for pick in picks for path in (pick.path, pick.sidecar) if path]
        source = f"{snapshot['id']}:{root}"
        failed: set[str] = set()
        try:
            _restore(restic, source, scratch, wanted, kind="sample")
        except BackupError:
            for path in dict.fromkeys(wanted):  # restic names no file in its error, so find the damaged one alone
                if len(failed) >= _FALLBACK_FAILURES:  # many failures mean the repository, not one file
                    failed.add(path)
                    entry["findings"].append(_finding(_kind_of(path), path, "not_attempted"))
                    continue
                try:
                    _restore(restic, source, scratch / "alone", [path], kind="sample")
                    shutil.copytree(scratch / "alone", scratch, dirs_exist_ok=True)
                except BackupError as error:
                    failed.add(path)
                    entry["findings"].append(_finding(_kind_of(path), path, error.code))
                shutil.rmtree(scratch / "alone", ignore_errors=True)
        counts = {"note": [0, 0], "original": [0, 0]}  # verified, failed
        for pick in picks:
            if pick.path in failed or pick.sidecar in failed:
                counts[pick.kind][1] += 1
            elif not (scratch / pick.path).is_file():
                counts[pick.kind][1] += 1
                entry["findings"].append(_finding(pick.kind, pick.path, "missing_from_snapshot"))
            elif pick.kind == "original" and _hash(scratch / pick.path) != _restored_digest(scratch, pick):
                counts[pick.kind][1] += 1
                entry["findings"].append(_finding(pick.kind, pick.path, "hash_mismatch"))
            else:
                counts[pick.kind][0] += 1
    except BackupError as error:
        return _stamp({**entry, "state": FAILED, "code": error.code}, previous, success=False, wait=retry)
    except OSError as error:
        return _stamp({**entry, "state": FAILED, "code": f"sample_{type(error).__name__}"}, previous, success=False,
                      wait=retry)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    results = {kind: {"verified": ok, "failed": bad} for kind, (ok, bad) in counts.items()}
    if not restic.config.include_originals:
        results["original"] = {"state": "excluded_by_owner"}
    failures = sum(bad for _ok, bad in counts.values())
    # A sample of nothing proves nothing, so it never reads as a passed restore.
    state = FAILED if failures else (OK if picks else EMPTY)
    return _stamp(
        {**entry, "state": state, "snapshot_id": snapshot["id"], "notes": results["note"],
         "originals": results["original"], "code": "sample_findings" if failures else None},
        previous, success=bool(picks) and not failures,  # an empty sample proves nothing, so it never counts as a pass
        wait=retry if failures else restic.config.sample_interval_days * 86400)


def _due(entry: dict[str, Any] | None, now: float) -> bool:
    """Whether an activity is due. Each finished run names its next due time itself, so a failed one waits
    longer than a poll and the repository is not hammered."""
    return entry is None or now >= (entry.get("due_at") or 0)


def _require_owner(vault: Path) -> None:
    """Owner operations are owner-only. A local call that no request surface bound passes, as everywhere else."""
    from .governance import egress

    if egress.owner_only_aggregate(vault) is not None:
        raise BackupError("owner_only")


def _prepare(restic: _Restic, *, init: bool) -> None:
    """Prove the repository is reachable. It is created only when the owner asked for that."""
    restic.require_version()
    try:
        restic.run(["cat", "config"], kind="query", retries=_IDEMPOTENT_RETRIES)
    except BackupError as error:
        if error.code != "repository_missing" or not init:
            raise
        restic.run(["init"], kind="query")


def _has_snapshot(backup: dict[str, Any] | None) -> bool:
    """Whether a backup produced a snapshot, now or on an earlier run: a sample needs one to restore."""
    return bool(backup) and (backup["state"] in (COMPLETE, PARTIAL) or bool(backup.get("ok_at")))


def run_cycle(vault: Path, *, force: bool = False, init: bool = False, stop: threading.Event | None = None,
              ) -> dict[str, Any]:
    """Run each due activity once, in one flight. `force` makes the backup due now. Returns what ran."""
    vault = Path(vault).resolve()
    config = load_config(vault)
    if config is None:
        raise BackupError(NOT_CONFIGURED)
    now = time.time()
    record = _read_record(vault) or {}
    if not (force or any(_due(record.get(name), now) for name in _ACTIVITIES)):
        return {"ran": []}
    lock = FileLock(private_state.store(vault, STORE_DIRNAME) / "flight.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise BackupError("already_running") from None
    try:
        # One bulk pass for the walks and the sample: they yield to foreground requests and end when the service stops.
        with foreground_priority.bulk(stop=stop):
            record = _read_record(vault) or {}  # another flight may have finished since the first look
            restic = _Restic(vault, config, stop)
            try:
                _prepare(restic, init=init)
            except BackupError as error:
                _write_record(vault, {**record, "error": {"code": error.code, "at": time.time()}})
                raise
            record.pop("error", None)
            ran: list[str] = []

            def finished(name: str, entry: dict[str, Any]) -> bool:
                """Record a step's outcome. A step that the service's stop cut short is not an outcome: it is
                neither recorded nor counted, so the next start runs it again."""
                if entry.get("code") == "stopped":
                    return False
                record[name] = entry
                ran.append(name)
                return True

            steps = []
            if force or _due(record.get("backup"), now):
                steps.append(("backup", lambda: _backup(vault, restic, record.get("backup"))))
            if _due(record.get("check"), now):
                steps.append(("check", lambda: _check(restic, record.get("check"))))
            if _due(record.get("sample"), now):
                steps.append(("sample", lambda: _sample(vault, restic, record.get("sample"), stop)))
            for name, step in steps:
                if name == "sample" and not _has_snapshot(record.get("backup")):
                    continue  # a sample restores from a snapshot, and none exists yet
                if not finished(name, step()):
                    break
                if name == "backup" and record["backup"]["state"] == COMPLETE:
                    finished("forget", _forget(restic, record.get("forget")))  # a partial or failed backup never prunes
                _write_record(vault, record)
            return {"ran": ran, **{name: record[name] for name in ran}}
    finally:
        lock.release()


# --- owner operations -------------------------------------------------------------------------


def run_now(vault: Path, *, init_repository: bool = False) -> dict[str, Any]:
    """Owner operation: back up now. The check and the sampled restore stay on their own intervals."""
    _require_owner(Path(vault))
    return run_cycle(vault, force=True, init=init_repository)


def _checked_target(vault: Path, target: Path) -> Path:
    """A restore target that is new or empty and neither inside, around nor beside the vault's state."""
    target = Path(target).expanduser().resolve(strict=False)
    protected = (vault, state_paths.vault_state_dir(vault).resolve(strict=False))
    if any(target == place or target.is_relative_to(place) or place.is_relative_to(target) for place in protected):
        raise BackupError("target_overlaps_vault")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError("target_not_empty")
    return target


def _relative(path: str) -> str:
    """A vault-relative path that names a place under the vault: no root, no `..`, no empty part."""
    parts = PurePosixPath(path).parts
    if not path or "\0" in path or PurePosixPath(path).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise BackupError("invalid_path", path)
    return PurePosixPath(*parts).as_posix()


def restore(vault: Path, target: Path, *, snapshot: str = "latest", paths: Iterable[str] = (),
            state: bool = False) -> dict[str, Any]:
    """Owner operation: restore paths (or the staged state) from a snapshot into a separate new directory.

    The operation never writes into the vault. A target that is inside the vault, holds it or overlaps its
    state directory is refused, and so is one that already holds files, so nothing is overwritten.
    """
    vault = Path(vault).resolve()
    _require_owner(vault)
    if not _POSIX:
        raise BackupError("unsupported_platform", "restore")
    config = load_config(vault)
    if config is None:
        raise BackupError(NOT_CONFIGURED)
    wanted = [_relative(path) for path in paths]
    if state and wanted:
        raise BackupError("invalid_path", "--state takes no paths")
    target = _checked_target(vault, target)
    restic = _Restic(vault, config)
    restic.require_version()
    chosen = _pick_snapshot(_snapshots(restic, own=False, wanted=snapshot), vault, restic.host, snapshot)
    root = _roots(chosen)[1 if state else 0]
    if root is None:
        raise BackupError("snapshot_layout", chosen["id"][:8])
    target.mkdir(parents=True, exist_ok=True)
    _restore(restic, f"{chosen['id']}:{root}", target, wanted, kind="restore")
    restored = sum(1 for place in target.rglob("*") if place.is_file())
    return {"snapshot_id": chosen["id"], "target": str(target), "files": restored}


# --- status -----------------------------------------------------------------------------------


def _iso(moment: float | None) -> str | None:
    return dt.datetime.fromtimestamp(moment, dt.UTC).isoformat(timespec="seconds") if moment else None


def _section(record: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """One activity as the owner reads it. A lost record is unknown; an activity not yet run is never."""
    if record is None:
        return {"state": UNKNOWN}
    entry = record.get(name)
    if not isinstance(entry, dict):
        return {"state": NEVER}
    shown = {key: value for key, value in entry.items() if key not in ("findings", "due_at", "next_part")}
    return {**shown, "at": _iso(entry.get("at")), "last_success": _iso(entry.get("ok_at")) or NEVER}


def _next_due(record: dict[str, Any] | None, name: str) -> str:
    if record is None:
        return UNKNOWN
    entry = record.get(name)
    return (_iso(entry.get("due_at")) if isinstance(entry, dict) else None) or DUE


def status(vault: Path | None) -> dict[str, Any]:
    """The `vault_backup` block of `exomem status`: configuration, restic, each activity and its findings.

    It reads the config file and the record and looks for the executable; it starts no restic process.
    """
    if not os.environ.get(CONFIG_ENV):
        return {"state": NOT_CONFIGURED, "message": "not configured"}
    if vault is None:
        return {"state": UNKNOWN, "message": "no vault"}
    vault = Path(vault).resolve()
    try:
        config = load_config(vault)
    except ConfigError as error:
        return {"state": INVALID_CONFIG, "message": "invalid config", "code": error.code, "field": error.detail}
    if config is None:
        return {"state": NOT_CONFIGURED, "message": "not configured"}
    if shutil.which("restic") is None:
        return {"state": RESTIC_NOT_FOUND, "message": "restic not found"}
    record = _read_record(vault)
    block: dict[str, Any] = {
        "state": CONFIGURED, "repository_id": config.fingerprint, "include_originals": config.include_originals,
        "keep": dict(config.keep),  # the effective rules after defaults: what retention keeps and prunes
        "last_backup": _section(record, "backup"), "last_retention": _section(record, "forget"),
        "last_check": _section(record, "check"), "last_sampled_restore": _section(record, "sample"),
        "next_due": {
            "backup": _next_due(record, "backup"),
            "check": _next_due(record, "check"),
            "sampled_restore": _next_due(record, "sample"),
        },
        # A lost record says nothing about errors or findings: unknown, not none.
        "last_error": UNKNOWN if record is None else None,
        "findings": UNKNOWN if record is None else [],
    }
    if record is not None:
        error = record.get("error")
        if isinstance(error, dict):
            block["last_error"] = {"code": error.get("code"), "at": _iso(error.get("at"))}
            if error.get("code") == "restic_too_old":
                block["message"] = "restic too old"
        block["findings"] = [
            {"activity": name, **finding}
            for name in ("backup", "check", "sample")
            for finding in (record.get(name) or {}).get("findings", [])
        ][:_FINDINGS_LIMIT]
    if not config.include_originals:
        # What the replica-custody change will count: originals outside the snapshot that no replica covers.
        excluded = ((record or {}).get("backup") or {}).get("excluded_originals")
        block["originals_excluded"] = {"count": excluded if isinstance(excluded, int) else UNKNOWN,
                                       "without_recoverable_replica": UNKNOWN,
                                       "warning": "original bytes are not in this backup"}
    return block


# --- the thread -------------------------------------------------------------------------------

_LOCK = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def start(vault_root: Path) -> threading.Thread | None:
    """Start the backup thread when a config file is named. Idempotent: a second call returns the live one."""
    global _thread
    if not os.environ.get(CONFIG_ENV):
        return None
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            if _stop.is_set():
                raise RuntimeError("vault backup is stopping")
            return _thread
        _stop.clear()
        thread = threading.Thread(target=_run, args=(Path(vault_root).resolve(),), name="exomem-vault-backup",
                                  daemon=True)
        _thread = thread
        thread.start()
        return thread


def stop(timeout: float = 2.0, *, worker: threading.Thread | None = None) -> None:
    """Stop the thread and wait briefly. A running restic process is terminated within a poll."""
    global _thread
    with _LOCK:
        thread = _thread
        if worker is not None and thread is not worker:
            return
        _stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    with _LOCK:
        if _thread is thread and (thread is None or not thread.is_alive()):
            _thread = None


def _run(vault: Path) -> None:
    while not _stop.is_set():
        try:
            run_cycle(vault, stop=_stop)
        except foreground_priority.BulkCancelled:
            return
        except BackupError as error:
            # Not configured, busy and a failed repository gate are states that status shows; the next cycle retries.
            log.info("vault backup cycle did not run: %s", error.code)
        except Exception:  # noqa: BLE001 - a failed cycle must never end the thread or deny the service
            log.warning("vault backup cycle failed; it runs again at the next poll", exc_info=True)
        _stop.wait(_POLL_SECONDS)
