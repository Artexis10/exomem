from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest
from test_hosted_activation_delivery import NOW, bundle, response

from exomem.governance import authorization_hosted_mount as mount
from exomem.hosted_activation_delivery import CustodyDeliveryUnavailable
from exomem.hosted_activation_publisher import CustodyPublisher

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Hosted custody publisher is Linux-only")


@pytest.fixture
def publisher(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "private"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    for name, raw in bundle().items():
        path = destination / name
        path.write_bytes(raw)
        path.chmod(0o600)
    return CustodyPublisher(source, destination, now=lambda: NOW)


def test_stale_projection_cannot_erase_fast_acknowledgement(publisher, monkeypatch):
    original = bundle()
    successor = bundle(activation=2)
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: original)
    publisher.install_response(response(successor))
    assert not publisher.refresh()
    assert publisher.current().control.activation_epoch == 2
    assert publisher.current().files == successor


def test_incomparable_refresh_fetches_one_whole_authoritative_bundle(publisher, monkeypatch):
    activated = bundle(activation=2)
    renewed = bundle(membership=2)
    latest = bundle(activation=2, membership=2)
    calls = []
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: activated)
    publisher.install_response(response(activated))
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: renewed)

    def authoritative(deadline):
        calls.append(deadline)
        return response(latest)

    publisher.fetch_current = authoritative
    assert publisher.refresh()
    assert len(calls) == 1
    assert publisher.current().files == latest


@pytest.mark.parametrize("after_replace", [1, 2, 3])
def test_restart_completes_only_the_journaled_generation(publisher, monkeypatch, after_replace):
    latest = bundle(activation=2, membership=2)
    real_replace = os.replace
    count = 0

    def interrupted(source, destination, *args, **kwargs):
        nonlocal count
        result = real_replace(source, destination, *args, **kwargs)
        if str(destination).endswith(("control.json", "keyring.json", "serving-membership.json")):
            count += 1
            if count == after_replace:
                raise RuntimeError("simulated process interruption")
        return result

    monkeypatch.setattr(mount, "_projected_payloads", lambda _: bundle())
    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises((RuntimeError, CustodyDeliveryUnavailable)):
        publisher.install_response(response(latest))
    monkeypatch.setattr(os, "replace", real_replace)
    restarted = CustodyPublisher(publisher.source, publisher.destination, now=lambda: NOW)
    assert restarted.recover_pending()
    assert restarted.current().files == latest
    assert not restarted.refresh()


def test_unrelated_installed_bytes_do_not_gain_journal_authority(publisher, monkeypatch):
    real_publish = mount.publish_custody_payloads
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: bundle())

    def stop_after_journal(*args, **kwargs):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(mount, "publish_custody_payloads", stop_after_journal)
    with pytest.raises(RuntimeError):
        publisher.install_response(response(bundle(activation=2)))
    (publisher.destination / "control.json").write_bytes(b"unrelated")
    monkeypatch.setattr(mount, "publish_custody_payloads", real_publish)
    with pytest.raises(CustodyDeliveryUnavailable):
        publisher.recover_pending()
    assert (publisher.destination / "control.json").read_bytes() == b"unrelated"


def test_refresh_and_fast_delivery_share_the_same_writer(publisher, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    real_publish = mount.publish_custody_payloads
    failures = []
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: bundle())

    def slow_publish(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(mount, "publish_custody_payloads", slow_publish)

    def install():
        try:
            publisher.install_response(response(bundle(activation=2)))
        except Exception as error:  # noqa: BLE001 - surface all background test errors
            failures.append(error)

    thread = threading.Thread(target=install)
    thread.start()
    assert entered.wait(2)
    assert not publisher._lock.acquire(blocking=False)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failures == []
    assert not publisher.refresh()


def test_access_time_change_does_not_refuse_unchanged_custody(publisher):
    for name in bundle():
        path = publisher.destination / name
        original = path.stat()
        os.utime(path, ns=(1, original.st_mtime_ns))
    assert publisher.current().files == bundle()


def test_process_death_before_journal_rename_reclaims_only_its_orphan(publisher, monkeypatch):
    monkeypatch.setattr(mount, "_projected_payloads", lambda _: bundle())
    unrelated = publisher.destination / ".unrelated.tmp"
    unrelated.write_bytes(b"another subsystem")
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, "tests")
from test_hosted_activation_delivery import NOW, bundle, response
from exomem.governance import authorization_hosted_mount as mount
from exomem.hosted_activation_publisher import CustodyPublisher
mount._projected_payloads = lambda _: bundle()
real_replace = os.replace
def die_before_journal(source, destination, *args, **kwargs):
    if str(destination).endswith(".activation-ack-pending.json"):
        os._exit(71)
    return real_replace(source, destination, *args, **kwargs)
os.replace = die_before_journal
CustodyPublisher(Path(sys.argv[1]), Path(sys.argv[2]), now=lambda: NOW).install_response(response(bundle(activation=2)))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(publisher.source), str(publisher.destination)],
        timeout=10,
        capture_output=True,
    )
    assert child.returncode == 71, child.stderr.decode()
    assert list(publisher.destination.glob("..activation-ack-pending.json.*.tmp"))
    restarted = CustodyPublisher(publisher.source, publisher.destination, now=lambda: NOW)
    assert not restarted.recover_pending()
    assert not list(publisher.destination.glob("..activation-ack-pending.json.*.tmp"))
    assert unrelated.read_bytes() == b"another subsystem"
    assert restarted.current().files == bundle()
