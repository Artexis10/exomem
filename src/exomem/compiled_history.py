"""Prior Markdown interpretations, retained atomically by the semantic writer.

The episode recorder keeps the same entries for the one page of a recap. A recap
Source is the only Source whose text is revised, and only that recorder revises
it (`plan_episode_entry`), so a revision never loses its previous text. The recap
fold (`episode_fold`) writes the same kind of entry, operation `episode_fold`, for a
superseded revision file of an earlier layout that it then retires from the Sources
tree. That entry is published alone (`plan_fold_entry`, `publish_fold_entry`), because
the head page it belongs to does not change.

One entry keeps one before image as read. History stays outside ordinary reads
and indexing; this owner admits every retained image before serving its fields.
It reuses the held history reader and Records' page bounds and continuation, without
registry history's rotation policy. Its envelope differs from Records': `status` is
`retained`, `incomplete` or `unavailable`, `coverage` is always `unverified`, an
optional `lookup` names stale name-resolution evidence, and no field claims a total. Each change costs one full before image. Only a returned
page of independently admitted text stays in memory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import (
    context_refs,
    held_fs,
    memory_refs,
    origin_bindings,
    provenance,
    recap_folder,
    record_history,
    registry_history,
    retained_inputs,
    semantic_index,
    source_closure,
    source_taxonomy,
    vault,
)
from .governance.principal import OWNER_AUDIENCE, effective_principal
from .kbdir import kb_dirname

ENTRY_VERSION = 1
#: The `operation` an episode revision's entry carries; the semantic writer names its own.
EPISODE_OPERATION = "episode_revision"
#: The `operation` of the entry that the recap fold writes for a revision it retires from the Sources tree.
FOLD_OPERATION = "episode_fold"
_DESCRIPTOR_ID = "compiled-history"  # The registry family of the history tree (`reserved_paths._REGISTRY`).


def history_root() -> str:
    """The canonical reserved tree owned only by compiled history."""
    return f"{kb_dirname()}/_Compiled/history"


def history_dir(parent_ref: str) -> str:
    return f"{history_root()}/{hashlib.sha256(parent_ref.encode()).hexdigest()}"


def validate_new_write(write: Any, *, path: str) -> None:
    """Keep the bound history exception create-only at every planned admission."""
    guard = getattr(write, "guard", None)
    if (
        not write.create_only or not isinstance(guard, vault.PathGuard)
        or guard.leaf_policy != "absent" or guard.target != path
    ):
        raise ValueError("VOCABULARY_AUXILIARY_GUARD_INVALID")


def validate_new_entry(
    root: Path, *, path: str, raw: bytes, primary_path: str,
    before_source: str, after_source: str,
) -> None:
    """Bind a new history leaf to the exact images its producer read and wrote."""
    try:
        entry = json.loads(raw)
        # Both images are hashed as stored, so a CRLF page chains entry to entry.
        parent_ref = semantic_index.build_parent_index_state(
            root, primary_path, source=before_source
        ).document.parent_ref
        moment = dt.datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
        name = registry_history.version_id(moment, entry["transition_id"]) + ".json"
        if (
            type(entry["version"]) is not int or entry["version"] != ENTRY_VERSION
            or parent_ref is None or entry["parent_ref"] != parent_ref
            or path != f"{history_dir(parent_ref)}/{name}"
            or entry["path"] != primary_path
            or entry["prior"]["source"] != before_source
            or entry["before_hash"] != vault.content_hash(before_source)
            or entry["after_hash"] != vault.content_hash(after_source)
        ):
            raise ValueError("compiled history does not match its primary")
        if entry["operation"] == EPISODE_OPERATION:
            # `plan_entry` leaves every Source out; only a recap page of the episode folders keeps one.
            fields = vault.parse_frontmatter(before_source, strict=True)[0]
            if (
                not recap_folder.under(recap_folder.recap_folders(root), primary_path)
                or fields.get("source_type") != source_taxonomy.EPISODE_KIND
            ):
                raise ValueError("an episode revision keeps only a recap page")
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED") from error


def plan_entry(
    root: Path, *, preflight: Any, token: dict, reason: str | None
) -> vault.PlannedWrite | None:
    """Keep one changed interpretation before the lifecycle auxiliary hash seals it."""
    if (
        source_closure._eligible_path(preflight.path)
        or preflight.before.document.parent_ref is None
    ):
        return None
    parent_ref = preflight.before.document.parent_ref
    moment = dt.datetime.fromisoformat(token["history_at"].replace("Z", "+00:00"))
    name = registry_history.version_id(moment, token["transition_id"]) + ".json"
    path = root / history_dir(parent_ref) / name
    if preflight.committed_replay:
        previous = registry_history.read_kept(root, history_dir(parent_ref), name)
        if previous is None:
            return None
        entry = json.loads(previous)
        if (
            entry.get("transition_id") != token["transition_id"]
            or entry.get("before_hash") != token["before_hash"]
            or entry.get("after_hash") != token["after_hash"]
            or entry.get("reason") != reason
        ):
            raise ValueError("LIFECYCLE_TRANSITION_MISMATCH: history does not match the transition")
        return vault.PlannedWrite(path, previous)
    before_fm, before_body, _ = vault.parse_frontmatter(preflight.before_source, strict=True)
    after_fm, after_body, _ = vault.parse_frontmatter(preflight.after_source, strict=True)
    # `updated` is the writer's clock, not an interpretation change.
    if {k: v for k, v in before_fm.items() if k != "updated"} == {
        k: v for k, v in after_fm.items() if k != "updated"
    } and before_body == after_body:
        return None
    who = effective_principal()
    parent_ref = preflight.before.document.parent_ref
    before_units = {
        unit.unit_ref: unit for unit in preflight.before.document.units if unit.unit_ref is not None
    }
    after_units = {
        unit.unit_ref: unit for unit in preflight.after.document.units if unit.unit_ref is not None
    }
    affected = [
        {
            "unit_ref": ref,
            "before_fingerprint": getattr(before_units.get(ref), "fingerprint", None),
            "after_fingerprint": getattr(after_units.get(ref), "fingerprint", None),
        }
        for ref in sorted(before_units.keys() | after_units.keys())
        if getattr(before_units.get(ref), "fingerprint", None)
        != getattr(after_units.get(ref), "fingerprint", None)
    ]
    metadata = provenance.parse_origin(preflight.before_source, managed=True)
    entry = {
        "version": ENTRY_VERSION,
        "parent_ref": parent_ref,
        "path": preflight.path,
        "at": token["history_at"],
        "operation": preflight.operation,
        "transition_id": token["transition_id"],
        "before_hash": preflight.before_hash,
        "after_hash": preflight.after.source_hash,
        "affected_units": affected,
        "reason": reason,
        "actor": {"audience": who.audience_id, "surface": who.surface},
        "prior": {"source": preflight.exact_before_source},
        "binding": metadata.payload,
    }
    # The release that planned the transition, taken from the token so an exact
    # retry re-plans identical bytes; a token minted before this field lacks it.
    if "exomem_version" in token:
        entry["exomem_version"] = token["exomem_version"]
    return _new_entry_write(root, path, entry)


def plan_episode_entry(
    root: Path, *, path: str, before_source: str, after_source: str
) -> vault.PlannedWrite:
    """Keep the exact previous bytes of an episode page that the recorder revises.

    The recorder is the only caller. Unlike `plan_entry` it plans for a Source,
    which that function leaves out, and it mints its own transition: a recap
    revision has no semantic preflight to take one from.
    """
    from . import __version__

    parent_ref = memory_refs.ref_from_markdown(before_source) or context_refs.vault_ref(path)
    moment = dt.datetime.now(dt.UTC)
    transition_id = uuid.uuid4().hex
    who = effective_principal()
    entry = {
        "version": ENTRY_VERSION,
        "parent_ref": parent_ref,
        "path": path,
        "at": moment.isoformat().replace("+00:00", "Z"),
        "operation": EPISODE_OPERATION,
        "transition_id": transition_id,
        "before_hash": vault.content_hash(before_source),
        "after_hash": vault.content_hash(after_source),
        "affected_units": [],
        "reason": None,
        "actor": {"audience": who.audience_id, "surface": who.surface},
        "prior": {"source": before_source},
        "binding": provenance.parse_origin(before_source, managed=True).payload,
        "exomem_version": __version__,
    }
    name = registry_history.version_id(moment, transition_id) + ".json"
    return _new_entry_write(root, root / history_dir(parent_ref) / name, entry)


def plan_fold_entry(
    root: Path, *, head_path: str, head_source: str, revision_path: str, revision_source: str,
    at: dt.datetime, actor: dict[str, Any], exomem_version: str,
) -> tuple[str, str]:
    """The vault-relative path and exact text of the entry that keeps one folded recap revision.

    The entry is a pure function of its inputs, so a fold that resumes plans the same
    bytes and finds an entry it already wrote; the fold seals `actor` and `exomem_version`
    at proposal, so an upgrade or another caller between proposal and apply cannot change them.
    Its parent is the episode's head, which the fold leaves unchanged: `after_hash` is the
    head's hash and `before_hash` the revision's.
    """
    parent_ref = memory_refs.ref_from_markdown(head_source) or context_refs.vault_ref(head_path)
    transition_id = hashlib.sha256(
        f"{revision_path}\n{vault.content_hash(revision_source)}".encode()
    ).hexdigest()[:32]
    entry = {
        "version": ENTRY_VERSION,
        "parent_ref": parent_ref,
        "path": revision_path,
        "at": at.isoformat().replace("+00:00", "Z"),
        "operation": FOLD_OPERATION,
        "transition_id": transition_id,
        "before_hash": vault.content_hash(revision_source),
        "after_hash": vault.content_hash(head_source),
        "affected_units": [],
        "reason": None,
        "actor": actor,
        "prior": {"source": revision_source},
        "binding": provenance.parse_origin(revision_source, managed=True).payload,
        "exomem_version": exomem_version,
    }
    name = registry_history.version_id(at, transition_id) + ".json"
    return f"{history_dir(parent_ref)}/{name}", _entry_text(entry)


def publish_fold_entry(root: Path, *, path: str, text: str) -> bool:
    """Create one fold entry; False when the same bytes already stood there.

    A fold has no changed primary page to seal an auxiliary write against, so the entry is
    published through the history owner's own held write, under its own scope. The owner write
    replaces, so this reads the leaf first and never publishes over bytes that differ from `text`.
    """
    from . import reserved_paths

    directory, _, name = path.rpartition("/")
    kept = registry_history.read_kept(root, directory, name)
    if kept == text:
        return False
    if kept is not None:
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED: a history entry with other bytes stands there")
    with reserved_paths._subsystem_authority_scope("compiled_history"):  # noqa: SLF001 - the store's own scope
        reserved_paths._publish_owner_bytes(  # noqa: SLF001 - the owner seam every private store writes through
            root, root / path, _DESCRIPTOR_ID, text.encode("utf-8")
        )
    return True


def retire_fold_entry(root: Path, *, path: str, trash_path: str) -> None:
    """Move one fold entry to its sealed trash identity; the undo of a fold is its only caller.

    The caller has proved that the revision's own file is back in place, so the bytes that
    the entry holds stay held. The owner seam has no rename, so the entry's exact bytes are
    created at `trash_path` first, then the entry is removed: a stop between the two leaves
    both, and a repeat finishes the removal. Other bytes at `trash_path` are never replaced.
    """
    from . import reserved_paths

    text = read_entry(root, path=path)
    if text is None:
        raise ValueError("NOT_FOUND: the history entry to retire is missing or unreadable")
    try:
        trashed, _guard = vault.read_guarded_text(root, root / trash_path)
    except FileNotFoundError:
        reserved_paths.publish_generic_bytes(root, trash_path, text.encode("utf-8"), expected_identity=None)
    else:
        if trashed != text:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED: other bytes stand at the trash identity")
    with reserved_paths._subsystem_authority_scope("compiled_history"):  # noqa: SLF001 - the store's own scope
        reserved_paths._remove_owner_file(root, root / path, _DESCRIPTOR_ID)  # noqa: SLF001 - the owner seam
    _remove_if_empty(root, path.rpartition("/")[0])


def _remove_if_empty(root: Path, directory: str) -> None:
    """Remove the history directory a fold created once its last entry is gone. A directory that holds anything stays.

    The fold's undo is the only caller. Best effort: an empty directory left behind changes no byte of the vault.
    """
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        return
    with acquired.require() as filesystem:
        found = filesystem.parent(directory, access="mutate")
        if found.ok:
            with found.require() as held:
                filesystem.unlink_directory(held)


def read_entry(root: Path, *, path: str) -> str | None:
    """The text of one entry as stored, or None when it is missing or unreadable."""
    directory, _, name = path.rpartition("/")
    return registry_history.read_kept(root, directory, name)


def _entry_text(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _new_entry_write(root: Path, path: Path, entry: dict) -> vault.PlannedWrite:
    guard = vault.PathGuard.capture(root, path.relative_to(root).as_posix(), leaf_policy="absent")
    return vault.PlannedWrite(path, _entry_text(entry), create_only=True, guard=guard)


def _folded_revision(root: Path, source: str, path: str, head_episode: Any) -> bool:
    """Whether `source` is a recap revision of the head's episode, held in a recap folder."""
    fields = vault.parse_frontmatter(source, strict=True)[0]
    return (
        isinstance(head_episode, str)
        and fields.get("episode") == head_episode
        and fields.get("source_type") == source_taxonomy.EPISODE_KIND
        and recap_folder.under(recap_folder.recap_folders(root), path)
    )


def _folded_siblings(
    root: Path, directory: str, names: list[str], head: str, head_episode: Any
) -> dict[str, tuple[str, ...]]:
    """Each way a wikilink can name a revision that the fold of `head` moved to trash, mapped to `head`.

    The revision's own page is gone, so the gate cannot decide it. It is the head's history, and the caller
    has read the head, so the name resolves to the head and is decided under the head's own admission. Only a
    revision that `_folded_revision` binds to the head's episode counts, as in `_admitted_entry`.
    """
    from .working_set_index import normalize

    named: dict[str, tuple[str, ...]] = {}
    for name in names:
        try:
            entry = json.loads(registry_history.read_kept(root, directory, name) or "")
            revision = entry["path"] if entry.get("operation") == FOLD_OPERATION else None
            if not isinstance(revision, str) or not _folded_revision(root, entry["prior"]["source"], revision, head_episode):
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        bare = revision.removesuffix(".md")
        for spelling in (revision, bare, bare.rpartition("/")[2]):
            named.setdefault(normalize(spelling), (head,))
    return named


def _admitted_entry(
    root: Path, text: str | None, *, path: str, parent_ref: str, head_episode: Any = None,
    siblings: Callable[[], dict[str, tuple[str, ...]]] | None = None,
) -> dict | None:
    try:
        entry = json.loads(text) if text is not None else None
        if (
            not isinstance(entry, dict)
            or entry.get("version") != ENTRY_VERSION
            or entry.get("parent_ref") != parent_ref
        ):
            return None
        historical_path = entry.get("path")
        if not isinstance(historical_path, str):
            return None
        vault.resolve_under_vault(root, historical_path)
        if (
            not isinstance(entry.get("actor"), dict)
            or not isinstance(entry.get("affected_units"), list)
            or any(not isinstance(unit, dict) for unit in entry["affected_units"])
        ):
            return None
        source = entry["prior"]["source"]
        if not isinstance(source, str) or vault.content_hash(source) != entry["before_hash"]:
            return None
        if entry.get("operation") == FOLD_OPERATION:
            # A folded revision keeps its own identity, so it binds to the head by episode, not by ref.
            if not _folded_revision(root, source, historical_path, head_episode):
                return None
        elif (
            memory_refs.ref_from_markdown(source) or context_refs.vault_ref(historical_path)
        ) != parent_ref:
            return None
        # Current-page admission cannot authorize bytes removed from that page.
        registry_history._require_snapshot_admission(
            root, historical_path, source.encode("utf-8"), markdown=True,
        )
        metadata = provenance.parse_origin(source, managed=True, strict=True)
        if metadata.payload is not None:
            for binding in metadata.payload["inputs"].values():
                origin_bindings.resolve_origin_input(root, binding, disclosure=True)
        shown = json.dumps(entry, ensure_ascii=False)
        # A revision names its siblings, which the same fold moved to trash, so the index no longer lists them.
        # Under the head's own admission a sibling that this history keeps counts as resolved.
        if not retained_inputs.exact_text_visible(root, shown) and not (
            entry.get("operation") == FOLD_OPERATION
            and siblings is not None
            and retained_inputs.exact_text_visible(root, shown, resolved_here=siblings())
        ):
            return None
    except (KeyError, TypeError, ValueError, provenance.OriginError):
        return None
    return entry


def _scan_cap(size: int) -> int:
    # A unit read opens entries that may not touch its unit; twice a page's lookahead
    # keeps one call near the cost of a page read, and the caller pages on.
    return 2 * (size + 1)


def read(
    root: Path,
    *,
    path: str,
    unit_ref: str | None = None,
    limit: int | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Return a bounded page of independently admitted prior interpretations.

    One call examines a bounded number of admitted entries, so a unit-scoped page can
    be short or empty while `truncated` is true; the continuation then names the last
    admitted entry examined, never an unavailable one. Such an empty page reports
    `incomplete`, not `unavailable`.

    `lookup` appears when prose names could not be checked against current lookup
    evidence, so a name-linked entry may have been refused for that alone. A
    `stale` read schedules the stamped build; repeating the same call, not paging
    on, can then admit what this one refused.
    """
    from . import commands, working_set_runtime
    from .governance import egress

    size, after = record_history._page_bounds(limit, continuation)
    current = commands.op_read_memory(root, path=path)
    if not isinstance(current.get("content_hash"), str):
        raise ValueError("NOT_FOUND: page is unavailable")
    # Only admission that resolves prose names depends on lookup currency.
    scan_start = working_set_runtime.name_lookup(root) if egress.name_lookup_required(root) else None
    if scan_start is not None:
        working_set_runtime.request_lookup_refresh(root, scan_start)
    canonical = current["path"]
    # The current read establishes the parent identity, never prior disclosure.
    identity = memory_refs.normalize_id(current["frontmatter"].get(memory_refs.ID_FIELD))
    parent_ref = memory_refs.memory_ref(identity) if identity else context_refs.vault_ref(canonical)
    directory = history_dir(parent_ref)
    names = registry_history.kept_names(root, directory, suffix=".json") or []
    cap = _scan_cap(size)
    folded: dict[str, tuple[str, ...]] | None = None

    def siblings() -> dict[str, tuple[str, ...]]:
        nonlocal folded
        if folded is None:
            folded = _folded_siblings(root, directory, names, canonical, current["frontmatter"].get("episode"))
        return folded

    examined = 0
    consumed = None  # The last admitted version this read finished with, touching or not.
    page = []
    truncated = False
    for name in names:
        version = name.removesuffix(".json")
        # Names are newest first and sorted by version: entries not older than the
        # continuation are skipped without being opened.
        if after is not None and version >= after:
            continue
        entry = _admitted_entry(
            root,
            registry_history.read_kept(root, directory, name),
            path=canonical,
            parent_ref=parent_ref,
            head_episode=current["frontmatter"].get("episode"),
            siblings=siblings,
        )
        if entry is None:
            continue
        if unit_ref is None or any(u.get("unit_ref") == unit_ref for u in entry["affected_units"]):
            if len(page) == size:
                # One further admitted entry proves more exist; nothing older is opened.
                truncated = True
                break
            page.append((version, entry))
        consumed = version
        examined += 1
        if examined == cap:
            # Only admitted entries count, so reaching the cap shows nothing about the rest.
            truncated = True
            break
    revisions = []
    who = effective_principal()
    for version, entry in page:
        prior = dict(entry["prior"])
        if unit_ref is not None:
            neutral = semantic_index.build_parent_index_state(
                root, entry["path"], source=prior["source"]
            )
            selected = semantic_index.selected_parent_index_state(
                root, entry["path"], state=neutral
            )
            resolution = selected.document.resolve_unit(unit_ref)
            prior["unit"] = resolution.unit.to_dict() if resolution.unit is not None else None
            prior["unit_status"] = (
                "unavailable"
                if selected.definitions_unavailable and resolution.status == "missing"
                else resolution.status
            )
        actor = entry["actor"]
        if who.audience_id not in {OWNER_AUDIENCE, actor.get("audience")}:
            actor = {"surface": actor.get("surface")}
        revisions.append(
            {**entry, "version": version, "status": "available", "prior": prior, "actor": actor}
        )
    result = {
        "parent_ref": parent_ref,
        "unit_ref": unit_ref,
        # These protocol outcomes describe admitted snapshots, never complete retention.
        # An empty truncated page means the examination bound ended the read, not absence.
        "status": "retained" if revisions else "incomplete" if truncated else "unavailable",
        "coverage": "unverified",
        "returned": len(revisions),
        "truncated": truncated,
        "continuation": consumed if truncated else None,
        "revisions": revisions,
    }
    if scan_start is not None:
        scan_end = working_set_runtime.name_lookup(root)
        # A write during the scan leaves the end stale; its build is owed too.
        working_set_runtime.request_lookup_refresh(root, scan_end)
        marker = _lookup_marker(root, scan_start, scan_end)
        if marker is not None:
            result["lookup"] = marker
    return result


def _lookup_marker(root: Path, scan_start: Any, scan_end: Any) -> str | None:
    """Name the lookup state when it alone may have refused name-linked entries.

    Both readings bracket the scan (`working_set_runtime.lookup_settled`). The state is
    vault-wide and says nothing about this page's entries, but it flips on any
    write, withheld pages included, so a restricted caller does not receive it,
    as recall withholds its vault generation.
    """
    from . import working_set_runtime as runtime
    from .governance import egress

    if runtime.lookup_settled(scan_start, scan_end):
        return None
    if egress.caller_restricted(root):
        return None
    if runtime.LOOKUP_UNAVAILABLE in (scan_start.state, scan_end.state):
        return runtime.LOOKUP_UNAVAILABLE
    return runtime.LOOKUP_STALE
