"""Reconcile tags that differ only by case, separator, or plural.

Tags are compared through ``vocabulary_fold.fold_term``. Tag usage has one
source, ``usage``: the lexical catalogue's per-page ``page.tags`` members,
after dropping every page the current reader may not see and every page in a
tree another subsystem owns. Within a fold group, pages are counted per
written normal form (lowercase, ``_`` and space as ``-``, as note, add, edit
and link write tags), and the canonical is the most-used normal form, so it is
never a raw spelling such as ``Machine_Learning``. A group whose two most-used
normal forms tie has no canonical and nothing in it is ever rewritten. A
spelling that differs from the canonical only by separator is that normal
form's own spelling, not a competitor.

No consumer rewrites an authored tag at write time:

- a write at prominence ``maximal`` keeps its tags and adds one warning line
  per minority variant (``advise_authored``);
- post-commit vocabulary delivery names the canonical tag in one
  ``vocabulary_advisory`` line at any non-``off`` level (``advisory_for_page``);
- ``maintain_memory(mode="tag-variants")`` lists the groups and, on an exact
  plan confirmation, rewrites minority variants in bounded batches
  (``preview`` / ``apply``). Only the ``tags`` frontmatter key changes; every
  other key and the body come through unchanged, and the log entry keeps each
  page's before and after tags plus the inverse mapping for rollback.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import vault
from .vocabulary_fold import fold_term

log = logging.getLogger(__name__)

FAMILY = "tag-variant/v1"
MODE = "tag-variants"
#: Pages rewritten per confirmed apply; later batches need a fresh preview.
BATCH_PAGES = 64
#: Groups listed per preview, most-used first.
GROUP_LIMIT = 50
_WHY_BYTES = 512
#: Seconds a usage index serves writes. Which spelling is most used moves
#: slowly, and re-reading the catalogue on every write cost tens of
#: milliseconds at 5,000 distinct tags, so write-time guidance reads a briefly
#: cached index. Only an unrestricted reader's index is cached.
INDEX_TTL_SECONDS = 120.0
_INDEX_CACHE: dict[str, tuple[float, Usage]] = {}
_INDEX_LOCK = threading.Lock()
_PLAIN_TAG = re.compile(r"[a-z0-9][a-z0-9_./-]*", re.IGNORECASE)
#: Infrastructure trees never counted or rewritten, beside the typed owners in
#: ``curation.PROTECTED_TREES``. Any dot-directory is skipped too.
_INFRASTRUCTURE_TREES = frozenset({"_trash", "_archive", "_attachments", ".exomem"})
_COMMENT_MARK = re.compile(r"(?:^|[ \t])#")
_STALE = "STALE_TAG_VARIANT_PLAN: the vault changed since the preview; preview again"


#: Edge whitespace a written form drops; the SQL aggregate trims the same set.
_WHITESPACE = " \t\n\r\f\v"
_EXCLUDE_LIMIT = 256


def written_form(spelling: str) -> str:
    """The form note, add, edit and link write for a casefolded catalogue spelling.

    Edge whitespace trimmed, ` ` and `_` as `-`; the SQL aggregate computes
    the same expression, so both count paths agree.
    """
    return str(spelling).strip(_WHITESPACE).replace(" ", "-").replace("_", "-")


def _owned_names() -> frozenset[str]:
    """Casefolded directory names whose trees another subsystem owns."""
    from .curation import PROTECTED_TREES

    names = PROTECTED_TREES | _INFRASTRUCTURE_TREES
    return frozenset(names | {name.replace("-", "_") for name in names})


def _owned_elsewhere(rel: str) -> bool:
    """True for a page inside a tree another subsystem owns, at any depth."""
    names = _owned_names()
    return any(
        part.startswith(".") or part.casefold() in names for part in rel.split("/")[1:-1]
    )


# ---------------- usage ----------------


@dataclass(frozen=True)
class Group:
    key: str
    canonical: str
    #: False on a tie: nothing in the group is advised or rewritten.
    decided: bool
    #: Catalogue spelling -> pages carrying it.
    spellings: dict[str, int]

    @property
    def uses(self) -> int:
        return sum(self.spellings.values())


def _spellings(members: Iterable[object]) -> frozenset[str]:
    return frozenset(
        member.casefold()
        for member in members
        if isinstance(member, str) and member.strip(_WHITESPACE)
    )


class Usage:
    """Pages per catalogue spelling and per written form; counts only.

    ``keys`` limits the fold groups indexed, for a caller that only needs to
    re-check the groups it touched.
    """

    def __init__(
        self,
        spellings: Mapping[str, int],
        forms: Mapping[str, int],
        keys: frozenset[str] | None = None,
    ):
        self.spellings: dict[str, int] = dict(spellings)
        self.forms: dict[str, int] = dict(forms)
        self.by_key: dict[str, set[str]] = {}
        for spelling in self.spellings:
            key = fold_term(spelling)
            if key and (keys is None or key in keys):
                self.by_key.setdefault(key, set()).add(spelling)

    @classmethod
    def from_rows(
        cls, rows: Iterable[tuple[str, Iterable[object]]], keys: frozenset[str] | None = None
    ) -> Usage:
        spellings: dict[str, int] = {}
        forms: dict[str, int] = {}
        for _path, members in rows:
            found = _spellings(members)
            for spelling in found:
                spellings[spelling] = spellings.get(spelling, 0) + 1
            for form in {written_form(spelling) for spelling in found}:
                forms[form] = forms.get(form, 0) + 1
        return cls(spellings, forms, keys)

    def group(self, key: str, extra: str | None = None) -> Group | None:
        """The fold group for ``key``, with ``extra`` as a spelling of no uses."""
        spellings = set(self.by_key.get(key, ()))
        if extra is not None:
            spellings.add(extra)
        if len(spellings) < 2:
            return None
        forms = {written_form(spelling) for spelling in spellings}
        ranked = sorted(forms, key=lambda form: (-self.forms.get(form, 0), len(form), form))
        top = self.forms.get(ranked[0], 0)
        decided = top > 0 and (len(ranked) == 1 or top > self.forms.get(ranked[1], 0))
        return Group(
            key,
            ranked[0],
            decided,
            {spelling: self.spellings.get(spelling, 0) for spelling in sorted(spellings)},
        )

    def target(self, tag: str) -> tuple[str, int] | None:
        """(canonical, its uses) when ``tag`` is a minority variant of a decided group."""
        spelling = str(tag).strip(_WHITESPACE).casefold()
        key = fold_term(spelling)
        if not key:
            return None
        group = self.group(key, extra=spelling)
        if group is None or not group.decided or spelling == group.canonical:
            return None
        return group.canonical, self.forms.get(group.canonical, 0)


def _catalogue_rows(vault_root: Path) -> list[tuple[str, list[str]]] | None:
    """Every Knowledge Base page's catalogued tags, or None when it cannot answer."""
    from . import lexstore

    try:
        return lexstore.get_store(Path(vault_root)).tag_members_by_page()
    except Exception as exc:  # noqa: BLE001 - optional vocabulary evidence fails open
        log.warning("tag usage unavailable: %s", type(exc).__name__)
        return None


def _catalogue_aggregate(vault_root: Path) -> tuple[dict[str, int], dict[str, int]] | None:
    """Spelling and written-form page counts outside owned trees, in one SQL pass."""
    from . import lexstore

    try:
        return lexstore.get_store(Path(vault_root)).tag_usage_aggregate(
            _owned_names(), _WHITESPACE
        )
    except Exception as exc:  # noqa: BLE001 - optional vocabulary evidence fails open
        log.warning("tag usage unavailable: %s", type(exc).__name__)
        return None


def _visible_rows(vault_root: Path, keep: Any) -> list[tuple[str, frozenset[str]]] | None:
    """Catalogued tags of pages outside owned trees that ``keep`` releases."""
    rows = _catalogue_rows(vault_root)
    if rows is None:
        return None
    out: list[tuple[str, frozenset[str]]] = []
    for path, members in rows:
        if _owned_elsewhere(path) or (keep is not None and not keep(path)):
            continue
        found = _spellings(members)
        if found:
            out.append((path, found))
    return out


def _usage(vault_root: Path, keep: Any, keys: frozenset[str] | None = None) -> Usage | None:
    if keep is None:
        counts = _catalogue_aggregate(vault_root)
        return None if counts is None else Usage(*counts, keys=keys)
    rows = _visible_rows(vault_root, keep)
    return None if rows is None else Usage.from_rows(rows, keys=keys)


def usage(vault_root: Path, *, keys: frozenset[str] | None = None) -> Usage | None:
    """Tag usage as the current reader may see it; the one count source.

    Pages the reader may not see and pages another subsystem owns never
    contribute, so a count, a group or a canonical choice reads exactly as if
    they were absent. An unrestricted reader is counted by one SQL aggregate;
    a restricted one from per-page rows its release filter decides. None when
    the catalogue cannot answer.
    """
    from .governance import egress

    root = Path(vault_root)
    return _usage(root, egress.restricted_release_filter(root), keys)


def _index_for(vault_root: Path) -> Usage | None:
    from .governance import egress

    root = Path(vault_root)
    keep = egress.restricted_release_filter(root)
    if keep is not None:
        # A restricted reader's counts are its own and never shared.
        return _usage(root, keep)
    key = str(root.resolve())
    now = time.monotonic()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
    if cached is not None and now - cached[0] < INDEX_TTL_SECONDS:
        return cached[1]
    index = _usage(root, None)
    if index is None:
        return None
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = (now, index)
    return index


# ---------------- write time ----------------


def advise_authored(
    vault_root: Path, tags: list[str], *, level: str | None = None
) -> list[str]:
    """At ``maximal``, one warning line per authored minority variant.

    Authored tags are never changed. Other levels leave the tag notice to
    post-commit delivery; an unreadable level or an unavailable catalogue
    gives no advice.
    """
    if not tags:
        return []
    if level is None:
        from . import prominence

        try:
            level = prominence.resolve()
        except Exception:  # noqa: BLE001 - an unreadable level only loses advice
            return []
    if level != "maximal":
        return []
    index = _index_for(vault_root)
    if index is None:
        return []
    notes: list[str] = []
    for tag in dict.fromkeys(tags):
        found = index.target(tag)
        if found:
            notes.append(
                f"tag {tag!r} kept as authored; it is a variant of {found[0]!r} "
                f"(used on {found[1]} pages): prefer {found[0]!r}, or reconcile "
                f"with maintain_memory(mode={MODE!r})"
            )
    return notes


def advisory(tag: str, chosen: str, uses: int) -> dict[str, Any]:
    """One compact tag advisory; the message is a single line."""
    return {
        "family": FAMILY,
        "tag": tag,
        "canonical": chosen,
        "message": (
            f"Tag {tag!r} is a variant of {chosen!r} (used on {uses} pages); "
            f"prefer {chosen!r}."
        ),
        "route": {"tool": "maintain_memory", "args": {"mode": MODE}},
    }


def valid_advisory(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"family", "tag", "canonical", "message", "route"}
        and value["family"] == FAMILY
        and all(isinstance(value[name], str) for name in ("tag", "canonical", "message"))
        and "\n" not in value["message"]
        and len(value["message"].encode("utf-8")) <= 512
        and value["route"] == {"tool": "maintain_memory", "args": {"mode": MODE}}
    )


def advisory_for_page(vault_root: Path, path: str) -> dict[str, Any] | None:
    """The first tag on a committed page that has a more-used canonical variant."""
    tags = _page_tags(Path(vault_root) / path)
    if not tags:
        return None
    index = _index_for(vault_root)
    if index is None:
        return None
    for tag in tags:
        found = index.target(tag)
        if found:
            return advisory(tag, found[0], found[1])
    return None


def _page_tags(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    tags = _tags_of(vault.parse_frontmatter(text)[0])
    return tags or []


def _tags_of(frontmatter: Mapping[str, Any]) -> list[str] | None:
    """Stripped string tags, or None when ``tags`` is absent or unreadable."""
    raw = frontmatter.get("tags")
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return None
    tags = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    return tags if len(tags) == len(raw) else None


# ---------------- frontmatter splice ----------------


def _plain(tag: str) -> bool:
    """True when ``tag`` reads back as the same string written unquoted."""
    if not _PLAIN_TAG.fullmatch(tag):
        return False
    try:
        return vault.yaml_safe_load(f"[{tag}]") == [tag]
    except yaml.YAMLError:
        return False


def _render_tags(tags: list[str]) -> str:
    return "tags: [" + ", ".join(
        tag if _plain(tag) else json.dumps(tag, ensure_ascii=False) for tag in tags
    ) + "]"


def _trailing_comment(line: str) -> str | None:
    """The comment ending the ``tags:`` line ("" when none), or None when unsure."""
    if not _COMMENT_MARK.search(line):
        return ""
    try:
        whole = vault.yaml_safe_load(line)
    except yaml.YAMLError:
        return None
    for match in re.finditer(r"[ \t]+#", line):
        try:
            if vault.yaml_safe_load(line[: match.start()]) == whole:
                return line[match.start() :]
        except yaml.YAMLError:
            continue
    return ""


def _replace_tags_key(fm_text: str, tags: list[str]) -> str | None:
    """Swap the top-level ``tags`` key in place; None when that would lose text.

    A trailing comment on the ``tags:`` line is kept. A comment inside a block
    list cannot be carried into the flow list, so such a page is refused.
    """
    newline = "\r\n" if "\r\n" in fm_text else "\n"
    lines = fm_text.split(newline)
    out: list[str] = []
    replaced = 0
    skipping = False
    for line in lines:
        if skipping:
            if line.startswith((" ", "\t", "- ")) or line == "-":
                if _COMMENT_MARK.search(line):
                    return None
                continue
            skipping = False
        if line.startswith("tags:"):
            comment = _trailing_comment(line)
            if comment is None:
                return None
            replaced += 1
            out.append(_render_tags(tags) + comment)
            skipping = True
            continue
        out.append(line)
    return newline.join(out) if replaced == 1 else None


def _rewrite(text: str, tags: list[str]) -> str | None:
    """New page text with only the tags key changed, or None when unsafe."""
    match = vault._FM_PATTERN.match(text)
    if match is None:
        return None
    fm_text = _replace_tags_key(match.group(1), tags)
    if fm_text is None:
        return None
    updated = text[: match.start(1)] + fm_text + text[match.end(1) :]
    fm_before, body_before, _ = vault.parse_frontmatter(text)
    fm_after, body_after, _ = vault.parse_frontmatter(updated)

    def others(frontmatter: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in frontmatter.items() if key != "tags"}

    if (
        body_after != body_before
        or _tags_of(fm_after) != tags
        or others(fm_after) != others(fm_before)
    ):
        return None
    return updated


# ---------------- maintenance ----------------


def _rewritten(tag: str, decided: Mapping[str, str]) -> str:
    chosen = decided.get(fold_term(tag))
    return chosen if chosen is not None and tag.casefold() != chosen else tag


def _exclusions(value: object) -> frozenset[str]:
    """Fold keys of groups the owner keeps out of maintenance."""
    if value is None:
        return frozenset()
    if (
        not isinstance(value, list)
        or len(value) > _EXCLUDE_LIMIT
        or any(
            not isinstance(item, str)
            or not item.strip()
            or "\n" in item
            or len(item.encode("utf-8")) > _EXCLUDE_LIMIT
            for item in value
        )
    ):
        raise ValueError(
            "INVALID_ARGUMENTS: exclude_groups takes up to 256 one-line group keys"
        )
    keys = frozenset(fold_term(item) for item in value)
    if "" in keys:
        raise ValueError("INVALID_ARGUMENTS: exclude_groups names an empty group key")
    return keys


def _plan(root: Path, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    from .governance import egress

    # Candidate pages need per-page rows, so the plan counts from the same rows;
    # the aggregate the write-time index reads computes identical counts.
    rows = _visible_rows(root, egress.restricted_release_filter(root))
    if rows is None:
        raise ValueError(
            "TAG_USAGE_UNAVAILABLE: the lexical catalogue cannot count tags yet; "
            "retry once it is indexed"
        )
    index = Usage.from_rows(rows)
    grouped = {key: group for key in index.by_key if (group := index.group(key)) is not None}
    active = {key: group for key, group in grouped.items() if group.decided and key not in exclude}
    decided = {key: group.canonical for key, group in active.items()}
    replaced = {
        spelling
        for group in active.values()
        for spelling in group.spellings
        if spelling != group.canonical
    }
    pending: list[dict[str, Any]] = []
    unrewritable: list[str] = []
    for rel in sorted(path for path, spellings in rows if spellings & replaced):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unrewritable.append(rel)
            continue
        tags = _tags_of(vault.parse_frontmatter(text)[0])
        if tags is None:
            unrewritable.append(rel)
            continue
        after = list(dict.fromkeys(_rewritten(tag, decided) for tag in tags))
        if after == tags:
            continue
        # Only pages whose tags key can be swapped with everything else
        # untouched are planned, so an unusual shape never pins later batches.
        if _rewrite(text, after) is None:
            unrewritable.append(rel)
            continue
        pending.append({"path": rel, "hash": vault.content_hash(text), "from": tags, "to": after})
    batch = pending[:BATCH_PAGES]
    plan_id = hashlib.sha256(
        json.dumps(
            {"decided": sorted(decided.items()), "exclude": sorted(exclude), "batch": batch},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    listed = sorted(grouped.values(), key=lambda group: (-group.uses, group.key))[:GROUP_LIMIT]
    return {
        "decided": decided,
        "exclude": sorted(exclude),
        "batch": batch,
        "public": {
            "mode": MODE,
            "plan_id": plan_id,
            "group_count": len(grouped),
            "variant_uses": sum(
                uses
                for group in active.values()
                for spelling, uses in group.spellings.items()
                if spelling != group.canonical
            ),
            "groups": [
                {
                    "key": group.key,
                    "canonical": group.canonical,
                    "uses": group.uses,
                    "tied": not group.decided,
                    "excluded": group.key in exclude,
                    "variants": [
                        {"tag": tag, "uses": uses}
                        for tag, uses in sorted(
                            group.spellings.items(), key=lambda kv: (-kv[1], kv[0])
                        )
                    ],
                }
                for group in listed
            ],
            "groups_truncated": len(grouped) > GROUP_LIMIT,
            "pages_pending": len(pending),
            "batch_pages": len(batch),
            "unrewritable": unrewritable[:GROUP_LIMIT],
        },
    }


def preview(vault_root: Path, *, exclude: list[str] | None = None) -> dict[str, Any]:
    """Read-only: variant groups with counts and the next bounded batch.

    ``exclude`` names fold keys (or any spelling of them) whose groups stay
    out of the batch; the exclusion is part of ``plan_id``.
    """
    plan = _plan(Path(vault_root), exclude=_exclusions(exclude))
    public = dict(plan["public"])
    public["batch"] = [
        {"path": entry["path"], "from": entry["from"], "to": entry["to"]}
        for entry in plan["batch"]
    ]
    if plan["batch"]:
        public["apply"] = {
            "tool": "maintain_memory",
            "args": {
                "mode": MODE,
                "apply": True,
                "plan_id": public["plan_id"],
                "why": "<reason>",
                **({"exclude_groups": plan["exclude"]} if plan["exclude"] else {}),
            },
        }
    return public


def _validate(plan_id: object, why: object) -> None:
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ValueError("INVALID_ARGUMENTS: tag-variants apply requires the preview plan_id")
    if (
        not isinstance(why, str)
        or not why.strip()
        or len(why.encode("utf-8")) > _WHY_BYTES
        or "\n" in why
    ):
        raise ValueError("INVALID_ARGUMENTS: tag-variants apply requires a one-line why")


def _verify_groups(root: Path, plan: Mapping[str, Any]) -> None:
    """Refuse when a group the batch rewrites no longer decides the same way."""
    keys = frozenset(
        fold_term(tag)
        for entry in plan["batch"]
        for tag in entry["from"]
        if _rewritten(tag, plan["decided"]) != tag
    )
    fresh = usage(root, keys=keys)
    if fresh is None:
        raise ValueError(_STALE)
    for key in keys:
        group = fresh.group(key)
        now = group.canonical if group is not None and group.decided else None
        if plan["decided"].get(key) != now:
            raise ValueError(_STALE)


def apply(
    vault_root: Path, *, plan_id: str, why: str, exclude: list[str] | None = None
) -> dict[str, Any]:
    """Rewrite one confirmed batch of minority variants to their canonical tag."""
    from . import writer_lease
    from .governance import egress

    _validate(plan_id, why)
    excluded = _exclusions(exclude)
    root = Path(vault_root)
    # Planning reads the catalogue and every candidate page, so it runs before
    # the mutation guard (the dispatcher narrows its own boundary for this
    # mode); under the guard only the batch is re-verified.
    plan = _plan(root, exclude=excluded)
    if plan["public"]["plan_id"] != plan_id:
        raise ValueError(_STALE)
    rewritten: list[str] = []
    with writer_lease.active_manager().mutation_guard(
        root,
        request_id=writer_lease.active_mutation_request_id(),
        operation="tag_variants_commit",
        holder_kind="command",
    ):
        if plan["batch"]:
            _verify_groups(root, plan)
        writes: list[vault.PlannedWrite] = []
        pages: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for entry in plan["batch"]:
            rel = entry["path"]
            # A page the caller may not see answers exactly as a changed one.
            if egress.write_target_withheld(root, rel):
                raise ValueError(_STALE)
            try:
                text, guard = vault.read_guarded_text(root, root / rel)
            except (OSError, UnicodeDecodeError, vault.PathGuardError):
                raise ValueError(_STALE) from None
            updated = _rewrite(text, entry["to"])
            if vault.content_hash(text) != entry["hash"] or updated is None:
                raise ValueError(_STALE)
            writes.append(
                vault.PlannedWrite(root / rel, updated, guard=guard, expected_hash=entry["hash"])
            )
            pages.append(
                {
                    "path": rel,
                    "before": entry["from"],
                    "after": entry["to"],
                    "before_hash": entry["hash"],
                    "after_hash": vault.content_hash(updated),
                }
            )
            for tag in entry["from"]:
                chosen = _rewritten(tag, plan["decided"])
                if chosen != tag:
                    mapping[tag] = chosen
            rewritten.append(rel)
        if writes:
            inverse: dict[str, list[str]] = {}
            for tag, chosen in sorted(mapping.items()):
                inverse.setdefault(chosen, []).append(tag)
            summary = "Reconciled tag variants " + json.dumps(
                {
                    "plan_id": plan_id,
                    "mapping": mapping,
                    "inverse": inverse,
                    "pages": pages,
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
                rel_path_no_ext=rewritten[0].removesuffix(".md"),
                body=summary,
                operation_token=f"{MODE}:{plan_id}",
            )
            if log_plan.warning is not None:
                # The log entry is the rollback record; without it nothing is written.
                raise ValueError(
                    "TAG_VARIANT_AUDIT_UNAVAILABLE: Knowledge Base/log.md is required "
                    "to record the rollback"
                )
            vault.batch_atomic_write([*writes, *log_plan.writes], vault_root=root)
    remaining = plan["public"]["pages_pending"] - len(rewritten)
    return {
        "mode": MODE,
        "outcome": "committed" if rewritten else "unchanged",
        "plan_id": plan_id,
        "paths": rewritten,
        "pages_remaining": remaining,
        **({"next": {"tool": "maintain_memory", "args": {"mode": MODE}}} if remaining else {}),
    }
