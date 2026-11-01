"""File readback of a stopped slot vault, run as a child under the arm's interpreter.

``python -m membench.utility.organisation_replay.readback MODE --out FILE``
with the slot's product environment. It reads files and the product's own
loaders only, never an MCP door, so it delivers and settles nothing
(SCHEMA.md section 2, "End-state readback"). The parent records the vault's
tree digest before and after to show that the read wrote nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

#: The registries whose overlay digests identify a seed (SCHEMA.md section 2,
#: ``seed.registry_overlay_digests``): the ones a seed writes to.
SEED_REGISTRIES = ("domains", "entity-types", "relations", "homes")
#: The page trees SCHEMA.md's end state lists ("Every page under Notes and Entities").
PAGE_TREES = ("Notes", "Entities")
#: The registries SCHEMA.md's ``registry_history`` covers, with the product's
#: history file stem for each.
HISTORY_STEMS = (("entity-types", "entity-types"), ("relations", "relation-registry"))
#: A fixed projection stamp. The projection still carries each build's own ids
#: and times, so its digest identifies one seed build, not the seed content.
_STAMP = "1970-01-01T00:00:00Z"


def _sha(value: object) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _registries(vault: Path) -> dict:
    from exomem import entity_types, project_keys, relation_registry
    from exomem.vocabulary import registry_spec
    from exomem.vocabulary.registry import load

    snapshots = {name: load(registry_spec(name), vault) for name in SEED_REGISTRIES}
    types = entity_types.load_entity_types(vault)
    relations = relation_registry.load_registry(vault)
    projects = project_keys.load_project_registry(vault)

    def type_chain(key: str) -> list[str]:
        chain, seen = [], {key}
        definition = types.by_id.get(key)
        while definition is not None and definition.parent and definition.parent not in seen:
            chain.append(definition.parent)
            seen.add(definition.parent)
            definition = types.by_id.get(definition.parent)
        return chain

    def relation_chain(key: str) -> list[str]:
        chain, seen = [], {key}
        definition = relations.definition(key)
        while definition is not None and definition.parent and definition.parent not in seen:
            chain.append(definition.parent)
            seen.add(definition.parent)
            definition = relations.definition(definition.parent)
        return chain

    return {
        "domains": sorted(snapshots["domains"].entries),
        "entity_types": {
            "core": sorted(types.core),
            "vault": {
                key: {
                    "label": d.label,
                    "folder": d.folder,
                    "parent_chain": type_chain(key),
                    "status": d.status,
                }
                for key, d in sorted(types.extensions.items())
            },
        },
        "relations": {
            key: {
                "parent_chain": relation_chain(key),
                "generic": d.generic,
                "direction": d.direction,
                "inverse": d.inverse,
                "family": d.family,
                "core": d.core,
                "aliases": list(d.aliases),
                "status": d.status,
            }
            for key in sorted(relations.keys)
            if (d := relations.definition(key)) is not None
        },
        "homes": {
            key: {
                "folder": entry.attributes.get("folder"),
                "topics": list(entry.attributes.get("topics") or []),
                "projects": list(entry.attributes.get("projects") or []),
                "status": entry.status,
            }
            for key, entry in sorted(snapshots["homes"].entries.items())
        },
        "projects": sorted(projects.keys),
        "overlay_digests": {
            **{name: snap.content_hash for name, snap in snapshots.items()},
            "projects": projects.content_hash,
        },
    }


def _page_files(vault: Path):
    """``(vault-relative path, bytes, frontmatter, body)`` for each page under the page trees."""

    from exomem import vault as vault_module

    root = vault_module.kb_root(vault)
    for tree in PAGE_TREES:
        for path in sorted((root / tree).rglob("*.md")):
            # The tree's own index page is the product's listing, not a page a session writes.
            if not path.is_file() or path == root / tree / "index.md":
                continue
            raw = path.read_bytes()
            frontmatter, body, _text = vault_module.parse_frontmatter(raw.decode("utf-8"))
            yield path.relative_to(vault).as_posix(), raw, frontmatter, body


def _pages(vault: Path) -> list[dict]:
    from exomem.vocabulary import registry_spec
    from exomem.vocabulary.registry import load

    note_types = load(registry_spec("note-types"), vault).entries
    pages = []
    for relative, raw, frontmatter, _body in _page_files(vault):
        page_type = str(frontmatter.get("type") or "")
        entry = note_types.get(page_type)
        pages.append(
            {
                "path": relative,
                "title": frontmatter.get("title"),
                "stem": Path(relative).stem,
                "role": entry.attributes.get("role") if entry is not None else None,
                "type": page_type,
                "entity_type": frontmatter.get("entity_type"),
                "topics": list(frontmatter.get("topics") or []),
                "projects": list(frontmatter.get("projects") or []),
                "project": frontmatter.get("project"),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return pages


def _edges(vault: Path, pages: list[dict]) -> list[dict]:
    """Every canonical relation bullet, with the page that holds it.

    The product's relation parser reads each page, the same parser the
    projector's entity-graph path calls. The projection itself is not used
    here: it folds frontmatter links into its edges and keeps no holding page.
    """

    # The projector's link rule, so an edge target resolves as the projection's ids do.
    from epistemic.projectors.exomem_vault import _normalize_link

    from exomem import relation_registry
    from exomem.markdown_relations import parse_markdown_relations

    registry = relation_registry.load_registry(vault)
    resolve = _resolver(pages)
    edges = []
    for relative, _raw, _frontmatter, body in _page_files(vault):
        # Unregistered keys are kept: a session may write one, and the scorer reads it.
        document = parse_markdown_relations(body, relation_types=registry.keys, retain_unknown=True)
        for relation in document.canonical_relations:
            definition = registry.definition(relation.kind)
            edges.append(
                {
                    "holder": relative,
                    "subject": relative,
                    "relation": relation.kind,
                    "registered": definition is not None,
                    "family": definition.family if definition is not None else None,
                    "target": relation.target,
                    "target_path": resolve(_normalize_link(relation.target)),
                    "line": relation.line,
                }
            )
    return edges


def _promotions(vault: Path) -> list[dict]:
    from exomem import promotions, review_state

    store = review_state.ReviewStateStore(vault)
    today = dt.datetime.now(dt.UTC).date()
    rows = []
    for promotion_id, entry in sorted(promotions.ledger(vault).items()):
        state, decision = store.effective_state(
            promotion_id, promotions.identity_fingerprint(promotion_id), today=today
        )
        rows.append(
            {
                "id": promotion_id,
                "kind": entry.get("kind"),
                "creator": entry.get("creator"),
                "write": entry.get("write"),
                "created_at": entry.get("created_at"),
                "subjects": entry.get("subjects"),
                "state": state,
                "decision": None
                if decision is None
                else {
                    name: getattr(decision, name, None)
                    for name in ("action", "until", "why", "updated_at")
                },
            }
        )
    return rows


def _projection(vault: Path):
    from epistemic.projectors.exomem_vault import VaultProjector

    return VaultProjector(vault, entity_graph=True).project(phase="readback", taken_at=_STAMP)


def _resolver(pages: list[dict]):
    """A link target to a page path: an exact path, else a unique stem, else a unique title."""

    by_path = {page["path"][: -len(".md")]: page["path"] for page in pages}
    by_stem: dict[str, list[str]] = {}
    by_title: dict[str, list[str]] = {}
    for page in pages:
        by_stem.setdefault(page["stem"].casefold(), []).append(page["path"])
        if isinstance(page["title"], str):
            by_title.setdefault(page["title"].casefold(), []).append(page["path"])

    def resolve(target: str) -> str | None:
        if target in by_path:
            return by_path[target]
        name = target.rsplit("/", 1)[-1].casefold()
        for index in (by_stem, by_title):
            found = index.get(name) or []
            if len(found) == 1:
                return found[0]
        return None

    return resolve


def _moment(stamp: object) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def _within(stamp: object, since: dt.datetime, until: dt.datetime) -> bool:
    moment = _moment(stamp)
    return moment is not None and since <= moment <= until


def facts(vault: Path) -> dict:
    """What the seed check and the seed snapshot need."""

    projection = _projection(vault)
    registries = _registries(vault)
    return {
        "registries": registries,
        "pages": _pages(vault),
        "promotions": _promotions(vault),
        "projection_digest": _sha(projection.model_dump(mode="json")),
    }


def end_state(
    vault: Path,
    *,
    since: dt.datetime,
    until: dt.datetime,
    seed_pages: dict,
    reason_capable: bool,
) -> dict:
    from exomem import registry_history
    from exomem import vault as vault_module
    from exomem.governance.principal import owner_principal, request_scope

    pages = _pages(vault)
    for page in pages:
        page["in_seed"] = page["path"] in seed_pages
    changed = sorted(
        page["path"] for page in pages if seed_pages.get(page["path"]) != page["content_sha256"]
    )
    edges = _edges(vault, pages)
    registries = _registries(vault)
    history = []
    # Registry history is an owner read; the readback acts as the vault's owner on its CLI.
    with request_scope(owner_principal(surface="cli")):
        for registry, stem in HISTORY_STEMS:
            for version in registry_history.versions(vault, stem=stem):
                if not _within(version.get("at"), since, until):
                    continue
                for key in (version.get("added") or {}).get(registry, []):
                    history.append({"registry": registry, "key": key, "at": version.get("at")})
    log = vault_module.read_operation_log(vault)
    entries = [e for e in log.get("entries", []) if _within(e.get("date"), since, until)]
    return {
        "read": {
            "door": "product file loaders and relation parser on the stopped slot vault",
            "projector": "epistemic.projectors.exomem_vault.VaultProjector(entity_graph=True)",
            "projection_digest": _sha(_projection(vault).model_dump(mode="json")),
        },
        "pages": pages,
        "pages_changed": changed,
        "edges": edges,
        "entity_types": registries["entity_types"],
        "relations": registries["relations"],
        "registry_history": sorted(history, key=lambda row: (str(row["at"]), row["key"])),
        "homes": registries["homes"],
        "operation_log": {
            "entries": entries,
            "files_read": log.get("files_read"),
            "files_unreadable": log.get("files_unreadable"),
            # Entries the session window cannot place, left out of ``entries`` above.
            "entries_unstamped": sum(
                1 for e in log.get("entries", []) if _moment(e.get("date")) is None
            ),
            "edge_review_reasons": edge_review_reasons(entries, reason_capable=reason_capable),
        },
        "promotions": _promotions(vault),
    }


def edge_review_reasons(entries: list[dict], *, reason_capable: bool) -> dict:
    """The edge-review reasons this arm's log recorded, when its writer can record them.

    The reason sits on each added edge (``provenance.added_edges[].reason``), flagged by
    ``reason_recorded``. Both writers emit the flag, and one that cannot record a reason
    leaves it false, so the log cannot tell that writer from one that recorded none. The
    binding states the arm's capability instead: a capable arm's count is a measurement,
    zero included, and an incapable arm's is unavailable, never zero.
    """

    added = [
        edge
        for entry in entries
        for edge in (entry.get("provenance") or {}).get("added_edges") or []
        if isinstance(edge, dict)
    ]
    generic = [edge for edge in added if edge.get("generic") is True]
    recorded = [edge for edge in generic if edge.get("reason_recorded") is True]
    return {
        "status": "available" if reason_capable else "unavailable",
        "field": "provenance.added_edges[].reason",
        "generic_added_edges": len(generic),
        "reasons_recorded": len(recorded) if reason_capable else None,
    }


def binding_facts(vault: Path, *, surfaces: list[str]) -> dict:
    """The package and engagement facts a binding record needs, read by this interpreter."""

    import exomem
    from exomem import envelope, prominence
    from exomem import vault as vault_module
    from exomem.install_info import package_identity
    from exomem.vocabulary_authority import VocabularyAuthority

    scaffold = Path(exomem.__file__).parent / "_scaffold" / "_Schema" / "SKILL.md"
    skill, _body, _text = vault_module.parse_frontmatter(scaffold.read_text(encoding="utf-8"))
    engagement = {}
    for surface in surfaces:
        resolved = prominence.resolved(surface)
        engagement[surface] = {
            "level": resolved.get("level"),
            "source": resolved.get("source"),
            "context": resolved.get("context"),
            "contract": resolved.get("contract"),
            "owner_promotion_classes": envelope.promotion_classes(owner=True),
        }
    from exomem import install_hook

    hook_sources = install_hook._HOOK_DIR_SRC  # noqa: SLF001 - the installer's own source directory
    role = "organisation-replay-binding"
    try:
        # An installed distribution names its revision in its own record; git run
        # inside its install directory would name whatever checkout holds it.
        identity = package_identity("installed", role=role)
    except ValueError:
        identity = package_identity("source", role=role)
        # A revision names only committed code; ``None`` when git cannot say.
        status = subprocess.run(  # noqa: S603 - git on the loaded package's own directory
            ["git", "-C", str(Path(exomem.__file__).parent), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            check=False,
        )
        identity["uncommitted_paths"] = (
            status.stdout.splitlines() if status.returncode == 0 else None
        )
    return {
        "interpreter": {"path": sys.executable, "version": sys.version},
        "loaded_source_path": str(Path(exomem.__file__).resolve()),
        "package_identity": identity,
        # The hook files this package ships; the bind holds the wired hooks to them.
        "hook_sources": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(hook_sources.iterdir())
            if path.is_file()
        },
        "product_version": exomem.__version__,
        "scaffold": dict((skill or {}).get("metadata") or {}),
        "authority_mode": VocabularyAuthority(vault).runtime_status().mode,
        "engagement": engagement,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="organisation_replay.readback")
    parser.add_argument("mode", choices=("facts", "end-state", "binding"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--seed-pages", help="JSON file: seed page path -> content sha256")
    parser.add_argument("--surface", action="append", default=[])
    parser.add_argument(
        "--reason-capable", action="store_true", help="the bound arm records edge-review reasons"
    )
    args = parser.parse_args(argv)
    vault = Path(os.environ["EXOMEM_VAULT_PATH"])
    if args.mode == "facts":
        result = facts(vault)
    elif args.mode == "binding":
        result = binding_facts(vault, surfaces=args.surface)
    else:
        result = end_state(
            vault,
            since=dt.datetime.fromisoformat(args.since),
            until=dt.datetime.fromisoformat(args.until),
            seed_pages=json.loads(Path(args.seed_pages).read_text(encoding="utf-8")),
            reason_capable=args.reason_capable,
        )
    Path(args.out).write_text(
        json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
