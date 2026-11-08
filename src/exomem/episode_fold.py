"""The `fold-recap-revisions` curation step: an episode's superseded recap revisions move into history.

An episode that an earlier layout recorded keeps one Sources file per recap revision. The
fold writes each superseded revision as a create-only compiled-history entry of the head,
holding its exact stored bytes, and then moves the revision file to the vault trash. It never
changes the head. A revision that a live page outside the episode references stays a file with
its superseded mark, so that reference resolves to the text it cited, before, during and after.

The step is the only retirement of a Source that curation allows, and it earns that by one
invariant (`fold_leaf`): a file moves to the trash only after the step re-reads its history
entry byte for byte, immediately before the move. The step seals every identity it touches
(the entry paths, the trash identities, the hashes, the pinned set), so resume decides from
live state, and `unfold` reverses it from that sealed manifest alone.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from . import (
    compiled_history,
    curation,
    delete_file,
    episode_capture,
    episode_memory,
    find_corpus,
    fold_leaf,
    memory_refs,
    recap_folder,
    source_capture,
    source_closure,
    source_taxonomy,
    vault,
)
from .curation import CurationError

# nosemgrep: ep-word-set -- the step schema fixes these argument and manifest field names.
_PAGE_FIELDS = frozenset({"path", "expected_hash"})
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_MANIFEST_FIELDS = frozenset(
    {"episode_key", "folder", "head", "now", "actor", "exomem_version", "revisions", "own_pages"}
)
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_REVISION_FIELDS = frozenset({"path", "hash", "order", "pinned", "entry", "trash"})
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_ENTRY_FIELDS = frozenset({"path", "hash", "at"})


def _refuse(code: str, reason: str) -> CurationError:
    return curation._error(code, reason)  # noqa: SLF001 - the lane's own error constructor


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and curation._HEX64.fullmatch(value) is not None  # noqa: SLF001


def _invalid(where: str, why: str) -> CurationError:
    return _refuse("INVALID_CURATION_PLAN", f"{where} {why}")


# ---------------------------------------------------------------- closed schemas


def _page(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _PAGE_FIELDS:
        raise _invalid(where, "must be an object of exactly path and expected_hash")
    if not _hex64(value["expected_hash"]):
        raise _invalid(where, "needs the reviewed expected_hash")
    path = curation.normalize_target_path(value["path"], field=f"{where}.path", allow_fold=True)
    return {"path": path, "expected_hash": value["expected_hash"]}


def validate_args(args: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """The closed `fold-recap-revisions` arguments: the head page and the ordered revision files."""
    where = f"steps[{ordinal}].args"
    args["head"] = _page(args["head"], f"{where}.head")
    revisions = args["revisions"]
    if not isinstance(revisions, list) or not revisions:
        raise _invalid(f"{where}.revisions", "must be a non-empty list")
    args["revisions"] = [_page(item, f"{where}.revisions[{i}]") for i, item in enumerate(revisions)]
    paths = [item["path"] for item in args["revisions"]]
    if len(set(paths)) != len(paths) or args["head"]["path"] in paths:
        raise _invalid(f"{where}.revisions", "must name each revision once, and never the head")
    return args


def validate_unfold_args(args: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """The private undo's arguments: the sealed manifest of the fold it reverses, nothing else."""
    args["fold"] = _manifest(args["fold"], f"steps[{ordinal}].args.fold")
    return args


def _manifest(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise _invalid(where, "is not a sealed fold manifest")
    head, revisions = value["head"], value["revisions"]
    if not isinstance(head, Mapping) or set(head) != {"path", "hash"} or not _hex64(head["hash"]):
        raise _invalid(where, "has an invalid head")
    if not isinstance(revisions, list) or not revisions:
        raise _invalid(where, "has no revisions")
    for key in ("episode_key", "folder", "exomem_version"):
        curation._require_string(value[key], f"{where}.{key}")  # noqa: SLF001
    actor = value["actor"]
    if not isinstance(actor, Mapping) or set(actor) != {"audience", "surface"}:
        raise _invalid(where, "has an invalid actor")
    own = value["own_pages"]
    if not isinstance(own, list) or not all(isinstance(item, str) for item in own):
        raise _invalid(where, "has invalid own pages")
    try:
        dt.datetime.fromisoformat(value["now"])
    except (TypeError, ValueError) as error:
        raise _invalid(where, "has an invalid time") from error
    checked: list[dict[str, Any]] = []
    for item in revisions:
        if not isinstance(item, Mapping) or set(item) != _REVISION_FIELDS:
            raise _invalid(where, "has an invalid revision")
        entry = item["entry"]
        if (
            not isinstance(entry, Mapping)
            or set(entry) != _ENTRY_FIELDS
            or not _hex64(item["hash"])
            or not _hex64(entry["hash"])
            or not isinstance(item["pinned"], bool)
            or not isinstance(item["order"], str)
            or (item["trash"] is None) != item["pinned"]
        ):
            raise _invalid(where, "has an invalid revision")
        checked.append(
            {
                **item,
                "path": curation.normalize_target_path(item["path"], field=where, allow_fold=True),
                "entry": {**entry, "path": curation.normalize_target_path(entry["path"], field=where, allow_fold=True)},
                "trash": None
                if item["trash"] is None
                else curation.normalize_target_path(item["trash"], field=where, allow_fold=True),
            }
        )
    return {
        **value,
        "head": {"path": curation.normalize_target_path(head["path"], field=where, allow_fold=True), "hash": head["hash"]},
        "revisions": checked,
    }


# ---------------------------------------------------------------- proposal


def _require_authority(vault_root: Path) -> None:
    """The whole-namespace content authority that source-identity lookup needs, as a curation refusal."""
    try:
        source_capture.require_authority(vault_root)
    except source_capture.SourceCaptureError as error:
        code, _, reason = str(error).partition(": ")
        raise _refuse(code, reason) from error


def _text_at(vault_root: Path, path: str, expected_hash: str) -> str:
    text, actual = curation._guarded_text(vault_root, path, stale=True)  # noqa: SLF001
    if actual != expected_hash:
        raise _refuse("CURATION_BINDING_STALE", f"{path!r} changed since it was reviewed")
    return text


def _entries(value: Any) -> list[str]:
    """The `ingested_into` entries, flattened: an unquoted `[[x]]` parses as a nested list."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple):
        return [entry for item in value for entry in _entries(item)]
    return []


#: A revision as the pinned-set computation reads it: its path and its frontmatter.
_Held = tuple[str, Mapping[str, Any]]
#: The closed form of a memory reference, as the reference scheme fixes it.
# Sound under C6: it checks the shape of a protocol token (scheme prefix plus a UUID), not what anyone meant.
_REFERENCE = re.compile(re.escape(memory_refs.REF_PREFIX) + r"([0-9a-fA-F-]{36})")


def _is_navigation(path: str) -> bool:
    """Whether `path` is a generated listing (`index.md`, `log.md`): a row there lists a page, it never cites it."""
    return PurePosixPath(path).name.lower() in find_corpus.NAVIGATION_BASENAMES


def _listing_pages(vault_root: Path, path: str) -> list[str]:
    """The generated listings that name `path`; the trash move leaves their row to age out of the capped list."""
    return sorted(
        {link.path for link in vault.find_inbound_wikilinks(vault_root, path) if _is_navigation(link.path)}
    )


def _live_references(
    vault_root: Path, revisions: Sequence[_Held], own: frozenset[str]
) -> dict[str, str]:
    """Each revision path that a live page outside `own` references, mapped to the first such page.

    Any live page pins, whatever its lifecycle class: a superseded or retired page still cites
    the text it cited, and the plan promises every citation keeps resolving. Three evidences, all
    read over the whole vault with no caller filter: a wikilink (which is how a `sources:`
    citation is written), a memory reference, and the `ingested_into` back-reference the
    revision itself carries.
    """
    named: dict[str, str] = {}
    paths = [path for path, _fields in revisions]
    for target, links in vault.find_inbound_wikilinks_many(vault_root, paths).items():
        for link in links:
            if link.path not in own and not _is_navigation(link.path):
                named.setdefault(target, link.path)
                break
    identities = {
        identity: path
        for path, fields in revisions
        if (identity := memory_refs.normalize_id(fields.get(memory_refs.ID_FIELD)))
    }
    for page in vault.walk_vault_md(vault_root):
        relative = page.relative_to(vault_root).as_posix()
        if relative in own:
            continue
        try:
            text = page.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _REFERENCE.finditer(text):
            path = identities.get(memory_refs.normalize_id(match.group(1)) or "")
            if path is not None:
                named.setdefault(path, relative)
    resolver = vault.WikilinkResolver(vault_root)
    for path, fields in revisions:
        for entry in _entries(fields.get("ingested_into")):
            located = source_closure.locate(vault_root, entry, resolver=resolver)
            if located is not None and located not in own and (vault_root / located).is_file():
                named.setdefault(path, located)
    return named


def _episode(vault_root: Path, args: Mapping[str, Any]) -> tuple[str, str, list[episode_memory._Revision]]:  # noqa: SLF001
    """The episode key, its folder and the head's group pages, once the head is proved the single live page."""
    head_path = args["head"]["path"]
    head_text = _text_at(vault_root, head_path, args["head"]["expected_hash"])
    fields = vault.parse_frontmatter(head_text)[0]
    folder = PurePosixPath(head_path).parent.as_posix()
    parts = episode_capture.filename_parts(PurePosixPath(head_path).name)
    key = fields.get("episode")
    if (
        fields.get("source_type") != source_taxonomy.EPISODE_KIND
        or folder not in recap_folder.recap_folders(vault_root)
        or parts is None
        or not isinstance(key, str)
    ):
        raise _refuse("CURATION_STEP_INVALID", "the head is not a recap page of an episode")
    pages = episode_memory.group_pages(vault_root, vault_root / folder, key, parts[0])
    if [page.path for page in pages if page.live] != [head_path]:
        raise _refuse("CURATION_STEP_INVALID", "the head is not the episode's single live page")
    return key, folder, pages


def prepare(vault_root: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    """The binding of one fold: every identity it touches, sealed before anything moves."""
    from . import __version__
    from .governance.principal import effective_principal

    _require_authority(vault_root)
    key, folder, pages = _episode(vault_root, args)
    head_path, head_hash = args["head"]["path"], args["head"]["expected_hash"]
    head_text = _text_at(vault_root, head_path, head_hash)
    by_path = {page.path: page for page in pages}
    chosen: list[episode_memory._Revision] = []  # noqa: SLF001
    for item in args["revisions"]:
        page = by_path.get(item["path"])
        if page is None or page.live or page.frontmatter.get("source_type") != source_taxonomy.EPISODE_KIND:
            raise _refuse(
                "CURATION_STEP_INVALID",
                f"{item['path']!r} is not a superseded recap revision of this episode and group",
            )
        if page.hash != item["expected_hash"]:
            raise _refuse("CURATION_BINDING_STALE", f"{item['path']!r} changed since it was reviewed")
        if chosen and page.order <= chosen[-1].order:
            raise _refuse("CURATION_STEP_INVALID", "revisions must be listed oldest first")
        chosen.append(page)
    own = frozenset(page.path for page in pages)
    pinned = _live_references(vault_root, [(page.path, page.frontmatter) for page in chosen], own)
    now = dt.datetime.now()
    entry_base = dt.datetime.now(dt.UTC)
    who = effective_principal()
    actor = {"audience": who.audience_id, "surface": who.surface}
    revisions: list[dict[str, Any]] = []
    for index, page in enumerate(chosen):
        at = entry_base + dt.timedelta(microseconds=index)
        entry_path, entry_text = compiled_history.plan_fold_entry(
            vault_root, head_path=head_path, head_source=head_text, revision_path=page.path,
            revision_source=_text_at(vault_root, page.path, page.hash), at=at,
            actor=actor, exomem_version=__version__,
        )
        trash = None if page.path in pinned else delete_file.planned_trash_path(vault_root, page.path, now)
        for fresh in (entry_path, trash):
            if fresh is not None and not curation._guarded_absent(vault_root, fresh):  # noqa: SLF001
                raise _refuse("CURATION_BINDING_STALE", f"{fresh!r} is already taken")
        revisions.append(
            {
                "path": page.path,
                "hash": page.hash,
                "order": page.order,
                "pinned": page.path in pinned,
                "entry": {
                    "path": entry_path,
                    "hash": vault.content_hash(entry_text),
                    "at": at.isoformat().replace("+00:00", "Z"),
                },
                "trash": trash,
            }
        )
    manifest = {
        "episode_key": key,
        "folder": folder,
        "head": {"path": head_path, "hash": head_hash},
        "now": now.isoformat(),
        "actor": actor,
        "exomem_version": __version__,
        "revisions": revisions,
        "own_pages": sorted(own),
    }
    return {
        "path": head_path,
        "before_hash": head_hash,
        "after_hash": head_hash,
        "prepared": manifest,
        "effect_before": fold_before(manifest),
        "effect_after": fold_after(manifest),
    }


def fold_before(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What a fold binds: the head, each revision file in place, and every new identity free."""
    rows: list[dict[str, Any]] = [{"path": manifest["head"]["path"], "content_hash": manifest["head"]["hash"]}]
    for revision in manifest["revisions"]:
        rows.append({"path": revision["path"], "content_hash": revision["hash"]})
        rows.append({"path": revision["entry"]["path"], "absent": True})
        if revision["trash"] is not None:
            rows.append({"path": revision["trash"], "absent": True})
    return rows


def fold_after(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What a fold leaves: the head unchanged, every entry, kept files kept, the rest in trash."""
    rows: list[dict[str, Any]] = [{"path": manifest["head"]["path"], "content_hash": manifest["head"]["hash"]}]
    for revision in manifest["revisions"]:
        rows.append({"path": revision["entry"]["path"], "content_hash": revision["entry"]["hash"]})
        if revision["pinned"]:
            rows.append({"path": revision["path"], "content_hash": revision["hash"]})
        else:
            rows.append({"path": revision["path"], "absent": True})
            rows.append({"path": revision["trash"], "content_hash": revision["hash"]})
    return rows


def prepare_unfold(vault_root: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    """The binding of the undo: the fold's sealed manifest, plus a free trash identity per entry."""
    _require_authority(vault_root)
    manifest = args["fold"]
    now = dt.datetime.now()
    entry_trash = {
        revision["entry"]["path"]: delete_file.planned_trash_path(vault_root, revision["entry"]["path"], now)
        for revision in manifest["revisions"]
    }
    for path in entry_trash.values():
        if not curation._guarded_absent(vault_root, path):  # noqa: SLF001
            raise _refuse("CURATION_BINDING_STALE", f"{path!r} is already taken")
    prepared = {"fold": manifest, "now": now.isoformat(), "entry_trash": entry_trash}
    return {
        "path": manifest["head"]["path"],
        "before_hash": manifest["head"]["hash"],
        "after_hash": manifest["head"]["hash"],
        "prepared": prepared,
        "effect_before": [*fold_after(manifest), *({"path": path, "absent": True} for path in entry_trash.values())],
        "effect_after": unfold_after(manifest, entry_trash),
    }


def unfold_after(manifest: Mapping[str, Any], entry_trash: Mapping[str, str]) -> list[dict[str, Any]]:
    """What the undo leaves: every revision file back, every entry in trash, the head unchanged."""
    rows: list[dict[str, Any]] = [{"path": manifest["head"]["path"], "content_hash": manifest["head"]["hash"]}]
    for revision in manifest["revisions"]:
        rows.append({"path": revision["path"], "content_hash": revision["hash"]})
        rows.append({"path": revision["entry"]["path"], "absent": True})
        rows.append({"path": entry_trash[revision["entry"]["path"]], "content_hash": revision["entry"]["hash"]})
        if revision["trash"] is not None:
            rows.append({"path": revision["trash"], "absent": True})
    return rows


# ---------------------------------------------------------------- blockers


def binding_blockers(vault_root: Path, item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Why a sealed fold or undo cannot run, deciding from live state so a half-done step resumes.

    Every sealed file may stand before or after its move. A file that changed, or that is
    gone from both places, is a blocker; so is a step whose entries or trash identities
    hold other bytes than the sealed ones.
    """
    try:
        if item["kind"] == curation.FOLD_RECAP_STEP_KIND:
            _check_fold(vault_root, item["prepared"])
        else:
            _check_unfold(vault_root, item["prepared"])
    except CurationError as error:
        return [{"code": error.code, "reason": error.reason}]
    return []


def _check_head(vault_root: Path, manifest: Mapping[str, Any]) -> None:
    _text_at(vault_root, manifest["head"]["path"], manifest["head"]["hash"])


def _entry_stands(vault_root: Path, revision: Mapping[str, Any]) -> bool:
    """Whether the sealed entry stands (True), is absent (False); other bytes there are stale."""
    path = revision["entry"]["path"]
    if curation._guarded_absent(vault_root, path):  # noqa: SLF001
        return False
    text = compiled_history.read_entry(vault_root, path=path)
    if text is None or vault.content_hash(text) != revision["entry"]["hash"]:
        raise _refuse("CURATION_BINDING_STALE", f"{path!r} does not hold the sealed bytes")
    return True


def _kept_in_place(vault_root: Path, revision: Mapping[str, Any]) -> None:
    _text_at(vault_root, revision["path"], revision["hash"])


def _check_fold(vault_root: Path, manifest: Mapping[str, Any]) -> None:
    _check_head(vault_root, manifest)
    for revision in manifest["revisions"]:
        _entry_stands(vault_root, revision)
        if revision["pinned"]:
            _kept_in_place(vault_root, revision)
        else:
            fold_leaf.state(
                vault_root, path=revision["path"], trash_path=revision["trash"], expected_hash=revision["hash"]
            )


def _check_unfold(vault_root: Path, prepared: Mapping[str, Any]) -> None:
    manifest, entry_trash = prepared["fold"], prepared["entry_trash"]
    _check_head(vault_root, manifest)
    for revision in manifest["revisions"]:
        if revision["pinned"]:
            _kept_in_place(vault_root, revision)
        else:
            fold_leaf.state(
                vault_root, path=revision["path"], trash_path=revision["trash"], expected_hash=revision["hash"]
            )
        _entry_retirement(vault_root, revision["entry"], entry_trash[revision["entry"]["path"]])


def _entry_retirement(vault_root: Path, entry: Mapping[str, Any], trash_path: str) -> str:
    """How far the entry's retirement went: `standing`, `copied` (also in trash) or `retired`.

    The entry cannot be renamed (its tree belongs to the history owner), so a stop between the
    copy and the removal is a state of its own. Bytes other than the sealed ones are stale.
    """
    standing = not curation._guarded_absent(vault_root, entry["path"])  # noqa: SLF001
    if standing:
        text = compiled_history.read_entry(vault_root, path=entry["path"])
        if text is None or vault.content_hash(text) != entry["hash"]:
            raise _refuse("CURATION_BINDING_STALE", f"{entry['path']!r} does not hold the sealed bytes")
    trashed = None
    if not curation._guarded_absent(vault_root, trash_path):  # noqa: SLF001
        trashed = curation._guarded_text(vault_root, trash_path, stale=False)[1]  # noqa: SLF001
        if trashed != entry["hash"]:
            raise _refuse("CURATION_BINDING_STALE", f"{trash_path!r} does not hold the sealed bytes")
    if standing:
        return "copied" if trashed is not None else "standing"
    if trashed is None:
        raise _refuse("CURATION_OUTCOME_UNCERTAIN", f"{entry['path']!r} is neither in place nor in trash")
    return "retired"


# ---------------------------------------------------------------- apply


def _revision_text(vault_root: Path, revision: Mapping[str, Any]) -> str:
    """The revision's sealed bytes, from its file or, once moved, from its trash identity."""
    in_place = not curation._guarded_absent(vault_root, revision["path"])  # noqa: SLF001
    where = revision["path"] if in_place or revision["trash"] is None else revision["trash"]
    return _text_at(vault_root, where, revision["hash"])


def _verify_entry(vault_root: Path, revision: Mapping[str, Any]) -> None:
    """Refuse unless the entry reads back byte-identical and holds the revision's exact bytes."""
    text = compiled_history.read_entry(vault_root, path=revision["entry"]["path"])
    held = None
    if text is not None and vault.content_hash(text) == revision["entry"]["hash"]:
        try:
            held = json.loads(text)["prior"]["source"]
        except (ValueError, KeyError, TypeError):
            held = None
    if not isinstance(held, str) or vault.content_hash(held) != revision["hash"]:
        raise _refuse(
            "CURATION_BINDING_STALE",
            f"the history entry of {revision['path']!r} does not read back with its exact bytes",
        )


def _commit_witness(vault_root: Path) -> None:
    """Commit the step's witness, last: it states the finished effect, so no earlier batch may carry it."""
    from . import curation_witness

    pending = curation_witness.pending_for(vault_root)
    if pending is None:
        raise _refuse("CURATION_OUTCOME_UNCERTAIN", "the fold has no witness to commit")
    pending.ready = True
    vault.batch_atomic_write([], vault_root=vault_root, post_commit_fanout=False)


def fold(vault_root: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Write every entry oldest first, then move each unpinned revision once its entry reads back."""
    manifest = binding["prepared"]
    _require_authority(vault_root)
    _check_head(vault_root, manifest)
    now = dt.datetime.fromisoformat(manifest["now"])
    head_text = _text_at(vault_root, manifest["head"]["path"], manifest["head"]["hash"])
    movable = [revision for revision in manifest["revisions"] if not revision["pinned"]]
    own = frozenset(manifest["own_pages"])
    # A revision that a live page came to reference since proposal is no longer movable: that
    # reference resolves only while the file stays. One already in trash is past the question.
    waiting = [
        revision for revision in movable if not curation._guarded_absent(vault_root, revision["path"])  # noqa: SLF001
    ]
    cited = _live_references(
        vault_root,
        [(revision["path"], vault.parse_frontmatter(_revision_text(vault_root, revision))[0]) for revision in waiting],
        own,
    )
    if cited:
        raise _refuse("CURATION_BINDING_STALE", f"{sorted(cited)[0]!r} is now referenced by a live page")
    written = 0
    for revision in manifest["revisions"]:
        entry_path, entry_text = compiled_history.plan_fold_entry(
            vault_root,
            head_path=manifest["head"]["path"],
            head_source=head_text,
            revision_path=revision["path"],
            revision_source=_revision_text(vault_root, revision),
            at=dt.datetime.fromisoformat(revision["entry"]["at"].replace("Z", "+00:00")),
            actor=manifest["actor"],
            exomem_version=manifest["exomem_version"],
        )
        if entry_path != revision["entry"]["path"] or vault.content_hash(entry_text) != revision["entry"]["hash"]:
            raise _refuse("CURATION_BINDING_STALE", f"{revision['path']!r} no longer plans its sealed entry")
        written += compiled_history.publish_fold_entry(vault_root, path=entry_path, text=entry_text)
    curation._fault_barrier("fold-after-entries")  # noqa: SLF001
    for revision in movable:
        _verify_entry(vault_root, revision)
    moved: list[str] = []
    for revision in movable:
        if fold_leaf.move_to_trash(
            vault_root,
            path=revision["path"],
            trash_path=revision["trash"],
            expected_hash=revision["hash"],
            verify=lambda revision=revision: _verify_entry(vault_root, revision),
            now=now,
            expected_dead_inbound=sorted(own | set(_listing_pages(vault_root, revision["path"]))),
        ):
            moved.append(revision["path"])
        curation._fault_barrier("fold-after-move")  # noqa: SLF001
    _commit_witness(vault_root)
    return {
        "path": manifest["head"]["path"],
        "entries_written": written,
        "moved": moved,
        "kept": [revision["path"] for revision in manifest["revisions"] if revision["pinned"]],
    }


def unfold(vault_root: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Restore each moved revision from its trash identity, then move the entries to trash.

    Files return first, so the bytes an entry holds stay held by the restored file while
    the entry is retired; only entries that the manifest names move.
    """
    prepared = binding["prepared"]
    manifest, entry_trash = prepared["fold"], prepared["entry_trash"]
    _require_authority(vault_root)
    _check_head(vault_root, manifest)
    restored: list[str] = []
    for revision in manifest["revisions"]:
        if revision["pinned"]:
            continue
        if fold_leaf.restore_from_trash(
            vault_root, path=revision["path"], trash_path=revision["trash"], expected_hash=revision["hash"]
        ):
            restored.append(revision["path"])
        curation._fault_barrier("unfold-after-restore")  # noqa: SLF001
    retired = 0
    for revision in manifest["revisions"]:
        entry, target = revision["entry"], entry_trash[revision["entry"]["path"]]
        if _entry_retirement(vault_root, entry, target) == "retired":
            continue
        # Immediately before the move: the revision's own file holds the bytes the entry holds.
        _kept_in_place(vault_root, revision)
        compiled_history.retire_fold_entry(vault_root, path=entry["path"], trash_path=target)
        retired += 1
        curation._fault_barrier("unfold-after-retire")  # noqa: SLF001
    _commit_witness(vault_root)
    return {"path": manifest["head"]["path"], "restored": restored, "entries_retired": retired}
