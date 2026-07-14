#!/usr/bin/env python3
"""Run a content-free smoke test against an immutable provisioner image."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence

_IMAGE_PATTERN = re.compile(r"^ghcr\.io/artexis10/exomem-provisioner@sha256:[0-9a-f]{64}$")
_ENTRYPOINTS = (
    "exomem-provisioner-database-bootstrap",
    "exomem-provisioner-database-migrate",
    "exomem-provisioner-database-validate",
    "exomem-provisioner-api",
    "exomem-provisioner-worker",
    "exomem-provisioner-volume-rebind",
    "exomem-durability-actions",
    "exomem-restore-fetch",
    "exomem-export-gc",
    "exomem-durability-backup-worker",
    "exomem-database-backup-worker",
    "exomem-deletion-worker",
    "exomem-volume-worker",
)
_MIGRATION_ROOT = "/opt/exomem/provisioner-migrations"
_POSTGRES_BINARIES = (
    "/usr/bin/createdb",
    "/usr/bin/dropdb",
    "/usr/bin/pg_dump",
    "/usr/bin/pg_restore",
    "/usr/bin/psql",
)
_PROBE = f"""
from importlib import metadata
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from exomem_provisioner.database import DATABASE_REVISION

required = {set(_ENTRYPOINTS)!r}
installed = {{entry.name: entry for entry in metadata.distribution('exomem-provisioner').entry_points}}
if not required.issubset(installed):
    raise SystemExit(11)
for name in required:
    entry = installed[name]
    if not callable(entry.load()):
        raise SystemExit(13)
for path in {_POSTGRES_BINARIES!r}:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise SystemExit(12)
migration_root = Path({_MIGRATION_ROOT!r})
for path in (migration_root, migration_root / "alembic.ini", migration_root / "alembic"):
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_gid != 0:
        raise SystemExit(14)
    if path.stat().st_mode & 0o222:
        raise SystemExit(15)
configuration = Config(str(migration_root / "alembic.ini"))
if ScriptDirectory.from_config(configuration).get_heads() != [DATABASE_REVISION]:
    raise SystemExit(16)
""".strip()


class ProvisionerImageVerificationError(RuntimeError):
    """Raised without carrying container output, which may contain environment data."""


def verify(
    *,
    image: str,
    container_binary: str,
    require_published: bool = False,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if not _IMAGE_PATTERN.fullmatch(image):
        raise ProvisionerImageVerificationError("provisioner image must use its immutable digest")
    if not container_binary or any(character.isspace() for character in container_binary):
        raise ProvisionerImageVerificationError("container binary is invalid")
    if require_published:
        pull = run(
            [container_binary, "pull", image],
            check=False,
            capture_output=True,
            text=True,
        )
        if pull.returncode != 0:
            raise ProvisionerImageVerificationError("provisioner published digest pull failed")
        inspection = run(
            [
                container_binary,
                "image",
                "inspect",
                image,
                "--format",
                "{{json .RepoDigests}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            repo_digests = json.loads(inspection.stdout) if inspection.returncode == 0 else None
        except json.JSONDecodeError:
            repo_digests = None
        if not isinstance(repo_digests, list) or image not in repo_digests:
            raise ProvisionerImageVerificationError(
                "provisioner published digest identity is unavailable"
            )
    result = run(
        [
            container_binary,
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "python",
            image,
            "-c",
            _PROBE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProvisionerImageVerificationError("provisioner image entrypoint smoke failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--container-binary", default="docker")
    parser.add_argument("--require-published", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify(
            image=args.image,
            container_binary=args.container_binary,
            require_published=args.require_published,
        )
    except ProvisionerImageVerificationError as error:
        print(str(error))
        return 1
    print("Provisioner image entrypoints verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
