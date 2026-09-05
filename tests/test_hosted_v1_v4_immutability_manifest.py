"""One complete v1-v4 immutability manifest, pinned to committed bytes.

Hosted v1-v4 are released identities, not editable files: a promoted package's
`compatibility_sha256`, `artifact_sha256` and `archive_sha256` are recorded off
these exact bytes, so a single changed byte silently invalidates a live
promotion record rather than failing loudly anywhere near the edit.

The pin covers every tracked file of the v1-v4 release surface -- source skills
and assets, candidate definitions and selection cases, generated packages,
locks, archives, compatibility descriptors, behavior and acceptance fixtures,
promotion records, and the generated directory packets, which embed v1's
compatibility, lock and archive digests and are therefore release identity
rather than listing state. What is excluded is excluded by name with its reason
recorded in the manifest itself, so the boundary is reviewable instead of
implied.

The digests were computed from the committed blobs at the manifest's own
`source_revision`, never from a working tree, so a dirty tree could not seed the
pin. `hosted-alpha-agent-v5` is excluded by name: v5 is the candidate this
change adds, and pinning it here would make the guard for the historical
releases move with the new one.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "hosted_v1_v4_immutability_manifest.json"
MANIFEST_SHA256 = "b57116b5df7e85013ce8a970b0609e03df2f47b35bc653037295504cbffc90c1"
SOURCE_REVISION = "9187a72ad83a2a560c9cca0f5b53a246e0680cb2"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _tracked_hosted_files() -> list[str]:
    """Every tracked file under `plugins/hosted`, from the index rather than disk.

    `rglob` also returns whatever a concurrent render or an interrupted
    promotion left lying around -- `.claude.promotion.lock`, a
    `.exomem-hosted-render-*` staging directory -- and a guard that reports
    those as unpinned release files is a guard that cries wolf. The index knows
    the difference.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "plugins/hosted"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [path for path in listed.split("\0") if path]


def _committed_bytes(relative: str) -> bytes:
    """The file's bytes, from disk when present and from the index when not.

    A tracked path can legitimately be absent from a working tree -- a sparse
    checkout, a partial clone -- and reading only from disk turns that into a
    stopped audit rather than a failed one. Absent from both is the real
    failure, and it names the path.
    """
    path = REPO_ROOT / relative
    if path.is_file():
        return path.read_bytes()
    completed = subprocess.run(
        ["git", "cat-file", "blob", f":0:{relative}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AssertionError(f"pinned v1-v4 file is absent from disk and from the index: {relative}")
    return completed.stdout


def _covered(relative: str) -> bool:
    manifest = _manifest()
    excluded = tuple(manifest["excluded_prefixes"])
    if relative.startswith(excluded):
        return False
    return str(manifest["excluded_candidate"]) not in relative.split("/")


def test_v1_v4_release_surface_is_byte_identical_to_the_pinned_manifest() -> None:
    manifest = _manifest()
    files = manifest["files"]
    assert isinstance(files, dict)
    assert manifest["file_count"] == len(files)

    mismatched: list[str] = []
    for relative, expected in sorted(files.items()):
        if hashlib.sha256(_committed_bytes(relative)).hexdigest() != expected:
            mismatched.append(relative)

    assert not mismatched, f"pinned v1-v4 files changed: {mismatched}"


def test_the_manifest_still_enumerates_every_v1_v4_file() -> None:
    """A guard that can be defeated by deleting a row is not a guard.

    The equality is two-way on purpose: the previous test proves each pinned
    file still hashes to its pinned digest, and this one proves no v1-v4 file
    escaped the pin -- including a file added under the historical candidates
    after the manifest was cut.
    """
    manifest = _manifest()
    tracked = {relative for relative in _tracked_hosted_files() if _covered(relative)}
    assert tracked == set(manifest["files"])


def test_the_generated_directory_packets_are_inside_the_pin() -> None:
    """They carry v1's release digests, so they are identity, not listing state."""
    manifest = _manifest()
    generated = {
        relative
        for relative in manifest["files"]
        if relative.startswith("plugins/hosted/directory/generated/")
    }
    assert generated, "the generated directory packets are not pinned"

    v1_lock = json.loads(
        (REPO_ROOT / "plugins/hosted/generated/claude.lock.json").read_text(encoding="utf-8")
    )
    packets = "".join(
        (REPO_ROOT / relative).read_text(encoding="utf-8") for relative in sorted(generated)
    )
    assert v1_lock["compatibility_sha256"] in packets


def test_every_exclusion_states_why_it_is_not_release_identity() -> None:
    """An exclusion with no reason is how a guard quietly stops covering things."""
    manifest = _manifest()
    excluded = manifest["excluded_prefixes"]
    assert isinstance(excluded, dict) and excluded

    for prefix, reason in excluded.items():
        assert prefix.startswith("plugins/hosted/") and prefix.endswith("/"), prefix
        assert isinstance(reason, str) and len(reason) > 80, f"{prefix}: reason is too thin"
        assert not any(
            placeholder in reason.lower()
            for placeholder in ("tbd", "todo", "for now", "later", "out of scope")
        ), f"{prefix}: placeholder reason"


def test_the_manifest_itself_is_pinned() -> None:
    """The manifest is evidence; its own digest is what a report can quote."""
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == MANIFEST_SHA256
    assert _manifest()["source_revision"] == SOURCE_REVISION
