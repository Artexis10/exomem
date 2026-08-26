"""Preview-first representation migration for Planning and Records items."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    access,
    move_file,
    record_formats,
    record_governance,
    reserved_paths,
    semantic_writes,
    vault,
    writer_lease,
)
from . import structured_collections as collections

_MAX_PLAN_ITEMS = 512
_MAX_CORPUS_FILES = 10_000
_MAX_CORPUS_BYTES = 128 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_BLOCKERS = 64
_RECEIPT_VERSION = 1
_WIKILINK = re.compile(r"\[\[([^\]\|\n]+?)(\|[^\]\n]*)?\]\]")


@dataclass(frozen=True, slots=True)
class _Move:
    item_key: str
    source: str
    target: str
    before_hash: str
    after_hash: str


@dataclass(slots=True)
class _Plan:
    public: dict[str, Any]
    manifest: collections.CollectionManifest
    snapshot: record_formats.AdapterSnapshot
    moves: tuple[_Move, ...]
    final_text: dict[str, str]
    guards: dict[str, vault.PathGuard]
    item_paths: dict[str, str]


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blocker(code: str, reason: str) -> dict[str, str]:
    return {"code": code, "reason": reason}


def _add_blocker(blockers: list[dict[str, str]], code: str, reason: str) -> None:
    finding = _blocker(code, reason)
    if finding not in blockers and len(blockers) < _MAX_BLOCKERS:
        blockers.append(finding)


def _corpus(
    root: Path,
) -> tuple[
    dict[str, bytes],
    dict[str, vault.PathGuard],
    list[dict[str, str]],
    bool,
]:
    files: dict[str, bytes] = {}
    guards: dict[str, vault.PathGuard] = {}
    blockers: list[dict[str, str]] = []
    truncated = False
    total = 0
    for index, path in enumerate(vault.walk_vault_md(root), start=1):
        if index > _MAX_CORPUS_FILES:
            truncated = True
            _add_blocker(
                blockers,
                "MIGRATION_SCAN_LIMIT",
                "the Markdown corpus is too large for an exact migration preview",
            )
            break
        relative = path.relative_to(root).as_posix()
        try:
            size = path.lstat().st_size
            if size > _MAX_FILE_BYTES or total + size > _MAX_CORPUS_BYTES:
                raise vault.PathGuardError(
                    "PATH_GUARD_LIMIT", "the migration corpus exceeds its byte limit"
                )
            data, guard = vault.read_bounded_guarded_bytes(root, relative, limit=size)
        except (OSError, vault.PathGuardError):
            truncated = True
            _add_blocker(
                blockers,
                "MIGRATION_SCAN_LIMIT",
                "the Markdown corpus cannot be read as one exact migration snapshot",
            )
            continue
        total += len(data)
        files[relative] = data
        guards[relative] = guard
    return files, guards, blockers, truncated


def _source_snapshot(snapshot: record_formats.AdapterSnapshot, corpus: Mapping[str, bytes]) -> str:
    return _canonical_hash(
        {
            "collection": snapshot.snapshot,
            "markdown": [
                [path, hashlib.sha256(data).hexdigest()] for path, data in sorted(corpus.items())
            ],
        }
    )


def _decode_markdown(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise collections.CollectionError(
            "INVALID_STRUCTURED_FILE", "a migration candidate is not UTF-8 Markdown"
        ) from error


def _rewrite_links(
    text: str,
    old_rel: str,
    new_rel: str,
    *,
    allow_bare: bool,
) -> tuple[str, int, bool]:
    old_no_ext = old_rel.removesuffix(".md")
    new_no_ext = new_rel.removesuffix(".md")
    prefix = vault.kb_prefix()
    old_full = old_no_ext if old_no_ext.startswith(prefix) else prefix + old_no_ext
    new_full = new_no_ext if new_no_ext.startswith(prefix) else prefix + new_no_ext
    old_stripped = old_full.removeprefix(prefix)
    new_stripped = new_full.removeprefix(prefix)
    old_basename = old_no_ext.rsplit("/", 1)[-1]
    new_basename = new_no_ext.rsplit("/", 1)[-1]
    changed = 0
    ambiguous = False

    def replace(match: re.Match[str]) -> str:
        nonlocal ambiguous, changed
        raw = match.group(1).strip()
        alias = match.group(2) or ""
        target_path, marker, anchor = raw.partition("#")
        anchor_suffix = f"#{anchor}" if marker else ""
        target_path = target_path.rstrip()
        target = target_path.removesuffix(".md")
        if target in {old_full, old_stripped}:
            changed += 1
            replacement = new_full if target_path.startswith(prefix) else new_stripped
            return f"[[{replacement}{anchor_suffix}{alias}]]"
        if "/" not in target and target == old_basename:
            if not allow_bare:
                ambiguous = True
                return match.group(0)
            changed += 1
            return f"[[{new_basename}{anchor_suffix}{alias}]]"
        return match.group(0)

    return _WIKILINK.sub(replace, text), changed, ambiguous


def _path_is_transactionally_writable(root: Path, relative: str) -> bool:
    if vault.in_append_only_tree(relative):
        return False
    if not record_governance.full_release_filter(root)(relative):
        return False
    if access.writable_reason(root, relative) is not None:
        return False
    try:
        reserved_paths.inspect_generic_file(root, relative)
    except reserved_paths.ReservedPathLeafError:
        return False
    return True


def _remove_all_presentations(text: str) -> str:
    return record_formats.remove_record_presentation(record_formats.remove_item_presentation(text))


def _render_presentation(
    text: str,
    manifest: collections.CollectionManifest,
    values: Mapping[str, Any],
    *,
    resolve_relationship: Any,
) -> tuple[str, str | None]:
    without_other = text
    if manifest.item_presentation is not None:
        without_other = record_formats.remove_record_presentation(without_other)
        return (
            record_formats.splice_item_presentation(
                without_other,
                manifest,
                values,
                resolve_relationship=resolve_relationship,
            ),
            "write",
        )
    if manifest.record_presentation is not None:
        without_other = record_formats.remove_item_presentation(without_other)
        return record_formats.splice_record_presentation(without_other, manifest, values), "write"
    removed = _remove_all_presentations(without_other)
    return removed, "remove" if removed != text else None


def _build_plan(root: Path, collection: str | Path) -> _Plan:
    manifest = record_governance.resolve_collection(root, collection)
    if manifest.semantic_profile not in {"planning", "records"}:
        raise collections.CollectionError(
            "UNSUPPORTED_COLLECTION_PROFILE",
            "structured-file migration requires Planning or Records",
        )
    if manifest.storage.strategy != "markdown-items":
        raise collections.CollectionError(
            "UNSUPPORTED_STORAGE",
            "structured-file migration requires Markdown-item storage",
        )
    record_formats.validate_storage_contract(manifest)
    authorize = record_governance.full_release_filter(root)
    snapshot = record_formats.load_adapter(root, manifest, authorize_path=authorize).read()
    if snapshot.diagnostics:
        diagnostic = snapshot.diagnostics[0]
        raise collections.CollectionError(diagnostic.code, diagnostic.reason)
    corpus, corpus_guards, blockers, truncated = _corpus(root)
    if len(snapshot.records) > _MAX_PLAN_ITEMS:
        truncated = True
        _add_blocker(
            blockers,
            "STRUCTURED_FILE_PLAN_LIMIT",
            "collection has too many items for one migration",
        )
    source_snapshot = _source_snapshot(snapshot, corpus)
    records = sorted(snapshot.records, key=lambda record: record.identity.key)[:_MAX_PLAN_ITEMS]
    item_source_paths = {record.source.path for record in records}
    occupied = {
        path
        for path, kind, _digest in snapshot.source_inventory
        if kind == "file" and path not in item_source_paths
    }
    item_paths: dict[str, str] = {}
    base_paths: dict[str, list[str]] = {}
    for record in records:
        if manifest.item_filename is None:
            final_path = record.source.path
        else:
            base = collections.render_item_path(manifest, record.values, record.identity.key)
            base_paths.setdefault(base, []).append(record.identity.key)
            final_path = collections.render_item_path(
                manifest,
                record.values,
                record.identity.key,
                occupied_paths=occupied,
            )
        occupied.add(final_path)
        item_paths[record.identity.key] = final_path

    collisions = []
    for base, keys in sorted(base_paths.items()):
        resolved = [item_paths[key] for key in sorted(keys)]
        if len(keys) > 1 or any(path != base for path in resolved):
            collisions.append(
                {
                    "path": base,
                    "item_keys": sorted(keys),
                    "resolved_paths": resolved,
                }
            )

    provisional_moves = [
        (record.identity.key, record.source.path, item_paths[record.identity.key])
        for record in records
        if record.source.path != item_paths[record.identity.key]
    ]
    moving_sources = {source for _key, source, _target in provisional_moves}
    for _key, source, target in provisional_moves:
        if target in corpus and target not in moving_sources:
            _add_blocker(
                blockers,
                "STRUCTURED_FILE_DESTINATION_EXISTS",
                "a rendered item destination is already occupied",
            )
        if source.rsplit("/", 1)[0] != target.rsplit("/", 1)[0]:
            _add_blocker(
                blockers,
                "STRUCTURED_FILE_CROSS_DIRECTORY",
                "structured item migration cannot cross collection directories",
            )

    stem_counts: dict[str, int] = {}
    for relative in corpus:
        stem = Path(relative).stem
        stem_counts[stem] = stem_counts.get(stem, 0) + 1

    texts: dict[str, str] = {}
    link_counts: dict[str, int] = {}
    for relative, data in corpus.items():
        try:
            texts[relative] = _decode_markdown(data)
        except collections.CollectionError:
            # A non-UTF-8 file can contain no safely rewritable Markdown for this
            # operation. Refuse without identifying a potentially withheld path.
            _add_blocker(
                blockers,
                "UNREADABLE_INBOUND_LINK",
                "an inbound-link candidate is not safely readable",
            )

    for _key, source, target in provisional_moves:
        allow_bare = stem_counts.get(Path(source).stem, 0) == 1
        for relative in sorted(texts):
            rewritten, changed, ambiguous = _rewrite_links(
                texts[relative], source, target, allow_bare=allow_bare
            )
            if ambiguous:
                _add_blocker(
                    blockers,
                    "AMBIGUOUS_INBOUND_LINK",
                    "an inbound wikilink cannot be resolved to one moved item",
                )
            if not changed:
                continue
            if relative not in item_source_paths and not _path_is_transactionally_writable(
                root, relative
            ):
                _add_blocker(
                    blockers,
                    "IMMUTABLE_INBOUND_LINK",
                    "an inbound link is not transactionally writable",
                )
                continue
            texts[relative] = rewritten
            if relative != source:
                link_counts[relative] = link_counts.get(relative, 0) + changed

    resolver = record_formats.presentation_relationship_resolver(
        root,
        manifest,
        snapshot,
        authorize_path=authorize,
        item_paths=item_paths,
    )
    presentations: list[dict[str, Any]] = []
    for record in records:
        source = record.source.path
        original = _decode_markdown(corpus[source])
        current = texts[source]
        try:
            rendered, action = _render_presentation(
                current,
                manifest,
                record.values,
                resolve_relationship=resolver,
            )
        except collections.CollectionError as error:
            _add_blocker(
                blockers,
                error.code,
                "a managed item presentation cannot be migrated safely",
            )
            rendered, action = current, None
        texts[source] = rendered
        if action is not None and rendered != original:
            presentations.append(
                {
                    "item_key": record.identity.key,
                    "path": item_paths[record.identity.key],
                    "action": action,
                    "before_hash": record.source.hash,
                    "after_hash": _text_hash(rendered),
                }
            )

    final_text: dict[str, str] = {}
    for relative, text in texts.items():
        original = _decode_markdown(corpus[relative])
        if text != original:
            final_text[relative] = text

    moves: list[_Move] = []
    record_by_key = {record.identity.key: record for record in records}
    for key, source, target in provisional_moves:
        record = record_by_key[key]
        moves.append(
            _Move(
                key,
                source,
                target,
                record.source.hash,
                _text_hash(final_text.get(source, texts[source])),
            )
        )

    inbound_rewrites = []
    for relative, count in sorted(link_counts.items()):
        before = hashlib.sha256(corpus[relative]).hexdigest()
        after = _text_hash(final_text.get(relative, texts[relative]))
        if before != after:
            inbound_rewrites.append(
                {
                    "path": relative,
                    "before_hash": before,
                    "after_hash": after,
                    "links": count,
                }
            )

    blockers.sort(key=lambda item: (item["code"], item["reason"]))
    collisions.sort(key=lambda item: item["path"])
    presentations.sort(key=lambda item: item["item_key"])
    public_moves = [
        {
            "item_key": move.item_key,
            "from": move.source,
            "to": move.target,
            "before_hash": move.before_hash,
            "after_hash": move.after_hash,
        }
        for move in moves
    ]
    identity_payload = {
        "version": 1,
        "collection_id": manifest.collection_id,
        "source_snapshot": source_snapshot,
        "moves": public_moves,
        "presentations": presentations,
        "inbound_rewrites": inbound_rewrites,
        "collisions": collisions,
        "blockers": blockers,
    }
    plan_id = _canonical_hash(identity_payload)
    public = {
        "operation": "preview",
        "collection_id": manifest.collection_id,
        "manifest_path": manifest.path,
        "plan_id": plan_id,
        "source_snapshot": source_snapshot,
        "moves": public_moves,
        "presentations": presentations,
        "inbound_rewrites": inbound_rewrites,
        "collisions": collisions,
        "blockers": blockers,
        "totals": {
            "moves": len(public_moves),
            "presentations": len(presentations),
            "inbound_rewrites": len(inbound_rewrites),
            "blockers": len(blockers),
        },
        "truncated": truncated or len(blockers) == _MAX_BLOCKERS,
    }
    return _Plan(
        public,
        manifest,
        snapshot,
        tuple(moves),
        final_text,
        corpus_guards,
        item_paths,
    )


def preview(vault_root: Path, collection: str | Path) -> dict[str, Any]:
    """Return one deterministic, read-only structured-file migration plan."""
    return _build_plan(Path(vault_root), collection).public


def _receipt_path(root: Path, plan_id: str) -> Path:
    return root / vault.kb_prefix() / "_Governance" / "structured-files" / f"{plan_id}.json"


def _load_receipt(root: Path, plan_id: str, source_snapshot: str) -> dict[str, Any] | None:
    path = _receipt_path(root, plan_id)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("receipt_version") != _RECEIPT_VERSION
        or value.get("plan_id") != plan_id
        or value.get("source_snapshot") != source_snapshot
        or value.get("outcome") != "committed"
        or not isinstance(value.get("inverse"), list)
    ):
        raise collections.CollectionError(
            "INVALID_STRUCTURED_FILE_RECEIPT", "migration receipt is invalid"
        )
    for entry in value["inverse"]:
        if not isinstance(entry, dict) or set(entry) != {
            "before_path",
            "after_path",
            "before_hash",
            "after_hash",
        }:
            raise collections.CollectionError(
                "INVALID_STRUCTURED_FILE_RECEIPT", "migration receipt is invalid"
            )
        after = root / entry["after_path"]
        try:
            actual = hashlib.sha256(after.read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != entry["after_hash"]:
            raise collections.CollectionError(
                "STALE_STRUCTURED_FILE_PLAN", "applied migration state has changed"
            )
        if entry["before_path"] != entry["after_path"] and os.path.lexists(
            root / entry["before_path"]
        ):
            raise collections.CollectionError(
                "STALE_STRUCTURED_FILE_PLAN", "applied migration state has changed"
            )
    return {**value, "outcome": "replayed"}


def _validate_apply_inputs(plan_id: str, source_snapshot: str, why: str) -> None:
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise collections.CollectionError(
            "INVALID_STRUCTURED_FILE_PLAN", "a complete preview plan identity is required"
        )
    if not isinstance(source_snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", source_snapshot):
        raise collections.CollectionError(
            "INVALID_STRUCTURED_FILE_PLAN", "the exact preview source snapshot is required"
        )
    if (
        not isinstance(why, str)
        or not why.strip()
        or "\n" in why
        or "\r" in why
        or len(why.encode("utf-8")) > 512
    ):
        raise collections.CollectionError(
            "INVALID_MUTATION_REASON", "a bounded single-line reason is required"
        )


def _inverse(plan: _Plan) -> list[dict[str, str]]:
    moves = {move.source: move for move in plan.moves}
    entries: list[dict[str, str]] = []
    affected = set(plan.final_text) | set(moves)
    for source in sorted(affected):
        move = moves.get(source)
        target = move.target if move is not None else source
        before = plan.guards[source].expected_content_hash
        if before is None:  # pragma: no cover - every corpus guard binds content
            raise collections.CollectionError(
                "INVALID_STRUCTURED_FILE_PLAN", "migration source lacks an exact hash"
            )
        after_text = plan.final_text.get(source)
        after = _text_hash(after_text) if after_text is not None else before
        entries.append(
            {
                "before_path": source,
                "after_path": target,
                "before_hash": before,
                "after_hash": after,
            }
        )
    return entries


def _stage_moves(root: Path, moves: tuple[_Move, ...], plan_id: str) -> list[tuple[str, str, str]]:
    staged: list[tuple[str, str, str]] = []
    try:
        for index, move in enumerate(moves):
            temporary = f"{move.source.rsplit('/', 1)[0]}/.structured-{plan_id[:16]}-{index:04d}.md"
            if os.path.lexists(root / temporary):
                raise collections.CollectionError(
                    "STRUCTURED_FILE_TEMPORARY_EXISTS",
                    "a migration staging path is already occupied",
                )
            move_file._held_rename(root, move.source, temporary)
            staged.append((move.source, temporary, move.target))
        for _source, temporary, target in staged:
            move_file._held_rename(root, temporary, target)
    except Exception:
        _rollback_moves(root, staged)
        raise
    return staged


def _rollback_moves(root: Path, staged: list[tuple[str, str, str]]) -> None:
    failures: list[str] = []
    for source, temporary, target in reversed(staged):
        try:
            if os.path.lexists(root / target):
                move_file._held_rename(root, target, temporary)
            if os.path.lexists(root / temporary):
                move_file._held_rename(root, temporary, source)
        except Exception:  # noqa: BLE001 - report complete rollback failure set
            failures.append(source)
    if failures:
        raise collections.CollectionError(
            "STRUCTURED_FILE_ROLLBACK_FAILED",
            "migration rollback could not restore every source path",
        )


def apply(
    vault_root: Path,
    collection: str | Path,
    *,
    plan_id: str,
    source_snapshot: str,
    why: str,
) -> dict[str, Any]:
    """Apply exactly one unchanged preview, or replay its terminal receipt."""
    _validate_apply_inputs(plan_id, source_snapshot, why)
    root = Path(vault_root)
    replay = _load_receipt(root, plan_id, source_snapshot)
    if replay is not None:
        return replay

    with writer_lease.active_manager().mutation_guard(root, operation="structured_files"):
        replay = _load_receipt(root, plan_id, source_snapshot)
        if replay is not None:
            return replay
        plan = _build_plan(root, collection)
        if plan.public["plan_id"] != plan_id or plan.public["source_snapshot"] != source_snapshot:
            raise collections.CollectionError(
                "STALE_STRUCTURED_FILE_PLAN", "migration preview is stale"
            )
        if plan.public["blockers"] or plan.public["truncated"]:
            raise collections.CollectionError(
                "BLOCKED_STRUCTURED_FILE_PLAN", "migration preview has unresolved blockers"
            )
        if not plan.moves and not plan.final_text:
            raise collections.CollectionError(
                "EMPTY_STRUCTURED_FILE_PLAN", "migration preview contains no changes"
            )

        inverse = _inverse(plan)
        receipt = {
            "_structured_files_receipt": "exomem.structured-files-migration",
            "receipt_version": _RECEIPT_VERSION,
            "operation": "structured-files",
            "collection_id": plan.manifest.collection_id,
            "manifest_path": plan.manifest.path,
            "plan_id": plan_id,
            "source_snapshot": source_snapshot,
            "outcome": "committed",
            "rationale": why,
            "inverse": inverse,
        }
        receipt_path = _receipt_path(root, plan_id)
        receipt_rel = receipt_path.relative_to(root).as_posix()
        receipt_guard = vault.PathGuard.capture(root, receipt_rel, leaf_policy="absent")
        audit_body = "Applied structured-file migration " + json.dumps(
            {
                "collection_id": plan.manifest.collection_id,
                "plan_id": plan_id,
                "source_snapshot": source_snapshot,
                "moves": len(plan.moves),
                "rewrites": len(plan.final_text),
                "inverse": inverse,
                "rationale": why,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        log_plan = vault.plan_log_writes(
            root,
            date_iso=dt.date.today().isoformat(),
            op="maintain_memory",
            rel_path_no_ext=plan.manifest.storage.source.removesuffix(".md"),
            body=audit_body,
            operation_token=f"structured-files:{plan_id}",
        )
        if log_plan.warning is not None:
            raise collections.CollectionError(
                "STRUCTURED_FILE_AUDIT_UNAVAILABLE", "Knowledge Base/log.md is required"
            )

        planned_paths = {
            plan.manifest.path,
            receipt_rel,
            *(move.source for move in plan.moves),
            *(move.target for move in plan.moves),
            *plan.final_text,
            *(write.path.relative_to(root).as_posix() for write in log_plan.writes),
        }
        record_governance.precommit_authorize_mutation(
            root,
            plan.manifest,
            plan.snapshot,
            planned_paths=planned_paths,
        )
        for guard in plan.snapshot.path_guards:
            guard.recheck(root)
        for guard in plan.snapshot.directory_guards:
            guard.recheck(root)
        for relative in plan.final_text:
            if relative not in {move.source for move in plan.moves}:
                plan.guards[relative].recheck(root)

        staged: list[tuple[str, str, str]] = []
        try:
            staged = _stage_moves(root, plan.moves, plan_id)
            move_by_source = {move.source: move for move in plan.moves}
            writes: list[vault.PlannedWrite] = []
            for source, text in sorted(plan.final_text.items()):
                move = move_by_source.get(source)
                target = move.target if move is not None else source
                writes.append(
                    vault.PlannedWrite(
                        root / target,
                        text,
                        guard=None if move is not None else plan.guards[source],
                        expected_hash=move.before_hash if move is not None else None,
                    )
                )
            writes.extend(log_plan.writes)
            writes.append(
                vault.PlannedWrite(
                    receipt_path,
                    json.dumps(
                        receipt,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    create_only=True,
                    guard=receipt_guard,
                    expected_hash=vault.MISSING_CONTENT_HASH,
                    ensure_directories=(receipt_path.parent,),
                )
            )
            vault.batch_atomic_write(writes, vault_root=root)
        except Exception as error:
            if staged:
                _rollback_moves(root, staged)
            if isinstance(error, collections.CollectionError):
                raise
            raise collections.CollectionError(
                "STRUCTURED_FILE_PUBLICATION_FAILED",
                "structured-file migration did not commit",
            ) from error

    try:
        from . import file_watcher, index_sync

        old_paths = [move.source for move in plan.moves]
        new_paths = [root / move.target for move in plan.moves]
        if old_paths:
            file_watcher.register_self_delete(root, old_paths)
            index_sync.delete_after_remove(root, old_paths)
        if new_paths:
            file_watcher.register_self_write(root, new_paths)
    except Exception:  # noqa: BLE001 - derived indexes reconcile from Markdown
        pass
    return receipt


_ = semantic_writes.rewrite_wikilinks_for_move  # keep the shared rewrite contract visible
