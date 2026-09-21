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
import time
from pathlib import Path

import pytest
from conftest import _VAULT_WALKING_THREAD_NAMES
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    reserved_paths,
    state_paths,
    structured_collections,
    working_set,
    working_set_index,
    working_set_runtime,
)
from exomem import find as find_module

TURN = "I'm planning to tow the Cargo Sled north — what are its constraints?"

COLLECTION_ID = "6f0f2b4c-1d3f-4a71-9c3d-2f9b5d2a7c11"

#: The directory-enumeration ceiling for one warm request, and the tripwire a
#: reintroduced sweep has to trip. Measured on the fixture vault below: 72
#: enumerations before the manifests moved off the request path, 3 after -- all
#: three inside the one collection the request's own answer came from. The
#: ceiling is generous against those 3 and still an order of magnitude under
#: the 72, so an unrelated extra read never fails it and a returning sweep
#: always does.
WARM_REQUEST_ENUMERATION_CEILING = 8

#: The total filesystem-call ceiling for one warm request, on the request
#: thread, after the background lanes the fixture started have drained.
#:
#: The convoy is paid PER GIL-RELEASING SYSTEM CALL, not per enumeration, so
#: removing the manifest sweep was only half the bill. Measured on this
#: fixture, same method for all three: 2,667 calls before either repair
#: (scandir 59, stat 133, lstat 2,382, open 93); 2,472 with the manifests off
#: the request path (scandir 3, lstat 2,246); 587 once the vault's state
#: location is resolved once per request instead of 72 times (scandir 3, stat
#: 131, lstat 361, open 92) — and 673 once independent review had the
#: placement VALIDATION taken back out of the memo (scandir 3, stat 131, lstat
#: 446, open 93), which is what that refusal costs when it is re-decided on
#: every call instead of once.
#:
#: The ceiling is a little under twice the 673 measured. Two reasons for that much
#: slack rather than less: several remaining `Path.resolve()` sites still cost
#: one `lstat` per path component, so the number moves with how deep the
#: temporary directory is on the machine running the suite; and a ceiling that
#: fails for a few dozen unrelated extra reads would be a false alarm in
#: someone else's lane. It is still less than half of either pre-repair
#: number, which is what it is for.
WARM_REQUEST_FILESYSTEM_CALL_CEILING = 1200


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


def _drain_background_walks(timeout: float = 10.0) -> None:
    """Let the warm-up's own vault-walking threads finish before measuring.

    The warm-up writes: it seeds the watcher, rebuilds the lexical store and
    builds the activation index, and a governed write legitimately starts a
    background graph rebuild. None of that is the request's cost, and the
    counter below already excludes it by counting only the request thread —
    but a rebuild that lands mid-request can still move the index generation
    under it, which would send the request to compute a manifest set it should
    have been served. Draining first removes that race too.

    Only the threads that WALK — `conftest`'s own list, which is where a new
    vault-walking daemon has to be declared. Joining every `exomem-` thread
    instead waits on the long-lived ones that never exit (the reaper, the mode
    watcher), which is a suite timeout rather than a drain. Best-effort by
    design: if a walk is genuinely still running the measurement proceeds and
    the thread filter keeps it honest.
    """
    deadline = time.monotonic() + timeout
    while True:
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name in _VAULT_WALKING_THREAD_NAMES and thread.is_alive()
        ]
        if not alive or time.monotonic() >= deadline:
            return
        for thread in alive:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


class _FilesystemCalls:
    """Counts the filesystem calls one request makes on the request thread.

    The request thread is the contract: a stage either enumerated a directory
    before answering or it did not, and a background lane that some earlier
    write started is not this request's cost. This is the same distinction
    `conftest.ScopeWalkSentinel` draws with `current_thread_only`, for the same
    reason — measuring every thread made the count a race with the graph
    rebuild rather than a fact about the request. The escape it appears to
    leave, "move the sweep to a helper thread", is closed separately and more
    directly: the discovery counter is process-wide, and the test asserts the
    request scheduled no background build at all.

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
        self._owner = threading.get_ident()
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

        def mine() -> bool:
            return threading.get_ident() == self._owner

        def counting_scandir(path=".", *args: object, **kwargs: object):
            if mine():
                self.counts["scandir"] += 1
                self._note_enumeration(path)
            return real_scandir(path, *args, **kwargs)

        def counting_listdir(path=None, *args: object, **kwargs: object):
            if mine():
                self.counts["listdir"] += 1
                self._note_enumeration("." if path is None else path)
            return real_listdir(path, *args, **kwargs)

        def counting_stat(path, *args: object, **kwargs: object):
            if mine():
                self.counts["stat"] += 1
            return real_stat(path, *args, **kwargs)

        def counting_lstat(path, *args: object, **kwargs: object):
            if mine():
                self.counts["lstat"] += 1
            return real_lstat(path, *args, **kwargs)

        def counting_open(*args: object, **kwargs: object):
            if mine():
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
    """T1/T2/T8: a warm request sweeps nothing.

    "Nothing" is exact: the only directory it may enumerate is the storage of
    the one collection its own answer came from — a markdown-items collection
    is READ by listing its items, so that enumeration is the answer, bounded by
    that collection and nothing else. Every other directory is a sweep, and a
    sweep is what turns a 54 ms call into a 23 s one on a busy interpreter.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _write_collection(vault)
    _warm_activation(vault, warm_managed_cell)
    _drain_background_walks()

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
    assert _records_state(packet), (
        "the measured request must actually reach the Records current-state "
        "lookup, or it proves nothing about the sweep that lookup used to run"
    )

    assert discovery.calls == [], (
        "a warm activation request must read the manifests the index already "
        f"discovered, never sweep for them again: {discovery.calls}"
    )
    storage = str(vault / "Knowledge Base" / "Records" / "Depot Stock")
    outside = [
        path
        for path in calls.enumerated
        if path != storage and not path.startswith(storage + os.sep)
    ]
    assert outside == [], calls.report()
    assert calls.unattributable == 0, calls.report()
    assert calls.enumerations <= WARM_REQUEST_ENUMERATION_CEILING, calls.report()


def test_warm_activation_request_resolves_the_state_location_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell
) -> None:
    """The request resolves where its state lives once, and spends a pinned
    number of filesystem calls in total.

    `vault_state_key` resolves the vault path, which costs one `lstat` per
    component and was being paid by every caller that needed a state
    directory — hundreds of times in one request. The answer cannot change
    within a request: it is a placement decision about configuration, not an
    observation of content. So it is computed once per request and reused,
    and this pins that it is.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _write_collection(vault)
    _warm_activation(vault, warm_managed_cell)
    _drain_background_walks()

    scheduled = _no_background_walks(monkeypatch)
    key_calls: list[str] = []
    real_key = state_paths.vault_state_key

    def counting_key(vault_root: Path) -> str:
        # Own vault only: the patch is process-wide, and a background thread an
        # earlier test left running resolves ITS vault through this same hook.
        if str(vault_root) == str(vault):
            key_calls.append(str(vault_root))
        return real_key(vault_root)

    monkeypatch.setattr(state_paths, "vault_state_key", counting_key)
    calls = _FilesystemCalls(vault)
    calls.install(monkeypatch)

    packet = commands.op_activate_context(vault, turn=TURN)

    assert scheduled == [], scheduled
    assert packet["abstained"] is False, packet.get("abstention")
    assert len(key_calls) <= 1, (
        "the vault's state location was resolved from scratch "
        f"{len(key_calls)} times in one request: {sorted(set(key_calls))}"
    )
    storage = str(vault / "Knowledge Base" / "Records" / "Depot Stock")
    outside = [
        path
        for path in calls.enumerated
        if path != storage and not path.startswith(storage + os.sep)
    ]
    assert outside == [], calls.report()
    assert calls.total <= WARM_REQUEST_FILESYSTEM_CALL_CEILING, calls.report()


def test_resolution_scope_holds_no_cache_outside_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a scope the memo does not exist: every call does the full work.

    The memo's whole safety argument is that its lifetime is one request. A
    module-level cache with the same contents would be a different, far more
    dangerous object — it would answer for a vault that had since moved, in a
    process that never asked — so "no scope, no memo" is pinned directly.
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    calls: list[str] = []
    real_key = state_paths.vault_state_key

    def counting_key(root: Path) -> str:
        # Own vault only: the patch is process-wide, and a background thread an
        # earlier test left running resolves ITS vault through this same hook.
        if str(root) == str(vault_root):
            calls.append(str(root))
        return real_key(root)

    monkeypatch.setattr(state_paths, "vault_state_key", counting_key)

    first = state_paths.vault_state_dir(vault_root)
    second = state_paths.vault_state_dir(vault_root)

    assert first == second
    assert len(calls) == 2, calls

    calls.clear()
    with state_paths.resolution_scope():
        scoped_first = state_paths.vault_state_dir(vault_root)
        scoped_second = state_paths.vault_state_dir(vault_root)
    assert scoped_first == scoped_second == first
    assert len(calls) == 1, calls

    calls.clear()
    state_paths.vault_state_dir(vault_root)
    assert len(calls) == 1, "the scope outlived itself"


def test_resolution_scope_does_not_leak_into_another_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request's memo is one request's. A thread that opened no scope of
    its own does the full work, so nothing can be answered from a scope it was
    never part of."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    calls: list[str] = []
    real_key = state_paths.vault_state_key

    def counting_key(root: Path) -> str:
        # Own vault only: the patch is process-wide, and a background thread an
        # earlier test left running resolves ITS vault through this same hook.
        if str(root) == str(vault_root):
            calls.append(str(root))
        return real_key(root)

    monkeypatch.setattr(state_paths, "vault_state_key", counting_key)

    with state_paths.resolution_scope():
        state_paths.vault_state_dir(vault_root)
        state_paths.vault_state_dir(vault_root)
        assert len(calls) == 1, calls

        other: list[int] = []

        def elsewhere() -> None:
            state_paths.vault_state_dir(vault_root)
            state_paths.vault_state_dir(vault_root)
            other.append(len(calls))

        thread = threading.Thread(target=elsewhere, name="scope-leak-probe")
        thread.start()
        thread.join(timeout=10.0)

    assert other == [3], (
        "a thread with no scope of its own must do the full resolution every "
        f"time, so the counter should have advanced by two: {calls}"
    )


def test_nested_resolution_scopes_share_one_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inner scope reuses the outer memo rather than starting a second one,
    and leaving the inner scope does not discard what the outer one holds."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    calls: list[str] = []
    real_key = state_paths.vault_state_key

    def counting_key(root: Path) -> str:
        # Own vault only: the patch is process-wide, and a background thread an
        # earlier test left running resolves ITS vault through this same hook.
        if str(root) == str(vault_root):
            calls.append(str(root))
        return real_key(root)

    monkeypatch.setattr(state_paths, "vault_state_key", counting_key)

    with state_paths.resolution_scope():
        state_paths.vault_state_dir(vault_root)
        with state_paths.resolution_scope():
            state_paths.vault_state_dir(vault_root)
        state_paths.vault_state_dir(vault_root)

    assert len(calls) == 1, calls


def test_a_failed_resolution_is_never_memoised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is re-decided every time it is asked for.

    Memoising a raised placement refusal would let one request's first failure
    answer for the rest of it, and the direction of that error is the unsafe
    one: this validation is what stops machine-local state being written inside
    the vault.
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    attempts: list[str] = []
    real_validate = state_paths.validate_vault_state_directory

    def refusing(root: Path, directory: Path) -> Path:
        attempts.append(str(directory))
        raise ValueError("EXOMEM_STATE_ROOT must resolve outside the vault")

    monkeypatch.setattr(state_paths, "validate_vault_state_directory", refusing)

    with state_paths.resolution_scope():
        for _ in range(3):
            with pytest.raises(ValueError):
                state_paths.vault_state_dir(vault_root)

    assert len(attempts) == 3, attempts
    assert real_validate is not None


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


def test_registry_served_packet_matches_the_compute_once_packet(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T7: the warm path serves exactly the packet the cold path compiles.

    The first call in a process finds no published entry for the current
    generation, computes once through the existing discovery and publishes it;
    the second is served from the registry. A caller must not be able to tell
    which one it got.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    index = _built_index(vault)
    # The rebuild above publishes, so without this the "cold" compile is a
    # registry HIT and the comparison is between two hits -- a tautology, and
    # the one path that still runs discovery on a request thread would have no
    # coverage at all.
    working_set_index.reset_collection_manifests_for_tests()

    misses: list[int] = []
    real_published = working_set_index.published_collection_manifests

    def counting(vault_root: Path, generation: int, **kwargs: object):
        entry = real_published(vault_root, generation, **kwargs)
        if entry is None:
            misses.append(generation)
        return entry

    monkeypatch.setattr(working_set_index, "published_collection_manifests", counting)

    cold = _compile(vault, index)
    cold_misses = list(misses)
    warm = _compile(vault, index)

    assert cold_misses, "the first compile was served from the registry, not computed"
    assert misses == cold_misses, "the second compile missed too; it was not warm"
    assert cold["abstained"] is False, cold.get("abstention")
    assert cold == warm
    assert _records_state(warm)


# --------------------------------------------------------------------------- #
# Independent review, round 2 -- what the corrections have to hold
# --------------------------------------------------------------------------- #


def test_a_state_root_flipped_into_the_vault_is_refused_on_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The placement refusal must observe the filesystem, memo or no memo.

    `ensure_vault_state_dir` validates AFTER the hosted creation on purpose, so
    that check has to see the world as it is when it runs. A memoised
    validation cannot: the reviewer flipped a symlinked state root into the
    vault between two calls and the second was allowed inside a scope while
    being refused outside one. Placement is what keeps machine-local state out
    of the vault, so the refusal is re-decided every time it is asked for.
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    inside = vault_root / "state"
    inside.mkdir()
    link = tmp_path / "state-root"
    link.symlink_to(elsewhere)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(link))

    with state_paths.resolution_scope():
        state_paths.ensure_vault_state_dir(vault_root)
        link.unlink()
        link.symlink_to(inside)
        with pytest.raises(ValueError):
            state_paths.ensure_vault_state_dir(vault_root)

    # And identically with no scope at all, which is the behaviour being kept.
    link.unlink()
    link.symlink_to(elsewhere)
    state_paths.ensure_vault_state_dir(vault_root)
    link.unlink()
    link.symlink_to(inside)
    with pytest.raises(ValueError):
        state_paths.ensure_vault_state_dir(vault_root)


def test_publishing_an_older_generation_never_evicts_a_newer_one(vault: Path) -> None:
    """The registry keeps the HIGHEST generations, not the most recent writes.

    Evicting by publish recency let a slow request that had captured an older
    generation miss, sweep, republish under its own stale number and push the
    live generations out — putting the sweep it had just paid for back on the
    next request thread. Two stale republishes were enough to hold nothing but
    stale entries.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()

    working_set_index.publish_collection_manifests(vault, 5, manifests)
    working_set_index.publish_collection_manifests(vault, 6, manifests)
    working_set_index.publish_collection_manifests(vault, 4, manifests)
    working_set_index.publish_collection_manifests(vault, 3, manifests)

    kept = [
        generation
        for generation in range(1, 8)
        if working_set_index.published_collection_manifests(vault, generation) is not None
    ]
    assert kept == [5, 6], kept


def test_a_below_highest_miss_serves_the_request_without_storing_anything(
    vault: Path,
) -> None:
    """A straggler computes for itself and leaves the registry alone.

    Storing its answer would evict nothing under the rule above, but it would
    still be a write on behalf of a generation nobody is serving any more. The
    request gets its manifests; the registry keeps holding what the index
    published.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()
    working_set_index.publish_collection_manifests(vault, 9, manifests)

    served = working_set_index.collection_manifests(vault, 4)

    assert [m.path for m in served] == [m.path for m in manifests]
    assert working_set_index.published_collection_manifests(vault, 4) is None
    assert working_set_index.published_collection_manifests(vault, 9) is not None


def test_two_spellings_of_one_vault_share_a_registry_entry(vault: Path) -> None:
    """A vault is its resolved path, not the spelling a caller happened to use.

    Keyed on the unresolved path, a symlinked or dotted spelling silently gets
    its own entry: every request through it misses, sweeps, and publishes into
    a second copy nothing else reads.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()

    working_set_index.publish_collection_manifests(vault, 11, manifests)
    other_spelling = vault / ".." / vault.name

    assert working_set_index.published_collection_manifests(other_spelling, 11) is not None


def test_a_failed_discovery_does_not_erase_another_update_s_pending_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One update's discovery failure is its own, not the other update's.

    The set an update discovered was held per vault, so a concurrent update
    whose discovery failed cleared the slot the first one was about to publish
    — and that generation then had no entry at all, sending every request at it
    back to the sweep.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    working_set_index.reset_collection_manifests_for_tests()

    real_discover = structured_collections.discover_collections
    real_projects = working_set_index._project_candidates
    failed_update_ran = threading.Event()
    discovery_held = threading.Event()

    def branching(root: Path, **kwargs: object):
        if threading.current_thread().name == "failing-update":
            # Fail only once the other update is holding its discovered set.
            discovery_held.wait(timeout=30)
            raise structured_collections.CollectionError("BOOM", "discovery failed")
        return real_discover(root, **kwargs)

    def pausing(vault_root: Path, member_paths):
        if threading.current_thread().name == "succeeding-update":
            # Hold this update AFTER its discovery and before its publish, so
            # the other update's failure lands squarely in between.
            discovery_held.set()
            failed_update_ran.wait(timeout=30)
        return real_projects(vault_root, member_paths)

    monkeypatch.setattr(structured_collections, "discover_collections", branching)
    monkeypatch.setattr(working_set_index, "_project_candidates", pausing)

    reports: dict[str, dict] = {}

    def succeeding() -> None:
        reports["ok"] = working_set_index.WorkingSetIndex(vault).rebuild()

    def failing() -> None:
        try:
            reports["failed"] = working_set_index.WorkingSetIndex(vault).rebuild()
        finally:
            failed_update_ran.set()

    winner = threading.Thread(target=succeeding, name="succeeding-update")
    loser = threading.Thread(target=failing, name="failing-update")
    winner.start()
    loser.start()
    loser.join(timeout=60)
    winner.join(timeout=60)

    generation = reports["ok"]["generation"]
    published = working_set_index.published_collection_manifests(
        vault, generation, token=working_set_index.WorkingSetIndex(vault).token()
    )

    assert published is not None, (
        "the update that discovered the manifests published nothing: another "
        "update's failure cleared the set it was holding"
    )
    assert [manifest.path for manifest in published]


# --------------------------------------------------------------------------- #
# The registry is keyed on the sidecar, not on a number
# --------------------------------------------------------------------------- #


def _rebuilt(vault: Path, times: int) -> working_set_index.WorkingSetIndex:
    """An index whose generation counter has been pushed past 1."""
    index = working_set_index.WorkingSetIndex(vault)
    for _ in range(times):
        index.rebuild()
    return index


def test_a_recreated_sidecar_is_served_rather_than_stranded(vault: Path) -> None:
    """A restarted generation counter must not strand every request.

    Deleting the sidecar restarts the counter at 1 while the registry still
    holds the dead sidecar's higher numbers. Keyed on the number alone, the
    rebuild's publish was evicted on arrival and every request after it missed
    AND declined to store — the sweep back on the request thread, for good,
    which is the whole defect this work removes. Keyed on the sidecar's own
    identity, the new sidecar's entries simply replace the dead one's.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    working_set_index.reset_collection_manifests_for_tests()

    index = _rebuilt(vault, 3)
    dead_token = index.token()
    assert dead_token[1] >= 2, dead_token
    assert (
        working_set_index.published_collection_manifests(
            vault, dead_token[1], token=dead_token
        )
        is not None
    )

    index.reset()
    index.rebuild()
    live_token = index.token()

    assert live_token[2] != dead_token[2], "the sidecar was not actually recreated"
    assert (
        working_set_index.published_collection_manifests(
            vault, live_token[1], token=live_token
        )
        is not None
    ), "the recreated sidecar's own rebuild was evicted by the dead one's numbers"
    assert (
        working_set_index.published_collection_manifests(
            vault, dead_token[1], token=dead_token
        )
        is None
    ), "the dead sidecar's manifests are still being served"


def test_a_dead_sidecar_s_manifests_are_never_served_to_a_live_one(
    vault: Path,
) -> None:
    """Same path, different sidecar: the entries do not carry over.

    A vault deleted and recreated at the same path — or any sidecar rebuilt
    from scratch — gets a new identity, and manifests discovered for the old
    one describe a tree that no longer exists. They are not evidence about
    this vault and are not served as if they were.
    """
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()

    old_token = (1, 4, 111_111)
    new_token = (1, 4, 222_222)
    working_set_index.publish_collection_manifests(
        vault, old_token[1], manifests, token=old_token
    )

    assert (
        working_set_index.published_collection_manifests(
            vault, new_token[1], token=new_token
        )
        is None
    )


def test_a_reader_holding_a_dead_token_stores_nothing_and_evicts_nothing(
    vault: Path,
) -> None:
    """A request whose token died under it serves itself and leaves the
    registry alone — it cannot prove its own answer is the current one."""
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()

    live_token = (1, 7, 333_333)
    dead_token = (1, 7, 444_444)
    working_set_index.publish_collection_manifests(
        vault, live_token[1], manifests, token=live_token
    )

    served = working_set_index.collection_manifests(vault, dead_token[1], token=dead_token)

    assert [m.path for m in served] == [m.path for m in manifests]
    assert (
        working_set_index.published_collection_manifests(
            vault, dead_token[1], token=dead_token
        )
        is None
    ), "the dead-token reader stored its answer"
    assert (
        working_set_index.published_collection_manifests(
            vault, live_token[1], token=live_token
        )
        is not None
    ), "the dead-token reader evicted the live sidecar's entry"


def test_generation_rules_still_hold_within_one_sidecar(vault: Path) -> None:
    """Inside one sidecar identity, the generation rules are unchanged: the
    highest generations are kept and a below-highest miss stores nothing."""
    _seed_without_collection(vault)
    _write_collection(vault)
    manifests = structured_collections.discover_collections(vault)
    working_set_index.reset_collection_manifests_for_tests()
    token = (1, 0, 555_555)

    for generation in (5, 6, 4, 3):
        working_set_index.publish_collection_manifests(
            vault, generation, manifests, token=(token[0], generation, token[2])
        )

    kept = [
        generation
        for generation in range(1, 8)
        if working_set_index.published_collection_manifests(
            vault, generation, token=(token[0], generation, token[2])
        )
        is not None
    ]
    assert kept == [5, 6], kept

    working_set_index.collection_manifests(vault, 4, token=(token[0], 4, token[2]))
    assert (
        working_set_index.published_collection_manifests(
            vault, 4, token=(token[0], 4, token[2])
        )
        is None
    )


def test_a_caller_with_no_scope_pays_nothing_for_the_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a scope the memo must cost nothing, not merely do nothing.

    Every placement resolution in the codebase now goes through this wrapper,
    including on paths that never open a scope. Building the memo key for them
    measured +5.3us per call against a 15.8us resolution — a third more, paid
    by callers that cannot use the answer. The key is built only when there is
    somewhere to put it.
    """
    built: list[int] = []
    real_environment = state_paths._placement_environment

    def counting() -> tuple[str, ...]:
        built.append(1)
        return real_environment()

    monkeypatch.setattr(state_paths, "_placement_environment", counting)

    state_paths.resolved_vault_path(tmp_path)
    state_paths.vault_state_dir(tmp_path)

    assert built == [], "a caller with no memo built the memo key anyway"

    with state_paths.resolution_scope():
        state_paths.resolved_vault_path(tmp_path)

    assert built, "inside a scope the key is what keeps the answer honest"
