from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
)

from exomem import record_formats, record_governance, records
from exomem import structured_collections as collections
from exomem.governance import egress
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
    receipt = record_governance.project_mutation_receipt(
        {
            "operation": "update",
            "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
            "affected_paths": ["Knowledge Base/Records/vehicle-maintenance/Events/released/a.md"],
            "outcome": "committed",
            "rows": [{"secret": "must not escape"}],
        }
    )
    assert receipt == {
        "operation": "update",
        "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
        "affected_paths": ["Knowledge Base/Records/vehicle-maintenance/Events/released/a.md"],
        "outcome": "committed",
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
    tmp_path: Path,
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
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    template = fixture / "Templates" / "private.md"
    template.parent.mkdir()
    template.write_text("private template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)

    projected = record_governance.project_manifest(tmp_path, manifest)

    assert projected["collection_id"] == manifest.collection_id
    assert projected["plans"] == [
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12},
        }
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
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12},
        }
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


def test_schema_link_projection_handles_anchors_forward_refs_and_hidden_targets(tmp_path: Path) -> None:
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

    assert projected == {"receipt": "[[Future evidence]]"}


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

    assert projected == {"services": ["Knowledge Base/Evidence/Future.md"]}
    assert calls.count("Knowledge Base/Evidence/Secret.md") == 1


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
