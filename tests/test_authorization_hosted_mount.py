from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from exomem.governance import authorization_hosted_mount

_FILES = {
    "keyring.json": b'{"keyring":"sentinel"}',
    "control.json": b'{"control":"sentinel"}',
    "serving-membership.json": b'{"membership":"sentinel"}',
}


def _projected_secret(root: Path) -> None:
    root.mkdir(mode=0o700)
    generation = root / "..2026_08_28_00_00_00.000000001"
    generation.mkdir(mode=0o700)
    for name, payload in _FILES.items():
        target = generation / name
        target.write_bytes(payload)
        target.chmod(0o440)
    (root / "..data").symlink_to(generation.name, target_is_directory=True)
    for name in _FILES:
        (root / name).symlink_to(f"..data/{name}")


def test_copy_projected_hosted_custody_publishes_owner_only_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o777)
    wrapper.chmod(0o777)

    authorization_hosted_mount.copy_projected_custody(source, destination)

    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o777
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert {path.name for path in destination.iterdir()} == set(_FILES)
    for name, payload in _FILES.items():
        copied = destination / name
        assert copied.read_bytes() == payload
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert copied.stat().st_uid == os.geteuid()


@pytest.mark.parametrize("mutation", ["extra", "leaf", "generation"])
def test_copy_projected_hosted_custody_rejects_ambiguous_sources(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o777)
    wrapper.chmod(0o777)
    if mutation == "extra":
        (source / "unexpected").write_text("no", encoding="utf-8")
    elif mutation == "leaf":
        (source / "keyring.json").unlink()
        (source / "keyring.json").write_text("no", encoding="utf-8")
    else:
        (source / "..data").unlink()
        (source / "..data").symlink_to("../outside", target_is_directory=True)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.copy_projected_custody(source, destination)

    assert not destination.exists()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_copy_projected_hosted_custody_rejects_a_preexisting_private_child(
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o777)
    wrapper.chmod(0o777)
    outside_file: Path | None = None
    if kind == "directory":
        destination.mkdir(mode=0o700)
    else:
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        outside_file = outside / "control.json"
        outside_file.write_text("must remain", encoding="utf-8")
        destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.copy_projected_custody(source, destination)

    if outside_file is not None:
        assert outside_file.read_text(encoding="utf-8") == "must remain"


def test_copy_projected_hosted_custody_cleans_a_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o777)
    wrapper.chmod(0o777)
    replace = authorization_hosted_mount.os.replace
    calls = 0

    def fail_second(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publication failure")
        replace(source_path, target_path)

    monkeypatch.setattr(authorization_hosted_mount.os, "replace", fail_second)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.copy_projected_custody(source, destination)

    assert not destination.exists()


def _republished_secret(root: Path, payloads: dict[str, bytes]) -> None:
    """Replace the projected generation the way kubelet refreshes a Secret."""
    generation = root / "..2026_09_06_00_00_00.000000002"
    generation.mkdir(mode=0o700)
    for name, payload in payloads.items():
        target = generation / name
        target.write_bytes(payload)
        target.chmod(0o440)
    replacement = root / "..data.tmp"
    replacement.symlink_to(generation.name, target_is_directory=True)
    previous = (root / "..data").resolve()
    os.replace(replacement, root / "..data")
    # kubelet removes the superseded generation once ..data points past it.
    for stale in previous.iterdir():
        stale.unlink()
    previous.rmdir()


def test_republish_projected_custody_replaces_a_live_generation(
    tmp_path: Path,
) -> None:
    """Renewal must reach a pod that is already reading the custody directory.

    `copy_projected_custody` mkdirs its destination and refuses a non-empty one,
    so it can only initialise. Without a republish path the runtime holds the
    generation its init container copied for the pod's whole life, which is why
    a renewed authorization bundle could only be delivered by restarting the pod.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    _republished_secret(source, renewed)

    authorization_hosted_mount.republish_projected_custody(source, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert {path.name for path in destination.iterdir()} == set(_FILES)
    for name, payload in renewed.items():
        published = destination / name
        assert published.read_bytes() == payload
        assert stat.S_IMODE(published.stat().st_mode) == 0o600
        assert published.stat().st_uid == os.geteuid()
        # The reader refuses a file with more than one link, so a republish must
        # produce a fresh inode rather than hard-linking the projected source.
        assert published.stat().st_nlink == 1


def test_republish_projected_custody_requires_an_existing_generation(
    tmp_path: Path,
) -> None:
    """Republishing is not a second way to initialise custody."""

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.republish_projected_custody(source, wrapper / "private")


def test_republish_projected_custody_keeps_the_previous_generation_on_a_bad_source(
    tmp_path: Path,
) -> None:
    """A torn or ambiguous source is rejected before anything is written."""

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    (source / "unexpected.json").symlink_to("..data/keyring.json")

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.republish_projected_custody(source, destination)

    assert {path.name for path in destination.iterdir()} == set(_FILES)
    for name, payload in _FILES.items():
        assert (destination / name).read_bytes() == payload


def test_republish_projected_custody_survives_a_write_failure_midway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure partway through must leave the live generation whole.

    Every replacement is written and fsynced before any is moved into place, so
    a write that fails on the second file strands nothing. Replacing each file
    as it is written would leave the runtime on a directory holding one renewed
    file beside two stale ones, which fails cross-validation and refuses writes
    until something republished it correctly.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    _republished_secret(source, renewed)

    real_write = os.write
    writes = {"count": 0}

    def failing_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
        writes["count"] += 1
        if writes["count"] == 2:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(authorization_hosted_mount.os, "write", failing_write)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.republish_projected_custody(source, destination)

    monkeypatch.undo()
    # Every file is still the previous generation, and no temp file was left.
    assert {path.name for path in destination.iterdir()} == set(_FILES)
    for name, payload in _FILES.items():
        assert (destination / name).read_bytes() == payload


def test_watch_republishes_only_when_the_generation_changes(tmp_path: Path) -> None:
    """Seamless renewal is the watch keeping the published generation current."""

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    swapped = {"done": False}

    def sleeper(_seconds: float) -> None:
        if not swapped["done"]:
            _republished_secret(source, renewed)
            swapped["done"] = True

    authorization_hosted_mount.watch_projected_custody(
        source, destination, sleeper=sleeper, ticks=3
    )

    for name, payload in renewed.items():
        assert (destination / name).read_bytes() == payload


def test_watch_survives_a_failed_republish_and_retries(tmp_path: Path) -> None:
    """A transient failure must not end the watch and strand the pod.

    Exiting would leave the runtime on a generation nothing will refresh, which
    is exactly the stranding this whole path exists to prevent. Waiting costs at
    most the remainder of the attestation window.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    steps = {"n": 0}

    def sleeper(_seconds: float) -> None:
        steps["n"] += 1
        if steps["n"] == 1:
            # A torn generation: ..data points at a directory that is not there.
            broken = source / "..data.tmp"
            broken.symlink_to("..2026_09_06_00_00_00.999999999", target_is_directory=True)
            os.replace(broken, source / "..data")
        elif steps["n"] == 2:
            for stale in source.iterdir():
                if stale.name.startswith("..2026") and stale.is_dir():
                    for child in stale.iterdir():
                        child.unlink()
                    stale.rmdir()
            _republished_secret_without_previous(source, renewed)

    authorization_hosted_mount.watch_projected_custody(
        source, destination, sleeper=sleeper, ticks=4
    )

    for name, payload in renewed.items():
        assert (destination / name).read_bytes() == payload


def _republished_secret_without_previous(root: Path, payloads: dict[str, bytes]) -> None:
    generation = root / "..2026_09_06_00_00_00.000000003"
    generation.mkdir(mode=0o700)
    for name, payload in payloads.items():
        target = generation / name
        target.write_bytes(payload)
        target.chmod(0o440)
    replacement = root / "..data.tmp"
    replacement.symlink_to(generation.name, target_is_directory=True)
    os.replace(replacement, root / "..data")


def test_watch_republishes_a_stale_destination_after_a_sidecar_restart(
    tmp_path: Path,
) -> None:
    """A restarted watch must reconcile, not re-seed and go quiet.

    The watch used to remember which source generation it had last seen. On
    restart that memory was re-seeded from the *current* source, so a generation
    swapped while the sidecar was down was never published — and the pod served
    on a bundle that would expire, which is the original defect exactly.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    # The swap happens while nothing is watching.
    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    _republished_secret(source, renewed)

    # A freshly started watch sees an unchanged source for its whole life.
    authorization_hosted_mount.watch_projected_custody(
        source, destination, sleeper=lambda _seconds: None, ticks=1
    )

    for name, payload in renewed.items():
        assert (destination / name).read_bytes() == payload


def test_watch_repairs_a_generation_left_mixed_by_an_interrupted_publish(
    tmp_path: Path,
) -> None:
    """A crash between file replacements must not strand the pod permanently.

    The published files are cross-validated against each other, so one renewed
    file beside two stale ones refuses every mutation. Nothing repaired it while
    the watch keyed on the source generation, because the source had not moved.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    _republished_secret(source, renewed)
    # Exactly the state a kill between os.replace calls leaves behind.
    first = sorted(_FILES)[0]
    (destination / first).write_bytes(renewed[first])
    assert (destination / sorted(_FILES)[1]).read_bytes() != renewed[sorted(_FILES)[1]]

    authorization_hosted_mount.watch_projected_custody(
        source, destination, sleeper=lambda _seconds: None, ticks=1
    )

    for name, payload in renewed.items():
        assert (destination / name).read_bytes() == payload


def test_watch_leaves_a_current_destination_untouched(tmp_path: Path) -> None:
    """The control: level-triggered must not mean republishing every tick.

    Without this, the two tests above would pass against an implementation that
    simply rewrote the files unconditionally, which would churn a tmpfs and
    widen the window in which a reader can see a torn generation.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)

    # Counted, not inferred from inode identity: a filesystem is free to reuse
    # an inode number immediately after an unlink, so that comparison passes
    # against an implementation that rewrites the files on every tick.
    republishes = {"count": 0}
    real = authorization_hosted_mount.republish_projected_custody

    def counting(*args, **kwargs):
        republishes["count"] += 1
        return real(*args, **kwargs)

    authorization_hosted_mount.republish_projected_custody = counting
    try:
        authorization_hosted_mount.watch_projected_custody(
            source, destination, sleeper=lambda _seconds: None, ticks=4
        )
    finally:
        authorization_hosted_mount.republish_projected_custody = real

    assert republishes["count"] == 0


def test_republish_sweeps_staging_files_a_crashed_attempt_left_behind(
    tmp_path: Path,
) -> None:
    """Orphaned temporaries accumulate unbounded in a 256 KiB tmpfs.

    Nothing reclaims them: the failure path unlinks only what the current
    attempt staged, and a killed process unlinks nothing at all.
    """

    source = tmp_path / "source"
    wrapper = tmp_path / "destination"
    destination = wrapper / "private"
    _projected_secret(source)
    wrapper.mkdir(mode=0o700)
    authorization_hosted_mount.copy_projected_custody(source, destination)
    orphan = destination / ".keyring.json.deadbeef.tmp"
    orphan.write_bytes(b"orphaned by a crashed publish")

    renewed = {name: payload.replace(b"sentinel", b"renewed") for name, payload in _FILES.items()}
    _republished_secret(source, renewed)
    authorization_hosted_mount.republish_projected_custody(source, destination)

    assert not orphan.exists()
    assert sorted(path.name for path in destination.iterdir()) == sorted(_FILES)
