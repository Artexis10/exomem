from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
)

from exomem import memory_refs, record_formats, record_governance, records
from exomem import structured_collections as collections
from exomem.governance import egress, receipts
from exomem.governance.principal import RequestPrincipal, request_scope

EXTERNAL = "external"


def _write_l6_rule(vault: Path, *, ceiling: int, paths: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "records.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "name: Records\n"
        f'paths: ["{paths}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\n"
        f"ceiling: {ceiling}\n",
        encoding="utf-8",
    )


def _disclosure_count(vault: Path) -> int:
    events = vault / "Knowledge Base" / "_Governance" / "events"
    return sum(
        1
        for event in events.rglob("*.jsonl")
        for line in event.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event_type") == "disclosure"
    )


@pytest.mark.parametrize("ceiling", range(7))
def test_records_full_release_requires_l6_before_manifest_parse(
    tmp_path: Path, ceiling: int
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    _write_l6_rule(tmp_path, ceiling=ceiling, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        if ceiling < 6:
            with pytest.raises(collections.CollectionError) as raised:
                record_governance.resolve_collection(tmp_path, manifest_path.relative_to(tmp_path))
            assert raised.value.code == "COLLECTION_NOT_FOUND"
        else:
            assert (
                record_governance.resolve_collection(
                    tmp_path, manifest_path.relative_to(tmp_path)
                ).path
                == manifest_path.relative_to(tmp_path).as_posix()
            )


def test_withheld_markdown_item_never_reaches_parse_caps_or_authorized_snapshot(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    withheld = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    withheld.write_bytes(b"---\nodometer: 999999999\nnot: [valid\n---\n" + b"x" * (600 * 1024))
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    # A narrower rule with the final scope wins for this one item.
    root = tmp_path / "Knowledge Base" / "_Governance" / "scopes"
    (root / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZZ\n"
        "name: Withheld\n"
        'paths: ["Records/vehicle-maintenance/Events/withheld/**"]\n',
        encoding="utf-8",
    )
    (tmp_path / "Knowledge Base" / "_Governance" / "rules" / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZY\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FZZ"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        result = record_governance.query_collection(tmp_path, manifest, limit=10)

    assert result.returned == 2
    assert all(row.get("odometer") != 999999999 for row in result.rows)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        first = record_governance.query_collection(
            tmp_path, manifest, limit=1, sort_by="occurred_on"
        )
        withheld.write_bytes(withheld.read_bytes() + b"hidden-only edit")
        continued = record_governance.query_collection(
            tmp_path, manifest, limit=1, sort_by="occurred_on", continuation=first.continuation
        )
    assert continued.rows


def test_log_and_dataset_source_require_full_release_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    calls: list[Path] = []
    original = record_formats.MarkdownItemsAdapter.read

    def watched(self: record_formats.MarkdownItemsAdapter):
        calls.append(self.source_path)
        return original(self)

    monkeypatch.setattr(record_formats.MarkdownItemsAdapter, "read", watched)
    (tmp_path / "Knowledge Base" / "_Governance" / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.query_collection(tmp_path, manifest)
    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert calls == []


@pytest.mark.parametrize(
    "aggregate",
    (
        "count",
        "sum:amount",
        "max:odometer",
        "latest:odometer",
        "distinct:provider",
        "profile",
        "group:status",
    ),
)
def test_authorized_rows_are_the_only_input_to_every_reduction_and_renderer(
    tmp_path: Path, aggregate: str
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    hidden = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    hidden.write_text(
        hidden.read_text(encoding="utf-8")
        .replace("odometer: 42750", "odometer: 999999999")
        .replace("amount: 54", "amount: 999999999")
        .replace("provider: Inspection Centre", "provider: Secret Provider"),
        encoding="utf-8",
    )
    hidden_relative = hidden.relative_to(tmp_path).as_posix()
    result = record_formats.query_collection(
        tmp_path,
        manifest,
        aggregate=aggregate,
        output_format="markdown",
        authorize_path=lambda path: path != hidden_relative,
    )

    assert result.total_matched == 2
    assert "999999999" not in result.rendered
    assert "Secret Provider" not in result.rendered
    assert "999999999" not in str(result.aggregate)
    assert "Secret Provider" not in str(result.aggregate)


def test_records_egress_envelopes_are_default_deny_and_never_use_l5_rows() -> None:
    valid = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "update",
        "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
        "item_key": "14d2bdca-7309-4852-9e1f-2fd1c9e60273",
        "before_item_hash": "a" * 64,
        "after_item_hash": "b" * 64,
        "before_container_hash": "c" * 64,
        "after_container_hash": "d" * 64,
        "payload_hash": None,
        "affected_paths": ["Knowledge Base/Records/vehicle-maintenance/Events/released/a.md"],
        "outcome": "committed",
        "audit_correlation": "e" * 24,
        "rows": [{"secret": "must not escape"}],
    }
    receipt = record_governance.project_mutation_receipt(
        valid
    )
    assert receipt == {
        "operation": "update",
        "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
        "item_key": "14d2bdca-7309-4852-9e1f-2fd1c9e60273",
        "before_item_hash": "a" * 64,
        "after_item_hash": "b" * 64,
        "before_container_hash": "c" * 64,
        "after_container_hash": "d" * 64,
        "affected_paths": ["Knowledge Base/Records/vehicle-maintenance/Events/released/a.md"],
        "outcome": "committed",
        "audit_correlation": "e" * 24,
    }
    assert record_governance.project_mutation_receipt({"operation": "update"}) == {
        "withheld": True,
        "reason": "invalid_record_receipt",
    }
    assert egress.project(
        record_governance._RecordEnvelope({"rows": [{"secret": "must not escape"}]}),
        egress.LEVEL_EXCERPT,
        kind="record_query",
    ) == {"withheld": True, "reason": "records_requires_full_release"}


@pytest.mark.parametrize(
    ("fixture_copy", "output_format", "aggregate"),
    (
        (copy_vehicle_maintenance_fixture, "json", None),
        (copy_x3_fixture, "markdown", None),
        (copy_dataset_fixture, "csv", "count"),
    ),
)
def test_query_projection_rebuilds_each_rendered_format_from_typed_result(
    tmp_path: Path,
    fixture_copy: Callable[[Path], Path],
    output_format: str,
    aggregate: str | None,
) -> None:
    fixture = fixture_copy(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    result = record_formats.query_collection(
        tmp_path, manifest, output_format=output_format, aggregate=aggregate, limit=3
    )

    projected = record_governance.project_query_result(
        replace(result, rendered="forged renderer input"), manifest, output_format=output_format
    )

    assert projected["collection_id"] == manifest.collection_id
    assert projected["aggregate"] == result.aggregate
    assert "forged renderer input" not in projected["rendered"]
    if output_format == "json":
        assert json.loads(projected["rendered"])["rows"] == result.rows
    elif output_format == "markdown":
        assert projected["rendered"].startswith("---\ncollection_id: ")
    else:
        assert projected["rendered"].startswith("# collection_id: ")


def test_query_projection_withholds_forged_query_system_source_and_aggregate_fields(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    result = record_formats.query_collection(tmp_path, manifest, limit=1)
    forged_row = dict(result.rows[0])
    forged_row.pop("item_version")

    for forged in (
        replace(result, query={**result.query, "unexpected": "secret"}),
        replace(result, rows=[forged_row]),
        replace(result, continuation="forged"),
        replace(result, source_versions=(collections.SourceVersion("../secret", "0" * 64),)),
        replace(
            record_formats.query_collection(tmp_path, manifest, aggregate="count"),
            aggregate={"count": 1, "secret": "must not escape"},
        ),
    ):
        assert record_governance.project_query_result(forged, manifest) == {
            "withheld": True,
            "reason": "invalid_record_query",
        }


@pytest.mark.parametrize(
    "aggregate",
    ("count", "sum:amount", "max:odometer", "latest:odometer", "distinct:provider", "profile", "group:status"),
)
def test_query_projection_accepts_every_supported_aggregate_shape(
    tmp_path: Path, aggregate: str
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    result = record_formats.query_collection(tmp_path, manifest, aggregate=aggregate)

    projected = record_governance.project_query_result(result, manifest)

    assert projected["aggregate"] == result.aggregate


def test_query_projection_accepts_manifest_declared_expanded_child_fields(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    result = record_formats.query_collection(tmp_path, manifest, expand_children=True, limit=3)

    projected = record_governance.project_query_result(result, manifest)

    assert projected["rows"] == result.rows


def test_manifest_projector_round_trips_opaque_plan_descriptor_without_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary",
            """
templates:
  - path: Templates/private.md
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query: {limit: 12}
    - reference: exomem://vault/Planning/private.md
      query: {limit: 12}
    - reference: exomem://source/Planning/private
      query: {limit: 12}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    template = fixture / "Templates" / "private.md"
    template.parent.mkdir()
    template.write_text("private template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)

    def unexpected_resolution(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("opaque planning references must not be resolved")

    monkeypatch.setattr(memory_refs, "resolve_identifier_read_only", unexpected_resolution)

    projected = record_governance.project_manifest(tmp_path, manifest)

    assert projected["collection_id"] == manifest.collection_id
    assert projected["plans"] == [
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12},
        },
        {"reference": "exomem://vault/Planning/private.md", "query": {"limit": 12}},
        {"reference": "exomem://source/Planning/private", "query": {"limit": 12}},
    ]
    assert "rows" not in projected

    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    root = tmp_path / "Knowledge Base" / "_Governance"
    (root / "scopes" / "template.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZX\n"
        "name: Template\n"
        'paths: ["Records/vehicle-maintenance/Templates/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "template.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZW\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FZX"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.project_manifest(tmp_path, manifest)
    assert raised.value.code == "COLLECTION_NOT_FOUND"


def test_opaque_plan_reference_has_missing_and_hidden_target_parity(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    reference = "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary",
            f"""
links:
  plans:
    - reference: {reference}
      query: {{limit: 12}}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Planning/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        missing = record_governance.project_manifest(tmp_path, manifest)
    target = tmp_path / "Knowledge Base" / "Planning" / "private.md"
    target.parent.mkdir(parents=True)
    target.write_text(f"---\nexomem_id: {reference.rsplit('/', 1)[1]}\n---\nprivate", encoding="utf-8")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        hidden = record_governance.project_manifest(tmp_path, manifest)

    assert hidden["plans"] == missing["plans"] == [{"reference": reference, "query": {"limit": 12}}]


@pytest.mark.parametrize(
    "reference",
    (
        "exomem://memory/99F6FA8B-5D6E-43F8-8CDF-E30767E8F4D7",
        "exomem://vault/Planning/../private.md",
        "exomem://vault/Planning%2Fprivate.md",
        "exomem://source/Planning//private",
    ),
)
def test_opaque_plan_reference_rejects_noncanonical_or_unsafe_uris(reference: str) -> None:
    assert record_governance._opaque_plan_reference(reference) is None


def _write_l0_rule(vault: Path, *, name: str, paths: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    suffix = {"private": ("C1", "C2"), "secret": ("C3", "C4"), "blocked": ("C5", "C6")}[name]
    (root / "scopes" / f"{name}.yaml").write_text(
        "governance_version: 1\n"
        f"id: 01ARZ3NDEKTSV4RRFFQ69G5F{suffix[0]}\n"
        f"name: {name}\n"
        f'paths: ["{paths}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / f"{name}.yaml").write_text(
        "governance_version: 1\n"
        f"id: 01ARZ3NDEKTSV4RRFFQ69G5F{suffix[1]}\n"
        f'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5F{suffix[0]}"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )


def test_manifest_projection_omits_hidden_links_and_unknown_nested_metadata(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    private = tmp_path / "Knowledge Base" / "Planning" / "private.md"
    private.parent.mkdir(parents=True)
    private.write_text(
        "---\nexomem_id: 81947000-4c22-46e4-9874-23fed028314b\n---\nprivate", encoding="utf-8"
    )
    secret = tmp_path / "Knowledge Base" / "Evidence" / "Secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("private", encoding="utf-8")
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace("items:\n        type: string", "items:\n        type: link")
        .replace(
            "\n---\n\nOne ordinary",
            """
templates:
  - path: Templates/project.md
    default_properties:
      asset: "[[Knowledge Base/Evidence/Secret.md]]"
      services: ["[[Knowledge Base/Evidence/Secret.md]]"]
      provider: Northside Garage
      unknown: must-not-escape
links:
  plans:
    - reference: exomem://memory/81947000-4c22-46e4-9874-23fed028314b
      query: {filters: {asset: "[[Knowledge Base/Evidence/Secret.md]]", status: completed}, limit: 12}
    - reference: exomem://vault/Planning/private.md
      query: {limit: 12}
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query: {limit: 12}
views:
  current:
    query: {filters: {asset: "[[Knowledge Base/Evidence/Secret.md]]", status: completed}, limit: 12}
  latest: {sort: [occurred_on, desc]}
  malformed: {secret: must-not-escape}
governance:
  classification: internal
  release: {tiers: [internal]}
  secret_path: Knowledge Base/Evidence/Secret.md
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    (fixture / "Templates").mkdir()
    (fixture / "Templates" / "project.md").write_text("template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Planning/**")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        projected = record_governance.project_manifest(tmp_path, manifest)

    assert projected["templates"] == [
        {"path": manifest.templates[0].path, "default_properties": {"provider": "Northside Garage"}}
    ]
    assert projected["plans"] == [
        {
            "reference": "exomem://memory/81947000-4c22-46e4-9874-23fed028314b",
            "query": {"filters": {"status": "completed"}, "limit": 12},
        },
        {"reference": "exomem://vault/Planning/private.md", "query": {"limit": 12}},
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12},
        },
    ]
    assert projected["views"] == {
        "current": {"query": {"filters": {"status": "completed"}, "limit": 12}},
        "latest": {"sort": ["occurred_on", "desc"]},
    }
    assert projected["governance"] == {
        "classification": "internal",
        "release": {"tiers": ["internal"]},
    }


def test_manifest_projection_omits_malformed_plan_and_view_descriptors(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary",
            """
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query: {filters: {unknown: secret}, limit: 0}
views:
  malformed: {query: {filters: {unknown: secret}, limit: 0}}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)

    projected = record_governance.project_manifest(tmp_path, manifest)

    assert projected["plans"] == []
    assert projected["views"] == {}


def test_manifest_projection_json_normalizes_frozen_nested_values(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    projected = record_governance.project_manifest(tmp_path, manifest)

    assert json.loads(json.dumps(projected))["plans"] == projected["plans"]


@pytest.mark.parametrize("target", ("Events", "Templates/**"))
def test_manifest_projection_requires_l6_for_source_and_templates(tmp_path: Path, target: str) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary", "\ntemplates: [{path: Templates/project.md}]\n---\n\nOne ordinary"
        ),
        encoding="utf-8",
    )
    (fixture / "Templates").mkdir()
    (fixture / "Templates" / "project.md").write_text("template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths=f"Records/vehicle-maintenance/{target}")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.project_manifest(tmp_path, manifest)

    assert raised.value.code == "COLLECTION_NOT_FOUND"


def test_schema_link_projection_omits_anchors_missing_and_hidden_targets(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    secret = tmp_path / "Knowledge Base" / "Evidence" / "Secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("hidden", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        projected = record_governance._project_links(
            tmp_path,
            manifest,
            {
                "asset": "[[Knowledge Base/Evidence/Secret.md#receipt|Secret]]",
                "receipt": "[[Future evidence]]",
            },
        )

    assert projected == {}


def test_schema_link_projection_supports_exact_paths_and_caches_normalized_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "items:\n        type: string", "items:\n        type: link"
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    secret = tmp_path / "Knowledge Base" / "Evidence" / "Secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("hidden", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")

    calls: list[str] = []
    original = record_governance._authorize

    def watched(root: Path, relative: str, *, receipt: bool = False) -> bool:
        calls.append(relative)
        return original(root, relative, receipt=receipt)

    monkeypatch.setattr(record_governance, "_authorize", watched)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        projected = record_governance._project_links(
            tmp_path,
            manifest,
            {
                "asset": "Knowledge Base/Evidence/Secret",
                "receipt": "[[Knowledge Base/Evidence/Secret.md#receipt|Secret]]",
                "services": ["Knowledge Base/Evidence/Future.md", "not a path"],
            },
        )

    assert projected == {}
    assert calls.count("Knowledge Base/Evidence/Secret.md") == 1


def test_missing_and_withheld_link_targets_have_identical_public_query_state(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = tmp_path / "Knowledge Base" / "Assets" / "Vehicle.md"
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        missing = record_governance.query_collection(
            tmp_path, manifest, limit=1, sort_by="occurred_on"
        )
    target.parent.mkdir(parents=True)
    target.write_text("withheld", encoding="utf-8")
    _write_l0_rule(tmp_path, name="blocked", paths="Assets/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        withheld = record_governance.query_collection(
            tmp_path, manifest, limit=1, sort_by="occurred_on"
        )

    assert (missing.rows, missing.returned, missing.total_matched, missing.snapshot, missing.continuation) == (
        withheld.rows,
        withheld.returned,
        withheld.total_matched,
        withheld.snapshot,
        withheld.continuation,
    )


def test_precommit_refusal_leaves_canonical_and_manifest_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    before_manifest = (fixture / "_collection.md").read_bytes()
    before_items = sorted(path.read_bytes() for path in (fixture / "Events").rglob("*.md"))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise collections.CollectionError("COLLECTION_NOT_FOUND", "collection was not found")

    monkeypatch.setattr(record_governance, "precommit_authorize_mutation", refuse)
    with pytest.raises(collections.CollectionError) as raised:
        records.append_record(
            tmp_path,
            manifest,
            item={
                "occurred_on": "2026-07-01",
                "asset": "[[Assets/Vehicle]]",
                "odometer": 46000,
                "provider": "Workshop",
                "services": ["oil"],
                "amount": 20,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
                "next_due_odometer": None,
            },
            why="test disclosure refusal",
        )
    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert (fixture / "_collection.md").read_bytes() == before_manifest
    assert sorted(path.read_bytes() for path in (fixture / "Events").rglob("*.md")) == before_items


def test_mixed_release_mutation_refuses_before_reading_the_hidden_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    root = tmp_path / "Knowledge Base" / "_Governance"
    (root / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZZ\n"
        "name: Withheld\n"
        'paths: ["Records/vehicle-maintenance/Events/withheld/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZY\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FZZ"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    hidden = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    hidden.write_bytes(b"---\nmalformed: [\n")
    read = records._read_record_bytes

    def reject_hidden(vault_root: Path, relative: str):
        assert relative != hidden.relative_to(tmp_path).as_posix()
        return read(vault_root, relative)

    monkeypatch.setattr(records, "_read_record_bytes", reject_hidden)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            records.append_record(
                tmp_path,
                manifest,
                item={
                    "occurred_on": "2026-07-01",
                    "asset": "[[Assets/Vehicle]]",
                    "odometer": 46000,
                    "provider": "Workshop",
                    "services": ["oil"],
                    "amount": 20,
                    "currency": "GBP",
                    "status": "completed",
                    "next_due_on": None,
                    "next_due_odometer": None,
                },
                why="must see the complete collection",
            )
    assert raised.value.code == "COLLECTION_NOT_FOUND"


def test_precommit_runs_before_publication_and_publication_failure_is_not_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    order: list[str] = []

    def authorized(*_args: object, **_kwargs: object) -> None:
        order.append("precommit")

    def publication_failure(*_args: object, **_kwargs: object) -> None:
        order.append("publish")
        raise OSError("simulated publication failure")

    monkeypatch.setattr(record_governance, "precommit_authorize_mutation", authorized)
    monkeypatch.setattr(records.vault, "batch_atomic_write", publication_failure)
    with pytest.raises(collections.CollectionError) as raised:
        records.append_record(
            tmp_path,
            manifest,
            item={
                "occurred_on": "2026-07-01",
                "asset": "[[Assets/Vehicle]]",
                "odometer": 46000,
                "provider": "Workshop",
                "services": ["oil"],
                "amount": 20,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
                "next_due_odometer": None,
            },
            why="publication must remain a distinct outcome",
        )
    assert raised.value.code == "RECORD_PUBLICATION_FAILED"
    assert order == ["precommit", "publish"]


def test_governed_append_records_authorization_without_claiming_commit(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        receipt = records.append_record(
            tmp_path,
            manifest,
            item={
                "occurred_on": "2026-07-01",
                "asset": "[[Assets/Vehicle]]",
                "odometer": 46000,
                "provider": "Workshop",
                "services": ["oil"],
                "amount": 20,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
                "next_due_odometer": None,
            },
            why="governed append",
        )

    assert receipt["outcome"] == "committed"
    events = list((tmp_path / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl"))
    payloads = [
        json.loads(line)
        for event in events
        for line in event.read_text(encoding="utf-8").splitlines()
    ]
    outcomes = [
        outcome
        for payload in payloads
        if payload.get("event_type") == "disclosure"
        for outcome in payload.get("outcomes", [])
    ]
    assert any(outcome.get("decision") == "release_authorized" for outcome in outcomes)
    assert all("committed" not in outcome.values() for outcome in outcomes)


def test_governed_append_emits_one_disclosure_receipt_at_precommit_only(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        records.append_record(
            tmp_path,
            manifest,
            item={
                "occurred_on": "2026-07-01",
                "asset": "[[Assets/Vehicle]]",
                "odometer": 46000,
                "provider": "Workshop",
                "services": ["oil"],
                "amount": 20,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
                "next_due_odometer": None,
            },
            why="one final authorization receipt",
        )

    payloads = [
        json.loads(line)
        for event in (tmp_path / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl")
        for line in event.read_text(encoding="utf-8").splitlines()
    ]
    disclosures = [payload for payload in payloads if payload.get("event_type") == "disclosure"]
    assert len(disclosures) == 1
    assert {outcome["command"] for outcome in disclosures[0]["outcomes"]} == {
        "record_mutation_precommit"
    }


def test_governed_append_persists_receipt_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    order: list[str] = []
    append_event = egress.receipts.append_event
    publish = records.vault.batch_atomic_write

    def receipt(*args: object, **kwargs: object) -> None:
        order.append("receipt")
        append_event(*args, **kwargs)

    def publication(*args: object, **kwargs: object) -> object:
        order.append("publish")
        return publish(*args, **kwargs)

    monkeypatch.setattr(egress.receipts, "append_event", receipt)
    monkeypatch.setattr(records.vault, "batch_atomic_write", publication)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        records.append_record(
            tmp_path,
            manifest,
            item={
                "occurred_on": "2026-07-01",
                "asset": "[[Assets/Vehicle]]",
                "odometer": 46000,
                "provider": "Workshop",
                "services": ["oil"],
                "amount": 20,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
                "next_due_odometer": None,
            },
            why="receipt precedes publication",
        )

    assert order == ["receipt", "publish"]


@pytest.mark.parametrize("operation", ("resolve", "query", "manifest", "template"))
def test_governed_public_records_operations_emit_one_receipt(
    tmp_path: Path, operation: str
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary", "\ntemplates: [{path: Templates/project.md}]\n---\n\nOne ordinary"
        ),
        encoding="utf-8",
    )
    template = fixture / "Templates/project.md"
    template.parent.mkdir()
    template.write_text("template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        if operation == "resolve":
            record_governance.resolve_collection(tmp_path, manifest)
        elif operation == "query":
            record_governance.query_collection(tmp_path, manifest, limit=1)
        elif operation == "manifest":
            record_governance.project_manifest(tmp_path, manifest)
        else:
            assert record_governance.read_template(tmp_path, manifest, manifest.templates[0].path) == b"template"

    payloads = [
        json.loads(line)
        for event in (tmp_path / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl")
        for line in event.read_text(encoding="utf-8").splitlines()
    ]
    assert len([payload for payload in payloads if payload.get("event_type") == "disclosure"]) == 1


def test_governed_records_query_reuses_an_ambient_disclosure_boundary(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with egress.disclosure_boundary(tmp_path, "outer_records_command") as collector:
            record_governance.query_collection(tmp_path, manifest, limit=1)
            egress.emit_boundary_receipt(collector)

    payloads = [
        json.loads(line)
        for event in (tmp_path / "Knowledge Base" / "_Governance" / "events").rglob("*.jsonl")
        for line in event.read_text(encoding="utf-8").splitlines()
    ]
    disclosures = [payload for payload in payloads if payload.get("event_type") == "disclosure"]
    assert len(disclosures) == 1
    assert {outcome["command"] for outcome in disclosures[0]["outcomes"]} == {"outer_records_command"}


def test_disclosure_boundary_rejects_a_nested_different_vault(tmp_path: Path) -> None:
    other = tmp_path / "other"
    with egress.disclosure_boundary(tmp_path, "outer"):
        with pytest.raises(RuntimeError, match="different vault"):
            with egress.disclosure_boundary(other, "inner", join_existing=True):
                pass


@pytest.mark.parametrize("explicit_key", (True, False))
def test_governed_append_refuses_an_l0_future_item_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_key: bool
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    key = "ed6854be-c236-4cf6-9c90-18dfb8ac2544"
    target = f"Records/vehicle-maintenance/Events/{key}.md"
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths=target)
    before = ((fixture / "_collection.md").read_bytes(), activity.read_bytes())
    if not explicit_key:
        monkeypatch.setattr(records.uuid, "uuid4", lambda: uuid.UUID(key))

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            records.append_record(
                tmp_path,
                manifest,
                item={
                    "occurred_on": "2026-07-01",
                    "asset": "[[Assets/Vehicle]]",
                    "odometer": 46000,
                    "provider": "Workshop",
                    "services": ["oil"],
                    "amount": 20,
                    "currency": "GBP",
                    "status": "completed",
                    "next_due_on": None,
                    "next_due_odometer": None,
                },
                item_key=key if explicit_key else None,
                why="future item must be authorized before publication",
            )

    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert ((fixture / "_collection.md").read_bytes(), activity.read_bytes()) == before
    assert not (fixture / "Events" / f"{key}.md").exists()


def test_governed_create_refuses_an_l0_future_source_before_writing(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest_path = "Knowledge Base/Records/future/_collection.md"
    manifest_text = (fixture / "_collection.md").read_text(encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths="Records/future/Events")
    before = activity.read_bytes()

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            records.create_collection(
                tmp_path,
                manifest_path,
                manifest_text,
                why="future source must be authorized before create publication",
            )

    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert activity.read_bytes() == before
    assert not (tmp_path / manifest_path).exists()
    assert not (tmp_path / "Knowledge Base/Records/future/Events").exists()


def test_governed_append_authorizes_the_activity_log_before_publication(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths="log.md")
    before = ((fixture / "_collection.md").read_bytes(), activity.read_bytes())

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            records.append_record(
                tmp_path,
                manifest,
                item={
                    "occurred_on": "2026-07-01",
                    "asset": "[[Assets/Vehicle]]",
                    "odometer": 46000,
                    "provider": "Workshop",
                    "services": ["oil"],
                    "amount": 20,
                    "currency": "GBP",
                    "status": "completed",
                    "next_due_on": None,
                    "next_due_odometer": None,
                },
                why="audit publication target must be authorized",
            )

    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert ((fixture / "_collection.md").read_bytes(), activity.read_bytes()) == before
    assert _disclosure_count(tmp_path) == 0


def test_governed_replay_and_update_each_emit_one_final_receipt(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    item = {
        "occurred_on": "2026-07-01",
        "asset": "[[Assets/Vehicle]]",
        "odometer": 46000,
        "provider": "Workshop",
        "services": ["oil"],
        "amount": 20,
        "currency": "GBP",
        "status": "completed",
        "next_due_on": None,
        "next_due_odometer": None,
    }
    key = "ed6854be-c236-4cf6-9c90-18dfb8ac2544"

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        appended = records.append_record(tmp_path, manifest, item=item, item_key=key, why="append")
        assert _disclosure_count(tmp_path) == 1
        replayed = records.append_record(tmp_path, manifest.path, item=item, item_key=key, why="replay")
        assert replayed["outcome"] == "replayed"
        assert _disclosure_count(tmp_path) == 2
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=key,
            changes={"status": "scheduled"},
            expected_container_hash=appended["after_container_hash"],
            expected_item_version=appended["after_item_hash"],
            why="update",
        )

    assert _disclosure_count(tmp_path) == 3


def test_governed_create_emits_one_final_receipt(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        records.create_collection(
            tmp_path,
            "Knowledge Base/Records/new/_collection.md",
            (fixture / "_collection.md").read_text(encoding="utf-8"),
            why="create",
        )

    assert _disclosure_count(tmp_path) == 1


def test_governed_validation_and_cas_failures_emit_no_receipt(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    item = {
        "occurred_on": "2026-07-01",
        "asset": "[[Assets/Vehicle]]",
        "odometer": 46000,
        "provider": "Workshop",
        "services": ["oil"],
        "amount": 20,
        "currency": "GBP",
        "status": "completed",
        "next_due_on": None,
        "next_due_odometer": None,
    }

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError, match="SCHEMA_UNKNOWN_FIELD"):
            records.append_record(tmp_path, manifest, item={**item, "unknown": "no"}, why="validate")
        with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
            records.append_record(
                tmp_path,
                manifest,
                item=item,
                expected_container_hash="0" * 64,
                why="cas",
            )

    assert _disclosure_count(tmp_path) == 0


def test_failed_create_receipt_leaves_all_publication_targets_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    target = tmp_path / "Knowledge Base/Records/new/_collection.md"
    source = target.parent / "Events"
    before = activity.read_bytes()

    def receipt_failure(*_args: object, **_kwargs: object) -> None:
        raise receipts.ReceiptError("simulated receipt outage")

    monkeypatch.setattr(egress.receipts, "append_event", receipt_failure)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(egress.ReceiptUnavailableError):
            records.create_collection(
                tmp_path,
                target.relative_to(tmp_path),
                (fixture / "_collection.md").read_text(encoding="utf-8"),
                why="receipt failure before create publication",
            )

    assert activity.read_bytes() == before
    assert not target.exists()
    assert not source.exists()


def test_receipt_publication_failure_prevents_append_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    before = ((fixture / "_collection.md").read_bytes(), activity.read_bytes())

    def receipt_failure(*_args: object, **_kwargs: object) -> None:
        raise receipts.ReceiptError("simulated receipt outage")

    monkeypatch.setattr(egress.receipts, "append_event", receipt_failure)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")), egress.disclosure_boundary(
        tmp_path, "ambient"
    ):
        with pytest.raises(egress.ReceiptUnavailableError):
            records.append_record(
                tmp_path,
                manifest,
                item={
                    "occurred_on": "2026-07-01",
                    "asset": "[[Assets/Vehicle]]",
                    "odometer": 46000,
                    "provider": "Workshop",
                    "services": ["oil"],
                    "amount": 20,
                    "currency": "GBP",
                    "status": "completed",
                    "next_due_on": None,
                    "next_due_odometer": None,
                },
                why="receipt failure precedes publication",
            )

    assert ((fixture / "_collection.md").read_bytes(), activity.read_bytes()) == before
    assert not list((fixture / "Events").glob("*.md"))
