from __future__ import annotations

import importlib
from pathlib import Path


def _registry():
    return importlib.import_module("exomem.entity_registry")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entity(title: str, *, status: str = "active", extra: str = "") -> str:
    return (
        f"---\ntype: entity\ntitle: {title}\nentity_type: person\n"
        f"status: {status}\n{extra}---\n# {title}\n"
    )


def test_registry_reads_active_entities_with_aliases_and_attributes(tmp_path: Path) -> None:
    rel = "Knowledge Base/Entities/People/aria.md"
    extra = (
        "aliases: [Ari]\ntags: [coastal]\nrelationship: friend\n"
        "affiliation: North Lab\n"
    )
    _write(tmp_path, rel, _entity("Aria Vale", extra=extra))
    records = _registry().load_entity_registry(tmp_path, freshness_key=(1,))
    record = records[rel]
    assert record.aliases == ("Ari",)
    assert record.tags == ("coastal",)
    assert record.relationship == "friend"
    assert record.affiliation == "North Lab"


def test_registry_records_inactive_status_and_skips_non_entity_pages(tmp_path: Path) -> None:
    inactive = "Knowledge Base/Entities/People/old.md"
    _write(tmp_path, inactive, _entity("Old Person", status="superseded"))
    _write(
        tmp_path,
        "Knowledge Base/Entities/People/not-entity.md",
        "---\ntype: insight\n---\n# Nope\n",
    )
    records = _registry().load_entity_registry(tmp_path, freshness_key=(2,))
    assert records[inactive].status == "superseded"
    assert len(records) == 1


def test_registry_skips_index_and_records_paths(tmp_path: Path) -> None:
    rel = "Knowledge Base/Entities/People/aria.md"
    _write(tmp_path, rel, _entity("Aria Vale"))
    _write(tmp_path, "Knowledge Base/Entities/People/index.md", _entity("Index"))
    records = _registry().load_entity_registry(tmp_path, freshness_key=(3,))
    assert list(records) == [rel]
    assert records[rel].path == rel


def test_registry_cache_hits_on_same_freshness_key_and_rebuilds_on_new_key(
    tmp_path: Path, monkeypatch
) -> None:
    module = _registry()
    module.clear_entity_registry_cache()
    _write(tmp_path, "Knowledge Base/Entities/People/aria.md", _entity("Aria Vale"))
    calls = 0
    original = module._build_registry

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_build_registry", counted)
    first = module.load_entity_registry(tmp_path, freshness_key=(4,))
    second = module.load_entity_registry(tmp_path, freshness_key=(4,))
    third = module.load_entity_registry(tmp_path, freshness_key=(5,))
    assert first is second
    assert third is not second
    assert calls == 2
