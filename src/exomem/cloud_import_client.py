"""Resumable ciphertext-only Cloud import using the ordinary OAuth client."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from fastmcp.client.auth import OAuth
from platformdirs import user_state_dir

from . import cloud_import_envelope, import_selection, import_stage
from .cell_init import _ensure_private_directory
from .cli_progress import Progress, human_bytes
from .cloud_import import ARCHIVE_TOO_LARGE, DEFAULT_MAX_MEMBERS, DEFAULT_RESERVE_BYTES
from .cloud_import_job import _private_json

# The production Cloud endpoints, defined here once for `exomem cloud import` and `exomem cloud export`.
# The Cloud owner confirmed both. The MCP URL is the resource of the Cloud plugin definition, which a
# test compares. The API base is Substrate's public origin, where the Cloud routes under /api/exomem are served.
PRODUCTION_MCP_URL = "https://exomem.substratesystems.io/mcp"
PRODUCTION_API_BASE_URL = "https://substratesystems.io"

# Where a run is when it fails; the failure report names it.
PREFLIGHT = "preflight"
REGISTER = "register"
ARCHIVE = "archive"
UPLOAD = "upload"
SERVER_IMPORT = "server_import"


@dataclass(frozen=True)
class CloudImportClientConfig:
    mcp_url: str
    api_base_url: str
    oauth_client_id: str = "exomem-cloud-cli"

    def __post_init__(self):
        for value in (self.mcp_url, self.api_base_url):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Cloud URLs must use HTTPS")


class CloudImportError(Exception):
    """A failure reported by phase and code. Neither can carry a file name or a signed URL."""

    def __init__(self, phase: str, code: str, message: str, import_id: str | None = None, *, resume: bool = True):
        super().__init__(f"{phase}: {code}")
        self.phase = phase
        self.code = code
        self.message = message
        self.import_id = import_id
        # False when a rerun cannot continue this import; the message then says what a rerun does instead.
        self.resume = resume


# Home's Cancel reaches every cell; the `import_cancel` action may not reach an older one, so both are named.
_HOME_IMPORT_STATUS = "Home (Import status\u2026)"
_STOP_ON_CLOUD = (f"press Cancel in {_HOME_IMPORT_STATUS}, or ask your assistant to call transfer_memory "
                  "with action import_cancel and that import id")


class _SourceChanged(Exception):
    """A file or folder changed while it was archived, so the archive would mix two versions."""


def safe_code(value: object) -> str | None:
    """A server-sent code, only if it has the shape of one, so a hostile body cannot put free text in the output."""
    # A format check on a protocol field.
    if isinstance(value, str) and 0 < len(value) <= 64 and value.isascii() and value.replace("_", "").isalnum():
        return value
    return None


def server_code(response: httpx2.Response) -> str:
    """The error code of a Cloud API refusal, or the status when the body has none."""
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    return safe_code(code) or f"HTTP_{response.status_code}"


def cloud_oauth(config: CloudImportClientConfig) -> OAuth:
    """The one OAuth setup for every `exomem cloud` command; share it so that a run signs in once."""
    return OAuth(mcp_url=config.mcp_url, client_id=config.oauth_client_id,
                 client_name="Exomem CLI", scopes=["exomem.read", "exomem.write", "offline_access"],
                 callback_host="127.0.0.1", callback_port=None)


def authorized_api_client(config: CloudImportClientConfig, oauth: OAuth | None = None) -> httpx2.AsyncClient:
    """The Cloud API client that signs in through a local browser."""
    return httpx2.AsyncClient(auth=oauth or cloud_oauth(config), timeout=60)


def add_endpoint_arguments(command: argparse.ArgumentParser) -> None:
    """The Cloud endpoint options that every `exomem cloud` command shares."""
    command.add_argument("--mcp-url", default=os.environ.get("EXOMEM_CLOUD_MCP_URL") or PRODUCTION_MCP_URL,
                         help="Cloud MCP URL (default: Exomem Cloud, or EXOMEM_CLOUD_MCP_URL)")
    command.add_argument("--api-base-url", default=os.environ.get("EXOMEM_CLOUD_API_BASE_URL") or PRODUCTION_API_BASE_URL,
                         help="Cloud API origin (default: Exomem Cloud, or EXOMEM_CLOUD_API_BASE_URL)")
    command.add_argument("--oauth-client-id", default="exomem-cloud-cli")


@dataclass
class _Where:
    """Where a run is, so a failure can name its phase and import id."""

    phase: str = PREFLIGHT
    import_id: str | None = None


@dataclass(frozen=True)
class Window:
    """One window of a series: the members to send and the series file that every window carries."""

    selection: import_selection.Selection
    series_file: bytes


@dataclass(frozen=True)
class _Plan:
    kind: str
    files: int
    bytes: int
    expected_ciphertext: int
    selection: import_selection.Selection | None


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _member_bytes(name: str, size: int) -> int:
    """About how many tar bytes a member takes: every member gets a pax header for its fractional mtime.

    The header's data holds the mtime and, for a long or non-ASCII name, the name. The estimate always
    counts the name, which can only overstate, and a disk check should not understate.
    """
    block = tarfile.BLOCKSIZE
    pax_data = len(name.encode("utf-8", "surrogateescape")) + 64
    return 2 * block + -(-pax_data // block) * block + -(-size // block) * block


def _expected_ciphertext(members: Iterable[tuple[str, int]], folders: Iterable[str]) -> int:
    """About how large the encrypted tar is: tar members, the tar's end padding and age's tag per 64 KiB chunk."""
    tar = sum(_member_bytes(name, size) for name, size in members)
    tar += sum(_member_bytes(name, 0) for name in folders) + tarfile.RECORDSIZE
    return tar + tar // 4096 + 1024


def _inventory(source: Path, envelope, *, include_hidden: bool, use_gitignore: bool, progress: Progress,
               max_members: int | None = DEFAULT_MAX_MEMBERS) -> _Plan:
    """What the import will carry, reported before anything is built."""
    if envelope is not None:
        encoded = envelope.encoded()
        members = [(cloud_import_envelope.METADATA_NAME, len(encoded)),
                   (cloud_import_envelope.ARCHIVE_NAME, source.stat().st_size)]
        return _Plan("archive_members", 2, sum(size for _, size in members), _expected_ciphertext(members, ()), None)
    selection = import_selection.select(source, include_hidden=include_hidden, use_gitignore=use_gitignore)
    for line in selection.report_lines():
        progress.say(line)
    if not selection.kept:
        raise CloudImportError(PREFLIGHT, "NOTHING_TO_IMPORT",
                               "no file is left to import; pass --include-hidden or --no-gitignore to take more")
    return _adopt_plan(selection, None, max_members)


def _adopt_plan(selection: import_selection.Selection, series_file: bytes | None, max_members: int | None) -> _Plan:
    """The plan of an `adopt` archive: the selected files, and the series file when the window carries one."""
    members = [(item.rel, item.size) for item in selection.kept]
    if series_file is not None:
        members.append((import_stage.SERIES_FILE, len(series_file)))
    # The archive also holds one entry per folder that leads to a selected file.
    folders: set[str] = set()
    for rel, _ in members:
        parts = rel.split("/")[:-1]
        folders.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    entries = len(members) + len(folders)
    if max_members is not None and entries > max_members:
        raise CloudImportError(
            PREFLIGHT, ARCHIVE_TOO_LARGE,
            f"the archive would hold {entries:,} entries (files and folders) and Cloud accepts at most "
            f"{max_members:,}; import subfolders one at a time")
    return _Plan("adopt", len(members), sum(size for _, size in members), _expected_ciphertext(members, folders), selection)


def _check_resources(plan: _Plan, checkpoint: Path, progress: Progress, free_bytes: Callable[[Path], int],
                     reserve_bytes: int | None = DEFAULT_RESERVE_BYTES) -> None:
    """Refuse before the archive is built when the encrypted copy cannot be written."""
    if shutil.which("age") is None:
        raise CloudImportError(PREFLIGHT, "AGE_MISSING", "the age tool is not on PATH; install age and run the command again")
    if reserve_bytes is None:
        return
    free = free_bytes(checkpoint)
    progress.say(f"The encrypted copy needs about {human_bytes(plan.expected_ciphertext)} in {checkpoint}, "
                 f"and the disk keeps a reserve of {human_bytes(reserve_bytes)}; {human_bytes(free)} is free there.")
    if plan.expected_ciphertext + reserve_bytes > free:
        raise CloudImportError(
            PREFLIGHT, "INSUFFICIENT_DISK",
            f"the encrypted copy needs about {plan.expected_ciphertext} bytes plus a reserve of {reserve_bytes} "
            f"bytes and {free} bytes are free; free space or pass --checkpoint on a larger disk")


class _CountingWriter:
    """Passes bytes to the encryption process and reports how many it has written."""

    def __init__(self, stream, on_written: Callable[[int], None]):
        self._stream = stream
        self._on_written = on_written
        self._written = 0

    def write(self, data) -> int:
        count = self._stream.write(data)
        self._written += len(data)
        self._on_written(self._written)
        return count

    def flush(self) -> None:
        self._stream.flush()


def _add_series_file(archive: tarfile.TarFile, series_file: bytes) -> None:
    """Add the reserved series file at the archive root; the Job stages it like any member."""
    info = tarfile.TarInfo(import_stage.SERIES_FILE)
    info.size, info.mode, info.mtime = len(series_file), 0o600, int(time.time())
    archive.addfile(info, io.BytesIO(series_file))


def _add_selected(archive: tarfile.TarFile, root: Path, selection: import_selection.Selection,
                  series_file: bytes | None = None) -> None:
    """Add the selected files, each opened without following a link and checked against the walk and while it is read."""
    if series_file is not None:
        _add_series_file(archive, series_file)
    made: set[str] = set()
    for item in selection.kept:
        parts = item.rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            name = "/".join(parts[:depth])
            if name not in made:
                made.add(name)
                info = archive.gettarinfo(str(root / name), arcname=name)
                if not info.isdir():
                    raise _SourceChanged
                archive.addfile(info)
        try:
            descriptor = import_selection.open_for_read(root / item.rel)
        except OSError:
            raise _SourceChanged from None
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != item.identity
                    or (before.st_size, before.st_mtime_ns) != (item.size, item.mtime_ns)):
                raise _SourceChanged
            try:
                archive.addfile(archive.gettarinfo(arcname=item.rel, fileobj=handle), handle)
            except OSError:
                # A file that shrinks while it is read ends early; a file that cannot be read is another failure.
                if _changed_since(descriptor, before):
                    raise _SourceChanged from None
                raise
            if _changed_since(descriptor, before):
                raise _SourceChanged


def _changed_since(descriptor: int, before: os.stat_result) -> bool:
    """Whether the open file's size or modification time differs from `before`."""
    now = os.fstat(descriptor)
    return (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns)


def _ciphertext(source: Path, target: Path, recipient: str, envelope, plan: _Plan, progress: Progress,
                series_file: bytes | None = None) -> None:
    pending = target.with_suffix(".pending")
    pending.unlink(missing_ok=True)
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    expected = human_bytes(plan.expected_ciphertext)
    try:
        with os.fdopen(descriptor, "wb") as output:
            process = subprocess.Popen(["age", "-r", recipient], stdin=subprocess.PIPE,
                                       stdout=output, stderr=subprocess.DEVNULL)
            try:
                try:
                    with process.stdin:
                        sink = _CountingWriter(process.stdin, lambda written: progress.tick(
                            f"Archiving and encrypting: {human_bytes(written)} of about {expected}"))
                        if envelope is not None:
                            cloud_import_envelope.write(sink, source, envelope)
                        else:
                            # dereference: a hard-linked file is added as a regular file, not as a link member.
                            with tarfile.open(fileobj=sink, mode="w|", dereference=True) as archive:
                                _add_selected(archive, source, plan.selection, series_file)
                except BrokenPipeError:
                    raise CloudImportError(ARCHIVE, "ENCRYPTION_FAILED", "the age process stopped early") from None
                # This CLI encryption process owns the archive lifetime, outside request serving.
                if process.wait() != 0:
                    raise CloudImportError(ARCHIVE, "ENCRYPTION_FAILED", "the age process failed")
                output.flush()
                os.fsync(output.fileno())
            finally:
                if process.poll() is None:
                    process.kill()
                    # Reap the killed CLI child before removing its private pending output.
                    process.wait()
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def _abandoned(record: Path, ciphertext: Path, import_id: str, code: str, why: str,
               *, phase: str = ARCHIVE) -> CloudImportError:
    """Remove a checkpoint that cannot resume, and say what happens next and how to stop the open import."""
    record.unlink(missing_ok=True)
    ciphertext.unlink(missing_ok=True)
    return CloudImportError(
        phase, code,
        f"{why}. The checkpoint is removed. Cancel Cloud import {import_id}: {_STOP_ON_CLOUD}, "
        "then start a new run, which starts a new import",
        import_id, resume=False)


def _coded(where: _Where, error: Exception) -> CloudImportError:
    """The content-free failure for an exception; a provider message can hold a signed URL."""
    if isinstance(error, httpx2.HTTPStatusError):
        code, message = server_code(error.response), "the Cloud API or storage refused the request"
        # C5 fixes these typed admission refusals; they never carry file names.
        # nosemgrep: ep-word-membership -- C5 fixes these admission refusal codes.
        if code in ("IMPORT_TOO_MANY_FILES", "IMPORT_STORAGE_LIMIT", "IMPORT_PARTS_LIMIT"):
            try:
                limit = error.response.json()["error"]["limit"]
                unit = safe_code(limit.get("unit"))
                maximum, requested = limit.get("value"), limit.get("requested")
                # C5 fixes the units; arbitrary server text must not reach the report.
                # nosemgrep: ep-word-membership -- C5 fixes these units.
                if unit not in ("files", "bytes") or type(maximum) is not int or type(requested) is not int:
                    raise ValueError
                message = f"Cloud accepts at most {maximum:,} {unit}; the import requests {requested:,} {unit}"
            except (KeyError, TypeError, ValueError, AttributeError):
                message = "Cloud refused the import's published limit"
            return CloudImportError(where.phase, code, message, where.import_id, resume=False)
        # IMPORT_CONFLICT is Cloud's documented refusal while another import is open. Every rerun is refused the
        # same way until that import ends, so the failure must not say to resume.
        if where.phase == REGISTER and code == "IMPORT_CONFLICT":
            return CloudImportError(
                REGISTER, code, f"another import is open; cancel it in {_HOME_IMPORT_STATUS} or wait for it to "
                "expire, then run the command again", where.import_id, resume=False)
    elif isinstance(error, httpx2.TransportError):
        code, message = "NETWORK_ERROR", "the connection to Cloud or storage failed"
    elif isinstance(error, TimeoutError):
        code, message = "WAIT_TIMED_OUT", "the import is still pending; run the command again to resume"
    elif isinstance(error, OSError):
        code, message = "LOCAL_IO_ERROR", "a local file operation failed"
    else:
        code, message = "UNEXPECTED_ERROR", "the command stopped on an unexpected error"
    return CloudImportError(where.phase, code, message, where.import_id)


async def import_source(source: Path, config: CloudImportClientConfig, checkpoint: Path, *,
                        scope: str | None = None, category: str | None = None,
                        include_hidden: bool = False, use_gitignore: bool = True,
                        api=None, uploads=None, poll_seconds: float = 2,
                        progress: Progress | None = None,
                        free_bytes: Callable[[Path], int] = _free_bytes,
                        window: Window | None = None) -> dict:
    """Reauthorize on restart and reuse the same private ciphertext and part receipts.

    A `window` sends only its members, with the series file; the source's other files stay behind.

    Every failure leaves as a `CloudImportError` that names its phase, a content-free code and the import id.
    """
    where = _Where()
    try:
        return await _import(source, config, checkpoint, where, scope=scope, category=category,
                             include_hidden=include_hidden, use_gitignore=use_gitignore, api=api,
                             uploads=uploads, poll_seconds=poll_seconds, progress=progress or Progress(),
                             free_bytes=free_bytes, window=window)
    except CloudImportError as error:
        error.import_id = error.import_id or where.import_id
        raise
    except Exception as error:  # noqa: BLE001 -- provider errors may contain presigned URLs
        raise _coded(where, error) from None


async def _import(source: Path, config: CloudImportClientConfig, checkpoint: Path, where: _Where, *,
                  scope, category, include_hidden, use_gitignore, api, uploads, poll_seconds,
                  progress: Progress, free_bytes, window: Window | None = None) -> dict:
    source = source.resolve()
    checkpoint = checkpoint.resolve()
    if source.is_dir() and checkpoint.is_relative_to(source):
        raise CloudImportError(PREFLIGHT, "CHECKPOINT_INSIDE_SOURCE", "the checkpoint folder must be outside the source folder")
    _ensure_private_directory(checkpoint)
    if os.name == "nt":
        from .state_paths import _prepare_windows_private_state_root

        _prepare_windows_private_state_root(checkpoint)
    record = checkpoint / "checkpoint.json"
    ciphertext = checkpoint / "archive.age"
    own_api, own_uploads = api is None, uploads is None
    if api is None:
        api = authorized_api_client(config)
    if uploads is None:
        # Presigned storage URLs never receive the API's OAuth bearer.
        uploads = httpx2.AsyncClient(timeout=120)
    base = config.api_base_url.rstrip("/") + "/api/exomem/cloud/imports"
    try:
        response = await api.request("GET", base)
        response.raise_for_status()
        listing = response.json()
        limits = listing.get("limits", {"max_members": DEFAULT_MAX_MEMBERS, "reserve_bytes": DEFAULT_RESERVE_BYTES})
        if limits is None:
            max_members = reserve_bytes = None
            progress.say("Import limits are not published; the cell checks limits during the import.")
        else:
            max_members, reserve_bytes = limits["max_members"], limits["reserve_bytes"]
        options = {"include_hidden": include_hidden, "use_gitignore": use_gitignore}
        plan: _Plan | None = None
        if record.exists():
            state = json.loads(record.read_text(encoding="utf-8"))
            if state["source"] != str(source) or state["config"] != asdict(config):
                raise CloudImportError(PREFLIGHT, "CHECKPOINT_MISMATCH", "the checkpoint belongs to another source or Cloud endpoint")
            if state.get("selection", options) != options:
                raise CloudImportError(PREFLIGHT, "CHECKPOINT_OPTIONS_CHANGED",
                                       "the checkpoint was made with other --include-hidden or --no-gitignore options")
            where.import_id = state["import_id"]
            progress.say(f"Resuming import {state['import_id']} from {checkpoint}.")
        else:
            if source.is_file():
                if not scope or not category:
                    raise CloudImportError(PREFLIGHT, "SCOPE_AND_CATEGORY_REQUIRED", "ZIP imports require --scope and --category")
                envelope = cloud_import_envelope.Envelope(scope=scope, category=category, filename=source.name)
            elif source.is_dir():
                envelope = None
            else:
                raise CloudImportError(PREFLIGHT, "SOURCE_NOT_FOUND", "the source must be a folder or a ZIP file")
            plan = (_adopt_plan(window.selection, window.series_file, max_members) if window else
                    _inventory(source, envelope, include_hidden=include_hidden, use_gitignore=use_gitignore,
                               progress=progress, max_members=max_members))
            _check_resources(plan, checkpoint, progress, free_bytes, reserve_bytes)
            state = {"source": str(source), "config": asdict(config), "import_id": str(uuid.uuid4()),
                     "kind": plan.kind, "declared_files": plan.files, "declared_bytes": plan.bytes, "parts": {},
                     "envelope": envelope.model_dump() if envelope else None, "selection": options}
            where.import_id = state["import_id"]
            _private_json(record, state)
        endpoint = base + "/" + state["import_id"]

    except BaseException:
        if own_api:
            await api.aclose()
        if own_uploads:
            await uploads.aclose()
        raise

    async def call(method, url, **kwargs):
        response = await api.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()["import"]

    async def wait_for(predicate):
        deadline = time.monotonic() + 7200
        shown = None
        while True:
            item = await call("GET", endpoint)
            observed = safe_code(item["observed_state"]) or "unknown"
            if observed != shown:
                progress.say(f"Cloud import state: {observed}")
                shown = observed
            # nosemgrep: ep-word-membership -- The Cloud import protocol fixes these terminal failure states.
            if item["observed_state"] in ("failed", "expired", "discarded"):
                ciphertext.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                raise CloudImportError(where.phase, safe_code(item.get("error_code")) or "IMPORT_ENDED",
                                       "the Cloud import ended without finishing, and the checkpoint is removed; "
                                       "a new run starts a new import", state["import_id"], resume=False)
            if predicate(item):
                return item
            if time.monotonic() >= deadline:
                raise TimeoutError("import remains pending; rerun with the same checkpoint")
            await asyncio.sleep(poll_seconds)

    def finished(result: dict) -> dict:
        if "ciphertext_bytes" in state:
            progress.say(f"Removed the encrypted copy ({human_bytes(state['ciphertext_bytes'])}) from {checkpoint}.")
        ciphertext.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        return result

    try:
        # A client-supplied UUID makes a dropped initial response safe to replay.
        where.phase = REGISTER
        await call("POST", base, json={name: state[name] for name in (
            "import_id", "kind", "declared_bytes", "declared_files")})
        item = await wait_for(lambda row: row.get("recipient") or row["observed_state"] == "done")
        if item["observed_state"] == "done":
            return finished(item)
        where.phase = ARCHIVE
        if not ciphertext.exists():
            if "ciphertext_sha256" in state:
                raise _abandoned(record, ciphertext, state["import_id"], "CIPHERTEXT_MISSING",
                                 "the checkpoint's encrypted copy is missing and cannot be made again")
            envelope = cloud_import_envelope.Envelope.model_validate(state["envelope"]) if state["envelope"] else None
            if plan is None:
                # A restart after the import was declared but before the archive was built: walk the source again.
                plan = (_adopt_plan(window.selection, window.series_file, max_members) if window else
                        _inventory(source, envelope, include_hidden=include_hidden, use_gitignore=use_gitignore,
                                   progress=progress, max_members=max_members))
                if (plan.files, plan.bytes) != (state["declared_files"], state["declared_bytes"]):
                    raise _abandoned(record, ciphertext, state["import_id"], "SOURCE_CHANGED",
                                     "the source changed since this import was declared")
                _check_resources(plan, checkpoint, progress, free_bytes, reserve_bytes)
            progress.say("Archiving and encrypting.")
            try:
                await asyncio.to_thread(_ciphertext, source, ciphertext, item["recipient"], envelope, plan, progress,
                                       window.series_file if window else None)
            except _SourceChanged:
                raise _abandoned(record, ciphertext, state["import_id"], "SOURCE_CHANGED",
                                 "the source changed while it was archived") from None
        with ciphertext.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = ciphertext.stat().st_size
        if state.get("ciphertext_sha256", digest) != digest or state.get("ciphertext_bytes", size) != size:
            raise _abandoned(record, ciphertext, state["import_id"], "CIPHERTEXT_CHANGED",
                             "the checkpoint's encrypted copy changed")
        state.update(ciphertext_sha256=digest, ciphertext_bytes=size)
        _private_json(record, state)
        progress.say(f"Encrypted copy: {human_bytes(size)} in {checkpoint}. It duplicates your data on this "
                     "disk until the import finishes, and the command removes it then.")
        cipher = {"ciphertext_sha256": digest, "ciphertext_bytes": size}
        where.phase = UPLOAD
        if item["requested_state"] == "requested":
            try:
                await call("POST", endpoint + "/prepare-upload", json=cipher)
            except httpx2.HTTPStatusError as error:
                if server_code(error.response) != "IMPORT_PARTS_LIMIT":
                    raise
                refusal = _coded(where, error)
                raise _abandoned(record, ciphertext, state["import_id"], refusal.code,
                                 refusal.message, phase=UPLOAD) from None
            item = await wait_for(lambda row: bool(row.get("part_urls")))
            parts = item["part_urls"]
            if sum(part["part_size_bytes"] for part in parts) != size:
                raise _abandoned(record, ciphertext, state["import_id"], "PARTS_INVALID",
                                 "the issued upload parts do not cover the encrypted copy", phase=UPLOAD)
            sent = 0
            try:
                with ciphertext.open("rb") as stream:
                    for ordinal, part in enumerate(parts, 1):
                        if part["part_number"] != ordinal:
                            raise CloudImportError(UPLOAD, "PARTS_INVALID", "the issued upload parts are out of order",
                                                   state["import_id"], resume=False)
                        payload = stream.read(part["part_size_bytes"])
                        if len(payload) != part["part_size_bytes"]:
                            raise CloudImportError(UPLOAD, "CIPHERTEXT_SHORT", "the encrypted copy ended early",
                                                   state["import_id"], resume=False)
                        if str(ordinal) not in state["parts"]:
                            response = await uploads.put(part["url"], content=payload,
                                                         headers={"Content-Length": str(len(payload))})
                            response.raise_for_status()
                            state["parts"][str(ordinal)] = response.headers["etag"]
                            _private_json(record, state)
                        sent += len(payload)
                        progress.tick(f"Uploading: part {ordinal} of {len(parts)}, "
                                      f"{human_bytes(sent)} of {human_bytes(size)}")
            except CloudImportError as error:
                # Windows cannot remove the encrypted copy until its read handle closes.
                raise _abandoned(record, ciphertext, state["import_id"], error.code,
                                 error.message, phase=UPLOAD) from None
            progress.say(f"Uploaded {len(parts)} parts, {human_bytes(size)}.")
            await call("POST", endpoint + "/complete", json={**cipher, "part_etags": [
                {"part_number": part["part_number"], "etag": state["parts"][str(part["part_number"]) ]}
                for part in parts]})
        where.phase = SERVER_IMPORT
        result = await wait_for(lambda row: row["observed_state"] == "done")
        return finished(result)
    finally:
        if own_api:
            await api.aclose()
        if own_uploads:
            await uploads.aclose()


def _report_failure(error: CloudImportError, checkpoint: Path, progress: Progress) -> int | None:
    """Say where the run stopped and what it still holds; return the encrypted copy's bytes when it holds one."""
    progress.say(f"The import stopped in the {error.phase} phase: {error.code}. {error.message}.")
    if error.import_id:
        progress.say(f"Import id: {error.import_id}." + (" Run the same command again to resume." if error.resume else ""))
    held = checkpoint / "archive.age"
    if not held.exists():
        return None
    size = held.stat().st_size
    progress.say(f"The encrypted copy ({human_bytes(size)}) stays in {checkpoint}. It duplicates your data on this "
                 f"disk until the import finishes. To discard it, delete that folder. To stop the import on Cloud, {_STOP_ON_CLOUD}.")
    return size


async def _run(args, config: CloudImportClientConfig, checkpoint: Path, progress: Progress) -> dict:
    """A folder goes by windows (one import on a cell without a window budget); a ZIP file goes as one import."""
    if not args.source.is_dir():
        if args.resend_residual:
            raise CloudImportError(PREFLIGHT, "NO_SERIES", "only a folder import has a residual to resend", resume=False)
        return await import_source(args.source, config, checkpoint, scope=args.scope, category=args.category,
                                   include_hidden=args.include_hidden, use_gitignore=not args.no_gitignore, progress=progress)
    from .cloud_import_windows import McpCell, import_folder

    oauth = cloud_oauth(config)
    api, uploads = authorized_api_client(config, oauth), httpx2.AsyncClient(timeout=120)
    try:
        async with McpCell(config, oauth) as cell:
            return await import_folder(
                args.source, config, checkpoint, cell=cell, api=api, uploads=uploads, include_hidden=args.include_hidden,
                use_gitignore=not args.no_gitignore, progress=progress, resend_residual=args.resend_residual)
    finally:
        await api.aclose()
        await uploads.aclose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem cloud", epilog="To download an owner export, run: exomem cloud export --help")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("import")
    command.add_argument("source", type=Path)
    add_endpoint_arguments(command)
    import_selection.add_selection_arguments(command)
    command.add_argument("--scope")
    command.add_argument("--category")
    command.add_argument("--checkpoint", type=Path)
    command.add_argument("--resend-residual", action="store_true",
                         help="send the members that discarded windows did not file, as a new series")
    args = parser.parse_args(argv)
    progress = Progress()
    checkpoint = args.checkpoint
    try:
        try:
            config = CloudImportClientConfig(args.mcp_url, args.api_base_url, args.oauth_client_id)
        except ValueError:
            raise CloudImportError(PREFLIGHT, "ENDPOINT_NOT_HTTPS", "the Cloud URLs must use HTTPS") from None
        key = hashlib.sha256((str(args.source.resolve()) + config.api_base_url).encode()).hexdigest()
        checkpoint = args.checkpoint or Path(user_state_dir("exomem")) / "cloud-imports" / key
        result = asyncio.run(_run(args, config, checkpoint, progress))
    except CloudImportError as error:
        held = _report_failure(error, checkpoint, progress) if checkpoint is not None else None
        failure = {"ok": False, "error": type(error).__name__, "phase": error.phase, "code": error.code,
                   "message": error.message, "import_id": error.import_id}
        if held is not None:
            failure["ciphertext_bytes"] = held
        print(json.dumps(failure))
        return 1
    except Exception as error:  # noqa: BLE001 -- provider errors may contain presigned URLs
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 1
    if "windows" in result:
        print(json.dumps({"ok": True, **result}))
        return 0
    print(json.dumps({"ok": True, "import_id": result["import_id"], "state": result["observed_state"],
                      **{key: result[key] for key in ("staged_files", "staged_bytes") if result.get(key) is not None}}))
    return 0
