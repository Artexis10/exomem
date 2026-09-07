"""One complete v1-v4 immutability manifest, pinned to committed source bytes.

Hosted v1-v4 are released identities, not editable files: a promoted package's
`compatibility_sha256`, `artifact_sha256` and `archive_sha256` are recorded off
these bytes, so a changed byte silently invalidates a live promotion record
rather than failing loudly anywhere near the edit.

What the pin covers is the part a human writes: source skills and assets,
candidate definitions and selection cases, behavior, acceptance and marketplace
fixtures, and promotion records.

What it deliberately does not cover is render output. `plugins/hosted/generated/**`
and `plugins/hosted/directory/generated/**` are rewritten by
`.github/workflows/release-please.yml`, which runs `hosted-plugin.py
regenerate`/`render` and commits the result, on every release that moves the
schema contract -- and by any feature PR that moves it. An earlier cut pinned
them, and the result was a guard that turned main red after a release with a
message reading as a release-identity violation: a control whose wrong-firing
cost lands on the operator, to prevent something two other controls already
catch. `hosted-plugin.py check` recomputes those bytes from source rather than
remembering them, and the release parity gate re-runs it; a feature PR that
alters a generated package without altering its source still fails there.

Each exclusion carries its reason in the manifest itself, so the boundary is
reviewable rather than implied, and `source_revision` records the moment the
digests were taken from committed blobs -- never from a working tree, so a dirty
tree could not seed the pin.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "hosted_v1_v4_immutability_manifest.json"
MANIFEST_SHA256 = "0f54900fbc09e9fd8b96dacf8ee4a593524c9f94b92ff3949dc971275f81430e"
#: Prefixes the release automation owns. Named here as well as in the manifest so
#: a test can state the policy rather than only consume it.
RELEASE_OWNED_PREFIXES = (
    "plugins/hosted/generated/",
    "plugins/hosted/directory/generated/",
)
HISTORICAL_CANDIDATES = frozenset(
    {
        "hosted-alpha-agent-v1",
        "hosted-alpha-agent-v2",
        "hosted-alpha-agent-v3",
        "hosted-alpha-agent-v4",
    }
)


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


def _read_bytes(root: Path, relative: str) -> bytes:
    """The file's bytes, from disk when present and from the index when not.

    A tracked path can legitimately be absent from a working tree -- a sparse
    checkout, a partial clone -- and reading only from disk turns that into a
    stopped audit rather than a failed one. Absent from both is the real
    failure, and it names the path. The index fallback applies only to the real
    repository; a scratch copy has no index and its files are simply there.
    """
    path = root / relative
    if path.is_file():
        return path.read_bytes()
    if root != REPO_ROOT:
        raise AssertionError(f"pinned v1-v4 file is missing from the copy: {relative}")
    completed = subprocess.run(
        ["git", "cat-file", "blob", f":0:{relative}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"pinned v1-v4 file is absent from disk and from the index: {relative}"
        )
    return completed.stdout


def _pinned_mismatches(root: Path) -> list[str]:
    """Pinned paths under `root` whose bytes no longer hash to their pin."""
    files = _manifest()["files"]
    return sorted(
        relative
        for relative, expected in files.items()
        if hashlib.sha256(_read_bytes(root, relative)).hexdigest() != expected
    )


def _hosted_copy(destination: Path) -> Path:
    shutil.copytree(REPO_ROOT / "plugins" / "hosted", destination / "plugins" / "hosted")
    return destination


def _covered(relative: str) -> bool:
    manifest = _manifest()
    if relative.startswith(tuple(manifest["excluded_prefixes"])):
        return False
    components = relative.split("/")
    if components[:3] == ["plugins", "hosted", "candidates"]:
        return len(components) > 3 and components[3] in HISTORICAL_CANDIDATES
    return str(manifest["excluded_candidate"]) not in components


def test_v1_v4_release_surface_is_byte_identical_to_the_pinned_manifest() -> None:
    manifest = _manifest()
    assert manifest["file_count"] == len(manifest["files"])

    mismatched = _pinned_mismatches(REPO_ROOT)

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


def test_v4_command_binding_candidate_is_not_a_historical_v4_component() -> None:
    assert _covered(
        "plugins/hosted/candidates/hosted-alpha-agent-v4/definition.json"
    )
    assert not _covered(
        "plugins/hosted/candidates/hosted-alpha-agent-v4-command-binding-v1/definition.json"
    )


def test_a_changed_v1_v4_source_byte_still_trips_the_manifest(tmp_path: Path) -> None:
    """The property the manifest exists for, stated as a test rather than trusted.

    Narrowing the pin to source bytes is only safe if the source bytes are still
    actually pinned, so a v2 skill is edited in a copy and the guard has to name
    it.
    """
    root = _hosted_copy(tmp_path / "repo")
    skill = root / "plugins/hosted/candidates/hosted-alpha-agent-v2/skills/exomem-records/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nDrift.\n", encoding="utf-8")

    assert _pinned_mismatches(root) == [
        "plugins/hosted/candidates/hosted-alpha-agent-v2/skills/exomem-records/SKILL.md"
    ]


def test_a_release_regeneration_does_not_trip_the_manifest(tmp_path: Path) -> None:
    """The wrong-firing this narrowing removes, reproduced.

    Release 0.69.0 and 0.70.0 each rewrote the v2 and v4 `openai.lock.json`
    files, and the schema-contract moves in between rewrote the claude locks,
    the compatibility descriptors and the generated channel packets. Every one
    of those is what a release is *supposed* to do, and none of them is a
    v1-v4 identity violation -- so none of them may make this guard red.
    """
    root = _hosted_copy(tmp_path / "repo")
    generated = root / "plugins/hosted/generated"

    for lock in (
        generated / "candidates/hosted-alpha-agent-v2/openai.lock.json",
        generated / "candidates/hosted-alpha-agent-v4/openai.lock.json",
        generated / "candidates/hosted-alpha-agent-v3/claude.lock.json",
        generated / "claude.lock.json",
    ):
        payload = json.loads(lock.read_text(encoding="utf-8"))
        payload["compatibility_sha256"] = "0" * 64
        payload["schema_contract_sha256"] = "1" * 64
        lock.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    for descriptor in (
        generated / "compatibility.json",
        generated / "candidates/hosted-alpha-agent-v2/compatibility.json",
    ):
        payload = json.loads(descriptor.read_text(encoding="utf-8"))
        payload["compatibility_sha256"] = "0" * 64
        descriptor.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    packet = root / "plugins/hosted/directory/generated/claude-connector.json"
    packet.write_text(packet.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert _pinned_mismatches(root) == []


def test_release_regenerated_output_is_outside_the_pin() -> None:
    """The exclusion is the decision; say it out loud rather than by absence."""
    manifest = _manifest()
    excluded = manifest["excluded_prefixes"]

    for prefix in RELEASE_OWNED_PREFIXES:
        assert prefix in excluded, f"{prefix} is not recorded as release-owned"
    assert not [path for path in manifest["files"] if path.startswith(RELEASE_OWNED_PREFIXES)]

    # And the source tree is still inside it, which is what makes the narrowing
    # a narrowing rather than a retreat.
    assert [path for path in manifest["files"] if path.startswith("plugins/hosted/candidates/")]
    assert [path for path in manifest["files"] if path.startswith("plugins/hosted/skills/")]
    assert [path for path in manifest["files"] if path.startswith("plugins/hosted/promotion/")]


def test_every_exclusion_states_why_it_is_not_pinned() -> None:
    """An exclusion with no reason is how a guard quietly stops covering things."""
    excluded = _manifest()["excluded_prefixes"]
    assert isinstance(excluded, dict) and excluded

    for prefix, reason in excluded.items():
        assert prefix.startswith("plugins/hosted/") and prefix.endswith("/"), prefix
        assert isinstance(reason, str) and len(reason) > 80, f"{prefix}: reason is too thin"
        assert not any(
            placeholder in reason.lower()
            for placeholder in ("tbd", "todo", "for now", "later", "out of scope")
        ), f"{prefix}: placeholder reason"


def test_the_manifest_itself_is_pinned() -> None:
    """The manifest is evidence; its own digest is what a report can quote.

    `source_revision` is provenance, not a second pin: it records where the
    digests were read from. It is deliberately not tied to any other file's
    revision, because a coupling like that restales on every release that moves
    something neither file pins.
    """
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == MANIFEST_SHA256

    revision = _manifest()["source_revision"]
    assert isinstance(revision, str) and len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)
