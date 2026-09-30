from __future__ import annotations

import datetime
import inspect
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
    ledger_item,
    setup_ledger_collection,
)
from record_presentation_fixtures import manifest_text

from exomem import (
    memory_refs,
    plan_progress,
    planning,
    record_formats,
    record_governance,
    record_memory,
    records,
    vault,
)
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


def test_unresolved_dataset_source_never_reaches_records_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    semantic_scope = (
        tmp_path
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "semantic-source.yaml"
    )
    semantic_scope.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZZ\n"
        "name: Semantic sources\n"
        'types: ["source"]\n',
        encoding="utf-8",
    )

    def forbidden_read(_self: record_formats.DatasetAdapter):
        raise AssertionError("unresolved dataset reached the Records adapter")

    monkeypatch.setattr(record_formats.DatasetAdapter, "read", forbidden_read)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.query_collection(tmp_path, manifest, aggregate="count")
    assert raised.value.code == "COLLECTION_NOT_FOUND"


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


@pytest.mark.parametrize("section", ("contract", "diagnostics", "audit", "saved_views"))
def test_record_inspection_egress_rejects_nested_untyped_payloads(section: str) -> None:
    """The inspection allow-list must not pass arbitrary nested public data."""
    payload: dict[str, object] = {
        "kind": "collection",
        "report_only": True,
        "contract": {
            "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/vehicle-maintenance/_collection.md",
            "title": "Vehicle maintenance",
            "semantic_profile": "records",
            "schema_version": 1,
            "storage": {
                "strategy": "markdown-items",
                "source": "Knowledge Base/Records/vehicle-maintenance/Events",
                "format_version": 1,
            },
            "plans": [],
        },
        "legacy": None,
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "baseline", "gaps": []},
        "saved_views": [],
    }
    if section == "contract":
        payload["contract"] = {**payload["contract"], "secret": "must not escape"}  # type: ignore[arg-type]
    elif section == "diagnostics":
        payload["diagnostics"] = [{"code": "OK", "reason": "safe", "secret": "must not escape"}]
    elif section == "audit":
        payload["audit"] = {"status": "baseline", "gaps": [], "secret": "must not escape"}
    else:
        payload["saved_views"] = [
            {
                "name": "recent",
                "definition": {"query": {"limit": 1}, "secret": "must not escape"},
                "identity": "a" * 64,
            }
        ]

    assert egress.project(
        record_governance._RecordEnvelope(payload), egress.LEVEL_FULL, kind="record_inspection"
    ) == {"withheld": True, "reason": "invalid_projector_payload"}


@pytest.mark.parametrize(
    ("section", "value"),
    (
        ("kind", []),
        ("contract.semantic_profile", []),
        ("contract.storage.strategy", []),
        ("contract.title", "\ud800"),
        ("audit.status", []),
        ("saved_views.filter.op", []),
        ("saved_views.columns", [["unhashable"]]),
    ),
)
def test_record_inspection_egress_fails_closed_on_unhashable_hostile_scalars(
    section: str, value: object
) -> None:
    payload: dict[str, object] = {
        "kind": "collection",
        "report_only": True,
        "contract": {
            "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/vehicle-maintenance/_collection.md",
            "title": "Vehicle maintenance",
            "semantic_profile": "records",
            "schema_version": 1,
            "storage": {
                "strategy": "markdown-items",
                "source": "Knowledge Base/Records/vehicle-maintenance/Events",
                "format_version": 1,
            },
            "plans": [],
        },
        "legacy": None,
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "baseline", "gaps": []},
        "saved_views": [],
    }
    if section == "kind":
        payload["kind"] = value
    elif section == "contract.semantic_profile":
        payload["contract"]["semantic_profile"] = value  # type: ignore[index]
    elif section == "contract.storage.strategy":
        payload["contract"]["storage"]["strategy"] = value  # type: ignore[index]
    elif section == "contract.title":
        payload["contract"]["title"] = value  # type: ignore[index]
    elif section == "audit.status":
        payload["audit"]["status"] = value  # type: ignore[index]
    elif section == "saved_views.filter.op":
        payload["saved_views"] = [
            {
                "name": "recent",
                "definition": {"query": {"filters": [{"column": "status", "op": value, "value": "ok"}]}},
                "identity": "a" * 64,
            }
        ]
    else:
        payload["saved_views"] = [
            {
                "name": "recent",
                "definition": {"query": {"columns": value}},
                "identity": "a" * 64,
            }
        ]

    assert egress.project(
        record_governance._RecordEnvelope(payload), egress.LEVEL_FULL, kind="record_inspection"
    ) == {"withheld": True, "reason": "invalid_projector_payload"}


def test_record_inspection_validator_survives_two_argument_projector_reregistration() -> None:
    allowed = egress._PROJECTORS["record_inspection"]

    egress.register_projector("record_inspection", allowed)

    assert egress.project(
        record_governance._RecordEnvelope({"kind": "collection"}),
        egress.LEVEL_FULL,
        kind="record_inspection",
    ) == {"withheld": True, "reason": "invalid_projector_payload"}


def test_egress_fails_closed_when_a_projector_validator_raises() -> None:
    kind = "test_throwing_projector_validator"

    def raise_for_test(_payload: object) -> dict[str, object] | None:
        raise RuntimeError("hostile validator")

    egress.register_projector(kind, ("value",), validator=raise_for_test)
    try:
        projected = egress.project(
            record_governance._RecordEnvelope({"value": "safe"}), egress.LEVEL_FULL, kind=kind
        )
    finally:
        egress._PROJECTORS.pop(kind, None)
        egress._PROJECTOR_VALIDATORS.pop(kind, None)

    assert projected == {"withheld": True, "reason": "invalid_projector_payload"}


def test_record_inspection_egress_keeps_a_valid_null_saved_view_filter_value() -> None:
    payload = {
        "kind": "collection",
        "report_only": True,
        "contract": {
            "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/vehicle-maintenance/_collection.md",
            "title": "Vehicle maintenance",
            "semantic_profile": "records",
            "schema_version": 1,
            "storage": {
                "strategy": "markdown-items",
                "source": "Knowledge Base/Records/vehicle-maintenance/Events",
                "format_version": 1,
            },
            "plans": [],
        },
        "legacy": None,
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "baseline", "gaps": []},
        "saved_views": [
            {
                "name": "unset",
                "definition": {"query": {"filters": [{"column": "status", "op": "eq", "value": None}]}},
                "identity": "a" * 64,
            }
        ],
    }

    projected = egress.project(
        record_governance._RecordEnvelope(payload), egress.LEVEL_FULL, kind="record_inspection"
    )

    assert projected is not None
    assert projected["saved_views"][0]["definition"]["query"]["filters"][0]["value"] is None


def test_record_inspection_egress_accepts_collection_field_names_with_spaces_and_hyphens() -> None:
    payload = {
        "kind": "collection",
        "report_only": True,
        "contract": {
            "collection_id": "49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/vehicle-maintenance/_collection.md",
            "title": "Vehicle maintenance",
            "semantic_profile": "records",
            "schema_version": 1,
            "storage": {
                "strategy": "markdown-items",
                "source": "Knowledge Base/Records/events",
                "format_version": 1,
            },
            "plans": [],
        },
        "legacy": None,
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "baseline", "gaps": []},
        "saved_views": [
            {
                "name": "body-weight",
                "definition": {"query": {"columns": ["body weight", "trend-kg"]}},
                "identity": "a" * 64,
            }
        ],
    }

    projected = egress.project(
        record_governance._RecordEnvelope(payload), egress.LEVEL_FULL, kind="record_inspection"
    )

    assert projected is not None
    assert projected["saved_views"][0]["definition"]["query"]["columns"] == ["body weight", "trend-kg"]


def test_record_inspection_egress_accepts_legacy_tracker_identity() -> None:
    payload = {
        "kind": "legacy_tracker",
        "report_only": True,
        "contract": None,
        "legacy": {
            "collection_id": "legacy-49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/legacy-tracker.md",
            "inspect_only": True,
        },
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "not_applicable", "gaps": []},
        "saved_views": [],
    }

    projected = egress.project(
        record_governance._RecordEnvelope(payload), egress.LEVEL_FULL, kind="record_inspection"
    )

    assert projected is not None
    assert projected["legacy"]["collection_id"] == "legacy-49622075-9ff4-4660-9ab7-414854b5bca2"


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


def test_query_projection_keeps_saved_view_inside_its_typed_envelope(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "---\n\nOne ordinary",
            """views:
  scheduled:
    query:
      filters: {status: scheduled}
      sort_by: occurred_on
      descending: true
      limit: 1
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    result = record_formats.query_collection(tmp_path, manifest, view="scheduled")

    projected = record_governance.project_query_result(result, manifest)

    assert projected["view"] == result.view
    assert json.loads(projected["rendered"])["query"]["view"] == result.view
    assert record_governance.project_query_result(
        replace(result, view={"name": "scheduled", "definition": {}, "identity": "forged"}),
        manifest,
    ) == {"withheld": True, "reason": "invalid_record_query"}


@pytest.mark.parametrize("view", ("asset-membership", "service-membership"))
def test_governed_saved_view_refuses_a_withheld_link_filter_before_query(
    tmp_path: Path, view: str
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace("items:\n        type: string", "items:\n        type: link")
        .replace(
            "---\n\nOne ordinary",
            """views:
  asset-membership:
    query:
      filters:
        - column: asset
          op: in
          value: ["[[Assets/Vehicle]]"]
  service-membership:
    query:
      filters:
        - column: services
          op: contains
          value: "[[Assets/Vehicle]]"
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    target = tmp_path / "Knowledge Base" / "Assets" / "Vehicle.md"
    target.parent.mkdir()
    target.write_text("private", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths="Assets/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.query_collection(tmp_path, manifest, view=view)

    assert raised.value.code == "SAVED_VIEW_NOT_AVAILABLE"


def _nested_link_saved_view(tmp_path: Path) -> collections.CollectionManifest:
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        manifest_text().replace(
            "record_presentation:\n",
            "views:\n"
            "  hidden-child-link:\n"
            "    query:\n"
            "      filters:\n"
            "        - column: source\n"
            "          op: eq\n"
            '          value: "[[Private/Target]]"\n'
            "      columns: [source]\n"
            "      sort_by: source\n"
            "      aggregate: distinct:source\n"
            "      expand_child: measurements\n"
            "record_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    (path.parent / "Items").mkdir()
    target = tmp_path / "Knowledge Base/Private/Target.md"
    target.parent.mkdir()
    target.write_text("# Withheld target\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Private/**")
    return collections.load_manifest(tmp_path, path)


def test_nested_saved_view_link_filter_is_authorized_before_query_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _nested_link_saved_view(tmp_path)
    reads: list[str] = []

    def forbidden_read(self: record_formats.MarkdownItemsAdapter):  # noqa: ANN202
        reads.append(self.manifest.storage.source)
        raise AssertionError("saved-view authorization must precede canonical source read")

    monkeypatch.setattr(record_formats.MarkdownItemsAdapter, "read", forbidden_read)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.query_collection(tmp_path, manifest, view="hidden-child-link")

    assert raised.value.code == "SAVED_VIEW_NOT_AVAILABLE"
    assert reads == []


def test_inspection_withholds_nested_saved_view_link_literal_without_target_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _nested_link_saved_view(tmp_path)
    opened: list[str] = []
    real_read = vault.read_bounded_guarded_bytes

    def observed_read(root: Path, relative: str, **kwargs: object):  # noqa: ANN202
        opened.append(relative)
        return real_read(root, relative, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", observed_read)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspected = record_governance.inspect_collection(tmp_path, manifest)

    serialized = json.dumps(inspected, sort_keys=True)
    assert inspected["saved_views"] == []
    assert "SAVED_VIEW_NOT_AVAILABLE" in serialized
    assert "Private/Target" not in serialized and "[[Private/Target]]" not in serialized
    assert "Knowledge Base/Private/Target.md" not in opened


def test_ambiguous_boolean_saved_view_never_releases_nested_link_literal_or_target_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        manifest_text(two_tables=True).replace(
            "record_presentation:\n",
            "views:\n"
            "  ambiguous-child:\n"
            "    query:\n"
            "      expand_children: true\n"
            "      filters:\n"
            "        - column: source\n"
            "          op: eq\n"
            '          value: "[[Private/Target]]"\n'
            "record_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    (path.parent / "Items").mkdir()
    target = tmp_path / "Knowledge Base/Private/Target.md"
    target.parent.mkdir()
    target.write_text("# Withheld target\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Private/**")
    manifest = collections.load_manifest(tmp_path, path)

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspected = record_governance.inspect_collection(tmp_path, manifest)

    serialized = json.dumps(inspected, sort_keys=True)
    assert inspected["saved_views"] == []
    assert "INVALID_SAVED_VIEW" in serialized
    assert "Private/Target" not in serialized
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.query_collection(tmp_path, manifest, view="ambiguous-child")
    assert raised.value.code == "INVALID_SAVED_VIEW"


def test_saved_view_child_authorization_fails_closed_when_row_shape_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(manifest_text(two_tables=True), encoding="utf-8")
    (path.parent / "Items").mkdir()
    manifest = collections.load_manifest(tmp_path, path)
    links = record_governance._LinkProjector.create(tmp_path, manifest)
    query = {
        "expand_children": True,
        "filters": [{"column": "source", "op": "eq", "value": "[[Private/Target]]"}],
    }

    assert record_governance._saved_view_field_spec(manifest, query, "source") is None
    monkeypatch.setattr(
        collections,
        "resolve_saved_view",
        lambda _manifest, _name: collections.SavedView(
            "ambiguous", {"query": query}, "0" * 64
        ),
    )
    with pytest.raises(collections.CollectionError, match="SAVED_VIEW_NOT_AVAILABLE"):
        record_governance._authorize_saved_view(tmp_path, manifest, "ambiguous", links)


@pytest.mark.parametrize("parent_source", (False, True))
def test_cross_table_saved_view_never_releases_sibling_link_literal(
    tmp_path: Path, parent_source: bool
) -> None:
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    source = manifest_text(two_tables=True)
    if parent_source:
        source = source.replace(
            "    note:\n", "    source:\n      type: string\n    note:\n", 1
        )
    path.write_text(
        source.replace(
            "record_presentation:\n",
            "views:\n"
            "  cross-table:\n"
            "    query:\n"
            "      expand_child: qualifiers\n"
            "      filters:\n"
            "        - column: source\n"
            "          op: eq\n"
            '          value: "[[Private/Target]]"\n'
            "record_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    (path.parent / "Items").mkdir()
    target = tmp_path / "Knowledge Base/Private/Target.md"
    target.parent.mkdir()
    target.write_text("# Withheld target\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Private/**")
    manifest = collections.load_manifest(tmp_path, path)

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspected = record_governance.inspect_collection(tmp_path, manifest)

    serialized = json.dumps(inspected, sort_keys=True)
    assert inspected["saved_views"] == []
    assert "INVALID_SAVED_VIEW" in serialized
    assert "Private/Target" not in serialized
    assert record_governance._saved_view_field_spec(
        manifest,
        {"expand_child": "qualifiers"},
        "source",
    ) is None


def test_governed_inspection_is_typed_report_only_and_requires_l6_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=0, paths="Records/**")
    calls: list[Path] = []
    original = record_formats.inspect_collection

    def watched(*args: object, **kwargs: object) -> record_formats.CollectionInspection:
        calls.append(Path(args[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(record_formats, "inspect_collection", watched)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.inspect_collection(tmp_path, manifest)
    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert calls == []

    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspection = record_governance.inspect_collection(tmp_path, manifest)
    assert inspection["kind"] == "collection"
    assert inspection["report_only"] is True
    assert inspection["contract"]["collection_id"] == manifest.collection_id
    assert inspection["legacy"] is None
    assert "snapshot" in inspection
    assert "source_versions" in inspection
    assert "diagnostics" in inspection
    assert "audit" in inspection
    assert "saved_views" in inspection


def test_governed_inspection_reports_malformed_saved_views_without_raising(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "---\n\nOne ordinary",
            """views:
  broken:
    query:
      filters: not-a-filter
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["report_only"] is True
    assert inspection["diagnostics"] == [
        {"code": "INVALID_SAVED_VIEW", "reason": "saved view filters are invalid"}
    ]
    assert inspection["saved_views"] == []


def test_governed_inspection_reports_hidden_and_missing_templates_the_same(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "---\n\nOne ordinary",
            "templates: [{path: Templates/session.md}]\n---\n\nOne ordinary",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        missing = record_governance.inspect_collection(tmp_path, manifest)
    template = fixture / "Templates" / "session.md"
    template.parent.mkdir()
    template.write_text("private", encoding="utf-8")
    _write_l0_rule(tmp_path, name="blocked", paths="Records/vehicle-maintenance/Templates/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        withheld = record_governance.inspect_collection(tmp_path, manifest)

    assert missing["diagnostics"] == withheld["diagnostics"]
    assert missing["diagnostics"][-1] == {
        "code": "TEMPLATE_UNAVAILABLE",
        "reason": "declared template is unavailable",
    }


def test_inspection_guard_reload_does_not_read_withheld_item_or_expose_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    hidden = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    hidden_rel = hidden.relative_to(tmp_path).as_posix()
    read = record_formats.vault.read_bounded_guarded_bytes

    def deny_hidden_read(root: Path, relative: str, **kwargs: object):
        assert relative != hidden_rel
        return read(root, relative, **kwargs)

    monkeypatch.setattr(record_formats.vault, "read_bounded_guarded_bytes", deny_hidden_read)
    authorize = record_governance._authorize
    monkeypatch.setattr(
        record_governance,
        "_authorize",
        lambda root, path, *, receipt, policy=None: path != hidden_rel
        and authorize(root, path, receipt=receipt, policy=policy),
    )

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert hidden_rel not in json.dumps(inspection)


def test_withheld_template_omits_inspection_lifecycle_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "---\n\nOne ordinary", "templates: [{path: Templates/session.md}]\n---\n\nOne ordinary"
        ), encoding="utf-8"
    )
    template = fixture / "Templates" / "session.md"
    template.parent.mkdir()
    template.write_text("private", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    template_rel = template.relative_to(tmp_path).as_posix()
    authorize = record_governance._authorize
    monkeypatch.setattr(
        record_governance,
        "_authorize",
        lambda root, path, *, receipt, policy=None: path != template_rel
        and authorize(root, path, receipt=receipt, policy=policy),
    )

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["diagnostics"][-1]["code"] == "TEMPLATE_UNAVAILABLE"
    assert "lifecycle_guards" not in inspection


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


def test_governed_inspection_round_trips_opaque_plans_without_resolving_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
      query: {{filters: {{status: completed}}, limit: 12}}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)

    def unexpected_resolution(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("opaque planning references must not be resolved")

    monkeypatch.setattr(memory_refs, "resolve_identifier_read_only", unexpected_resolution)
    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["contract"]["plans"] == [
        {"reference": reference, "query": {"filters": {"status": "completed"}, "limit": 12}}
    ]


def test_governed_inspection_refuses_a_denied_source_before_plan_or_format_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _write_l0_rule(
        tmp_path,
        name="blocked",
        paths="Records/vehicle-maintenance/Events",
    )

    def unexpected_format_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a denied storage source must not be inspected")

    monkeypatch.setattr(record_formats, "inspect_collection", unexpected_format_read)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            record_governance.inspect_collection(tmp_path, manifest)

    assert raised.value.code == "COLLECTION_NOT_FOUND"
    disclosed = str(raised.value)
    assert reference not in disclosed
    assert manifest.path not in disclosed
    assert manifest.storage.source not in disclosed


def test_record_inspection_validator_accepts_only_bounded_exact_plan_descriptors(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    payload = record_governance.inspect_collection(tmp_path, manifest)
    contract = dict(payload["contract"])
    contract["plans"] = [
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"filters": {"status": "completed"}, "limit": 12},
        }
    ]
    payload["contract"] = contract

    accepted = record_governance._validate_record_inspection(payload)
    assert accepted is not None
    assert accepted["contract"]["plans"] == contract["plans"]

    contract["plans"][0]["extra"] = "must-not-escape"
    assert record_governance._validate_record_inspection(payload) is None


def test_governed_inspection_plans_keep_hidden_targets_opaque_and_filter_query_links(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    reference = "exomem://vault/Planning/private.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace("items:\n        type: string", "items:\n        type: link")
        .replace(
            "\n---\n\nOne ordinary",
            f"""
links:
  plans:
    - reference: {reference}
      query: {{filters: {{asset: "[[Knowledge Base/Evidence/Secret.md]]", status: completed}}, limit: 12}}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="private", paths="Planning/**")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        missing = record_governance.inspect_collection(tmp_path, manifest)
    target = tmp_path / "Knowledge Base" / "Planning" / "private.md"
    target.parent.mkdir(parents=True)
    target.write_text("private", encoding="utf-8")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        hidden = record_governance.inspect_collection(tmp_path, manifest)

    assert hidden["contract"]["plans"] == missing["contract"]["plans"] == [
        {"reference": reference, "query": {"filters": {"status": "completed"}, "limit": 12}}
    ]


def test_record_inspection_validator_refuses_more_than_thirty_two_plan_descriptors(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    payload = record_governance.inspect_collection(tmp_path, manifest)
    contract = dict(payload["contract"])
    contract["plans"] = [
        {"reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7", "query": {"limit": 12}}
    ] * 33
    payload["contract"] = contract

    assert record_governance._validate_record_inspection(payload) is None


@pytest.mark.parametrize(
    "plan",
    (
        {"reference": "exomem://vault/Planning/../private.md", "query": {"limit": 12}},
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12, "extra": "must-not-escape"},
        },
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"filters": {"status": object()}, "limit": 12},
        },
    ),
)
def test_record_inspection_validator_refuses_hostile_plan_values(
    tmp_path: Path, plan: dict[str, object]
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    payload = record_governance.inspect_collection(tmp_path, manifest)
    contract = dict(payload["contract"])
    contract["plans"] = [plan]
    payload["contract"] = contract

    assert record_governance._validate_record_inspection(payload) is None


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
        "exomem://vault/C%3A/Windows/secret.md",
        "exomem://vault/C%3a/Windows/secret.md",
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

    calls: list[tuple[str, bool]] = []
    original = record_governance._authorize

    def watched(root: Path, relative: str, *, receipt: bool = False, policy: object | None = None) -> bool:
        calls.append((relative, receipt))
        return original(root, relative, receipt=receipt, policy=policy)

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
    secret_calls = [receipt for relative, receipt in calls if relative == "Knowledge Base/Evidence/Secret.md"]
    assert secret_calls == [True]


def test_schema_link_projection_ignores_withheld_colliding_wikilink_titles(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = tmp_path / "Knowledge Base" / "Notes" / "Public" / "target.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: Target\n---\npublic", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        before = record_governance._project_links(tmp_path, manifest, {"asset": "[[Target]]"})

    hidden = tmp_path / "Knowledge Base" / "Evidence" / "secret-target.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("---\ntitle: Target\n---\nwithheld", encoding="utf-8")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        after = record_governance._project_links(tmp_path, manifest, {"asset": "[[Target]]"})

    assert before == after == {"asset": "[[Target]]"}


def test_schema_link_projection_ignores_withheld_colliding_memory_ids(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    identity = "4e482aed-3f70-4b78-9342-1ba08f3a5bd3"
    reference = memory_refs.memory_ref(identity)
    target = tmp_path / "Knowledge Base" / "Notes" / "Public" / "target.md"
    target.parent.mkdir(parents=True)
    target.write_text(f"---\nexomem_id: {identity}\n---\npublic", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        before = record_governance._project_links(tmp_path, manifest, {"asset": reference})

    hidden = tmp_path / "Knowledge Base" / "Evidence" / "secret-target.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(f"---\nexomem_id: {identity}\n---\nwithheld", encoding="utf-8")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        after = record_governance._project_links(tmp_path, manifest, {"asset": reference})

    assert before == after == {"asset": reference}


def test_numeric_record_query_does_not_build_a_link_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault

    fixture = copy_dataset_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    def unexpected_walk(_root: Path) -> object:
        raise AssertionError("link-free query must not scan the vault")

    monkeypatch.setattr(vault, "walk_vault_md", unexpected_walk)

    result = record_governance.query_collection(tmp_path, manifest, columns=["value"], limit=1)

    assert result.rows[0]["value"] == 101.0


@pytest.mark.parametrize(
    "reference",
    (
        "[[Knowledge Base/Evidence/Target.md]]",
        "exomem://vault/Knowledge Base/Evidence/Target.md",
        "exomem://source/Knowledge Base/Evidence/Target",
    ),
)
def test_exact_record_link_does_not_build_a_candidate_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    from exomem import vault

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = tmp_path / "Knowledge Base" / "Evidence" / "Target.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Target\n", encoding="utf-8")

    def unexpected_walk(_root: Path) -> object:
        raise AssertionError("exact link must not scan the vault")

    monkeypatch.setattr(vault, "walk_vault_md", unexpected_walk)

    projected = record_governance._project_links(tmp_path, manifest, {"asset": reference})

    assert projected == {"asset": reference}


def test_over_cap_bare_link_resolution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = tmp_path / "Knowledge Base" / "Notes" / "Target.md"
    other = tmp_path / "Knowledge Base" / "Notes" / "Other.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Target\n", encoding="utf-8")
    other.write_text("# Other\n", encoding="utf-8")
    monkeypatch.setattr(record_governance, "_PUBLIC_LINK_INDEX_RAW_CANDIDATES", 1)
    monkeypatch.setattr(vault, "walk_vault_md", lambda _root: iter((target, other)))

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        projected = record_governance._project_links(tmp_path, manifest, {"asset": "[[Target]]"})

    assert projected == {}


def test_schema_link_projection_records_authorized_target_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = tmp_path / "Knowledge Base" / "Notes" / "Public" / "Target.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Target\n", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    calls: list[tuple[str, bool]] = []
    original = record_governance._authorize

    def watched(root: Path, relative: str, *, receipt: bool = False, policy: object | None = None) -> bool:
        calls.append((relative, receipt))
        return original(root, relative, receipt=receipt, policy=policy)

    monkeypatch.setattr(record_governance, "_authorize", watched)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        projected = record_governance._project_links(tmp_path, manifest, {"asset": "[[Target]]"})

    assert projected == {"asset": "[[Target]]"}
    target_calls = [receipt for relative, receipt in calls if relative == "Knowledge Base/Notes/Public/Target.md"]
    assert target_calls == [False, True]


def test_manifest_link_projections_ignore_withheld_colliding_titles(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "\n---\n\nOne ordinary",
            """
templates:
  - path: Templates/session.md
    default_properties: {asset: "[[Target]]"}
links:
  plans:
    - reference: exomem://memory/81947000-4c22-46e4-9874-23fed028314b
      query: {filters: {asset: "[[Target]]"}, limit: 12}
views:
  current:
    query: {filters: {asset: "[[Target]]"}, limit: 12}
---

One ordinary""",
        ),
        encoding="utf-8",
    )
    (fixture / "Templates").mkdir()
    (fixture / "Templates" / "session.md").write_text("template", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    target = tmp_path / "Knowledge Base" / "Notes" / "Public" / "target.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: Target\n---\npublic", encoding="utf-8")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        before = record_governance.project_manifest(tmp_path, manifest)

    hidden = tmp_path / "Knowledge Base" / "Evidence" / "secret-target.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("---\ntitle: Target\n---\nwithheld", encoding="utf-8")
    _write_l0_rule(tmp_path, name="secret", paths="Evidence/**")
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        after = record_governance.project_manifest(tmp_path, manifest)

    assert before == after


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


def test_query_requesting_a_link_field_still_resolves_and_withholds_it(tmp_path: Path) -> None:
    """G1: an ordinary query (no `late_link_projection` opt-in -- nothing in
    the public surface can set it, see the guard test below) keeps full,
    eager link governance exactly as on base: an authorized bare-title
    target still resolves and an unauthorized (here, missing) one is still
    withheld from the row.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    (tmp_path / "Knowledge Base" / "Vehicle.md").write_text("# Vehicle\n", encoding="utf-8")
    (fixture / "Events" / "2026-07-20-bare.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: 33333333-3333-4333-8333-333333333333\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-20\n"
        'asset: "[[Vehicle]]"\n'
        'receipt: "[[Missing Receipt]]"\n'
        "status: completed\n"
        "---\n",
        encoding="utf-8",
    )

    result = record_governance.query_collection(
        tmp_path,
        manifest,
        columns=["asset", "receipt"],
        sort_by="occurred_on",
        descending=True,
        limit=1,
    )

    row = result.rows[0]
    assert row["asset"] == "[[Vehicle]]"
    assert row["receipt"] is None


def test_public_tool_surfaces_cannot_set_late_link_projection() -> None:
    """`late_link_projection` is an internal opt-in on `query_collection`,
    never a request parameter: no public tool entry point declares it, so
    nothing in a caller's input can ever reach or set it.
    """
    for entry_point in (record_memory.record_memory, planning.query, plan_progress.review):
        assert "late_link_projection" not in inspect.signature(entry_point).parameters, (
            f"{entry_point.__qualname__} must not expose late_link_projection"
        )
    with pytest.raises(TypeError, match="late_link_projection"):
        record_memory.record_memory(
            Path("/nonexistent"), "query", collection="x", late_link_projection=True
        )


def _empty_columns_fixture(tmp_path: Path) -> collections.CollectionManifest:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    (fixture / "Events" / "2026-07-20-bare.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: 33333333-3333-4333-8333-333333333333\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-20\n"
        'asset: "[[Vehicle]]"\n'
        'receipt: "[[Missing Receipt]]"\n'
        "status: completed\n"
        "---\n",
        encoding="utf-8",
    )
    return manifest


def test_empty_columns_through_record_memory_still_withholds_link_fields(tmp_path: Path) -> None:
    """B1 (base-identical): `columns=[]` (falsy, so `evaluate_rows` treats it
    as "no column restriction" and returns the full row) must still return
    the fully *governed* row through the actual public record_memory tool
    surface -- an unresolvable bare-title `asset`/`receipt` withheld, never
    the raw wikilink text.
    """
    manifest = _empty_columns_fixture(tmp_path)

    result = record_memory.record_memory(
        tmp_path,
        "query",
        collection=manifest.path,
        columns=[],
        sort_by="occurred_on",
        descending=True,
        limit=1,
    )

    row = result["rows"][0]
    assert "asset" not in row or row["asset"] is None
    assert "receipt" not in row or row["receipt"] is None
    assert row["status"] == "completed"


def _parts_collection(tmp_path: Path) -> collections.CollectionManifest:
    """A Records collection with an array-of-link field, for B2/M3/parity."""
    coll_dir = tmp_path / "Knowledge Base" / "Records" / "parts"
    coll_dir.mkdir(parents=True)
    (coll_dir / "_collection.md").write_text(
        "---\n"
        "type: collection\n"
        "exomem_id: 44444444-5555-4666-8777-888888888888\n"
        "title: Parts inventory\n"
        "semantic_profile: records\n"
        "collection_version: 1\n"
        "schema_version: 1\n"
        "lifecycle: active\n"
        "storage:\n"
        "  strategy: markdown-items\n"
        "  source: Items\n"
        "  format_version: 1\n"
        "item_schema:\n"
        "  natural_key: [observed_on]\n"
        "  fields:\n"
        "    observed_on:\n"
        "      type: date\n"
        "      required: true\n"
        "    status:\n"
        "      type: string\n"
        "    parts:\n"
        "      type: array\n"
        "      items:\n"
        "        type: link\n"
        "---\n\nParts inventory fixture.\n",
        encoding="utf-8",
    )
    (coll_dir / "Items").mkdir()
    return collections.load_manifest(tmp_path, coll_dir / "_collection.md")


def _coll_dir(tmp_path: Path, manifest: collections.CollectionManifest) -> Path:
    return (tmp_path / manifest.path).parent


def _write_parts_item(coll_dir: Path, name: str, **fields: object) -> None:
    lines = ["---", "type: record", f"collection_id: {fields.pop('collection_id')}"]
    for key, value in fields.items():
        if isinstance(value, list):
            rendered = json.dumps(value)
            lines.append(f"{key}: {rendered}")
        elif isinstance(value, str) and value.startswith("[["):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---\n")
    (coll_dir / "Items" / name).write_text("\n".join(lines), encoding="utf-8")


def test_dotted_array_of_link_column_matches_between_eager_and_late_projection(
    tmp_path: Path,
) -> None:
    """B2 (base-identical): a dotted column into an array-of-link field
    (`parts.0`) must withhold an unresolvable bare-title element. An
    explicit `columns` restriction now refuses late projection outright
    (`_late_link_projection_safe` requires `columns` to be empty -- see
    NEW-1/NEW-2 in review round 3), so `late_link_projection=True` here is a
    no-op that falls back to the identical eager path: this proves that
    fallback, not a genuine late-projected dotted-column read (which no
    caller shape uses -- see the array-of-string sort test below and the
    large-row byte-cap test for what NEW-1/NEW-3 actually needed fixed).
    """
    manifest = _parts_collection(tmp_path)
    _write_parts_item(
        _coll_dir(tmp_path, manifest),
        "a.md",
        collection_id=manifest.collection_id,
        record_id="11111111-1111-4111-8111-111111111111",
        schema_version=1,
        observed_on="2026-07-20",
        status="active",
        parts=["[[Missing Part A]]"],
    )

    eager = record_governance.query_collection(
        tmp_path, manifest, columns=["status", "parts.0"], sort_by="observed_on", limit=1
    )
    late = record_governance.query_collection(
        tmp_path,
        manifest,
        columns=["status", "parts.0"],
        sort_by="observed_on",
        limit=1,
        late_link_projection=True,
    )

    assert eager.rows == late.rows
    assert eager.rows[0]["parts.0"] is None
    assert eager.rows[0]["status"] == "active"


def test_empty_array_field_is_identical_with_or_without_an_unrelated_link_column(
    tmp_path: Path,
) -> None:
    """M3 (base-identical): link governance pops an *empty* array field
    (`spec.type == "array" and not value`) for every array field, link
    items or not -- `services`/`parts` must come back the same (absent /
    None) whether or not an unrelated link column is also requested. Both
    queries here pass a non-empty `columns`, so `_late_link_projection_safe`
    always refuses (see NEW-1/NEW-2 in review round 3) and
    `late_link_projection=True` is a no-op over the identical eager path --
    this loop proves that no-op holds, not a genuine late-projected read.
    """
    manifest = _parts_collection(tmp_path)
    _write_parts_item(
        _coll_dir(tmp_path, manifest),
        "b.md",
        collection_id=manifest.collection_id,
        record_id="22222222-2222-4222-8222-222222222222",
        schema_version=1,
        observed_on="2026-07-21",
        status="active",
        parts=[],
    )

    for late in (False, True):
        only_status = record_governance.query_collection(
            tmp_path,
            manifest,
            columns=["parts"],
            sort_by="observed_on",
            limit=1,
            late_link_projection=late,
        )
        with_status = record_governance.query_collection(
            tmp_path,
            manifest,
            columns=["parts", "status"],
            sort_by="observed_on",
            limit=1,
            late_link_projection=late,
        )
        assert only_status.rows[0]["parts"] == with_status.rows[0]["parts"] is None, late


def test_late_projection_selects_the_same_row_as_eager_when_sort_field_is_sometimes_absent(
    tmp_path: Path,
) -> None:
    """Selection parity: `next_due_on` is optional, scalar and link-free, and
    `columns` is left empty (the only shape `_late_link_projection_safe`
    admits -- see NEW-1/NEW-2 in review round 3). With some records leaving
    `next_due_on` unset, the rows `late_link_projection=True` selects (sort,
    then limit, on raw values) must equal the rows the eager path selects
    (sort, then limit, on governed values) -- proving selection does not
    depend on projection for a scalar, non-array, non-link sort field, and
    proving this query shape genuinely takes the late path (an empty
    `columns` is required, not merely tolerated).
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    (fixture / "Events" / "2026-07-10-no-due-date.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: 55555555-5555-4555-8555-555555555555\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-10\n"
        'asset: "[[Assets/Vehicle]]"\n'
        "status: completed\n"
        "---\n",
        encoding="utf-8",
    )
    (fixture / "Events" / "2026-07-11-with-due-date.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: 66666666-6666-4666-8666-666666666666\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-11\n"
        'asset: "[[Assets/Vehicle]]"\n'
        "status: completed\n"
        "next_due_on: 2026-09-01\n"
        "---\n",
        encoding="utf-8",
    )

    eager = record_governance.query_collection(
        tmp_path,
        manifest,
        sort_by="next_due_on",
        descending=True,
        limit=5,
    )
    late = record_governance.query_collection(
        tmp_path,
        manifest,
        sort_by="next_due_on",
        descending=True,
        limit=5,
        late_link_projection=True,
    )

    eager_ids = [row["record_id"] for row in eager.rows]
    late_ids = [row["record_id"] for row in late.rows]
    assert late_ids == eager_ids
    assert "55555555-5555-4555-8555-555555555555" in eager_ids
    assert "66666666-6666-4666-8666-666666666666" in eager_ids


def test_current_state_shape_still_takes_the_late_projection_path(tmp_path: Path) -> None:
    """Review round 3: `_late_link_projection_safe` was narrowed (refuses a
    non-empty `columns` or an array-typed sort/date root) to close NEW-1 and
    NEW-3. `working_set_state._from_records` (exercised end-to-end by R1,
    R2 and the M4 tests) asks for `columns=None` and sorts by a plain
    scalar `date` field, so it must still clear this check -- asserted
    directly here (white-box), not just inferred from R1's "no vault walk"
    and R2's "exactly one projector call" side effects, so a future
    narrowing of this precondition cannot silently fall the current-state
    lookup back to the eager path without a visible failure right here.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    assert record_formats._late_link_projection_safe(
        manifest,
        filters=None,
        columns=None,
        sort_by="occurred_on",
        date_column=None,
        date_from=None,
        date_to=None,
        aggregate=None,
        expand_children=False,
        expand_child=None,
    )


def test_explicit_columns_refuses_late_projection_and_matches_eager_byte_cap(
    tmp_path: Path,
) -> None:
    """NEW-1 (resolved by construction, not by column-projected byte
    capping): the late path's inner window step runs with `columns=None`,
    so its 64 KiB response-byte cap always sees raw *all-field* rows, never
    the small column-projected rows eager caps. `_late_link_projection_safe`
    now refuses whenever `columns` is non-empty, so requesting a narrow
    `columns=["status"]` over 60 records that each carry a large unrelated
    field must fall back to the identical eager path -- same rows, same
    `truncated` flag, same continuation presence -- rather than the
    late path's byte cap triggering early on the large raw rows.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    large_value = "x" * 1800
    start = datetime.date(2026, 1, 1)
    for index in range(60):
        occurred_on = (start + datetime.timedelta(days=index)).isoformat()
        (fixture / "Events" / f"{occurred_on}-bulk.md").write_text(
            "---\n"
            "type: record\n"
            f"collection_id: {manifest.collection_id}\n"
            f"record_id: bbbbbbbb-bbbb-4bbb-8bbb-{index:012d}\n"
            "schema_version: 1\n"
            f"occurred_on: {occurred_on}\n"
            'asset: "[[Assets/Vehicle]]"\n'
            "status: completed\n"
            f"provider: {large_value}\n"
            "---\n",
            encoding="utf-8",
        )

    eager = record_governance.query_collection(
        tmp_path, manifest, columns=["status"], sort_by="occurred_on", limit=100
    )
    late = record_governance.query_collection(
        tmp_path,
        manifest,
        columns=["status"],
        sort_by="occurred_on",
        limit=100,
        late_link_projection=True,
    )

    assert late.rows == eager.rows
    assert len(eager.rows) >= 60, "the 60 large-row records must all be present, unbounded by size"
    assert eager.truncated is False
    assert late.truncated is False
    assert (late.continuation is None) == (eager.continuation is None) is True


def test_sort_by_array_field_refuses_late_projection_and_matches_eager_order(
    tmp_path: Path,
) -> None:
    """NEW-3 (resolved by construction): `_LinkProjector.__call__` pops any
    array field (link items or not) whose governed value is empty, so an
    empty raw `[]` and its governed, dropped-key `None` can sort
    differently. `_late_link_projection_safe` now also refuses when the
    sort/date root field is array-typed, so sorting by `services` (array of
    plain strings -- non-link, but still unsafe) must fall back to the
    identical eager path and return eager-identical row order.
    """
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    (fixture / "Events" / "2026-07-01-empty-services.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: cccccccc-cccc-4ccc-8ccc-cccccccccccc\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-01\n"
        'asset: "[[Assets/Vehicle]]"\n'
        "services: []\n"
        "status: completed\n"
        "---\n",
        encoding="utf-8",
    )
    (fixture / "Events" / "2026-07-02-with-services.md").write_text(
        "---\n"
        "type: record\n"
        f"collection_id: {manifest.collection_id}\n"
        "record_id: dddddddd-dddd-4ddd-8ddd-dddddddddddd\n"
        "schema_version: 1\n"
        "occurred_on: 2026-07-02\n"
        'asset: "[[Assets/Vehicle]]"\n'
        "services: [oil change]\n"
        "status: completed\n"
        "---\n",
        encoding="utf-8",
    )

    eager = record_governance.query_collection(
        tmp_path, manifest, sort_by="services", descending=True, limit=5
    )
    late = record_governance.query_collection(
        tmp_path,
        manifest,
        sort_by="services",
        descending=True,
        limit=5,
        late_link_projection=True,
    )

    eager_ids = [row["record_id"] for row in eager.rows]
    late_ids = [row["record_id"] for row in late.rows]
    assert late_ids == eager_ids
    assert "cccccccc-cccc-4ccc-8ccc-cccccccccccc" in eager_ids
    assert "dddddddd-dddd-4ddd-8ddd-dddddddddddd" in eager_ids


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
    tmp_path: Path, explicit_key: bool
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    activity = tmp_path / "Knowledge Base" / "log.md"
    activity.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
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
    # An omitted item key is derived from the declared natural key, so the future
    # path is known to the test the same way the writer knows it.
    key = (
        "ed6854be-c236-4cf6-9c90-18dfb8ac2544"
        if explicit_key
        else collections.derived_item_key(manifest, item)
    )
    target = f"Records/vehicle-maintenance/Events/{key}.md"
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths=target)
    before = ((fixture / "_collection.md").read_bytes(), activity.read_bytes())

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as raised:
            records.append_record(
                tmp_path,
                manifest,
                item=item,
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


# --- the authored join on a Planning reference (design D5) ----------------------


_JOIN_FIXTURE = """
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query: {limit: 12}
      join:
        asset: title
        provider: area
---

One ordinary"""


def _with_manifest_tail(fixture: Path, tail: str) -> Path:
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("\n---\n\nOne ordinary", tail),
        encoding="utf-8",
    )
    return manifest_path


def test_a_plan_link_join_round_trips_through_inspect_without_resolving_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join is an authored declaration, not a lookup.

    Records must be able to say which of its fields correspond to which plan
    fields without ever reading Planning; the attention surface is the only
    consumer, and it resolves the reference on its own time.
    """
    from exomem import planning

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = _with_manifest_tail(fixture, _JOIN_FIXTURE)
    manifest = collections.load_manifest(tmp_path, manifest_path)

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Records must not resolve the Planning side of a join")

    monkeypatch.setattr(memory_refs, "resolve_identifier_read_only", unexpected)
    monkeypatch.setattr(planning, "inspect", unexpected)
    monkeypatch.setattr(planning, "query", unexpected)

    assert manifest.links.plans[0].join == {"asset": "title", "provider": "area"}

    inspection = record_governance.inspect_collection(tmp_path, manifest)
    projected = record_governance.project_manifest(tmp_path, manifest)

    assert inspection["contract"]["plans"] == [
        {
            "reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7",
            "query": {"limit": 12},
            "join": {"asset": "title", "provider": "area"},
        }
    ]
    assert projected["plans"] == inspection["contract"]["plans"]


def test_a_plan_link_without_a_join_projects_exactly_as_before(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = _with_manifest_tail(
        fixture,
        """
links:
  plans:
    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7
      query: {limit: 12}
---

One ordinary""",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)

    assert manifest.links.plans[0].join is None
    assert record_governance.project_manifest(tmp_path, manifest)["plans"] == [
        {"reference": "exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7", "query": {"limit": 12}}
    ]


@pytest.mark.parametrize(
    "join, offending",
    [
        ("        nonexistent: title\n", "nonexistent"),
        ("        asset: title\n        provider: area\n        odometer: a\n"
         "        status: b\n        currency: c\n", None),
        ('        asset: ""\n', "asset"),
        ("        asset: 7\n", "asset"),
    ],
    ids=["undeclared-record-field", "five-pairs", "empty-plan-name", "non-text-plan-name"],
)
def test_a_malformed_join_refuses_before_acceptance(
    tmp_path: Path, join: str, offending: str | None
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest_path = _with_manifest_tail(
        fixture,
        "\nlinks:\n  plans:\n"
        "    - reference: exomem://memory/99f6fa8b-5d6e-43f8-8cdf-e30767e8f4d7\n"
        "      query: {limit: 12}\n"
        "      join:\n" + join + "---\n\nOne ordinary",
    )

    with pytest.raises(collections.CollectionError) as raised:
        collections.load_manifest(tmp_path, manifest_path)

    assert raised.value.code == "INVALID_COLLECTION_LINKS"
    if offending is not None:
        assert offending in str(raised.value.details)


def test_describe_documents_the_plan_link_join() -> None:
    """An authored shape nobody can discover is an undocumented private field."""
    contract = collections.manifest_authoring_contract()

    join = contract["plan_links"]["join"]
    assert join["maximum_pairs"] == 4
    assert "declared" in join["record_side"]
    assert "not resolve" in contract["plan_links"]["resolution"]


# --- Observed free-string vocabulary on inspection ---------------------------
#
# A manifest declaring `provider: {type: string}` gives an appending agent no
# way to discover the vocabulary the collection already uses. Declared `enum`
# fields already carry their own vocabulary, so only free strings need this.


def _write_event(fixture: Path, *, record_id: str, occurred_on: str, provider: str) -> Path:
    path = fixture / "Events" / "released" / f"{occurred_on}-{record_id[:8]}.md"
    path.write_text(
        "---\n"
        "type: record\n"
        "collection_id: 49622075-9ff4-4660-9ab7-414854b5bca2\n"
        f"record_id: {record_id}\n"
        "schema_version: 1\n"
        f"occurred_on: {occurred_on}\n"
        'asset: "[[Assets/Vehicle]]"\n'
        f'provider: "{provider}"\n'
        "currency: GBP\n"
        "status: completed\n"
        "---\n\nObserved event.\n",
        encoding="utf-8",
    )
    return path


def _event_id(index: int) -> str:
    return f"7c9a{index:04d}-0000-4000-8000-000000000000"


def test_inspection_surfaces_observed_free_string_vocabulary_with_counts(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _write_event(fixture, record_id=_event_id(1), occurred_on="2026-06-03", provider="Harbour Auto")
    _write_event(
        fixture, record_id=_event_id(2), occurred_on="2026-06-04", provider="Eastside Motors"
    )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["observed_values"]["provider"] == {
        "values": [
            {"value": "Northside Garage", "count": 2, "value_truncated": False},
            {"value": "Eastside Motors", "count": 1, "value_truncated": False},
            {"value": "Harbour Auto", "count": 1, "value_truncated": False},
            {"value": "Inspection Centre", "count": 1, "value_truncated": False},
        ],
        "truncated": False,
    }


def test_observed_vocabulary_caps_distinct_values_and_flags_truncation(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    for index in range(25):
        _write_event(
            fixture,
            record_id=_event_id(100 + index),
            occurred_on="2026-07-01",
            provider=f"Provider {index:02d}",
        )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    summary = inspection["observed_values"]["provider"]
    assert len(summary["values"]) == 20
    assert summary["truncated"] is True
    assert all(entry["count"] >= 1 for entry in summary["values"])
    assert all(set(entry) == {"value", "count", "value_truncated"} for entry in summary["values"])


def test_enum_declared_field_is_not_summarized_as_observed_vocabulary(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    observed = inspection["observed_values"]
    assert "provider" in observed
    assert "currency" not in observed
    assert "status" not in observed


def test_observed_vocabulary_omits_values_seen_only_on_withheld_items(tmp_path: Path) -> None:
    """Observed values are item-derived, so they need the same serve-time filter."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths="Records/vehicle-maintenance/Events/withheld/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspection = record_governance.inspect_collection(tmp_path, manifest)

    values = [entry["value"] for entry in inspection["observed_values"]["provider"]["values"]]
    assert "Northside Garage" in values
    assert "Inspection Centre" not in values
    assert "Inspection Centre" not in json.dumps(inspection)


def test_observed_vocabulary_keeps_the_most_frequent_values_when_the_cap_binds(
    tmp_path: Path,
) -> None:
    """The cap must drop the rarest terms, not the ones the walk happened to meet last."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    for index in range(25):
        for repeat in range(3 if index >= 20 else 1):
            _write_event(
                fixture,
                record_id=_event_id(200 + index * 4 + repeat),
                occurred_on="2026-08-01",
                provider=f"Provider {index:02d}",
            )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    summary = inspection["observed_values"]["provider"]
    values = [entry["value"] for entry in summary["values"]]
    assert values[:6] == [
        "Provider 20",
        "Provider 21",
        "Provider 22",
        "Provider 23",
        "Provider 24",
        "Northside Garage",
    ]
    assert "Provider 19" not in values
    assert summary["truncated"] is True


def test_long_observed_values_stay_distinct_and_are_flagged_truncated(tmp_path: Path) -> None:
    """Two terms sharing a long prefix are two terms, not one term counted twice."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    # Both values share the full 120-char display window, so a counter keyed on the
    # CUT value would merge them into one entry with count 2.
    prefix = "A" * 120
    _write_event(
        fixture, record_id=_event_id(301), occurred_on="2026-09-01", provider=f"{prefix}one"
    )
    _write_event(
        fixture, record_id=_event_id(302), occurred_on="2026-09-02", provider=f"{prefix}two"
    )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    summary = inspection["observed_values"]["provider"]
    long_entries = [entry for entry in summary["values"] if entry["value"].startswith("A")]
    assert len(long_entries) == 2
    assert [entry["count"] for entry in long_entries] == [1, 1]
    assert all(entry["value_truncated"] is True for entry in long_entries)
    assert all(len(entry["value"]) == 120 for entry in long_entries)
    short = next(entry for entry in summary["values"] if entry["value"] == "Northside Garage")
    assert short["value_truncated"] is False


def test_observed_vocabulary_counts_exclude_withheld_items(tmp_path: Path) -> None:
    """The count is item-derived too, so a withheld item must not raise it."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    hidden = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    hidden.write_bytes(
        hidden.read_bytes().replace(b"provider: Inspection Centre", b"provider: Northside Garage")
    )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    _write_l0_rule(tmp_path, name="blocked", paths="Records/vehicle-maintenance/Events/withheld/**")

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["observed_values"]["provider"] == {
        "values": [{"value": "Northside Garage", "count": 2, "value_truncated": False}],
        "truncated": False,
    }


def test_unreadable_collection_reports_no_observed_vocabulary(tmp_path: Path) -> None:
    """An absent summary says no item pass ran; an empty one would claim a clean sweep."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    broken = fixture / "Events" / "released" / "2026-06-05-broken.md"
    broken.write_text(
        "---\n"
        "type: record\n"
        "collection_id: 49622075-9ff4-4660-9ab7-414854b5bca2\n"
        "record_id: 7c9a0999-0000-4000-8000-000000000000\n"
        "schema_version: 2\n"
        "occurred_on: 2026-06-05\n"
        'asset: "[[Assets/Vehicle]]"\n'
        "---\n\nFuture schema version.\n",
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    inspection = record_governance.inspect_collection(tmp_path, manifest)

    assert inspection["diagnostics"] == [
        {"code": "UNSUPPORTED_ITEM_SCHEMA_VERSION", "reason": "item schema version differs"}
    ]
    assert "observed_values" not in inspection


def test_record_inspection_egress_refuses_observed_vocabulary_on_a_legacy_tracker() -> None:
    """A manifest-less tracker parses no items, so it can carry no observed vocabulary."""
    payload = {
        "kind": "legacy_tracker",
        "report_only": True,
        "contract": None,
        "legacy": {
            "collection_id": "legacy-49622075-9ff4-4660-9ab7-414854b5bca2",
            "path": "Knowledge Base/Records/legacy-tracker.md",
            "inspect_only": True,
        },
        "snapshot": None,
        "source_versions": [],
        "diagnostics": [],
        "audit": {"status": "not_applicable", "gaps": []},
        "saved_views": [],
    }

    smuggled = egress.project(
        record_governance._RecordEnvelope(
            {
                **payload,
                "observed_values": {
                    "provider": {
                        "values": [
                            {"value": "Northside Garage", "count": 1, "value_truncated": False}
                        ],
                        "truncated": False,
                    }
                },
            }
        ),
        egress.LEVEL_FULL,
        kind="record_inspection",
    )

    assert smuggled == {"withheld": True, "reason": "invalid_projector_payload"}


# --------------------------------------------------------------------------
# Coverage: committed items and held candidates
# --------------------------------------------------------------------------


def _ledger_with_one_item(tmp_path: Path) -> collections.CollectionManifest:
    manifest = collections.load_manifest(tmp_path, setup_ledger_collection(tmp_path))
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(),
        expected_container_hash=snapshot.snapshot,
        why="record a published entry",
    )
    return collections.load_manifest(tmp_path, tmp_path / manifest.path)


def _hold_one(tmp_path: Path, manifest: collections.CollectionManifest) -> str:
    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(slug="held-entry", unlisted_channel="digest"),
            why="record a published entry",
        )
    return caught.value.details["held"]["held_id"]


def test_inspect_reports_coverage_for_committed_and_held(tmp_path: Path) -> None:
    manifest = _ledger_with_one_item(tmp_path)
    held_id = _hold_one(tmp_path, manifest)

    inspection = record_governance.inspect_collection(tmp_path, manifest.path)

    coverage = inspection["coverage"]
    assert coverage["committed"] == 1
    assert coverage["held"] == 1
    assert [reference["held_id"] for reference in coverage["held_refs"]] == [held_id]
    reference = coverage["held_refs"][0]
    assert reference["attempted_action"] == "append"
    assert reference["held_at"].startswith("20")
    # A caller-supplied key may itself be a value in the wrong position, so the
    # summary counts the undeclared fields rather than echoing their names.
    assert reference["diagnostics"] == "SCHEMA_UNKNOWN_FIELD: 1 undeclared fields"
    assert coverage["unreadable"] == 0
    # A reference is a pointer, never the observation it stands for.
    assert "quarterly" not in json.dumps(coverage)
    assert "held-entry" not in json.dumps(coverage)
    assert "unlisted_channel" not in json.dumps(coverage)


def test_inspect_bounds_held_references(tmp_path: Path) -> None:
    manifest = _ledger_with_one_item(tmp_path)
    for index in range(21):
        records.hold_candidate(
            tmp_path,
            manifest,
            attempted_action="append",
            candidate={"action": "append", "item": {"slug": f"entry-{index}"}, "body": ""},
            why="record a published entry",
            issues=[{"field": "slug", "code": "SCHEMA_FIELD_TYPE", "reason": "x", "received": "str"}],
            held_id=f"11111111-1111-4111-8111-{index:012d}",
        )

    coverage = record_governance.inspect_collection(tmp_path, manifest.path)["coverage"]

    assert coverage["held"] == 21
    assert len(coverage["held_refs"]) == 20


def _clone_held_files(
    tmp_path: Path, manifest: collections.CollectionManifest, count: int
) -> Path:
    """Fill the held directory with `count` well-formed files, cheaply."""
    seed = "11111111-1111-4111-8111-000000000000"
    records.hold_candidate(
        tmp_path,
        manifest,
        attempted_action="append",
        candidate={"action": "append", "item": {"slug": "entry"}, "body": ""},
        why="record a published entry",
        issues=[
            {"field": "slug", "code": "SCHEMA_FIELD_TYPE", "reason": "x", "received": "str"}
        ],
        held_id=seed,
    )
    directory = tmp_path / "Knowledge Base/Records/Publications/Held"
    template = (directory / f"{seed}.md").read_text(encoding="utf-8")
    for index in range(1, count):
        reference = f"11111111-1111-4111-8111-{index:012d}"
        (directory / f"{reference}.md").write_text(
            template.replace(seed, reference), encoding="utf-8"
        )
    return directory


def test_coverage_counts_every_held_file_beyond_the_listing_bound(tmp_path: Path) -> None:
    """A capped count is a wrong count: the bound belongs to references only."""
    manifest = _ledger_with_one_item(tmp_path)
    directory = _clone_held_files(tmp_path, manifest, 505)
    assert len(list(directory.glob("*.md"))) == 505

    coverage = record_governance.inspect_collection(tmp_path, manifest.path)["coverage"]

    assert coverage["held"] == 505
    assert coverage["unreadable"] == 0
    assert len(coverage["held_refs"]) == 20


def test_a_held_file_that_cannot_be_loaded_is_reported_not_hidden(tmp_path: Path) -> None:
    manifest = _ledger_with_one_item(tmp_path)
    readable = _hold_one(tmp_path, manifest)
    directory = tmp_path / "Knowledge Base/Records/Publications/Held"
    (directory / "11111111-1111-4111-8111-000000000009.md").write_text(
        "this is not a held record\n", encoding="utf-8"
    )

    coverage = record_governance.inspect_collection(tmp_path, manifest.path)["coverage"]

    assert coverage["held"] == 2
    assert coverage["unreadable"] == 1
    assert [reference["held_id"] for reference in coverage["held_refs"]] == [readable]


def test_a_count_only_inventory_never_reads_a_held_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory answers "how many", so it has no reason to open a candidate."""
    manifest = _ledger_with_one_item(tmp_path)
    _hold_one(tmp_path, manifest)
    opened: list[str] = []
    real = records._load_held_candidate

    def spy(root, target, held_id):
        opened.append(held_id)
        return real(root, target, held_id)

    monkeypatch.setattr(records, "_load_held_candidate", spy)

    inventory = record_governance.inventory_collections(tmp_path)

    row = next(
        entry for entry in inventory["collections"] if entry["manifest_path"] == manifest.path
    )
    assert row["held"] == 1
    assert opened == []


def test_the_inventory_docstring_states_the_bounded_census() -> None:
    """The docstring claimed it opened nothing while it read every collection."""
    docstring = record_governance.inventory_collections.__doc__ or ""

    assert "without opening canonical item data" not in docstring
    assert "census" in docstring


def test_inventory_reports_committed_and_held_per_collection(tmp_path: Path) -> None:
    manifest = _ledger_with_one_item(tmp_path)
    _hold_one(tmp_path, manifest)

    inventory = record_governance.inventory_collections(tmp_path)

    row = next(
        entry
        for entry in inventory["collections"]
        if entry["manifest_path"] == manifest.path
    )
    assert row["committed"] == 1
    assert row["held"] == 1


def _withhold_the_held_directory(tmp_path: Path) -> None:
    _write_l6_rule(tmp_path, ceiling=6, paths="Records/**")
    governance = tmp_path / "Knowledge Base" / "_Governance"
    (governance / "scopes" / "held.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZZ\n"
        "name: Held\n"
        'paths: ["Records/Publications/Held/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "held.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FZY\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FZZ"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )


def test_a_withheld_held_candidate_contributes_neither_reference_nor_count(
    tmp_path: Path,
) -> None:
    manifest = _ledger_with_one_item(tmp_path)
    _hold_one(tmp_path, manifest)
    _withhold_the_held_directory(tmp_path)

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        inspection = record_governance.inspect_collection(tmp_path, manifest.path)
        inventory = record_governance.inventory_collections(tmp_path)

    assert inspection["coverage"]["held"] == 0
    assert inspection["coverage"]["held_refs"] == []
    row = next(
        entry
        for entry in inventory["collections"]
        if entry["manifest_path"] == manifest.path
    )
    assert row["held"] == 0


def test_a_hold_into_a_withheld_directory_soft_fails_to_the_original_refusal(
    tmp_path: Path,
) -> None:
    """Holding writes into the vault, so it obeys the same release the items do."""
    manifest = _ledger_with_one_item(tmp_path)
    _withhold_the_held_directory(tmp_path)
    held_directory = tmp_path / "Knowledge Base/Records/Publications/Held"

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(collections.CollectionError) as caught:
            records.append_record(
                tmp_path,
                manifest.path,
                item=ledger_item(slug="held-entry", unlisted_channel="digest"),
                why="record a published entry",
            )

    assert caught.value.code == "SCHEMA_UNKNOWN_FIELD"
    assert caught.value.details["issues"]
    assert "held" not in caught.value.details
    assert caught.value.details["warnings"]
    assert not held_directory.exists()
