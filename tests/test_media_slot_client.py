"""A cell's media-slot client against the real lease service: waiting, holding and failing safe."""

from __future__ import annotations

import socket

import pytest
from test_media_slot_lease import (
    BIG,
    JOB_SECONDS,
    LEASE_SECONDS,
    SMALL,
    A,
    B,
    C,
    Clock,
    _config,
    _listening,
    _one_address_per_cell,
)

from exomem import media_slot_lease as lease_module
from exomem.media_slot_client import SlotClient


def _ask(client: SlotClient, mib: int) -> bool:
    return client.acquire("speech", mib << 20, JOB_SECONDS)


@pytest.fixture
def lease():
    """The service behind a loopback URL, past its restart hold."""
    clock = Clock()
    app = _one_address_per_cell(lease_module.MediaSlotLease(_config(), clock))
    with _listening(app) as (host, port):
        clock.now += LEASE_SECONDS
        yield f"http://{host}:{port}"


def test_a_cell_holds_a_slot_until_it_releases_and_the_next_cell_then_gets_it(lease) -> None:
    first, second = SlotClient(lease, A), SlotClient(lease, B)

    assert _ask(first, BIG) is True
    assert _ask(second, BIG) is False  # waiting in the queue
    assert _ask(second, BIG) is False
    first.release()
    assert _ask(second, BIG) is True


def test_a_cell_whose_next_job_needs_other_bytes_asks_again_for_those_bytes(lease) -> None:
    holder, waiter = SlotClient(lease, A), SlotClient(lease, B)
    assert _ask(holder, BIG) is True  # 1024 of 1280 MiB
    assert _ask(waiter, BIG) is False
    # The job that queued for 1024 MiB is gone; the next needs 640, which also does not fit
    # beside the holder, so the cell must not keep a place sized for the other job.
    assert _ask(waiter, SMALL) is False
    holder.release()
    assert _ask(waiter, SMALL) is True


def test_an_unreachable_service_is_not_a_grant_and_not_an_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = SlotClient(f"http://127.0.0.1:{port}", A)  # nothing listens here

    assert _ask(client, BIG) is False
    client.release()  # leaving a queue it never joined is a no-op


def test_a_new_worker_of_a_cell_that_still_has_an_entry_waits_for_it_to_end(lease) -> None:
    assert _ask(SlotClient(lease, A), BIG) is True  # a worker that then died
    restarted = SlotClient(lease, A)
    assert _ask(restarted, BIG) is False  # the service refuses a second entry


def test_a_cell_that_asks_again_after_its_grant_starts_a_fresh_lease(lease) -> None:
    """A second ask after a grant means the cell never received the grant reply.

    That lease began before the cell's child, so the child could outlive it while the next
    cell's grant shared the pool. The service drops it and the cell queues afresh.
    """
    first, second, third = SlotClient(lease, A), SlotClient(lease, B), SlotClient(lease, C)
    assert _ask(first, BIG) is True  # say the cell never received this reply

    assert _ask(first, BIG) is False  # the service drops the grant the cell never learned of
    assert _ask(second, BIG) is True  # so the pool is free for the next cell
    assert _ask(first, BIG) is False  # and the first cell waits behind it with a new request
    assert _ask(third, BIG) is False
