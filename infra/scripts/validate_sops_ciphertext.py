#!/usr/bin/env python3
"""Reject plaintext or malformed tracked secret artifacts under infra/secrets."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _tracked_secret_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "infra/secrets"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not enumerate tracked secret artifacts")
    return [ROOT / line for line in result.stdout.splitlines() if line]


def _contains_encrypted_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_encrypted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_encrypted_value(item) for item in value)
    return isinstance(value, str) and value.startswith("ENC[")


def validate() -> int:
    checked = 0
    for path in _tracked_secret_files():
        relative = path.relative_to(ROOT)
        lowered = relative.name.lower()
        if any(token in lowered for token in (".dec.", ".plain.", ".decrypted.", "age.key", ".agekey")):
            raise RuntimeError("tracked plaintext secret artifact is forbidden")
        if relative.name == "README.md" or "receipts" in relative.parts:
            continue
        if ".sops." not in relative.name:
            raise RuntimeError("tracked secret artifact must be SOPS ciphertext")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("tracked SOPS artifact is invalid") from exc
        if not isinstance(document, dict):
            raise RuntimeError("tracked SOPS artifact has no encrypted payload")
        encrypted_payload = {key: value for key, value in document.items() if key != "sops"}
        if (
            not isinstance(document.get("sops"), dict)
            or not document["sops"]
            or not _contains_encrypted_value(encrypted_payload)
        ):
            raise RuntimeError("tracked SOPS artifact has no encrypted payload")
        checked += 1
    return checked


def main() -> int:
    try:
        checked = validate()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"SOPS ciphertext validation passed: {checked} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
