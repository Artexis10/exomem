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
from record_presentation_fixtures import manifest_text

from exomem import memory_refs, record_formats, record_governance, records, vault
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
        lambda root, path, *, receipt: path != hidden_rel and authorize(root, path, receipt=receipt),
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
        lambda root, path, *, receipt: path != template_rel and authorize(root, path, receipt=receipt),
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

    def watched(root: Path, relative: str, *, receipt: bool = False) -> bool:
        calls.append((relative, receipt))
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

    def watched(root: Path, relative: str, *, receipt: bool = False) -> bool:
        calls.append((relative, receipt))
        return original(root, relative, receipt=receipt)

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
