"""One complete v1-v4 immutability manifest, pinned to committed bytes.

Hosted v1-v4 are released identities, not editable files: a promoted package's
`compatibility_sha256`, `artifact_sha256` and `archive_sha256` are recorded off
these exact bytes, so a single changed byte silently invalidates a live
promotion record rather than failing loudly anywhere near the edit.

The pin covers every tracked file of the v1-v4 release surface -- source skills
and assets, candidate definitions and selection cases, generated packages,
locks, archives, compatibility descriptors, behavior and acceptance fixtures,
and promotion records. `plugins/hosted/directory/**` is deliberately outside it:
directory publication state is operator-driven listing state, not a release
identity.

The digests were computed from the committed blobs at the manifest's own
`source_revision`, never from a working tree, so a dirty tree could not seed the
pin. `plugins/hosted/generated/candidates/hosted-alpha-agent-v5/**` and
`plugins/hosted/candidates/hosted-alpha-agent-v5/**` are excluded by name: v5 is
the candidate this change adds, and pinning it here would make the guard for the
historical releases move with the new one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "hosted_v1_v4_immutability_manifest.json"
HOSTED_ROOT = REPO_ROOT / "plugins" / "hosted"


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _covered(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT).as_posix()
    manifest = _manifest()
    excluded_prefixes = tuple(manifest["excluded_prefixes"])  # type: ignore[arg-type]
    if relative.startswith(excluded_prefixes):
        return False
    return str(manifest["excluded_candidate"]) not in relative.split("/")


def test_v1_v4_release_surface_is_byte_identical_to_the_pinned_manifest() -> None:
    manifest = _manifest()
    files = manifest["files"]
    assert isinstance(files, dict)
    assert manifest["file_count"] == len(files)

    mismatched: list[str] = []
    missing: list[str] = []
    for relative, expected in sorted(files.items()):
        path = REPO_ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatched.append(relative)

    assert not missing, f"pinned v1-v4 files are gone: {missing}"
    assert not mismatched, f"pinned v1-v4 files changed: {mismatched}"


def test_the_manifest_still_enumerates_every_v1_v4_file_on_disk() -> None:
    """A guard that can be defeated by deleting a row is not a guard.

    The equality is two-way on purpose: the previous test proves each pinned
    file still hashes to its pinned digest, and this one proves no v1-v4 file
    escaped the pin -- including a file added under the historical candidates
    after the manifest was cut.
    """
    manifest = _manifest()
    on_disk = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in HOSTED_ROOT.rglob("*")
        if path.is_file() and _covered(path)
    }
    assert on_disk == set(manifest["files"])  # type: ignore[arg-type]


def test_the_manifest_itself_is_pinned() -> None:
    """The manifest is evidence; its own digest is what a report can quote."""
    body = MANIFEST_PATH.read_bytes()
    assert (
        hashlib.sha256(body).hexdigest()
        == "415cc748638291ac987e8ae33f07666fab9354cc413b0f674afc440fc567fb8e"
    )
    assert _manifest()["source_revision"] == "ee5f4a675a7948a7e6cc2f0d3bd8b5ebecfc786c"
