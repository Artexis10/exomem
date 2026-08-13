"""Stdlib-only worker for the Bubblewrap driver namespace.

This file is executed with the system Python under ``-I -S -B``.  It never
imports the benchmark package and never receives provider capabilities.  Its
only authority is a line-delimited, bounded request/reply protocol on stdin and
stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


IPC_PROTOCOL = "epistemic-driver-ipc.v1"
MAX_MESSAGE_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32


class WorkerProtocolError(RuntimeError):
    pass


class BrokerRemoteError(RuntimeError):
    pass


def _depth(value: object, current: int = 0) -> int:
    if current > MAX_JSON_DEPTH:
        return current
    if isinstance(value, dict):
        return max((current, *(_depth(item, current + 1) for item in value.values())))
    if isinstance(value, list):
        return max((current, *(_depth(item, current + 1) for item in value)))
    return current


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise WorkerProtocolError("invalid broker reply framing")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise WorkerProtocolError("duplicate broker reply member")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                WorkerProtocolError("nonfinite broker reply")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("malformed broker reply") from exc
    if not isinstance(decoded, dict) or _depth(decoded) > MAX_JSON_DEPTH:
        raise WorkerProtocolError("invalid broker reply shape")
    return decoded


def _encode(payload: dict[str, object]) -> bytes:
    try:
        encoded = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("driver value is not serializable") from exc
    if len(encoded) > MAX_MESSAGE_BYTES or _depth(payload) > MAX_JSON_DEPTH:
        raise WorkerProtocolError("driver message exceeds protocol bounds")
    return encoded


def _send(payload: dict[str, object]) -> None:
    sys.stdout.buffer.write(_encode(payload))
    sys.stdout.buffer.flush()


class BrokerProxy:
    """The child's entire provider-facing surface: one serializable method."""

    __slots__ = ("_next_id",)

    def __init__(self) -> None:
        self._next_id = 1

    def invoke(self, driver_surface_id: str, *args: object, **kwargs: object) -> object:
        request_id = self._next_id
        self._next_id += 1
        _send(
            {
                "protocol": IPC_PROTOCOL,
                "type": "invoke",
                "id": request_id,
                "surface": driver_surface_id,
                "args": list(args),
                "kwargs": kwargs,
            }
        )
        raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        reply = _decode(raw)
        if reply.get("protocol") != IPC_PROTOCOL or reply.get("type") != "result":
            raise WorkerProtocolError("broker reply protocol confusion")
        if reply.get("id") != request_id or not isinstance(reply.get("ok"), bool):
            raise WorkerProtocolError("broker reply request binding differs")
        if reply["ok"] is True:
            if set(reply) != {"protocol", "type", "id", "ok", "result"}:
                raise WorkerProtocolError("broker success reply shape differs")
            return reply["result"]
        if set(reply) != {"protocol", "type", "id", "ok", "error"}:
            raise WorkerProtocolError("broker error reply shape differs")
        if reply.get("error") != "provider_exception":
            raise WorkerProtocolError("unknown broker error code")
        raise BrokerRemoteError("provider_exception")


def _load_driver(path: Path) -> Any:
    source = path.read_bytes()
    if len(source) > 512 * 1024:
        raise WorkerProtocolError("driver source exceeds bound")
    namespace: dict[str, object] = {"__name__": "sandboxed_driver", "__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace, namespace)
    entrypoint = namespace.get("run")
    if not callable(entrypoint):
        raise WorkerProtocolError("driver must define callable run(broker)")
    return entrypoint


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "/worker/driver.py":
        return 64
    try:
        entrypoint = _load_driver(Path(sys.argv[1]))
        result = entrypoint(BrokerProxy())
        _send({"protocol": IPC_PROTOCOL, "type": "complete", "result": result})
        return 0
    except BaseException as exc:  # noqa: BLE001 - child reports only a class, never values
        try:
            _send(
                {
                    "protocol": IPC_PROTOCOL,
                    "type": "driver_exception",
                    "exception_type": type(exc).__name__,
                }
            )
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
