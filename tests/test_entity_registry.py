from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import yaml


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


def test_cache_only_registry_lookup_never_builds(tmp_path: Path, monkeypatch) -> None:
    module = _registry()
    module.clear_entity_registry_cache()
    monkeypatch.setattr(
        module,
        "_build_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("managed request must not build a cold entity registry")
        ),
    )

    assert (
        module.load_entity_registry(
            tmp_path,
            freshness_key=("cold",),
            allow_build=False,
        )
        is None
    )


def test_background_registry_warm_populates_the_exact_cache_key(
    tmp_path: Path, monkeypatch
) -> None:
    module = _registry()
    module.clear_entity_registry_cache()
    type_registry = SimpleNamespace(core_version="core", extension_hash="extension")
    expected = {"Knowledge Base/Entities/People/aria.md": object()}
    built = threading.Event()

    def build(*_args, **_kwargs):
        built.set()
        return expected

    monkeypatch.setattr(module, "_build_registry", build)
    module.schedule_entity_registry_warm(
        tmp_path,
        freshness_key=("live",),
        type_registry=type_registry,
    )
    assert built.wait(timeout=1.0)

    deadline = time.monotonic() + 1.0
    cached = None
    while cached is None and time.monotonic() < deadline:
        cached = module.load_entity_registry(
            tmp_path,
            freshness_key=("live",),
            type_registry=type_registry,
            allow_build=False,
        )
        time.sleep(0.01)
    assert cached is expected


def test_registry_enumerates_extension_type_folders(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Knowledge Base/_Schema/entity-types.yaml",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "place": {
                        "folder": "Places",
                        "label": "Place",
                        "aliases": ["location"],
                        "cue_nouns": ["venue"],
                        "capture_guidance": "A stable place identity.",
                        "parent": "concept",
                    }
                },
            },
            sort_keys=False,
        ),
    )
    rel = "Knowledge Base/Entities/Places/aster-hall.md"
    _write(
        tmp_path,
        rel,
        "---\ntype: entity\ntitle: Aster Hall\nentity_type: place\n"
        "status: active\ntags: [local]\n---\n# Aster Hall\n",
    )

    records = _registry().load_entity_registry(tmp_path, freshness_key=("extension",))

    assert records[rel].entity_type == "place"
    assert records[rel].tags == ("local",)
