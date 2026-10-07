"""In-PR parity: the shipped packs reproduce the Python constants they replace.

`add-vocabulary-registries` design decision 2. This file is deleted in the
same pull request, together with the `_LEGACY_*` constants it compares. The
`BASE_*` digests were computed by `effective_dump` on origin/main 2e77ac33e,
before any registry module read a pack.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from exomem import entity_types, relation_registry, source_taxonomy

#: A legacy overlay per registry, in the grammar each file has today.
LEGACY_OVERLAYS = {
    "entity-types.yaml": {
        "schema_version": 1,
        "entity_types": {
            "site": {
                "folder": "Sites",
                "label": "Site",
                "aliases": ["location"],
                "capture_guidance": "A stable physical site identity.",
            },
            "venue": {
                "folder": "Venues",
                "label": "Venue",
                "aliases": [],
                "capture_guidance": "A place that hosts events.",
                "parent": "site",
            },
            "old-place": {
                "folder": "Old Places",
                "label": "Old place",
                "aliases": [],
                "capture_guidance": "Retired.",
                "status": "deprecated",
                "replaced_by": "site",
            },
            "people-two": {"folder": "People", "label": "Person", "capture_guidance": "x"},
        },
        "facets": {"site": {"operator": {"cardinality": "single", "value": "wikilink"}}},
    },
    "relation-registry.yaml": {
        "schema_version": 1,
        "extensions": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "An organisation policy applies to a case",
                "direction": "directed",
                "aliases": ["applies_to"],
            },
            "vault.old": {
                "parent": "relates_to",
                "description": "Old",
                "direction": "directed",
                "status": "deprecated",
                "replaced_by": "vault.applies_to",
            },
        },
    },
    "source-taxonomy.yaml": {
        "schema_version": 1,
        "source_kinds": {
            "field-recording": {"label": "Field recording", "path_label": "Field Recordings"},
            "article": {"aliases": ["essay"], "description": "web essays"},
            "bad key!": {"label": "x"},
        },
        "domains": {"marine-biology": None, "travel": {"label": "Trips"}},
    },
}


def _canonical(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=sorted).encode("utf-8")
    ).hexdigest()


def effective_dump(vault_root: Path | None) -> dict[str, object]:
    """The effective vocabulary through APIs that exist before and after the change."""
    types = entity_types.load_entity_types(vault_root)
    relations = relation_registry.load_registry(vault_root)
    taxonomy = (
        source_taxonomy.load_taxonomy(vault_root)
        if vault_root is not None
        else source_taxonomy.core_taxonomy()
    )
    return {
        "entity_types": {
            "active": [
                {
                    "id": item.id,
                    "folder": item.folder,
                    "label": item.label,
                    "aliases": list(item.aliases),
                    "cue_nouns": list(item.cue_nouns),
                    "optional_frontmatter": list(item.optional_frontmatter),
                    "guidance": item.capture_guidance,
                    "parent": item.parent,
                    "core": item.core,
                    "family": types.family_of(item.id),
                }
                for item in types.active_definitions
            ],
            "extensions": sorted(types.extensions),
            "fingerprint": types.fingerprint,
            "extension_hash": types.extension_hash,
            "findings": [dict(item) for item in types.findings],
            "facets": {key: [f.as_dict() for f in value] for key, value in types.facets.items()},
            "by_alias": sorted((key, value.id) for key, value in types.by_alias.items()),
        },
        "relations": {
            "core_version": relations.core_version,
            "core": {key: value.as_dict() for key, value in relations.core.items()},
            "extensions": {key: value.as_dict() for key, value in relations.extensions.items()},
            "aliases": dict(relations.aliases),
            "extension_hash": relations.extension_hash,
            "findings": [dict(item) for item in relations.findings],
            "terminals": dict(relations.terminal_replacements),
        },
        "source_taxonomy": {
            "kinds": {key: value.as_dict() for key, value in taxonomy.kinds.items()},
            "domains": {key: value.as_dict() for key, value in taxonomy.domains.items()},
            "kind_aliases": dict(taxonomy.kind_aliases),
            "domain_aliases": dict(taxonomy.domain_aliases),
            "findings": list(taxonomy.findings),
        },
    }


def legacy_vault(root: Path) -> Path:
    schema = root / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True)
    for name, document in LEGACY_OVERLAYS.items():
        (schema / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return root


BASE_DIGESTS = {
    "pack_only": {
        "entity_types": "2c173ed85eb4a7c7f58edc760678c2975506a688ccf6c7197de6c11a852a9fed",
        "relations": "79e2942e112a953afc6a200af1219e44dfab223564bdf5b1ed08027abf63e96a",
        "source_taxonomy": "9145953b9c71c5d423616ce6add70e3a25fcfdab01be5438049887ffeb31aade",
    },
    "legacy_overlays": {
        "entity_types": "371214147c89fb49119d414c4fca53bf3ed8ec84c8992e75725646b0f0e6c296",
        "relations": "dc47e39a8d341e0e23224b374e2a0e319c90e32b78759d25a625ebe1cd8b432d",
        "source_taxonomy": "73340fd72ee3a04208f7b8134d03ef7870ccef8f85906986ec94f3b5a1b286cd",
    },
}


def test_the_packs_equal_the_constants_they_replace() -> None:
    legacy_types = {item.id: item for item in entity_types._LEGACY_ENTITY_TYPE_REGISTRY}
    assert {item.id: item for item in entity_types.ENTITY_TYPE_REGISTRY} == legacy_types
    assert dict(source_taxonomy.builtin_kinds()) == {
        item.key: item for item in source_taxonomy._LEGACY_BUILTIN_KINDS
    }
    assert dict(source_taxonomy.builtin_domains()) == {
        item.key: item for item in source_taxonomy._LEGACY_BUILTIN_DOMAINS
    }


def test_an_unchanged_vault_resolves_as_it_did_on_main(tmp_path: Path) -> None:
    pack_only = effective_dump(None)
    with_overlays = effective_dump(legacy_vault(tmp_path / "vault"))
    for name in ("entity_types", "relations", "source_taxonomy"):
        assert _canonical(pack_only[name]) == BASE_DIGESTS["pack_only"][name], name
        assert _canonical(with_overlays[name]) == BASE_DIGESTS["legacy_overlays"][name], name
    # The legacy overlays are read, never rewritten.
    schema = tmp_path / "vault" / "Knowledge Base" / "_Schema"
    for name, document in LEGACY_OVERLAYS.items():
        assert (schema / name).read_text(encoding="utf-8") == yaml.safe_dump(
            document, sort_keys=False
        )
