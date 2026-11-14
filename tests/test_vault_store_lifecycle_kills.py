"""Hard kills of the store-mode lifecycle writers, each followed by a fresh process and a write.

OpenSpec make-vault-store-canonical task 2.4. Each case runs one lifecycle write in a spawned
interpreter that the operating system kills (SIGKILL) at one durable step: a file placed before the
store commit, the commit, a rename after it, or a later step. A second fresh interpreter then starts
as the service starts, does what a served start does (the projector's temporary sweep and pass and
the importer's sweep, in either order, then the maintenance reconcile) and makes the next write
that a caller would make, each through the service's door to the store thread. The parent only
reads the store and the disk.

Every case of one test shares one vault and works on its own folder with its own bytes, so the
importer cannot pair a file of one case with a record of another.
"""

from __future__ import annotations

import functools
import hashlib
import json
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_vault_store_read_write as read_write
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import RELEASED, vault  # noqa: F401

from exomem import (
    delete_directory,
    delete_file,
    extraction_parts,
    move_file,
    recover_from_trash,
    reserved_paths,
)
from exomem.collection_store import capability, connection, preview
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import membership, policy
from exomem.governance.principal import library_scope
from exomem.vault_store import binaries, governed, importer, ownership, projector, write
from exomem.vault_store import media as store_media

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

stored = read_write.stored

KB = "Knowledge Base"
TRASH = f"{KB}/_trash/"
CASES = f"{KB}/Kills"
SIDECAR = ".meta.json"


# -- the vault of one case ---------------------------------------------------------------------


def folder_of(case: str) -> str:
    return f"{CASES}/{case}"


def moved_folder_of(case: str) -> str:
    return f"{CASES}/{case}-moved"


def _record(root: Path, rel: str) -> write.BinaryRecord:
    raw = (root / rel).read_bytes()
    return write.BinaryRecord(rel, hashlib.sha256(raw).hexdigest(), len(raw))


def build_folder(root: Path, writer: CollectionWriter, case: str) -> None:
    """A folder of one stored page, a recorded binary, a binary that nothing imported and a dot
    folder holding one part of the page's extraction, each with bytes of this case alone."""
    folder = folder_of(case)
    plan = f"{folder}/Plan.md"
    chart = f"{folder}/chart.bin"
    (root / folder / "drafts").mkdir(parents=True)
    (root / chart).write_bytes(f"chart of {case}".encode())
    (root / folder / "drafts/sketch.bin").write_bytes(f"sketch of {case}".encode())
    body = f"---\ntitle: Plan {case}\ntype: note\n---\n# Plan {case}\n\nThe works plan.\n"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "a case folder",
            puts=(write.PagePut(plan, body.encode()),),
            binaries=(_record(root, chart),),
        ),
    )
    part = root / extraction_parts.part_path(Path(plan), 2)
    part.parent.mkdir(parents=True)
    part.write_text(f"the second part of {case}\n", encoding="utf-8")


def build_binary(root: Path, writer: CollectionWriter, case: str) -> None:
    """A folder that holds one recorded binary and nothing else."""
    chart = f"{folder_of(case)}/chart.bin"
    (root / chart).parent.mkdir(parents=True)
    (root / chart).write_bytes(f"chart of {case}".encode())
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "a case binary", binaries=(_record(root, chart),)
        ),
    )


def build_artifact(root: Path, writer: CollectionWriter, case: str) -> None:
    """A paired artifact: a recorded binary, the stored page that names it, and a dot folder that
    holds one part of the page's extraction."""
    folder = folder_of(case)
    report = f"{folder}/report.bin"
    (root / folder).mkdir(parents=True)
    (root / moved_folder_of(case)).mkdir(parents=True)
    (root / report).write_bytes(f"report of {case}".encode())
    page = (
        f"---\ntitle: Report {case}\ntype: note\nevidence_file: {report}\n---\n# Report {case}\n"
    )
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "a case artifact",
            puts=(write.PagePut(f"{report}.md", page.encode()),),
            binaries=(_record(root, report),),
        ),
    )
    part = root / extraction_parts.part_path(Path(f"{report}.md"), 2)
    part.parent.mkdir(parents=True)
    part.write_text(f"the second part of {case}\n", encoding="utf-8")


def govern(root: Path, cases: list[str]) -> None:
    """A real policy file pair that restricts each case's page."""
    governance = root / KB / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir(parents=True)
    paths = ", ".join(f'"Kills/{case}/Plan.md"' for case in cases)
    (governance / "scopes/lifecycle.yaml").write_text(
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: [{paths}]\n",
        encoding="utf-8",
    )
    (governance / "rules/lifecycle.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 4\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()


@contextmanager
def store_writer(root: Path):
    """The store's writer as the store thread holds it, bound so a store-mode batch may commit."""
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        writer = CollectionWriter(root, handle)
        token = preview._BOUND.set((root.resolve(), writer, False))
        try:
            yield writer
        finally:
            preview._BOUND.reset(token)


def settle(root: Path, writer: CollectionWriter | None = None) -> None:
    """What a served start runs: the projector's sweep and pass, then the importer's sweep, twice
    so that a change the first round made is settled too. With no ``writer`` they reach the store
    through the service's door (``projector.store_thread_runner``)."""
    run = None if writer is None else functools.partial(_run_on, writer)
    projecting = projector.Projector(root, run=run)
    projecting.sweep_temporaries()
    for _ in range(2):
        projecting.run_pass()
        importer.Importer(root, run=run).sweep()


def _run_on(writer: CollectionWriter, work: Callable[[CollectionWriter], None]) -> None:
    work(writer)


def command(root: Path, name: str, *, surface: str = "mcp", **arguments) -> str:
    """One command as the owner, through the dispatcher that routes a write to the store thread:
    ``ok`` or the refusal's code."""
    from exomem import commands, writer_lease
    from exomem.governance.principal import owner_principal, request_scope

    found = next(c for c in commands.product_commands_for(surface) if c.name == name)
    try:
        with request_scope(owner_principal(surface=surface)):
            result = writer_lease.invoke_command(found, root, **arguments)
    except Exception as error:  # noqa: BLE001 - the code is the finding
        return getattr(error, "code", None) or str(error).split(":", 1)[0]
    if isinstance(result, dict) and result.get("ok") is False:
        return str(result.get("code") or result.get("error_code") or result)
    return "ok"


def reconcile(root: Path) -> str:
    """The owner's maintenance reconcile on the host, through the served door: it settles receipts
    and lifecycle markers. A remote caller's reconcile runs the same pass as a maintenance job."""
    return command(root, "maintain_memory", surface="cli", mode="reconcile", dry_run=False)


# -- the writes ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One write of one case, killed at ``kill``. ``kind`` names the write: ``trash folder``,
    ``recover folder``, ``trash file`` or ``move artifact``."""

    name: str
    kind: str
    kill: str
    slot: str | None = None

    def arguments(self) -> dict:
        """The ``manage_memory_file`` arguments of this write, as a caller sends it."""
        folder = folder_of(self.name)
        if self.kind == "trash folder":
            return {"operation": "delete", "path": folder, "confirm": True, "recursive": True,
                    "force_orphan": True}
        if self.kind == "recover folder":
            return {"operation": "recover", "trash_path": self.slot}
        if self.kind == "trash file":
            return {"operation": "delete", "path": f"{folder}/chart.bin", "confirm": True,
                    "force_orphan": True}
        if self.kind == "move artifact":
            return {"operation": "move", "old_path": f"{folder}/report.bin",
                    "new_path": f"{moved_folder_of(self.name)}/report.bin"}
        raise ValueError(self.kind)

    def run(self, root: Path) -> None:
        folder = folder_of(self.name)
        if self.kind == "trash folder":
            delete_directory.delete_directory(
                root, path=folder, confirm=True, recursive=True, force_orphan=True
            )
        elif self.kind == "recover folder":
            assert self.slot is not None
            recover_from_trash.recover_from_trash(root, trash_path=self.slot)
        elif self.kind == "trash file":
            delete_file.delete_file(
                root, path=f"{folder}/chart.bin", confirm=True, force_orphan=True
            )
        elif self.kind == "move artifact":
            move_file.move_file(
                root,
                old_path=f"{folder}/report.bin",
                new_path=f"{moved_folder_of(self.name)}/report.bin",
            )
        else:
            raise ValueError(self.kind)


def next_write(root: Path, case: Case) -> str:
    """The write a caller makes next, through the served door: the same write again while the store
    still holds what it moves at its source, else the write that undoes it. ``ok`` or the code."""
    with closing(connection.open_reader(connection.store_path(root))) as store:
        held = [
            path
            for (path,) in store.execute(
                "SELECT path FROM store_pages UNION ALL SELECT path FROM store_binaries"
            )
        ]
    sides = {(_side(case.name, rel) or ("",))[0] for rel in held} - {""}
    if case.kind == "recover folder":
        again = case.slot is not None and any(rel.startswith(f"{case.slot}/") for rel in held)
    elif case.kind == "trash file":
        again = f"{folder_of(case.name)}/chart.bin" in held
    else:
        again = "source" in sides
    if again:
        return f"again: {command(root, 'manage_memory_file', **case.arguments())}"
    if case.kind == "recover folder":
        undo = {"operation": "delete", "path": folder_of(case.name), "confirm": True,
                "recursive": True}
    elif case.kind == "move artifact":
        undo = {"operation": "move", "old_path": f"{moved_folder_of(case.name)}/report.bin",
                "new_path": f"{folder_of(case.name)}/report.bin"}
    else:
        slots = sorted(side.removeprefix("trash ") for side in sides if side != "source")
        if len(slots) != 1:
            return f"undo: no single trash slot ({slots})"
        dated = next(rel for rel in held if f"/{slots[0]}" in rel)
        undo = {"operation": "recover", "trash_path": "/".join(dated.split("/")[:4])}
    return f"undo: {command(root, 'manage_memory_file', **undo)}"


# -- the two child interpreters -----------------------------------------------------------------


def _as_the_vault_fixture_runs(environment: dict[str, str]) -> None:
    """The process state that the vault fixture sets with ``monkeypatch``, which a spawned
    interpreter does not inherit."""
    os.environ.clear()
    os.environ.update(environment)
    from exomem import records

    capability.RELEASED = RELEASED
    records._capture_sweep_carrier = lambda *a, **kw: None
    records._due_state_carrier = lambda *a, **kw: None


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _after(module, name: str, *, nth: int = 1, counts: Callable[..., bool] | None = None) -> None:
    """Kill the process when the ``nth`` counted call of ``module.name`` has returned."""
    real = getattr(module, name)
    seen = [0]

    def wrapped(*arguments, **options):
        result = real(*arguments, **options)
        if counts is None or counts(*arguments, **options):
            seen[0] += 1
            if seen[0] == nth:
                _die()
        return result

    setattr(module, name, wrapped)


def _arm(kill: str, handle) -> None:
    """Make ``kill`` the step after which the process dies.

    ``placed``: a file that the commit records (a trash sidecar) sits in its temporary.
    ``begun``: a governed deletion's tombstone and receipt intent are written. ``commit``: the
    store transaction has written its rows and is at its revision step, before it commits.
    ``committed``: the store commit has returned, before any file moves. ``rename N``: the Nth
    rename after the commit has returned. ``pruned``, ``media`` and ``governed``: the step after
    the commit of that name has returned.
    """
    if kill == "placed":
        _after(reserved_paths, "publish_generic_bytes")
    elif kill == "begun":
        _after(governed, "begin_deletion")
    elif kill.startswith("rename "):
        _after(
            reserved_paths,
            "move_generic_path",
            nth=int(kill.split()[1]),
            counts=lambda *a, rollback=False, **k: not rollback,
        )
    elif kill == "commit":

        def trace(statement: str) -> None:
            if statement.startswith("UPDATE store_mode SET revision"):
                _die()

        handle.connection.set_trace_callback(trace)
    elif kill == "committed":
        _after(write, "commit")
    elif kill == "pruned":
        _after(binaries, "prune_empty")
    elif kill == "media":
        for name in ("after_removal", "derive"):
            _after(store_media, name)
    elif kill == "governed":
        _after(governed, "settle")
    else:
        raise ValueError(kill)


def _write_and_die(environment: dict[str, str], root: Path, case: Case) -> None:
    """One lifecycle write in a fresh interpreter that the operating system kills at its step."""
    _as_the_vault_fixture_runs(environment)
    with library_scope(), store_writer(root) as writer:
        _arm(case.kill, writer.handle)
        case.run(root)
    os._exit(0)


def _start_fresh(
    environment: dict[str, str], root: Path, cases: list[Case], report: str, order: str
) -> None:
    """A fresh interpreter that starts as the service starts (``start_server_lifecycle``), so its
    projector, importer, reconcile and next writes reach the store as a served process's do. With
    ``order`` "importer first", the importer sweeps before the projector's first pass."""
    _as_the_vault_fixture_runs(environment)
    from exomem import writer_lease
    from exomem.collection_store import runtime

    found: dict[str, dict] = {}
    manager = writer_lease.start_server_lifecycle()
    try:
        deadline = time.monotonic() + 120
        while (server := runtime._SERVERS.get(root.resolve())) is None or server.runtime is None:
            assert time.monotonic() < deadline, "the store thread did not open the store"
            time.sleep(0.05)
        with library_scope():
            if order == "importer first":
                importer.Importer(root).sweep()
            settle(root)
        reconciled = [reconcile(root)]
        with library_scope():
            settle(root)
        for case in cases:
            found[case.name] = {"kill": case.kill, "after the restart": findings(root, case.name)}
        for case in cases:
            found[case.name]["next write"] = next_write(root, case)
        with library_scope():
            settle(root)
        reconciled.append(reconcile(root))
        for case in cases:
            found[case.name]["after the next write"] = findings(root, case.name)
            found[case.name]["reconcile"] = reconciled
        # What a kill before a commit leaves: a placed file's temporary, a dot name the store does
        # not own, which no case's side counts.
        found["temporaries"] = sorted(
            path.relative_to(root).as_posix()
            for path in (root / KB).rglob(".exomem-pending-*")
        )
    finally:
        manager.close()
    Path(report).write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    os._exit(0)


# -- what a case finds --------------------------------------------------------------------------


def _side(case: str, rel: str) -> tuple[str, str] | None:
    """Which side of the case a path is on, and the path below that side's root: ``source`` is the
    case's folder, ``moved`` a move's destination folder, and ``trash <slot>`` each trash slot that
    names the case (the date folder left out)."""
    for side, folder in (("source", folder_of(case)), ("moved", moved_folder_of(case))):
        if rel.startswith(f"{folder}/"):
            return side, rel[len(folder) + 1 :]
        if rel == folder:
            return side, "(itself)"
    mark = folder_of(case).removeprefix(f"{KB}/").replace("/", "__")
    if not rel.startswith(TRASH) or "/" not in rel[len(TRASH) :]:
        return None  # a date folder, which file mode keeps too, belongs to no case
    _date, slot, *below = rel[len(TRASH) :].split("/")
    name = slot.removesuffix(SIDECAR)
    if not (name.endswith(mark) or f"{mark}__" in name or f"{mark}-" in name):
        return None
    if not below and slot.endswith(SIDECAR):
        return f"trash {name}", "(sidecar)"
    return f"trash {slot}", "/".join(below) or "(the slot itself)"


def findings(root: Path, case: str) -> dict:
    """What a case's folders and trash slots hold in the store and on disk, and each way the store
    and the disk disagree there."""
    with closing(connection.open_reader(connection.store_path(root))) as store:
        pages = [path for (path,) in store.execute("SELECT path FROM store_pages")]
        records = dict(store.execute("SELECT path, sha256 FROM store_binaries"))
        held = [path for (path,) in store.execute("SELECT path FROM store_held")]
        queued = [path for (path,) in store.execute("SELECT path FROM store_projection_queue")]
    disk: dict[str, str] = {}
    empty: list[str] = []
    for path in (root / KB).rglob("*"):
        if path.is_file():
            disk[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir() and not any(path.iterdir()):
            empty.append(path.relative_to(root).as_posix())
    sides: dict[str, dict[str, list[str]]] = {}

    def note(where: str, rel: str) -> None:
        side = _side(case, rel)
        if side is not None:
            sides.setdefault(side[0], {}).setdefault(where, []).append(side[1])

    for rel in pages:
        note("store pages", rel)
    for rel in records:
        note("store records", rel)
    for rel in disk:
        note("disk", rel)
    for rel in empty:
        note("empty folders", rel)
    mine = [rel for rel in {*pages, *records, *disk} if _side(case, rel) is not None]
    disagree = sorted(
        [
            f"record, no file: {rel}"
            for rel in mine
            if rel in records and disk.get(rel) != records[rel]
        ]
        + [
            f"file, no record: {rel}"
            for rel in mine
            if rel in disk
            and rel not in records
            and ownership.classify(rel) is ownership.Ownership.BINARY
        ]
        + [f"held: {rel}" for rel in held if rel in mine]
        + [f"queued: {rel}" for rel in queued if rel in mine]
    )
    mark = folder_of(case).removeprefix(f"{KB}/").replace("/", "__")
    markers = []
    for path in sorted((root / KB / "_Governance/deletion-tombstones").glob("*.json")):
        text = path.read_text(encoding="utf-8")
        if folder_of(case) in text or mark in text:
            marker = json.loads(text)
            markers.append(f"{marker.get('operation', '?')}: {marker.get('state', '?')}")
    return {
        "sides": {side: {k: sorted(v) for k, v in held_.items()} for side, held_ in sides.items()},
        "disagree": disagree,
        "governance markers": markers,
    }


# -- the harness --------------------------------------------------------------------------------


def kill_each(root: Path, cases: list[Case]) -> None:
    """Run each case's write in its own interpreter and check that the kill landed."""
    context = multiprocessing.get_context("spawn")
    for case in cases:
        child = context.Process(target=_write_and_die, args=(dict(os.environ), root, case))
        child.start()
        child.join(300)
        assert child.exitcode == -signal.SIGKILL, f"{case.name}: the kill at {case.kill!r} landed"


def restart(root: Path, cases: list[Case], tmp_path: Path, order: str) -> dict:
    report = tmp_path / "restart.json"
    child = multiprocessing.get_context("spawn").Process(
        target=_start_fresh, args=(dict(os.environ), root, cases, str(report), order)
    )
    child.start()
    child.join(600)
    assert child.exitcode == 0
    return json.loads(report.read_text(encoding="utf-8"))


def prepare(root: Path, plan: list[tuple[str, str, bool]]) -> list[Case]:
    """One case for each ``(kind, kill, governed)``: its folder or artifact built, a governed
    case's page restricted by policy, and a recovery's folder trashed first, all settled before
    the kills."""
    names = [f"case-{number:02d}" for number in range(len(plan))]
    slots: dict[str, str | None] = dict.fromkeys(names)
    with library_scope(), store_writer(root) as writer:
        for name, (kind, _kill, _governed) in zip(names, plan, strict=True):
            builders = {"move artifact": build_artifact, "trash file": build_binary}
            builders.get(kind, build_folder)(root, writer, name)
        governed_names = [name for name, (*_, governs) in zip(names, plan, strict=True) if governs]
        if governed_names:
            govern(root, governed_names)
        settle(root, writer)
        for name, (kind, *_rest) in zip(names, plan, strict=True):
            if kind == "recover folder":
                slots[name] = delete_directory.delete_directory(
                    root, path=folder_of(name), confirm=True, recursive=True, force_orphan=True
                ).trash_path
        settle(root, writer)
    return [
        Case(name, kind, kill, slots[name])
        for name, (kind, kill, _governed) in zip(names, plan, strict=True)
    ]


FOLDER_WRITES = ("trash folder", "recover folder")


def problems(found: dict, kind: str) -> list[str]:
    """Each way one case still needs a person after a fresh start: its pages, records or files on
    more than one side, the store and the disk disagreeing, a governance marker left pending, or a
    next write that refuses. A folder write and its undo move a folder as a whole, which file mode
    leaves nothing of at its old path, so a side that they leave as empty folders alone counts too.
    A single file's write leaves its folder, as file mode does."""
    out: list[str] = []
    for phase in ("after the restart", "after the next write"):
        state = found[phase]
        holding = sorted(
            side for side, where in state["sides"].items() if set(where) - {"empty folders"}
        )
        if len(holding) != 1:
            out.append(f"{phase}: split across {holding}")
        out.extend(
            f"{phase}: empty folders left at {side}: {where['empty folders']}"
            for side, where in state["sides"].items()
            if kind in FOLDER_WRITES and set(where) == {"empty folders"}
        )
        out.extend(f"{phase}: {disagreement}" for disagreement in state["disagree"])
        out.extend(
            f"{phase}: governance {marker}"
            for marker in state["governance markers"]
            if marker.endswith("pending")
        )
    if not found["next write"].endswith(": ok"):
        out.append(f"next write: {found['next write']}")
    out.extend(f"reconcile: {code}" for code in found["reconcile"] if code != "ok")
    return out


def kill_restart_and_judge(
    root: Path, tmp_path: Path, plan, order: str = "projector first"
) -> dict[str, list[str]]:
    cases = prepare(root, plan)
    kill_each(root, cases)
    found = restart(root, cases, tmp_path, order)
    judged = {
        f"{'governed ' if governs else ''}{case.kind} killed at {case.kill}": problems(
            found[case.name], case.kind
        )
        for case, (_kind, _kill, governs) in zip(cases, plan, strict=True)
    }
    assert len(judged) == len(plan), "each case is judged under its own name"
    print(json.dumps({"judged": judged, "found": found}, sort_keys=True))
    return judged


ORDERS = ("projector first", "importer first")


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.timeout(600)
def test_a_lifecycle_write_killed_at_or_after_its_commit_converges_in_a_fresh_process(
    stored, tmp_path, order
):
    """A step after the store commit that a kill leaves undone and that nothing redoes: a trashed
    folder's files left at its source, a receipt left pending, a record that names no file, an
    importer sweep that records a file whose move is queued, or a next write through the served
    door that refuses."""
    plan = [
        ("trash folder", "committed", False),
        ("trash folder", "pruned", False),
        ("trash folder", "media", False),
        ("trash folder", "committed", True),
        ("trash folder", "governed", True),
        ("trash file", "committed", False),
        ("move artifact", "committed", False),
    ]

    judged = kill_restart_and_judge(stored, tmp_path, plan, order)

    assert judged == dict.fromkeys(judged, [])


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a served store-mode process refuses the owner's recursive folder delete with "
    "AUDIENCE_RESTRICTED (`op_delete`: `egress.restricted_release_filter` filters every caller "
    "while a writer is bound), so the undo of a recovery refuses",
)
@pytest.mark.timeout(600)
def test_a_folder_recovery_killed_after_its_commit_converges_and_can_be_undone(stored, tmp_path):
    """A recovery killed after its store commit that a fresh process leaves split or pending, or
    whose undo, a trash of the recovered folder through the served door, refuses."""
    plan = [
        ("recover folder", "committed", False),
        ("recover folder", "rename 1", False),
        ("recover folder", "pruned", False),
        ("recover folder", "committed", True),
    ]

    judged = kill_restart_and_judge(stored, tmp_path, plan)

    assert judged == dict.fromkeys(judged, [])


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.timeout(600)
def test_a_lifecycle_write_killed_between_its_renames_and_its_commit_converges(
    stored, tmp_path, order
):
    """A kill before the store commit, in it, or between it and the writer's last rename: a folder
    split between its source and the trash, a binary that a sweep records at a path its queued move
    leaves, a trash slot with no sidecar to recover it by, a governed tombstone left pending, or a
    moved artifact's page that names a binary no longer at that path. A folder trash killed before
    its commit, and a recovery killed after it, retry or undo with a recursive folder trash, so they
    are in the two tests that the served door's refusal of that trash holds back."""
    plan = [
        ("trash folder", "rename 1", False),
        ("trash folder", "rename 2", False),
        ("trash folder", "rename 1", True),
        ("trash file", "placed", False),
        ("trash file", "commit", False),
        ("trash file", "rename 1", False),
        ("move artifact", "commit", False),
        ("move artifact", "rename 1", False),
    ]

    judged = kill_restart_and_judge(stored, tmp_path, plan, order)

    assert judged == dict.fromkeys(judged, [])


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="a served store-mode process refuses the owner's recursive folder delete with "
    "AUDIENCE_RESTRICTED (`op_delete`: `egress.restricted_release_filter` filters every caller "
    "while a writer is bound), so the retry of a folder trash refuses",
)
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.timeout(600)
def test_a_folder_trash_killed_before_its_commit_converges_and_its_retry_succeeds(
    stored, tmp_path, order
):
    """A folder trash killed before its store commit, or in it, that moves a file before the
    commit, leaves a governed tombstone pending, or whose retry through the served door refuses."""
    plan = [
        ("trash folder", "placed", False),
        ("trash folder", "commit", False),
        ("trash folder", "begun", True),
        ("trash folder", "commit", True),
    ]

    judged = kill_restart_and_judge(stored, tmp_path, plan, order)

    assert judged == dict.fromkeys(judged, [])
