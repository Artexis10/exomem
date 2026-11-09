"""The `fold-duplicate` curation step: one copy of a Source or Evidence page folds into its canonical page.

The copy and the canonical page hold identical bytes (`duplicates`). The fold carries everything the copy
held onto the canonical page, repoints every reference to the copy, marks the copy superseded, and moves the
copy's original file to the vault trash (`fold_leaf`). A text Source has no separate file, so its marked page
stays as the whole copy. The canonical page's own payload and original are never touched.

The fold earns its place among the steps that retire content by one invariant, which `fold_leaf` states: the
original moves to the trash only after the step re-proves, immediately before the move, that the canonical
page's original holds the same bytes. The step seals every page it rewrites as a line delta in both
directions, so resume decides from live state and `unfold` reverses the fold byte for byte from the sealed
manifest alone, with no full copy of any page in the plan.

A plan may fold several copies into one canonical page. `Projection` carries each page's text from one
step to the next, and each page row seals the whole chain of hashes the page takes across the plan, so a
preview, a resume and an undo accept the page at any state of that chain and no other.
"""

from __future__ import annotations

import datetime as dt
import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    add,
    adoption_plan,
    curation,
    delete_file,
    duplicates,
    evidence_reuse,
    fold_leaf,
    frontmatter_update,
    memory_refs,
    preserve,
    provenance,
    semantic_writes,
    temporal,
    vault,
)
from .curation import CurationError

#: The share of a plan's size that the sealed manifests of its folds may take. The undo of a fold embeds the
#: fold's manifest in a plan of its own, so the manifests must fit that plan before any fold runs.
MANIFEST_BUDGET = curation.MAX_PLAN_BYTES // 2

# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_MANIFEST_FIELDS = frozenset({"lane", "payload", "now", "canonical", "copy", "pages", "referenced_by", "artifact", "merged"})
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_PAGE_FIELDS = frozenset({"path", "before", "after", "chain", "delta"})
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_ARTIFACT_FIELDS = frozenset({"path", "canonical_path", "hash", "trash"})
# nosemgrep: ep-word-set -- the sealed manifest's own field names, validated on every read.
_MERGED_FIELDS = frozenset({"families", "mappings", "back_references"})

FOLDED = "folded"
OPEN = "open"


# ---------------------------------------------------------------- closed schemas


def validate_args(args: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """The closed `fold-duplicate` arguments: the canonical page, the copy, and a one-line reason."""
    where = f"steps[{ordinal}].args"
    args["canonical"] = fold_leaf.page_arg(args["canonical"], f"{where}.canonical")
    args["copy"] = fold_leaf.page_arg(args["copy"], f"{where}.copy")
    if args["canonical"]["path"] == args["copy"]["path"]:
        raise fold_leaf.invalid(where, "must name two different pages")
    args["why"] = curation._require_string(args["why"], f"{where}.why", max_chars=curation.MAX_WHY_CHARS)  # noqa: SLF001
    return args


def validate_unfold_args(args: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """The private undo's arguments: the sealed manifest of the fold it reverses, nothing else."""
    args["fold"] = _manifest(args["fold"], f"steps[{ordinal}].args.fold")
    return args


def _paths(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise fold_leaf.invalid(where, "has an invalid path list")
    return [curation.normalize_target_path(item, field=where, allow_fold=True) for item in value]


def _page_row(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PAGE_FIELDS:
        raise fold_leaf.invalid(where, "has an invalid page row")
    chain, delta = value["chain"], value["delta"]
    if (
        not fold_leaf.is_hex64(value["before"])
        or not fold_leaf.is_hex64(value["after"])
        or not isinstance(chain, list)
        or not all(fold_leaf.is_hex64(item) for item in chain)
        or value["before"] not in chain
        or value["after"] not in chain
        or not isinstance(delta, list)
    ):
        raise fold_leaf.invalid(where, "has an invalid page row")
    for op in delta:
        if not (
            isinstance(op, list)
            and len(op) == 4
            and all(type(at) is int and at >= 0 for at in op[:2])
            and all(isinstance(part, list) and all(isinstance(line, str) for line in part) for part in op[2:])
        ):
            raise fold_leaf.invalid(where, "has an invalid line delta")
    if (value["before"] == value["after"]) != (not delta):
        raise fold_leaf.invalid(where, "has a delta that disagrees with its hashes")
    return {**value, "path": curation.normalize_target_path(value["path"], field=where, allow_fold=True)}


def _manifest(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise fold_leaf.invalid(where, "is not a sealed duplicate-fold manifest")
    pages = value["pages"]
    if not isinstance(pages, list) or len(pages) < 2:
        raise fold_leaf.invalid(where, "has no pages")
    rows = [_page_row(item, where) for item in pages]
    canonical = curation.normalize_target_path(value["canonical"], field=where, allow_fold=True)
    copy = curation.normalize_target_path(value["copy"], field=where, allow_fold=True)
    if [rows[0]["path"], rows[1]["path"]] != [canonical, copy]:
        raise fold_leaf.invalid(where, "does not list its canonical page and copy first")
    if not fold_leaf.is_hex64(value["payload"]):
        raise fold_leaf.invalid(where, "has an invalid payload hash")
    curation._require_string(value["lane"], f"{where}.lane")  # noqa: SLF001
    try:
        dt.datetime.fromisoformat(value["now"])
    except (TypeError, ValueError) as error:
        raise fold_leaf.invalid(where, "has an invalid time") from error
    artifact = value["artifact"]
    if artifact is not None:
        if not isinstance(artifact, Mapping) or set(artifact) != _ARTIFACT_FIELDS or not fold_leaf.is_hex64(artifact["hash"]):
            raise fold_leaf.invalid(where, "has an invalid original")
        artifact = {
            **artifact,
            **{
                key: curation.normalize_target_path(artifact[key], field=where, allow_fold=True)
                for key in ("path", "canonical_path", "trash")
            },
        }
    merged = value["merged"]
    if not isinstance(merged, Mapping) or set(merged) != _MERGED_FIELDS or not all(isinstance(item, list) for item in merged.values()):
        raise fold_leaf.invalid(where, "has an invalid merge record")
    return {
        **value,
        "canonical": canonical,
        "copy": copy,
        "pages": rows,
        "artifact": artifact,
        "referenced_by": _paths(value["referenced_by"], where),
    }


# ---------------------------------------------------------------- the projection of a chained plan


@dataclass
class Projection:
    """The pages that earlier `fold-duplicate` steps of one plan rewrite, as the next step reads them.

    `chains[path]` lists the page's hashes from the bytes the vault holds to the projected text, so the page
    that two folds share (the canonical page, a note that cites two copies) keeps one history across steps.
    """

    texts: dict[str, str] = field(default_factory=dict)
    chains: dict[str, list[str]] = field(default_factory=dict)


def _current(vault_root: Path, path: str, projection: Projection, expected: str | None) -> tuple[str, list[str]]:
    """The page's text as this step reads it, and its chain so far. `expected` is the reviewed hash, or None."""
    if path in projection.texts:
        chain = projection.chains[path]
        # The reviewer saw the page as the vault holds it, which is the chain's first hash.
        if expected is not None and expected != chain[0]:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{path!r} changed since it was reviewed")
        return projection.texts[path], list(chain)
    if expected is not None:
        return fold_leaf.text_at(vault_root, path, expected), [expected]
    text, actual = curation._guarded_text(vault_root, path, stale=True)  # noqa: SLF001
    return text, [actual]


# ---------------------------------------------------------------- line deltas


def _delta(before: str, after: str) -> list[list[Any]]:
    """The changed line runs from `before` to `after`, each with its position in both texts and both sides' lines."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    return [
        [i1, j1, old[i1:i2], new[j1:j2]]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes()
        if tag != "equal"
    ]


def _apply(text: str, delta: Sequence[Sequence[Any]], *, forward: bool, path: str) -> str:
    """`text` with the delta applied forward, or in reverse, refusing as stale when its lines are not there."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    position = 0
    for at_old, at_new, old, new in delta:
        start, held, put = (at_old, old, new) if forward else (at_new, new, old)
        if start < position or lines[start : start + len(held)] != held:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{path!r} no longer holds the lines the fold sealed")
        out.extend(lines[position:start])
        out.extend(put)
        position = start + len(held)
    out.extend(lines[position:])
    return "".join(out)


# ---------------------------------------------------------------- what makes two pages one


def _payload(path: str, text: str) -> duplicates.PagePayload:
    """The payload identity of a live, unprotected page that carries a memory reference, or a refusal."""
    payload = duplicates.derive_page_payload(path, text.encode("utf-8"))
    if payload is None or payload.ref is None:
        raise fold_leaf.refuse(
            "CURATION_STEP_INVALID", f"{path!r} is not a live Source or Evidence page that holds a payload and a memory reference"
        )
    if payload.protected:
        raise fold_leaf.refuse(
            "CURATION_TARGET_PROTECTED", f"{path!r} is raw-protected, and a fold never rewrites a protected page"
        )
    return payload


def _pair(
    vault_root: Path, canonical: tuple[str, str], copy: tuple[str, str], *, code: str
) -> tuple[duplicates.PagePayload, duplicates.PagePayload]:
    """Both payloads, once they are proved identical by re-hash and the canonical original is readable.

    `code` is how a mismatch refuses: a request that never held (`CURATION_STEP_INVALID`) or a sealed
    binding that stopped holding (`CURATION_BINDING_STALE`).
    """
    kept, retired = _payload(*canonical), _payload(*copy)
    if kept.lane != retired.lane:
        raise fold_leaf.refuse(code, "a Source and an Evidence page never fold into each other")
    if kept.sha256 != retired.sha256 or (kept.artifact_path is None) != (retired.artifact_path is None):
        raise fold_leaf.refuse(code, "the two pages do not hold identical bytes")
    for path, payload in ((canonical[0], kept), (copy[0], retired)):
        if payload.artifact_path is not None and not duplicates.original_intact(vault_root, path, payload):
            raise fold_leaf.refuse(code, f"the original of {path!r} no longer hashes to the payload the page records")
    return kept, retired


# ---------------------------------------------------------------- the merge onto the canonical page


def _merge(
    vault_root: Path,
    *,
    lane: str,
    canonical: tuple[str, str],
    copy: tuple[str, str],
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, list[Any]]]:
    """The fields and list items that carry what the copy held, and the record of them for the preview."""
    kept = vault.parse_frontmatter(canonical[1], strict=True)[0]
    retired = vault.parse_frontmatter(copy[1], strict=True)[0]
    fields: dict[str, Any] = {}
    appended: dict[str, list[str]] = {}
    merged: dict[str, list[Any]] = {"families": [], "mappings": [], "back_references": []}
    if lane == duplicates.EVIDENCE:
        homes = (evidence_reuse.folder_family(vault_root, canonical[0]), evidence_reuse.folder_family(vault_root, copy[0]))
        if None in homes:
            raise fold_leaf.refuse("CURATION_STEP_INVALID", "an Evidence page outside the family folders cannot fold")
        held = evidence_reuse.listed_families(kept, homes[0])
        carried = evidence_reuse.listed_families(retired, homes[1])
        if held is None or carried is None:
            raise fold_leaf.refuse("CURATION_STEP_INVALID", "an Evidence page with a malformed `families` list cannot fold")
        added: list[evidence_reuse.Family] = []
        for pair in carried:
            if not any(evidence_reuse.same_family(pair, other) for other in (*held, *added)):
                added.append(pair)
        if added:
            fields["families"] = [{"scope": scope, "category": category} for scope, category in (*held, *added)]
            appended["tags"] = [tag for pair in added for tag in preserve.family_tags(*pair)]
            merged["families"] = [list(pair) for pair in added]
    else:
        current = dict(kept)
        for mapping in adoption_plan.page_mappings(retired):
            if adoption_plan.mapping_recorded(current, mapping):
                continue
            more = adoption_plan.mapping_fields(current, mapping)
            if more is None:
                raise fold_leaf.refuse("CURATION_STEP_INVALID", "a Source page with a malformed `also_imported_from` cannot fold")
            current.update(more)
            merged["mappings"].append(mapping)
        if merged["mappings"]:
            fields["also_imported_from"] = current["also_imported_from"]
    listed = set(fold_leaf.ingested_entries(kept.get("ingested_into")))
    carried_entries = [entry for entry in fold_leaf.ingested_entries(retired.get("ingested_into")) if entry not in listed]
    if carried_entries:
        merged["back_references"] = carried_entries
        if "ingested_into" in kept:
            appended["ingested_into"] = carried_entries
        else:
            fields["ingested_into"] = carried_entries
    return fields, appended, merged


def _repoint(
    text: str, moves: Sequence[tuple[str, str]], identity: tuple[str, str] | None
) -> str:
    """`text` with each link and memory reference to the copy naming the canonical page, origin carriers excepted."""
    for old, new in moves:
        text = semantic_writes.rewrite_wikilinks_for_move(text, old, new)[0]
    if identity is None:
        return text
    old_id, new_id = identity
    inside_carrier = provenance.in_carrier(text)

    def swap(match) -> str:
        if inside_carrier(match.start(), match.end()) or memory_refs.normalize_id(match.group(1)) != old_id:
            return match.group(0)
        return f"{memory_refs.REF_PREFIX}{new_id}"

    return fold_leaf.MEMORY_REFERENCE.sub(swap, text)


def _retire(vault_root: Path, text: str, canonical_path: str, now: dt.datetime) -> str:
    """The copy's page marked superseded by the canonical page, through the writer that marks recap revisions."""
    from .replace import _mark_superseded

    link = vault.render_wikilink_target(canonical_path.removesuffix(".md"), vault_root)
    marked = _mark_superseded(text, link, temporal.stamp(now))
    if marked == text:
        raise fold_leaf.refuse("CURATION_STEP_INVALID", "the copy's frontmatter cannot take a supersession mark")
    return frontmatter_update.updated_text(marked, {"duplicate_of": f"[[{link}]]"}, {})


# ---------------------------------------------------------------- proposal


def _referencing(
    vault_root: Path, copy_path: str, copy_ref: str, artifact: str | None, canonical_path: str
) -> list[str]:
    """The live pages outside the copy that cite it or its original, whatever their lifecycle class."""
    targets = {copy_path: copy_ref} | ({artifact: None} if artifact is not None else {})
    cited = fold_leaf.referencing_pages(vault_root, targets, frozenset({copy_path}))
    pages = sorted({page for found in cited.values() for page in found})
    if canonical_path in pages:
        raise fold_leaf.refuse("CURATION_STEP_INVALID", "the canonical page cites the copy, and its body is never rewritten")
    for page in pages:
        refusal = repoint_refusal(page)
        if refusal is not None:
            raise refusal
    return pages


def repoint_refusal(page: str) -> CurationError | None:
    """The refusal that stops a fold from rewriting `page` when it cites a copy, or None when it may.

    The audit asks this too, so a finding never suggests a canonical page whose copy the fold would refuse.
    """
    if vault.in_append_only_tree(page) is not None:
        return fold_leaf.refuse(
            "CURATION_TARGET_PROTECTED", f"{page!r} cites the copy and sits in an append-only tree, so the fold cannot repoint it"
        )
    try:
        curation.normalize_target_path(page, field="the citing page", allow_fold=True)
    except CurationError as error:
        return error
    return None


def prepare(vault_root: Path, args: Mapping[str, Any], projection: Projection) -> dict[str, Any]:
    """The binding of one fold: every page and file it touches, sealed before anything changes."""
    fold_leaf.require_authority(vault_root)
    canonical_path, copy_path = args["canonical"]["path"], args["copy"]["path"]
    canonical_text, canonical_chain = _current(vault_root, canonical_path, projection, args["canonical"]["expected_hash"])
    copy_text, copy_chain = _current(vault_root, copy_path, projection, args["copy"]["expected_hash"])
    kept, retired = _pair(vault_root, (canonical_path, canonical_text), (copy_path, copy_text), code="CURATION_STEP_INVALID")
    now = temporal.now()
    artifact = None
    if retired.artifact_path is not None and kept.artifact_path is not None:
        trash = delete_file.planned_trash_path(vault_root, retired.artifact_path, now)
        if not curation._guarded_absent(vault_root, trash):  # noqa: SLF001
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{trash!r} is already taken")
        artifact = {"path": retired.artifact_path, "canonical_path": kept.artifact_path, "hash": retired.sha256, "trash": trash}
    cited = _referencing(vault_root, copy_path, retired.ref, retired.artifact_path, canonical_path)
    moves = [(copy_path, canonical_path)]
    if artifact is not None:
        moves.append((artifact["path"], artifact["canonical_path"]))
    identity = (memory_refs.normalize_id(retired.ref), memory_refs.normalize_id(kept.ref))
    fields, appended, merged = _merge(vault_root, lane=kept.lane, canonical=(canonical_path, canonical_text), copy=(copy_path, copy_text))
    texts: dict[str, tuple[str, str, list[str]]] = {
        canonical_path: (canonical_text, frontmatter_update.updated_text(canonical_text, fields, appended), canonical_chain),
        copy_path: (copy_text, _retire(vault_root, copy_text, canonical_path, now), copy_chain),
    }
    for page in cited:
        text, chain = _current(vault_root, page, projection, None)
        texts[page] = (text, _repoint(text, moves, identity), chain)
    pages = []
    for path, (before, after, chain) in texts.items():
        after_hash = vault.content_hash(after)
        pages.append(
            {
                "path": path,
                "before": vault.content_hash(before),
                "after": after_hash,
                "chain": chain if after_hash in chain else [*chain, after_hash],
                "delta": _delta(before, after),
            }
        )
        if after != before:
            projection.texts[path] = after
            projection.chains[path] = list(pages[-1]["chain"])
    manifest = {
        "lane": kept.lane,
        "payload": kept.sha256,
        "now": now.isoformat(),
        "canonical": canonical_path,
        "copy": copy_path,
        "pages": pages,
        "referenced_by": cited,
        "artifact": artifact,
        "merged": merged,
    }
    return {
        "path": canonical_path,
        "before_hash": pages[0]["before"],
        "after_hash": pages[0]["after"],
        "prepared": manifest,
        "effect_before": fold_before(manifest),
        "effect_after": fold_after(manifest),
    }


def seal_chains(items: Sequence[dict[str, Any]], projection: Projection) -> None:
    """Give each page row of the plan's folds the whole chain its page takes across the plan.

    A step is prepared before the steps after it, so it cannot know the later hashes of a page it shares.
    """
    for item in items:
        if item.get("kind") != curation.FOLD_DUPLICATE_STEP_KIND:
            continue
        for row in item["prepared"]["pages"]:
            row["chain"] = list(projection.chains.get(row["path"], row["chain"]))


def sealed_size(items: Sequence[Mapping[str, Any]]) -> int:
    """The bytes the sealed manifests of a plan's folds take, which the undo plan embeds again."""
    return sum(
        len(curation.canonical_json_bytes(item["prepared"]))
        for item in items
        if item.get("kind") == curation.FOLD_DUPLICATE_STEP_KIND
    )


def changed_paths(item: Mapping[str, Any]) -> set[str]:
    """The paths a sealed fold or undo writes: the rows whose bound state differs before and after."""
    before = {row["path"]: row for row in item["effect_before"]}
    return {row["path"] for row in item["effect_after"] if before.get(row["path"]) != row}


def live_states(binding: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """Each page of a sealed fold or undo mapped to every hash it may hold while the plan runs."""
    prepared = binding.get("prepared") or {}
    manifest = prepared.get("fold", prepared)
    return {row["path"]: frozenset(row["chain"]) for row in manifest.get("pages", ())}


def fold_before(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What a fold binds: each page at its reviewed hash, the original in place beside the canonical original, the trash free."""
    rows: list[dict[str, Any]] = [{"path": page["path"], "content_hash": page["before"]} for page in manifest["pages"]]
    artifact = manifest["artifact"]
    if artifact is not None:
        rows += [
            {"path": artifact["path"], "content_hash": artifact["hash"]},
            {"path": artifact["canonical_path"], "content_hash": artifact["hash"]},
            {"path": artifact["trash"], "absent": True},
        ]
    return rows


def fold_after(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What a fold leaves: each page rewritten, the copy's original in trash, the canonical original untouched."""
    rows: list[dict[str, Any]] = [{"path": page["path"], "content_hash": page["after"]} for page in manifest["pages"]]
    artifact = manifest["artifact"]
    if artifact is not None:
        rows += [
            {"path": artifact["path"], "absent": True},
            {"path": artifact["canonical_path"], "content_hash": artifact["hash"]},
            {"path": artifact["trash"], "content_hash": artifact["hash"]},
        ]
    return rows


def prepare_unfold(vault_root: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    """The binding of the undo: the fold's sealed manifest, bound from its after state to its before state."""
    fold_leaf.require_authority(vault_root)
    manifest = args["fold"]
    first = manifest["pages"][0]
    return {
        "path": manifest["canonical"],
        "before_hash": first["after"],
        "after_hash": first["before"],
        "prepared": {"fold": manifest},
        "effect_before": fold_after(manifest),
        "effect_after": fold_before(manifest),
    }


# ---------------------------------------------------------------- blockers


def binding_blockers(vault_root: Path, item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Why a sealed fold or undo cannot run, deciding from live state so a half-done step resumes."""
    try:
        if item["kind"] == curation.FOLD_DUPLICATE_STEP_KIND:
            _check(vault_root, item["prepared"], undo=False)
        else:
            _check(vault_root, item["prepared"]["fold"], undo=True)
    except CurationError as error:
        return [{"code": error.code, "reason": error.reason}]
    return []


def _live(vault_root: Path, path: str, row: Mapping[str, Any]) -> str:
    """The page's live hash, which must be one it takes across the plan."""
    actual = curation._guarded_digest(vault_root, path, stale=True)  # noqa: SLF001
    if actual not in row["chain"]:
        raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{path!r} changed since it was reviewed")
    return actual


def _state(vault_root: Path, manifest: Mapping[str, Any]) -> str:
    """`FOLDED` once the copy's page holds its sealed superseded bytes, else `OPEN`. Every page must be in its chain."""
    copy_row = manifest["pages"][1]
    live = {row["path"]: _live(vault_root, row["path"], row) for row in manifest["pages"]}
    return FOLDED if live[copy_row["path"]] == copy_row["after"] else OPEN


def _original_state(vault_root: Path, manifest: Mapping[str, Any]) -> str | None:
    artifact = manifest["artifact"]
    if artifact is None:
        return None
    return fold_leaf.state(vault_root, path=artifact["path"], trash_path=artifact["trash"], expected_hash=artifact["hash"])


def _check(vault_root: Path, manifest: Mapping[str, Any], *, undo: bool) -> None:
    state = _state(vault_root, manifest)
    original = _original_state(vault_root, manifest)
    if undo:
        if state == OPEN and original == fold_leaf.MOVED:
            raise fold_leaf.refuse("CURATION_OUTCOME_UNCERTAIN", "the copy's page is restored while its original stays in trash")
        return
    if state == OPEN:
        if original == fold_leaf.MOVED:
            raise fold_leaf.refuse("CURATION_OUTCOME_UNCERTAIN", "the copy's original moved while its page stayed unmarked")
        _identity(vault_root, manifest)
    else:
        _canonical_original(vault_root, manifest)


def _identity(vault_root: Path, manifest: Mapping[str, Any]) -> None:
    """Re-prove from the live pages that the copy and the canonical page still hold identical bytes."""
    canonical, copy = manifest["canonical"], manifest["copy"]
    kept, retired = _pair(
        vault_root,
        (canonical, curation._guarded_text(vault_root, canonical, stale=True)[0]),  # noqa: SLF001
        (copy, curation._guarded_text(vault_root, copy, stale=True)[0]),  # noqa: SLF001
        code="CURATION_BINDING_STALE",
    )
    if kept.sha256 != manifest["payload"] or retired.sha256 != manifest["payload"]:
        raise fold_leaf.refuse("CURATION_BINDING_STALE", "the payload changed since it was sealed")


def _canonical_original(vault_root: Path, manifest: Mapping[str, Any]) -> None:
    """Refuse unless the canonical page's original still holds the sealed bytes. A text Source holds them in its page."""
    path = manifest["canonical"]
    payload = _payload(path, curation._guarded_text(vault_root, path, stale=True)[0])  # noqa: SLF001
    if payload.sha256 != manifest["payload"] or (
        payload.artifact_path is not None and not duplicates.original_intact(vault_root, path, payload)
    ):
        raise fold_leaf.refuse("CURATION_BINDING_STALE", f"the original of {path!r} no longer holds the sealed bytes")


# ---------------------------------------------------------------- apply


def _write_pages(vault_root: Path, manifest: Mapping[str, Any], *, undo: bool) -> int:
    """Commit every page that still holds the state the step starts from, in one atomic batch."""
    start, end = ("after", "before") if undo else ("before", "after")
    writes: list[vault.PlannedWrite] = []
    for row in manifest["pages"]:
        if row["before"] == row["after"]:
            continue
        text, guard = vault.read_guarded_text(vault_root, vault_root / row["path"])
        actual = vault.content_hash(text)
        if actual == row[end]:
            continue
        if actual != row[start]:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{row['path']!r} is not at the state the step was sealed from")
        updated = _apply(text, row["delta"], forward=not undo, path=row["path"])
        if vault.content_hash(updated) != row[end]:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{row['path']!r} no longer renders its sealed bytes")
        writes.append(vault.PlannedWrite(vault_root / row["path"], updated, guard=guard, expected_hash=actual))
    if not writes:
        return 0
    intents: list[object] = []
    try:
        committed = preserve.commit_media_sidecar_writes(
            vault_root, tuple(writes), post_commit_fanout=False, publication_intents_out=intents
        )
    except vault.ContentHashMismatchError as error:
        raise fold_leaf.refuse("CURATION_BINDING_STALE", "a page changed while the fold wrote it") from error
    add.deferred_fanout(vault_root, list(committed), created_paths=[], publication_intents=intents)
    return len(writes)


def fold(vault_root: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite every page in one batch, then move the copy's original once the canonical original reads back."""
    manifest = binding["prepared"]
    fold_leaf.require_authority(vault_root)
    state = _state(vault_root, manifest)
    original = _original_state(vault_root, manifest)
    written = 0
    if state == OPEN:
        if original == fold_leaf.MOVED:
            raise fold_leaf.refuse("CURATION_OUTCOME_UNCERTAIN", "the copy's original moved while its page stayed unmarked")
        _identity(vault_root, manifest)
        cited = fold_leaf.referencing_pages(
            vault_root,
            {manifest["copy"]: memory_refs.normalize_id(_payload(manifest["copy"], _text(vault_root, manifest["copy"])).ref)}
            | ({manifest["artifact"]["path"]: None} if manifest["artifact"] is not None else {}),
            frozenset({manifest["copy"]}),
        )
        extra = sorted({page for found in cited.values() for page in found} - set(manifest["referenced_by"]))
        if extra:
            raise fold_leaf.refuse("CURATION_BINDING_STALE", f"{extra[0]!r} now cites the copy")
        written = _write_pages(vault_root, manifest, undo=False)
    curation._fault_barrier("fold-duplicate-after-pages")  # noqa: SLF001
    moved: list[str] = []
    artifact = manifest["artifact"]
    if artifact is not None:
        if fold_leaf.move_to_trash(
            vault_root,
            path=artifact["path"],
            trash_path=artifact["trash"],
            expected_hash=artifact["hash"],
            verify=lambda: _canonical_original(vault_root, manifest),
            now=dt.datetime.fromisoformat(manifest["now"]),
            expected_dead_inbound=sorted(
                {manifest["copy"], *manifest["referenced_by"], *fold_leaf.listing_pages(vault_root, artifact["path"])}
            ),
        ):
            moved.append(artifact["path"])
        curation._fault_barrier("fold-duplicate-after-move")  # noqa: SLF001
    fold_leaf.commit_witness(vault_root)
    return {"path": manifest["canonical"], "copy": manifest["copy"], "pages_written": written, "moved": moved}


def _text(vault_root: Path, path: str) -> str:
    return curation._guarded_text(vault_root, path, stale=True)[0]  # noqa: SLF001


def unfold(vault_root: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the copy's original from its trash identity, then return every page to its sealed bytes."""
    manifest = binding["prepared"]["fold"]
    fold_leaf.require_authority(vault_root)
    _check(vault_root, manifest, undo=True)
    artifact = manifest["artifact"]
    restored: list[str] = []
    if artifact is not None:
        if fold_leaf.restore_from_trash(
            vault_root, path=artifact["path"], trash_path=artifact["trash"], expected_hash=artifact["hash"]
        ):
            restored.append(artifact["path"])
        curation._fault_barrier("unfold-duplicate-after-restore")  # noqa: SLF001
    written = _write_pages(vault_root, manifest, undo=True)
    fold_leaf.commit_witness(vault_root)
    return {"path": manifest["canonical"], "copy": manifest["copy"], "pages_written": written, "restored": restored}


# ---------------------------------------------------------------- the step an audit finding suggests


def suggested_step(
    step_id: str, *, canonical: tuple[str, str], copy: tuple[str, str], why: str
) -> dict[str, Any]:
    """The exact `fold-duplicate` step for one copy; each page is a `(path, content hash)` pair as read live."""
    return {
        "step_id": step_id,
        "kind": curation.FOLD_DUPLICATE_STEP_KIND,
        "args": {
            "canonical": {"path": canonical[0], "expected_hash": canonical[1]},
            "copy": {"path": copy[0], "expected_hash": copy[1]},
            "why": why,
        },
    }
