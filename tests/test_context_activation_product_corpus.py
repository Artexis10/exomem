"""Product-shape acceptance for the synthetic context-activation corpus.

These tests stop before scoring.  A corpus is eligible for the real-compiler
gate only when its logical keys name canonical governed structures and those
structures are visible to a freshly published activation index.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from epistemic.corpora.context_activation import (
    _corpus_hash,
    _governed_resource,
    build_corpus,
)

from exomem import structured_collections, working_set_index
from exomem import vault as vault_module

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(scope="module")
def product_corpus(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("context-activation-product-corpus")
    return root, build_corpus(root, distractor_count=0)


def _frontmatter(root: Path, relative: str) -> dict[str, object]:
    text = (root / relative).read_text(encoding="utf-8")
    frontmatter, _body, _warnings = vault_module.parse_frontmatter(text)
    return frontmatter


def test_corpus_keys_resolve_to_canonical_product_structures(product_corpus) -> None:
    root, manifest = product_corpus

    assert all(path.startswith("Knowledge Base/") for path in manifest.key_to_path.values())

    for key in (
        "c4_entity_profile",
        "t4_shared_first_name_entity_a",
        "t4_shared_first_name_entity_b",
    ):
        frontmatter = _frontmatter(root, manifest.key_to_path[key])
        assert (frontmatter["type"], frontmatter["entity_type"]) == ("entity", "person")
        assert frontmatter["exomem_id"]

    for key in ("c7_hub_feature", "c7_hub_market", "c7_hub_search_ux"):
        frontmatter = _frontmatter(root, manifest.key_to_path[key])
        assert "hub" in frontmatter["tags"]

    for key in (
        "c2_grill_equipment_page",
        "t2_camera_gear_note",
        "c5_resource_profile",
        "t5_available_resource",
    ):
        relative = manifest.key_to_path[key]
        frontmatter = _frontmatter(root, relative)
        text = (root / relative).read_text(encoding="utf-8")
        assert str(frontmatter["created"]).endswith("Z")
        assert "## Observations" in text
        assert "- [resource]" in text

    for key in ("c1_subscriptions_collection", "c5_records_latest_unavailable"):
        relative = manifest.key_to_path[key]
        assert relative.startswith("Knowledge Base/Records/")
        collection = structured_collections.load_manifest(root, root / relative)
        assert collection.semantic_profile == "records"

    for key in ("c3_planning_item", "t3_other_project_planning_item"):
        relative = manifest.key_to_path[key]
        assert relative.startswith("Knowledge Base/Planning/")
        assert "/Items/" in relative
        assert "exomem-plan-audit" in (root / relative).read_text(encoding="utf-8")


def test_records_and_planning_are_populated_through_their_canonical_writers(
    product_corpus,
) -> None:
    root, manifest = product_corpus

    records_path = root / manifest.key_to_path["c5_records_latest_unavailable"]
    records_manifest = structured_collections.load_manifest(root, records_path)
    record_items = sorted((root / records_manifest.storage.source).glob("*.md"))
    assert len(record_items) == 2
    assert all("exomem-record-audit" in path.read_text(encoding="utf-8") for path in record_items)

    planning_manifests = sorted((root / "Knowledge Base" / "Planning").glob("*/_collection.md"))
    assert len(planning_manifests) == 2
    assert all(
        structured_collections.load_manifest(root, path).semantic_profile == "planning"
        for path in planning_manifests
    )


def test_fresh_activation_index_publishes_every_canonical_anchor_kind(product_corpus) -> None:
    root, manifest = product_corpus

    index = working_set_index.WorkingSetIndex(root)
    report = index.rebuild()
    anchors = index.anchors()
    by_path = {anchor.path: anchor for anchor in anchors}

    assert report["anchors"] == len(anchors)
    assert {anchor.kind for anchor in anchors} >= {"entity", "hub", "resource", "plan", "collection"}
    assert by_path[manifest.key_to_path["c4_entity_profile"]].kind == "entity"
    assert by_path[manifest.key_to_path["c7_hub_feature"]].kind == "hub"
    assert by_path[manifest.key_to_path["c1_subscriptions_collection"]].kind == "collection"
    assert any(
        anchor.kind == "plan" and anchor.title == "Extending the reporting module"
        for anchor in anchors
    )
    assert by_path[manifest.key_to_path["c5_resource_profile"]].kind == "resource"


def test_exact_corpus_hash_binds_canonical_entity_identity(tmp_path: Path) -> None:
    entity_path = tmp_path / "Knowledge Base" / "People" / "rowan-ashfield.md"
    entity_path.parent.mkdir(parents=True)
    original = """---
type: entity
entity_type: person
exomem_id: 11111111-1111-4111-8111-111111111111
title: Rowan Ashfield
---

# Rowan Ashfield
"""
    entity_path.write_text(original, encoding="utf-8")
    before = _corpus_hash(tmp_path)
    edited = re.sub(
        r"(?m)^exomem_id: [0-9a-f-]+$",
        "exomem_id: ffffffff-ffff-4fff-8fff-ffffffffffff",
        original,
        count=1,
    )
    assert edited != original
    entity_path.write_text(edited, encoding="utf-8")

    assert _corpus_hash(tmp_path) != before


def test_resource_fixture_refuses_to_fall_back_when_the_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands

    def refuse(*_args, **_kwargs):
        raise RuntimeError("writer refused fixture")

    monkeypatch.setattr(commands, "op_manage_memory_file", refuse)

    with pytest.raises(RuntimeError, match="writer refused fixture"):
        _governed_resource(
            tmp_path,
            path="Knowledge Base/Systems/resource.md",
            title="Resource",
            observation="A governed resource.",
            tags=("resource",),
        )
    assert not (tmp_path / "Knowledge Base/Systems/resource.md").exists()


def test_public_builder_isolates_hostile_ambient_runtime_state(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[1]
    hostile = tmp_path / "hostile-ambient"
    corpus = tmp_path / "corpus"
    script = r'''
import json
import os
from pathlib import Path

from epistemic.corpora.context_activation import build_corpus
from exomem import writer_lease


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes().hex()
        for path in root.rglob("*")
        if path.is_file()
    }


hostile = Path(os.environ["CORPUS_TEST_HOSTILE_ROOT"])
manager = writer_lease.get_manager()
before_files = snapshot(hostile)
before_env = dict(os.environ)
manifest = build_corpus(Path(os.environ["CORPUS_TEST_OUTPUT"]), distractor_count=0)
assert snapshot(hostile) == before_files
assert dict(os.environ) == before_env
assert writer_lease.get_manager() is manager
print(json.dumps({"corpus_id": manifest.corpus_id, "paths": len(manifest.key_to_path)}))
'''
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_") and key != "PYTHONPATH"
    }
    env.update(
        {
            "PYTHONPATH": f"{repo_root / 'src'}:{repo_root / 'benchmarks'}",
            "CORPUS_TEST_HOSTILE_ROOT": str(hostile),
            "CORPUS_TEST_OUTPUT": str(corpus),
            "EXOMEM_STATE_ROOT": str(hostile / "state"),
            "EXOMEM_CONFIG_PATH": str(hostile / "config.json"),
            "EXOMEM_LOG_DIR": str(hostile / "logs"),
            "EXOMEM_CALL_LEDGER_DIR": str(hostile / "call-ledger"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(hostile / "writer-lease"),
            "EXOMEM_LEASE_COORDINATOR_DB": str(hostile / "lease-coordinator.sqlite"),
            "XDG_STATE_HOME": str(hostile / "xdg-state"),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    assert '"paths": 24' in completed.stdout
