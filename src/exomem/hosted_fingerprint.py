"""Content-free, offline hosted-vault preservation proof."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .hosted_portability import PortabilityError, canonical_vault_fingerprint

VAULT_ROOT = Path("/var/lib/exomem/vault")
TERMINATION_LOG = Path("/dev/termination-log")


def _write(output_path: Path, value: dict[str, object]) -> None:
    output_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def run(*, vault_root: Path, output_path: Path) -> int:
    """Write one bounded Kubernetes termination record and no vault metadata."""

    try:
        digest = canonical_vault_fingerprint(vault_root)
    except (OSError, PortabilityError):
        try:
            _write(
                output_path,
                {
                    "artifact": "exomem-hosted-vault-fingerprint",
                    "schemaVersion": 1,
                    "error": "vault-fingerprint-failed",
                },
            )
        except OSError:
            pass
        return 1
    try:
        _write(
            output_path,
            {
                "artifact": "exomem-hosted-vault-fingerprint",
                "schemaVersion": 1,
                "sha256": digest,
            },
        )
    except OSError:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the fixed in-cell fingerprint command with no runtime parameters."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        return 2
    return run(vault_root=VAULT_ROOT, output_path=TERMINATION_LOG)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
