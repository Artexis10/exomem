#!/usr/bin/env python3
"""Create and validate the durable authority for one desktop stop transition."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_SERVICE_ID = re.compile(r"^[A-Za-z0-9_.@-]+$")
_PHASES = {
    "captured",
    "stopped",
    "bound",
    "installed",
    "migrated",
    "doctor-passed",
    "starting",
    "started",
    "accepted",
    "failed",
}


class ReceiptError(RuntimeError):
    """A receipt is absent, invalid, or bound to another transition."""


def _resolved(raw: str, field: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ReceiptError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def _contains(parent: Path, child: Path) -> bool:
    parent_text = os.path.normcase(os.fspath(parent))
    child_text = os.path.normcase(os.fspath(child))
    try:
        return os.path.commonpath((parent_text, child_text)) == parent_text
    except ValueError:
        return False


def _identity(args: argparse.Namespace) -> dict[str, Any]:
    if not _SERVICE_ID.fullmatch(args.service_id):
        raise ReceiptError("service identity is invalid")
    receipt_path = _resolved(args.path, "receipt path")
    binding_path = _resolved(args.binding_path, "binding path")
    state_root = _resolved(args.state_root, "state root")
    vault_root = _resolved(args.vault, "vault root")
    if _contains(vault_root, receipt_path):
        raise ReceiptError("transition receipt must resolve outside the vault")
    if _contains(vault_root, state_root):
        raise ReceiptError("state root must resolve outside the vault")
    if args.target_port < 1 or args.target_port > 65535:
        raise ReceiptError("target port is invalid")
    return {
        "path": receipt_path,
        "service_id": args.service_id,
        "binding_path": os.fspath(binding_path),
        "state_root": os.fspath(state_root),
        "vault_root": os.fspath(vault_root),
        "target_port": args.target_port,
    }


def _validate_pid(value: int, field: str) -> int:
    if value < 1:
        raise ReceiptError(f"{field} must be a running process id")
    return value


def _flush_directory(path: Path) -> None:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    # Python cannot open a Windows directory for fsync. Retain it explicitly
    # and fence metadata through FlushFileBuffers instead.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (ctypes.c_void_p,)
    flush_file_buffers.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    handle = create_file(
        os.fspath(path),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def _encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_temporary(path: Path, payload: dict[str, Any]) -> Path:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encode(payload))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return temporary


def _create(path: Path, payload: dict[str, Any]) -> None:
    if os.name == "nt":
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    temporary = _write_temporary(path, payload)
    try:
        # A hard-link publish preserves O_EXCL semantics without exposing a
        # partially written authority file. An existing receipt is authority
        # for another/resumed run and must never be overwritten.
        os.link(temporary, path)
        _flush_directory(path.parent)
    except FileExistsError as error:
        raise ReceiptError("transition receipt already exists; resume it exactly") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _flush_directory(path.parent)
    if os.name == "nt":
        # Apply the platform's private-mode request only after publication: a
        # restricted test token can lose traversal immediately, while the
        # normal service operator retains it.
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)


def _replace(path: Path, payload: dict[str, Any]) -> None:
    temporary = _write_temporary(path, payload)
    try:
        os.replace(temporary, path)
        _flush_directory(path.parent)
        if os.name == "nt":
            os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read(expected: dict[str, Any]) -> dict[str, Any]:
    path: Path = expected["path"]
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise ReceiptError("transition receipt is missing; bare resume is refused") from error
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError("transition receipt is invalid JSON") from error
    if not isinstance(payload, dict):
        raise ReceiptError("transition receipt is invalid")
    required = {
        "schema_version",
        "service_id",
        "binding_path",
        "state_root",
        "vault_root",
        "port",
        "target_port",
        "phase",
        "worker_pid",
        "listener_pids",
        "captured_pids",
        "observed_pids",
    }
    if set(payload) != required or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ReceiptError("transition receipt schema is invalid")
    for field in ("service_id", "binding_path", "state_root", "vault_root", "target_port"):
        if payload.get(field) != expected[field]:
            raise ReceiptError(f"transition receipt {field} does not match this transition")
    if payload.get("phase") not in _PHASES:
        raise ReceiptError("transition receipt phase is invalid")
    if not isinstance(payload.get("port"), int) or not 1 <= payload["port"] <= 65535:
        raise ReceiptError("transition receipt original port is invalid")
    worker_pid = payload.get("worker_pid")
    if not isinstance(worker_pid, int) or worker_pid < 1:
        raise ReceiptError("transition receipt worker pid is invalid")
    for field in ("listener_pids", "captured_pids", "observed_pids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, int) or value < 1 for value in values)
            or values != sorted(set(values))
        ):
            raise ReceiptError(f"transition receipt {field} is invalid")
    captured = sorted({worker_pid, *payload["listener_pids"]})
    if payload["captured_pids"] != captured:
        raise ReceiptError("transition receipt captured pid proof is invalid")
    # Re-resolve stored paths too; lexical aliases are not alternate authority.
    for field in ("binding_path", "state_root", "vault_root"):
        if os.fspath(_resolved(payload[field], field)) != payload[field]:
            raise ReceiptError(f"transition receipt {field} is not resolved")
    receipt_resolved = path.resolve(strict=False)
    vault_resolved = Path(payload["vault_root"])
    if _contains(vault_resolved, receipt_resolved) or _contains(
        vault_resolved, Path(payload["state_root"])
    ):
        raise ReceiptError("transition receipt placement is not outside the vault")
    payload["proof_pids"] = sorted(
        {*payload["captured_pids"], *payload["observed_pids"]}
    )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "verify", "phase", "clear"):
        command = subparsers.add_parser(action)
        command.add_argument("--path", required=True)
        command.add_argument("--service-id", required=True)
        command.add_argument("--binding-path", required=True)
        command.add_argument("--state-root", required=True)
        command.add_argument("--vault", required=True)
        command.add_argument("--target-port", type=int, required=True)
        if action == "create":
            command.add_argument("--port", type=int, required=True)
            command.add_argument("--worker-pid", type=int, required=True)
            command.add_argument("--listener-pid", type=int, action="append", default=[])
        elif action == "verify":
            command.add_argument("--json", action="store_true")
            command.add_argument(
                "--field",
                choices=(
                    "phase",
                    "port",
                    "target_port",
                    "worker_pid",
                    "proof_pids",
                    "state_root",
                ),
            )
        elif action == "phase":
            command.add_argument("--phase", choices=sorted(_PHASES), required=True)
            command.add_argument("--observed-pid", type=int, action="append", default=[])
    return parser


def _run(args: argparse.Namespace) -> None:
    expected = _identity(args)
    path: Path = expected["path"]
    if args.action == "create":
        worker_pid = _validate_pid(args.worker_pid, "worker pid")
        listener_pids = sorted(
            {_validate_pid(value, "listener pid") for value in args.listener_pid}
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "service_id": expected["service_id"],
            "binding_path": expected["binding_path"],
            "state_root": expected["state_root"],
            "vault_root": expected["vault_root"],
            "port": args.port,
            "target_port": expected["target_port"],
            "phase": "captured",
            "worker_pid": worker_pid,
            "listener_pids": listener_pids,
            "captured_pids": sorted({worker_pid, *listener_pids}),
            "observed_pids": [],
        }
        if not 1 <= payload["port"] <= 65535:
            raise ReceiptError("original port is invalid")
        _create(path, payload)
        return

    payload = _read(expected)
    if args.action == "verify":
        if args.field:
            value = payload[args.field]
            if isinstance(value, list):
                for item in value:
                    print(item)
            else:
                print(value)
        elif args.json:
            print(json.dumps(payload, sort_keys=True))
        return
    if args.action == "phase":
        observed = {
            _validate_pid(value, "observed pid") for value in args.observed_pid
        }
        payload.pop("proof_pids", None)
        payload["phase"] = args.phase
        payload["observed_pids"] = sorted({*payload["observed_pids"], *observed})
        _replace(path, payload)
        return
    payload.pop("proof_pids", None)
    path.unlink()
    _flush_directory(path.parent)


def main() -> int:
    try:
        _run(_parser().parse_args())
    except (OSError, ReceiptError, ValueError) as error:
        print(f"transition receipt error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
