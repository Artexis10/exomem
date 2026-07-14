#!/usr/bin/env python3
"""Run content-free black-box checks without retaining URLs or response bodies."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class _Response(Protocol):
    status: int
    headers: Any


@dataclass(frozen=True)
class Observation:
    name: str
    ok: bool
    status_code: int | None
    duration_ms: int
    reason: str

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "reason": self.reason,
        }


def _fetch(target: str, timeout_seconds: int) -> _Response:
    request = urllib.request.Request(
        target,
        method="GET",
        headers={"User-Agent": "exomem-hosted-blackbox/v1", "Accept": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=timeout_seconds)  # noqa: S310


def observe(
    *,
    name: str,
    target: str,
    fetch: Callable[[str, int], _Response] = _fetch,
    timeout_seconds: int,
    maximum_age_header: str | None = None,
    maximum_age_seconds: int | None = None,
) -> Observation:
    started = time.monotonic()
    status: int | None = None
    reason = "transport-failed"
    ok = False
    try:
        response = fetch(target, timeout_seconds)
        status = int(response.status)
        ok = status == 200
        reason = "ok" if ok else "unexpected-status"
        if ok and maximum_age_header is not None:
            raw_age = response.headers.get(maximum_age_header)
            try:
                age = int(raw_age)
            except (TypeError, ValueError):
                ok = False
                reason = "freshness-signal-invalid"
            else:
                if maximum_age_seconds is None or age < 0 or age > maximum_age_seconds:
                    ok = False
                    reason = "freshness-threshold-exceeded"
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        pass
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    return Observation(name=name, ok=ok, status_code=status, duration_ms=duration_ms, reason=reason)


def _contract_observations(contract: dict[str, Any]) -> list[Observation]:
    if contract.get("schema_version") != 1 or not isinstance(contract.get("checks"), list):
        raise ValueError("black-box contract is invalid")
    observations: list[Observation] = []
    for check in contract["checks"]:
        if not isinstance(check, dict):
            raise ValueError("black-box contract is invalid")
        target_env = check.get("target_environment_variable")
        if not isinstance(target_env, str) or not os.environ.get(target_env):
            raise ValueError("black-box target is not configured")
        observations.append(
            observe(
                name=check["name"],
                target=os.environ[target_env],
                timeout_seconds=check["timeout_seconds"],
                maximum_age_header=check.get("maximum_age_header"),
                maximum_age_seconds=check.get("maximum_age_seconds"),
            )
        )
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        observations = _contract_observations(contract)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps([item.as_dict() for item in observations], sort_keys=True))
    return 0 if all(item.ok for item in observations) else 2


if __name__ == "__main__":
    raise SystemExit(main())
