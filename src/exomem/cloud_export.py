"""Owner export of a held Cloud cell: the canonical ZIP, uploaded to presigned parts.

cellctl runs `exomem cell-export` as a Job on the cell image while the C6
export hold keeps the runtime stopped. The Job holds no storage credential: it
uploads to the part URLs cellctl presigned for this export's own object, and
cellctl checks the parts against this command's report before it completes the
upload. Only content-free results leave the Job: sizes, digests and fixed error
tokens, written to its termination message.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import vault_portability
from .cloud_import_job import _check_cell
from .governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope

# C6 fixes these failure tokens; they are protocol values, not user vocabulary.
EXPORT_FAILED = "EXPORT_FAILED"
EXPORT_TOO_LARGE = "EXPORT_TOO_LARGE"
EXPORT_UPLOAD_FAILED = "EXPORT_UPLOAD_FAILED"
SOURCE_CHANGED_DURING_EXPORT = "SOURCE_CHANGED_DURING_EXPORT"
EXPORT_CASE_COLLISION = "CASE_COLLISION"
# A part PUT is idempotent, so a transient failure is retried this often.
UPLOAD_ATTEMPTS = 3
# A socket idle this long fails the part; a 64 MiB part has minutes on a slow link.
UPLOAD_TIMEOUT_SECONDS = 300


class ExportRefused(Exception):
    """A content-free export failure with its C6 code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def etag_digest(etags: list[str]) -> str:
    """C6's part report: SHA-256 over one "<part number> <ETag>" line per part,
    in part order, each ETag without its surrounding quotes. cellctl computes
    the same digest from the storage's own part listing before it completes."""

    lines = "".join(f"{number} {etag.strip(chr(34))}\n" for number, etag in enumerate(etags, 1))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def filesystem_use(root: Path) -> dict[str, int]:
    """The used and total bytes of the filesystem holding the vault, the same
    statvfs measurement every backup Job reports."""

    usage = os.statvfs(root)
    return {"used": (usage.f_blocks - usage.f_bfree) * usage.f_frsize, "total": usage.f_blocks * usage.f_frsize}


def _put(url: str, chunk) -> str:
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            headers = {}
            if not isinstance(chunk, bytes):
                chunk.seek(0)
                headers["Content-Length"] = str(os.fstat(chunk.fileno()).st_size)
            with urlopen(Request(url, data=chunk, headers=headers, method="PUT"), timeout=UPLOAD_TIMEOUT_SECONDS) as response:
                etag = response.headers.get("ETag")
                if not etag:
                    raise ExportRefused(EXPORT_UPLOAD_FAILED)
                return etag
        except HTTPError as error:
            # A refused signature or an expired URL does not heal on retry.
            if error.code < 500 or attempt == UPLOAD_ATTEMPTS:
                raise ExportRefused(EXPORT_UPLOAD_FAILED) from None
        except OSError:
            if attempt == UPLOAD_ATTEMPTS:
                raise ExportRefused(EXPORT_UPLOAD_FAILED) from None
    raise AssertionError("unreachable")


class MultipartSink(io.RawIOBase):
    """Stage one part on the encrypted volume, then reuse that file for the next part."""

    def __init__(self, staging: Path, part_urls: list[str], part_bytes: int):
        self.part_urls, self.part_bytes = part_urls, part_bytes
        self.part = tempfile.TemporaryFile(dir=staging)
        self.offset = 0
        self.etags: list[str] = []
        self.digest = hashlib.sha256()

    def tell(self):
        return self.offset

    def writable(self):
        return True

    def write(self, data):
        view = memoryview(data)
        self.digest.update(view)
        self.offset += len(view)
        remaining = view
        while remaining:
            count = min(len(remaining), self.part_bytes - self.part.tell())
            self.part.write(remaining[:count])
            remaining = remaining[count:]
            if self.part.tell() == self.part_bytes:
                self._upload()
        return len(view)

    def _upload(self):
        if len(self.etags) >= len(self.part_urls):
            raise ExportRefused(EXPORT_FAILED)
        self.part.flush()
        self.etags.append(_put(self.part_urls[len(self.etags)], self.part))
        self.part.seek(0)
        self.part.truncate()

    def finish(self):
        if self.part.tell():
            self._upload()
        return {"bytes": self.offset, "sha256": self.digest.hexdigest(), "parts": len(self.etags),
                "etag_digest": etag_digest(self.etags)}

    def close(self):
        self.part.close()
        super().close()


def run_export(vault: Path, staging: Path, *, export_id: str, cell_id: str,
               part_urls: list[str], part_bytes: int, max_bytes: int) -> dict:
    """Stream the canonical archive; the completed object must pass the same verifier before publication."""
    context = vault_portability.PortabilityContext(
        cell_id=cell_id, vault_id=cell_id, operation_id=export_id, created_at=datetime.now(UTC).isoformat(),
        operator_authorized=True, lifecycle_state="quiesced", routing_stopped=True, active_mutations=0,
        background_writers_stopped=True, reads_allowed=True,
    )
    if staging.resolve().is_relative_to(vault.resolve()):
        raise ExportRefused(EXPORT_FAILED)
    staging.mkdir(parents=True, exist_ok=True)
    with request_scope(RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="cloud-export",
                                        issuer_family=f"cloud-cell:{cell_id}")):
        with MultipartSink(staging, part_urls, part_bytes) as sink:
            vault_portability.stream_quiesced_vault(vault, sink, context=context,
                limits=vault_portability.PortabilityLimits(max_total_bytes=max_bytes))
            return sink.finish()


def verify_remote_export(url: str, *, cell_id: str, archive_bytes: int, archive_sha256: str,
                         max_bytes: int) -> dict:
    """Verify a completed private object with the runtime's canonical reader and bounded HTTP cache."""
    from fsspec.implementations.http import HTTPFileSystem

    # The maintained reader logs signed URLs at DEBUG. This dedicated Job never emits them.
    logging.getLogger("fsspec.http").disabled = True
    logging.getLogger("aiohttp.client").disabled = True
    # Each verification owns and closes its HTTP session.
    fs = HTTPFileSystem(encoded=True, skip_instance_cache=True, client_kwargs={"trust_env": True})
    try:
        with fs.open(url, "rb", size=archive_bytes, block_size=1024 * 1024,
                     cache_type="blockcache", cache_options={"maxblocks": 2}) as stream:
            verified = vault_portability.verify_export_stream(stream, archive_bytes, expected_cell_id=cell_id,
                expected_vault_id=cell_id, limits=vault_portability.PortabilityLimits(max_total_bytes=max_bytes))
        if verified.archive_sha256 != archive_sha256:
            raise ExportRefused(EXPORT_FAILED)
        return {"bytes": verified.archive_size, "sha256": verified.archive_sha256, "verified": True}
    except Exception:  # noqa: BLE001 -- dependency failures must never expose signed URLs
        raise ExportRefused(EXPORT_FAILED) from None
    finally:
        from fsspec.asyn import sync
        if fs._session is not None:
            sync(fs.loop, fs._session.close)


def _portability_code(error: vault_portability.PortabilityError) -> str:
    # The portability module's own fixed codes, mapped onto C6's.
    if error.code == "SOURCE_CHANGED_DURING_EXPORT":
        return SOURCE_CHANGED_DURING_EXPORT
    if error.code == "CASE_COLLISION":
        return EXPORT_CASE_COLLISION
    if error.code == "RESOURCE_LIMIT_EXCEEDED" and error.limit == "max_file_bytes":
        return EXPORT_TOO_LARGE
    return EXPORT_FAILED


def _failure_diagnostic(error: Exception) -> dict:
    """Keep code identifiers for operators, never exception text or file paths."""
    detail = {"type": type(error).__name__}
    frame = error.__traceback__
    while frame is not None:
        # The portability raise helper is never the cause; its caller is the check that refused.
        if frame.tb_frame.f_code is not vault_portability._fail.__code__:
            detail["function"] = frame.tb_frame.f_code.co_name
        frame = frame.tb_next
    if isinstance(error, vault_portability.PortabilityError):
        detail["code"] = error.code
        if error.limit is not None:
            detail["limit"] = error.limit
    cause = error
    if not isinstance(error, OSError):
        # A refusal raised from or while handling an OSError keeps that error's class and number,
        # unless the raise suppressed its context (`from None`).
        cause = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__)
        if isinstance(cause, OSError):
            detail["cause"] = type(cause).__name__
    # OSError accepts any first argument; only an integer is an error number, never a path.
    if isinstance(cause, OSError) and isinstance(cause.errno, int):
        detail["errno"] = cause.errno
    return detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exomem cell-export")
    parser.add_argument("--export-id", required=True)
    parser.add_argument("--part-url", dest="part_urls", action="append", default=[])
    parser.add_argument("--part-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--archive-bytes", type=int)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--archive-version-id")
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--vault", type=Path, default=Path("/data/vault"))
    parser.add_argument("--staging", type=Path, default=Path("/data/scratch/export"))
    parser.add_argument("--termination-log", type=Path, default=Path("/dev/termination-log"))
    args = parser.parse_args(argv)
    usage: dict[str, int] = {}
    try:
        # Measured first, so a failure report still carries the volume's use. A
        # platform without statvfs fails here and is reported like any failure.
        cell_id = os.environ["EXOMEM_CLOUD_CELL_ID"]
        if args.verify:
            verify_url = os.environ.get("EXOMEM_EXPORT_VERIFY_URL", "")
            if not verify_url.startswith("https://") or not args.archive_bytes or not args.archive_sha256:
                raise ExportRefused(EXPORT_FAILED)
            result = verify_remote_export(verify_url, cell_id=cell_id, archive_bytes=args.archive_bytes,
                                          archive_sha256=args.archive_sha256, max_bytes=args.max_bytes)
            result["version"] = args.archive_version_id
            args.termination_log.write_text(json.dumps(result), encoding="ascii")
            return 0
        usage = filesystem_use(args.vault.parent)
        _check_cell(args.vault.parent, cell_id)
        # A plain-HTTP part URL would carry the owner's plaintext archive unencrypted;
        # refusing it costs an export only when the storage endpoint is misconfigured.
        if (str(uuid.UUID(args.export_id)) != args.export_id or args.part_bytes <= 0 or args.max_bytes <= 0
                or not args.part_urls or not all(url.startswith("https://") for url in args.part_urls)):
            raise ExportRefused(EXPORT_FAILED)
        result = {**run_export(args.vault, args.staging, export_id=args.export_id, cell_id=cell_id,
                               part_urls=args.part_urls, part_bytes=args.part_bytes,
                               max_bytes=args.max_bytes), **usage}
    except ExportRefused as error:
        result = {"error": error.code, "diagnostic": _failure_diagnostic(error), **usage}
    except vault_portability.PortabilityError as error:
        result = {"error": _portability_code(error), "diagnostic": _failure_diagnostic(error), **usage}
    except Exception as error:  # noqa: BLE001 -- only code identifiers leave the Job
        result = {"error": EXPORT_FAILED, "diagnostic": _failure_diagnostic(error), **usage}
    args.termination_log.write_text(json.dumps(result), encoding="ascii")
    return 1 if "error" in result else 0
