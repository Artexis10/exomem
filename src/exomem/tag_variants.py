"""Reconcile tags that differ only by case, separator, or inflection.

Tags are compared through ``vocabulary_fold.fold_term``. Within one fold group
the most-used spelling is canonical. Three consumers share that rule:

- a write at prominence ``maximal`` records the canonical spelling of an
  authored minority variant (``reconcile_authored``);
- post-commit vocabulary delivery names the canonical spelling in one
  ``vocabulary_advisory`` line at lower levels (``advisory_for_page``);
- ``maintain_memory(mode="tag-variants")`` lists the groups and, on an exact
  plan confirmation, rewrites minority variants in bounded batches
  (``preview`` / ``apply``). Only the ``tags`` frontmatter key changes; the
  body is spliced back byte for byte.
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
from pathlib import Path
from typing import Any

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
#: Seconds a folded usage index serves writes. Which spelling is most used
#: moves slowly, and re-folding every tag on every write cost ~60 ms at 5,000
#: distinct tags, so write-time guidance reads a briefly cached index.
INDEX_TTL_SECONDS = 120.0
_INDEX_CACHE: dict[str, tuple[float, _Index]] = {}
_INDEX_LOCK = threading.Lock()
_PLAIN_TAG = re.compile(r"[a-z0-9][a-z0-9_./-]*", re.IGNORECASE)
_SKIP_KB_SUBDIRS = frozenset(
    {"Sources", "Evidence", "_Schema", "_trash", "_archive", "_attachments", "_Governance"}
)


# ---------------- grouping ----------------


def groups(counts: Mapping[str, int]) -> dict[str, dict[str, int]]:
    """Fold key -> {spelling: uses} for every key with two or more spellings."""
    grouped: dict[str, dict[str, int]] = {}
    for tag, uses in counts.items():
        key = fold_term(tag)
        if key:
            grouped.setdefault(key, {})[tag] = int(uses)
    return {key: members for key, members in grouped.items() if len(members) > 1}


def canonical(members: Mapping[str, int]) -> str:
    """The most-used spelling; ties prefer the write-time normal form, then order."""

    def rank(tag: str) -> tuple[int, int, int, str]:
        normal = tag == tag.strip().lower().replace(" ", "-").replace("_", "-")
        return (-members[tag], 0 if normal else 1, len(tag), tag)

    return min(members, key=rank)


def minority(tag: str, counts: Mapping[str, int]) -> tuple[str, int] | None:
    """(canonical, its uses) when ``tag`` is a less-used variant of it."""
    key = fold_term(tag)
    if not key:
        return None
    members = {other: uses for other, uses in counts.items() if fold_term(other) == key}
    members.setdefault(tag, 0)
    if len(members) < 2:
        return None
    chosen = canonical(members)
    if chosen == tag or members[chosen] <= members[tag]:
        return None
    return chosen, members[chosen]


class _Index:
    """Fold key -> {spelling: uses}, built once per call from usage counts."""

    def __init__(self, counts: Mapping[str, int]):
        self.by_key: dict[str, dict[str, int]] = {}
        for tag, uses in counts.items():
            key = fold_term(tag)
            if key:
                self.by_key.setdefault(key, {})[tag] = int(uses)

    def minority(self, tag: str) -> tuple[str, int] | None:
        members = self.by_key.get(fold_term(tag))
        if not members:
            return None
        return minority(tag, members)


def _index_for(vault_root: Path) -> _Index | None:
    key = str(Path(vault_root).resolve())
    now = time.monotonic()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
    if cached is not None and now - cached[0] < INDEX_TTL_SECONDS:
        return cached[1]
    counts = usage_counts(vault_root)
    if not counts:
        return None
    index = _Index(counts)
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = (now, index)
    return index


def usage_counts(vault_root: Path) -> dict[str, int] | None:
    """Pages per tag from the lexical catalogue, or None when it cannot answer."""
    from . import lexstore

    try:
        return lexstore.get_store(Path(vault_root)).tag_usage_counts()
    except Exception as exc:  # noqa: BLE001 - optional vocabulary evidence fails open
        log.warning("tag usage unavailable: %s", type(exc).__name__)
        return None


# ---------------- write time ----------------


def reconcile_authored(
    vault_root: Path, tags: list[str], *, level: str | None = None
) -> tuple[list[str], list[str]]:
    """At ``maximal``, record the canonical spelling of authored minority variants.

    Returns the tags to write and one warning line per rewrite. Any other
    level, or an unavailable catalogue, returns the authored tags unchanged.
    """
    if not tags:
        return tags, []
    if level is None:
        from . import prominence

        try:
            level = prominence.resolve()
        except Exception:  # noqa: BLE001 - an unreadable level never rewrites
            return tags, []
    if level != "maximal":
        return tags, []
    index = _index_for(vault_root)
    if index is None:
        return tags, []
    out: list[str] = []
    notes: list[str] = []
    for tag in tags:
        found = index.minority(tag)
        chosen = found[0] if found else tag
        if found:
            notes.append(
                f"tag {tag!r} recorded as its canonical variant {chosen!r} "
                f"(used on {found[1]} pages)"
            )
        if chosen not in out:
            out.append(chosen)
    return out, notes


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
        found = index.minority(tag.casefold())
        if found and found[0] != tag:
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


# ---------------- maintenance ----------------


def _compiled_pages(root: Path) -> Iterable[Path]:
    kb = vault.kb_root(root)
    if not kb.is_dir():
        return
    stack = [kb]
    while stack:
        directory = stack.pop()
        for child in sorted(directory.iterdir(), reverse=True):
            if child.is_dir():
                if child.name not in _SKIP_KB_SUBDIRS and not child.name.startswith("."):
                    stack.append(child)
            elif child.is_file() and child.suffix.lower() == ".md":
                yield child


def _render_tags(tags: list[str]) -> str:
    return "tags: [" + ", ".join(
        tag if _PLAIN_TAG.fullmatch(tag) else json.dumps(tag, ensure_ascii=False)
        for tag in tags
    ) + "]"


def _replace_tags_key(fm_text: str, tags: list[str]) -> str | None:
    """Swap the top-level ``tags`` key in place; None when it is not found once."""
    newline = "\r\n" if "\r\n" in fm_text else "\n"
    lines = fm_text.split(newline)
    out: list[str] = []
    replaced = 0
    skipping = False
    for line in lines:
        if skipping:
            if line.startswith((" ", "\t", "- ")) or line == "-":
                continue
            skipping = False
        if line.startswith("tags:"):
            replaced += 1
            out.append(_render_tags(tags))
            skipping = True
            continue
        out.append(line)
    return newline.join(out) if replaced == 1 else None


def _rewrite(text: str, tags: list[str]) -> str | None:
    """New page text with only the tags key changed and the body identical."""
    match = vault._FM_PATTERN.match(text)
    if match is None:
        return None
    fm_text = _replace_tags_key(match.group(1), tags)
    if fm_text is None:
        return None
    updated = text[: match.start(1)] + fm_text + text[match.end(1) :]
    fm_after, body_after, _ = vault.parse_frontmatter(updated)
    if body_after != vault.parse_frontmatter(text)[1] or _tags_of(fm_after) != tags:
        return None
    return updated


def _scan(root: Path) -> tuple[dict[str, int], list[tuple[str, str, list[str]]]]:
    counts: dict[str, int] = {}
    pages: list[tuple[str, str, list[str]]] = []
    for path in _compiled_pages(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tags = _tags_of(vault.parse_frontmatter(text)[0])
        if not tags:
            continue
        rel = path.relative_to(root).as_posix()
        pages.append((rel, vault.content_hash(text), tags))
        for tag in dict.fromkeys(tags):
            counts[tag] = counts.get(tag, 0) + 1
    return counts, pages


def _plan(root: Path) -> dict[str, Any]:
    counts, pages = _scan(root)
    grouped = groups(counts)
    mapping: dict[str, str] = {}
    for members in grouped.values():
        chosen = canonical(members)
        for tag in members:
            if tag != chosen:
                mapping[tag] = chosen
    pending: list[dict[str, Any]] = []
    unrewritable: list[str] = []
    for rel, digest, tags in sorted(pages):
        if not any(tag in mapping for tag in tags):
            continue
        after = list(dict.fromkeys(mapping.get(tag, tag) for tag in tags))
        # Only pages whose tags key can be swapped with the body untouched are
        # planned, so an unusual shape never pins every later batch.
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unrewritable.append(rel)
            continue
        if vault.content_hash(text) != digest or _rewrite(text, after) is None:
            unrewritable.append(rel)
            continue
        pending.append({"path": rel, "hash": digest, "from": tags, "to": after})
    batch = pending[:BATCH_PAGES]
    plan_id = hashlib.sha256(
        json.dumps(
            {"mapping": sorted(mapping.items()), "batch": batch},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    listed = sorted(
        grouped.items(), key=lambda item: (-sum(item[1].values()), item[0])
    )[:GROUP_LIMIT]
    return {
        "mapping": mapping,
        "batch": batch,
        "public": {
            "mode": MODE,
            "plan_id": plan_id,
            "group_count": len(grouped),
            "variant_uses": sum(counts[tag] for tag in mapping),
            "groups": [
                {
                    "canonical": canonical(members),
                    "uses": sum(members.values()),
                    "variants": [
                        {"tag": tag, "uses": uses}
                        for tag, uses in sorted(members.items(), key=lambda kv: (-kv[1], kv[0]))
                    ],
                }
                for _key, members in listed
            ],
            "groups_truncated": len(grouped) > GROUP_LIMIT,
            "pages_pending": len(pending),
            "batch_pages": len(batch),
            "unrewritable": unrewritable[:GROUP_LIMIT],
        },
    }


def preview(vault_root: Path) -> dict[str, Any]:
    """Read-only: variant groups with counts and the next bounded batch."""
    plan = _plan(Path(vault_root))
    public = dict(plan["public"])
    public["batch"] = [
        {"path": entry["path"], "from": entry["from"], "to": entry["to"]}
        for entry in plan["batch"]
    ]
    if plan["batch"]:
        public["apply"] = {
            "tool": "maintain_memory",
            "args": {"mode": MODE, "apply": True, "plan_id": public["plan_id"], "why": "<reason>"},
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


def apply(vault_root: Path, *, plan_id: str, why: str) -> dict[str, Any]:
    """Rewrite one confirmed batch of minority variants to their canonical tag."""
    from . import writer_lease

    _validate(plan_id, why)
    root = Path(vault_root)
    with writer_lease.active_manager().mutation_guard(root, operation="tag_variants"):
        plan = _plan(root)
        if plan["public"]["plan_id"] != plan_id:
            raise ValueError(
                "STALE_TAG_VARIANT_PLAN: the vault changed since the preview; preview again"
            )
        writes: list[vault.PlannedWrite] = []
        rewritten: list[str] = []
        for entry in plan["batch"]:
            path = root / entry["path"]
            text, guard = vault.read_guarded_text(root, path)
            updated = _rewrite(text, entry["to"])
            if vault.content_hash(text) != entry["hash"] or updated is None:
                raise ValueError(
                    "STALE_TAG_VARIANT_PLAN: a page changed since the preview; preview again"
                )
            writes.append(
                vault.PlannedWrite(path, updated, guard=guard, expected_hash=entry["hash"])
            )
            rewritten.append(entry["path"])
        if writes:
            summary = "Reconciled tag variants " + json.dumps(
                {
                    "plan_id": plan_id,
                    "pages": len(rewritten),
                    "mapping": {
                        tag: plan["mapping"][tag]
                        for tag in sorted(
                            {t for e in plan["batch"] for t in e["from"] if t in plan["mapping"]}
                        )
                    },
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
