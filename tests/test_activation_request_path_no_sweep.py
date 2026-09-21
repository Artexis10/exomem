"""The activation request path must not enumerate the vault.

Measured on a production-shaped vault (26 collection manifests, roughly 1,200
directories): `structured_collections.discover_collections` takes 54 ms in an
idle interpreter and 23,041 ms with ONE busy pure-Python thread in the same
process. The sweep is thousands of short, GIL-releasing system calls, and after
each one the sweeping thread waits up to the interpreter switch interval to get
the GIL back from a CPU-bound thread. Activation ran that sweep TWICE per
request -- `working_set._routing_targets` inside the resolve stage and
`working_set_state._records_manifests` inside the current-state stage -- which
is how two stages of a 6 s budget measured 2.15 s and 1.95 s under light
background work and 5-16 s under heavier work, against 57-65 ms offline.

Every test here is STRUCTURAL, never a timing test. A convoy is a scheduling
accident and a latency assertion about one is flaky by construction; a stage
either enumerated a directory or it did not, and it either called discovery or
it did not.

The staleness contract these also pin, because it is the price of the fix:

* matching evidence (`_routing_targets`) may be as stale as the index -- more
  or less evidence, never disclosure, and the same contract anchors already
  have;
* governed current state may NOT be stale: it takes the manifest PATHS from the
  index-published registry and loads the manifest CONTENT fresh, for only the
  collections the request actually needs.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
import threading
from pathlib import Path

import pytest
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    reserved_paths,
    structured_collections,
    working_set,
    working_set_index,
    working_set_runtime,
)
from exomem import find as find_module

TURN = "I'm planning to tow the Cargo Sled north — what are its constraints?"

COLLECTION_ID = "6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c11"

#: The filesystem-call ceiling one warm request may spend. Measured at 116 calls
#: after the fix on the fixture vault below; the ceiling sits far above that so
#: it never fails for an unrelated few dozen reads, and far below the ~3,000 a
#: single `rglob` sweep of a real vault costs -- which is the regression it
#: exists to catch.
WARM_REQUEST_FILESYSTEM_CALL_CEILING = 600


def _manifest_text(*, profile: str = "records", identifier: str = COLLECTION_ID) -> str:
    return (
        "---\n"
        "type: collection\n"
        f"exomem_id: {identifier}\n"
        "title: Depot stock\n"
        f"semantic_profile: {profile}\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  source: Items\n"
        "  format_version: 1\n"
        # Two claim terms the anchor's own title carries: `collection_claims`
        # routes on a coverage of two, so one shared word would never route.
        "claims:\n"
        "  terms: [cargo, sled, depot]\n"
        "item_schema:\n"
        "  natural_key: [observed_on, asset]\n"
        "  fields:\n"
        "    observed_on:\n"
        "      type: date\n"
        "      required: true\n"
        "    asset:\n"
        "      type: string\n"
        "      required: true\n"
        "    state:\n"
        "      type: string\n"
        "---\n"
        "\n"
        "Depot stock observations.\n"
    )


def _write_collection(vault: Path, *, profile: str = "records") -> Path:
    """Write the Records collection that claims the Cargo Sled anchor."""
    directory = vault / "Knowledge Base" / "Records" / "Depot Stock"
    (directory / "Items").mkdir(parents=True, exist_ok=True)
    manifest = directory / "_collection.md"
    manifest.write_text(_manifest_text(profile=profile), encoding="utf-8")
    (directory / "Items" / "2026-09-05.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {COLLECTION_ID}\n"
        "record_id: 2b1c4d5e-6f70-4812-9a3b-4c5d6e7f8091\n"
        "schema_version: 1\n"
        "observed_on: 2026-09-05\n"
        "asset: Cargo Sled\n"
        "state: at the northern depot\n"
        "---\n"
        "\n"
        "Observed at the depot.\n",
        encoding="utf-8",
    )
    return manifest


def _seed_without_collection(vault: Path) -> None:
    """The activation fixture, minus any Records collection.

    `_seed_structure` ships one; removing it is what lets a test add a
    collection to a vault whose index has already been built, which is the only
    way to observe when a new collection becomes visible to activation.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    shutil.rmtree(vault / "Knowledge Base" / "Records" / "Depot Stock")


def _built_index(vault: Path) -> working_set_index.WorkingSetIndex:
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    return index


def _records_state(packet: dict) -> list[dict]:
    return [
        entry
        for entry in packet.get("current_state") or ()
        if entry.get("source") == "records"
    ]


def _compile(vault: Path, index: working_set_index.WorkingSetIndex) -> dict:
    return working_set.compile_packet(vault, turn=TURN, index=index)


class _FilesystemCalls:
    """Counts the filesystem calls one request makes, process-wide.

    Process-wide rather than thread-local on purpose: a sweep moved onto a
    helper thread is still a sweep this request paid for. The tests that use it
    stub the background schedulers instead, so a count that rises is the
    request's own work and nothing else's.

    Nothing here resolves a path (no `realpath`, no `stat`): resolution would
    re-enter the very calls being counted. Attribution is a string prefix test,
    and a descriptor-relative enumeration -- `os.scandir(fd)`, which `vault.py`
    uses -- carries no path at all, so it is counted as unattributable rather
    than silently dropped. An enumeration the counter cannot place is its own
    state, not a pass.
    """

    NAMES = ("scandir", "listdir", "stat", "lstat", "open")

    def __init__(self, *roots: Path) -> None:
        self._roots = tuple(str(Path(root)) for root in roots)
        self.counts = dict.fromkeys(self.NAMES, 0)
        self.enumerated: list[str] = []
        self.unattributable = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def enumerations(self) -> int:
        return len(self.enumerated) + self.unattributable

    def report(self) -> str:
        lines = [
            f"{self.total} filesystem calls " + ", ".join(
                f"{name}={self.counts[name]}" for name in self.NAMES
            ),
            f"{self.enumerations} enumerations of the vault "
            f"({self.unattributable} unattributable)",
        ]
        lines += sorted(set(self.enumerated))[:40]
        return "\n".join(lines)

    @staticmethod
    def _text(target: object) -> str | None:
        if isinstance(target, int):
            return None
        try:
            raw = os.fspath(target)  # type: ignore[arg-type]
        except TypeError:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return raw

    def _note_enumeration(self, target: object) -> None:
        text = self._text(target)
        if text is None:
            self.unattributable += 1
            return
        for root in self._roots:
            if text == root or text.startswith(root + os.sep):
                self.enumerated.append(text)
                return

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_scandir = os.scandir
        real_listdir = os.listdir
        real_stat = os.stat
        real_lstat = os.lstat
        real_open = builtins.open

        def counting_scandir(path=".", *args: object, **kwargs: object):
            self.counts["scandir"] += 1
            self._note_enumeration(path)
            return real_scandir(path, *args, **kwargs)

        def counting_listdir(path=None, *args: object, **kwargs: object):
            self.counts["listdir"] += 1
            self._note_enumeration("." if path is None else path)
            return real_listdir(path, *args, **kwargs)

        def counting_stat(path, *args: object, **kwargs: object):
            self.counts["stat"] += 1
            return real_stat(path, *args, **kwargs)

        def counting_lstat(path, *args: object, **kwargs: object):
            self.counts["lstat"] += 1
            return real_lstat(path, *args, **kwargs)

        def counting_open(*args: object, **kwargs: object):
            self.counts["open"] += 1
            return real_open(*args, **kwargs)

        monkeypatch.setattr(os, "scandir", counting_scandir)
        monkeypatch.setattr(os, "listdir", counting_listdir)
        monkeypatch.setattr(os, "stat", counting_stat)
        monkeypatch.setattr(os, "lstat", counting_lstat)
        monkeypatch.setattr(builtins, "open", counting_open)
        monkeypatch.setattr(io, "open", counting_open)


class _DiscoveryCounter:
    """Counts collection-discovery sweeps, at both of its entry points."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_discover = structured_collections.discover_collections
        real_with_errors = structured_collections.discover_collections_with_errors

        def counting_discover(*args: object, **kwargs: object):
            self.calls.append("discover_collections")
            return real_discover(*args, **kwargs)

        def counting_with_errors(*args: object, **kwargs: object):
            self.calls.append("discover_collections_with_errors")
            return real_with_errors(*args, **kwargs)

        monkeypatch.setattr(
            structured_collections, "discover_collections", counting_discover
        )
        monkeypatch.setattr(
            structured_collections,
            "discover_collections_with_errors",
            counting_with_errors,
        )


def _no_background_walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the two schedulers that would start a vault-walking thread.

    A background build is legitimate production behaviour, but a test that let
    one start would be measuring a race rather than a request -- and a thread
    that walks the vault would both pollute the counter and outlive the test.
    Recorded rather than silently dropped: a request that SCHEDULED a build was
    not warm, and a zero-enumeration count for it would prove nothing.
    """
    scheduled: list[str] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root: scheduled.append("index")
    )
    monkeypatch.setattr(
        reserved_paths,
        "schedule_identity_catalogue_warm",
        lambda root: scheduled.append("identity"),
    )
    return scheduled


def _warm_activation(vault: Path, warm_managed_cell) -> None:
    """Put the vault in the state a live managed cell serves activation from."""
    warm_managed_cell(vault)
    reserved_paths._baseline_identity_catalogue(vault)
    stamp = working_set_runtime._key_text(
        find_module.FreshnessSnapshot(vault).projection_key("kb")
    )
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild(freshness_stamp=stamp)


# --------------------------------------------------------------------------- #
# T1, T2, T8 -- the request path itself
# --------------------------------------------------------------------------- #


def test_warm_activation_request_enumerates_no_directory(
    vault: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell
) -> None:
    """T1/T2/T8: a warm request walks no directory, runs no discovery sweep,
    and spends a pinned, bounded number of filesystem calls."""
    _seed_structure(vault)
    _seed_planning(vault)
    _write_collection(vault)
    _warm_activation(vault, warm_managed_cell)

    scheduled = _no_background_walks(monkeypatch)
    discovery = _DiscoveryCounter()
    discovery.install(monkeypatch)
    calls = _FilesystemCalls(vault)
    calls.install(monkeypatch)

    packet = commands.op_activate_context(vault, turn=TURN)

    assert scheduled == [], (
        "the request was not warm: it scheduled background work, so a zero "
        f"enumeration count would prove nothing ({scheduled})"
    )
    assert packet["abstained"] is False, packet.get("abstention")
    assert calls.enumerations == 0, calls.report()
    assert discovery.calls == [], (
        "a warm activation request must read the manifests the index already "
        f"discovered, never sweep for them again: {discovery.calls}"
    )
    assert calls.total <= WARM_REQUEST_FILESYSTEM_CALL_CEILING, calls.report()


# --------------------------------------------------------------------------- #
# T3, T4, T5 -- the staleness contract the fix buys
# --------------------------------------------------------------------------- #


def test_added_collection_is_visible_only_after_the_next_index_update(
    vault: Path,
) -> None:
    """T3: resolution evidence and current state may be as stale as the index.

    A collection added to the vault is not visible to activation until the
    index update that discovers it. That is the same contract anchors already
    have -- a page added after the last update is not an anchor yet either --
    and it is what lets the request path stop sweeping.
    """
    _seed_without_collection(vault)
    index = _built_index(vault)

    assert _records_state(_compile(vault, index)) == []

    _write_collection(vault)

    assert _records_state(_compile(vault, index)) == [], (
        "a collection the index has not discovered yet must not appear in a "
        "packet: activation reads the index's manifests, not the disk's"
    )

    index.update()
    entries = _records_state(_compile(vault, index))

    assert [entry["statement"] for entry in entries] == ["state: at the northern depot"]


def test_removed_collection_yields_no_rows_rather_than_raising(vault: Path) -> None:
    """T4: a manifest that vanished fails closed for its own collection only.

    Between the removal and the next index update the registry still names the
    manifest. Loading it fresh is what proves it gone, and the packet still
    serves -- fail closed for that collection, never for the request.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    index = _built_index(vault)

    assert _records_state(_compile(vault, index))

    shutil.rmtree(vault / "Knowledge Base" / "Records" / "Depot Stock")

    packet = _compile(vault, index)

    assert packet["abstained"] is False, packet.get("abstention")
    assert _records_state(packet) == []

    index.update()
    after = _compile(vault, index)

    assert after["abstained"] is False, after.get("abstention")
    assert _records_state(after) == []


def test_governance_relevant_manifest_edit_is_honoured_without_an_index_update(
    vault: Path,
) -> None:
    """T5: governed current state reads its manifest fresh, every request.

    Changing a collection's semantic profile changes which governance applies
    to it: a Records query may not run against a collection that is no longer a
    Records collection. That decision is taken from the manifest's CONTENT, so
    it must not wait for an index update -- unlike the routing evidence above,
    which may.
    """
    _seed_without_collection(vault)
    manifest = _write_collection(vault)
    index = _built_index(vault)

    assert _records_state(_compile(vault, index))

    manifest.write_text(_manifest_text(profile="planning"), encoding="utf-8")

    packet = _compile(vault, index)

    assert packet["abstained"] is False, packet.get("abstention")
    assert _records_state(packet) == [], (
        "a Records current-state query ran against a collection whose manifest "
        "no longer declares the records profile"
    )


# --------------------------------------------------------------------------- #
# T6, T7 -- the registry itself
# --------------------------------------------------------------------------- #


def test_registry_never_serves_a_partial_entry_and_keeps_bounded_generations(
    vault: Path,
) -> None:
    """T6: readers see a whole generation's manifests or nothing at all, and
    the registry holds at most the bounded number of generations per vault."""
    _seed_without_collection(vault)
    _write_collection(vault)
    index = _built_index(vault)
    manifests = structured_collections.discover_collections(vault)
    assert manifests

    working_set_index.reset_collection_manifests_for_tests()

    seen: list[int] = []
    stop = threading.Event()
    failures: list[str] = []

    def read() -> None:
        while not stop.is_set():
            for generation in range(1, 6):
                entry = working_set_index.published_collection_manifests(
                    vault, generation
                )
                if entry is None:
                    continue
                seen.append(len(entry))
                if len(entry) != len(manifests):
                    failures.append(f"partial entry of {len(entry)} manifests")

    readers = [threading.Thread(target=read, name=f"registry-reader-{n}") for n in range(2)]
    for reader in readers:
        reader.start()
    try:
        for generation in range(1, 6):
            working_set_index.publish_collection_manifests(vault, generation, manifests)
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=5.0)

    assert failures == [], failures
    assert seen, "the readers never observed a published generation"

    kept = [
        generation
        for generation in range(1, 6)
        if working_set_index.published_collection_manifests(vault, generation) is not None
    ]
    assert kept == [4, 5], kept
    assert len(kept) == working_set_index.MANIFEST_REGISTRY_GENERATIONS

    del index


def test_registry_served_packet_matches_the_compute_once_packet(vault: Path) -> None:
    """T7: the warm path serves exactly the packet the cold path compiles.

    The first call in a process finds no published entry for the current
    generation, computes once through the existing discovery and publishes it;
    the second is served from the registry. A caller must not be able to tell
    which one it got.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    index = _built_index(vault)

    cold = _compile(vault, index)
    warm = _compile(vault, index)

    assert cold["abstained"] is False, cold.get("abstention")
    assert cold == warm
    assert _records_state(warm)
