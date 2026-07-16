#!/usr/bin/env python3
"""Run a content-free smoke test against an immutable provisioner image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

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
_PROVISIONER_ROOT = Path(__file__).resolve().parents[1] / "provisioner"
_EXPECTED_MIGRATION_FILES = {
    str(path.relative_to(_PROVISIONER_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [
        _PROVISIONER_ROOT / "alembic.ini",
        *sorted((_PROVISIONER_ROOT / "alembic").rglob("*")),
    ]
    if path.is_file() and "__pycache__" not in path.parts
}
_EXPECTED_MIGRATION_DIRECTORIES = {
    ".",
    *{
        str(path.relative_to(_PROVISIONER_ROOT))
        for path in (_PROVISIONER_ROOT / "alembic").rglob("*")
        if path.is_dir() and "__pycache__" not in path.parts
    },
    "alembic",
}
_POSTGRES_BINARIES = (
    "/usr/bin/createdb",
    "/usr/bin/dropdb",
    "/usr/bin/pg_dump",
    "/usr/bin/pg_restore",
    "/usr/bin/psql",
)
_PROBE = f"""
from importlib import metadata
import hashlib
import os
from pathlib import Path
import stat

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
expected_files = {_EXPECTED_MIGRATION_FILES!r}
expected_directories = {_EXPECTED_MIGRATION_DIRECTORIES!r}
observed_files = {{}}
observed_directories = set()
for path in (migration_root, *sorted(migration_root.rglob("*"))):
    if path.is_symlink():
        raise SystemExit(14)
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise SystemExit(14)
    if metadata.st_mode & 0o222:
        raise SystemExit(15)
    relative = "." if path == migration_root else str(path.relative_to(migration_root))
    if stat.S_ISDIR(metadata.st_mode):
        observed_directories.add(relative)
    elif stat.S_ISREG(metadata.st_mode):
        observed_files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        raise SystemExit(14)
if observed_files != expected_files or observed_directories != expected_directories:
    raise SystemExit(17)
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
