from __future__ import annotations

from pathlib import Path

import pytest
from record_fixtures import copy_x3_fixture
from record_presentation_fixtures import ITEM_KEY, manifest_text, setup_collection, values

from exomem import query_data, record_formats, records, writer_lease
from exomem import structured_collections as collections
from exomem.governance import scrubber


@pytest.fixture(autouse=True)
def _writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    writer_lease.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
    yield
    writer_lease.reset_managers_for_tests()


def _append(vault: Path, manifest: collections.CollectionManifest, *, count: int = 3) -> None:
    records.append_record(
        vault,
        manifest,
        item=values(child_count=count),
        item_key=ITEM_KEY,
        why="seed safe child projection",
    )


def test_zero_expansion_boolean_expansion_and_explicit_selection_are_compatible(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest)

    unexpanded = record_formats.query_collection(tmp_path, manifest, limit=20)
    boolean = record_formats.query_collection(tmp_path, manifest, expand_children=True, limit=20)
    explicit = record_formats.query_collection(tmp_path, manifest, expand_child="measurements", limit=20)

    assert len(unexpanded.rows) == 1
    assert "private" not in json_dump(unexpanded.rows)
    assert boolean.rows == explicit.rows
    assert [row["child_index"] for row in explicit.rows] == [0, 1, 2]
    assert all("measurements" not in row for row in explicit.rows)


def test_missing_open_conflicting_and_ambiguous_selectors_refuse_actionably(tmp_path: Path) -> None:
    without = setup_collection(tmp_path / "without", presentation=False)
    with pytest.raises(collections.CollectionError, match="unambiguous child"):
        record_formats.query_collection(tmp_path / "without", without, expand_children=True)
    with pytest.raises(collections.CollectionError, match="not a declared"):
        record_formats.query_collection(tmp_path / "without", without, expand_child="measurements")

    manifest = setup_collection(tmp_path / "two", two_tables=True)
    with pytest.raises(collections.CollectionError) as error:
        record_formats.query_collection(tmp_path / "two", manifest, expand_children=True)
    assert error.value.details == {"selectors": ["measurements", "qualifiers"]}
    with pytest.raises(collections.CollectionError, match="not both"):
        record_formats.query_collection(
            tmp_path / "two", manifest, expand_children=True, expand_child="measurements"
        )


@pytest.mark.parametrize(
    "operation",
    ["filter", "sort", "aggregate", "columns", "markdown", "page"],
)
def test_undeclared_and_withheld_child_values_never_enter_any_query_operation(
    tmp_path: Path, operation: str
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest, count=4)
    kwargs: dict[str, object] = {"expand_child": "measurements", "limit": 20}
    if operation == "filter":
        kwargs["filters"] = [{"column": "private", "op": "eq", "value": "not projected 0"}]
    elif operation == "sort":
        kwargs["sort_by"] = "private"
    elif operation == "aggregate":
        kwargs["aggregate"] = "count:private"
    elif operation == "columns":
        kwargs["columns"] = ["name", "private", "source"]
    elif operation == "markdown":
        kwargs["output_format"] = "markdown"
    elif operation == "page":
        kwargs["limit"] = 2

    if operation in {"filter", "sort", "aggregate", "columns"}:
        with pytest.raises((collections.CollectionError, query_data.QueryDataError), match="QUERY_FIELD"):
            record_formats.query_collection(
                tmp_path,
                manifest,
                project_child_value=lambda value, column: None if column.type == "link" else value,
                **kwargs,
            )
        return

    result = record_formats.query_collection(
        tmp_path,
        manifest,
        project_child_value=lambda value, column: None if column.type == "link" else value,
        **kwargs,
    )

    serialized = json_dump(result.rows) + result.rendered
    assert "not projected" not in serialized
    assert "[[Sources/Observed]]" not in serialized
    if operation == "page":
        assert result.continuation is not None
        second = record_formats.query_collection(
            tmp_path,
            manifest,
            expand_child="measurements",
            continuation=result.continuation,
            limit=2,
            project_child_value=lambda value, column: None if column.type == "link" else value,
        )
        assert [row["child_index"] for row in result.rows + second.rows] == [0, 1, 2, 3]


def test_child_metadata_and_parent_collisions_refuse_without_partial_rows(tmp_path: Path) -> None:
    root = tmp_path
    activity = root / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = root / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    from record_presentation_fixtures import manifest_text

    text = manifest_text().replace("- field: name\n          label: Name", "- field: subject\n          label: Name", 1)
    path.write_text(text, encoding="utf-8")
    (path.parent / "Items").mkdir()
    manifest = collections.load_manifest(root, path)
    item = values(child_count=1)
    item["measurements"][0]["subject"] = "collision"  # type: ignore[index]
    records.append_record(
        tmp_path, manifest, item=item, item_key=ITEM_KEY, why="seed collision refusal"
    )

    with pytest.raises(collections.CollectionError, match="collide"):
        record_formats.query_collection(tmp_path, manifest, expand_child="measurements")


def test_total_child_cap_precedes_materialization_and_response_cap_is_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest, count=3)
    calls: list[object] = []
    monkeypatch.setattr(record_formats, "_MAX_CHILD_ROWS", 2)
    with pytest.raises(collections.CollectionError, match="RECORD_CHILD_LIMIT"):
        record_formats.query_collection(
            tmp_path,
            manifest,
            expand_child="measurements",
            project_child_value=lambda value, _column: calls.append(value) or value,
        )
    assert calls == []

    monkeypatch.setattr(record_formats, "_MAX_CHILD_ROWS", 10)
    monkeypatch.setattr(query_data, "MAX_RESPONSE_BYTES", 1, raising=False)
    with pytest.raises(collections.CollectionError, match="RECORD_RESPONSE_TOO_LARGE"):
        record_formats.query_collection(tmp_path, manifest, expand_child="measurements")


def test_saved_view_explicit_child_selection_keeps_cursor_identity(tmp_path: Path) -> None:
    activity = tmp_path / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    from record_presentation_fixtures import manifest_text

    text = manifest_text().replace(
        "record_presentation:\n",
        "views:\n  child-pages:\n    query:\n      expand_child: measurements\n      limit: 2\nrecord_presentation:\n",
        1,
    )
    path.write_text(text, encoding="utf-8")
    (path.parent / "Items").mkdir()
    manifest = collections.load_manifest(tmp_path, path)
    _append(tmp_path, manifest, count=4)
    manifest = collections.load_manifest(tmp_path, path)

    first = record_formats.query_collection(tmp_path, manifest, view="child-pages")
    assert first.continuation is not None
    second = record_formats.query_collection(
        tmp_path, collections.load_manifest(tmp_path, path), view="child-pages", continuation=first.continuation
    )
    assert [row["child_index"] for row in first.rows + second.rows] == [0, 1, 2, 3]
    with pytest.raises(collections.CollectionError, match="continuation does not match"):
        record_formats.query_collection(
            tmp_path,
            collections.load_manifest(tmp_path, path),
            expand_child="measurements",
            continuation=first.continuation,
            limit=2,
        )


def test_saved_view_boolean_child_selector_requires_one_eligible_container(tmp_path: Path) -> None:
    one = setup_collection(tmp_path / "one")
    one_path = tmp_path / "one" / one.path
    one_path.write_text(
        one_path.read_text(encoding="utf-8").replace(
            "record_presentation:\n",
            "views:\n  boolean-child:\n    query:\n      expand_children: true\nrecord_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    one = collections.load_manifest(tmp_path / "one", one_path)
    assert collections.resolve_saved_view(one, "boolean-child").definition["query"][
        "expand_children"
    ] is True

    two = setup_collection(tmp_path / "two", two_tables=True)
    two_path = tmp_path / "two" / two.path
    two_path.write_text(
        two_path.read_text(encoding="utf-8").replace(
            "record_presentation:\n",
            "views:\n  ambiguous-child:\n    query:\n      expand_children: true\nrecord_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    two = collections.load_manifest(tmp_path / "two", two_path)
    assert two.view_diagnostics == (
        collections.CollectionDiagnostic(
            "INVALID_SAVED_VIEW",
            "saved view expand_children is ambiguous",
            "views.ambiguous-child",
        ),
    )
    with pytest.raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        collections.resolve_saved_view(two, "ambiguous-child")


@pytest.mark.parametrize(
    "query_shape",
    [
        '      filters: [{column: source, op: eq, value: "[[Private/Target]]"}]\n',
        "      columns: [source]\n",
        "      sort_by: source\n",
        "      aggregate: distinct:source\n",
        '      date_column: source\n      date_from: "2026-01-01"\n',
    ],
)
def test_saved_view_fields_bind_only_to_the_explicit_selected_table(
    tmp_path: Path, query_shape: str
) -> None:
    activity = tmp_path / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        manifest_text(two_tables=True).replace(
            "record_presentation:\n",
            "views:\n"
            "  sibling-column:\n"
            "    query:\n"
            "      expand_child: qualifiers\n"
            + query_shape
            + "  selected-column:\n"
            "    query:\n"
            "      expand_child: measurements\n"
            + query_shape
            + "record_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    with pytest.raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        collections.resolve_saved_view(manifest, "sibling-column")
    assert collections.resolve_saved_view(manifest, "selected-column").definition["query"][
        "expand_child"
    ] == "measurements"


def test_saved_view_selected_shape_does_not_retype_a_sibling_column_from_parent(
    tmp_path: Path,
) -> None:
    activity = tmp_path / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    source = manifest_text(two_tables=True).replace(
        "    note:\n", "    source:\n      type: string\n    note:\n", 1
    )
    path.write_text(
        source.replace(
            "record_presentation:\n",
            "views:\n"
            "  wrong-parent-type:\n"
            "    query:\n"
            "      expand_child: qualifiers\n"
            '      filters: [{column: source, op: eq, value: "[[Private/Target]]"}]\n'
            "record_presentation:\n",
            1,
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    with pytest.raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        collections.resolve_saved_view(manifest, "wrong-parent-type")


@pytest.mark.parametrize(
    ("presentation", "selector"),
    [(True, "subject"), (False, "measurements")],
)
def test_saved_view_explicit_child_selector_requires_an_eligible_declared_container(
    tmp_path: Path, presentation: bool, selector: str
) -> None:
    activity = tmp_path / "Knowledge Base/log.md"
    activity.parent.mkdir(parents=True)
    activity.write_text("# Activity\n", encoding="utf-8")
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    from record_presentation_fixtures import manifest_text

    source = manifest_text(presentation=presentation)
    head, marker, tail = source.rpartition("---")
    assert marker
    path.write_text(
        head
        + f"views:\n  invalid-child:\n    query:\n      expand_child: {selector}\n"
        + marker
        + tail,
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    assert manifest.view_diagnostics == (
        collections.CollectionDiagnostic(
            "INVALID_SAVED_VIEW", "saved view expand_child is invalid", "views.invalid-child"
        ),
    )
    with pytest.raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        collections.resolve_saved_view(manifest, "invalid-child")


def test_markdown_log_saved_view_selector_must_equal_the_declared_container(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    path = fixture / "_collection.md"
    source = path.read_text(encoding="utf-8")
    head, marker, tail = source.rpartition("---")
    assert marker
    path.write_text(
        head
        + "views:\n"
        + "  valid-child:\n    query:\n      expand_child: movements\n"
        + "  invalid-child:\n    query:\n      expand_child: title\n"
        + marker
        + tail,
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    assert collections.resolve_saved_view(manifest, "valid-child").definition["query"][
        "expand_child"
    ] == "movements"
    with pytest.raises(collections.CollectionError, match="INVALID_SAVED_VIEW"):
        collections.resolve_saved_view(manifest, "invalid-child")


def test_child_page_continuation_survives_the_terminal_egress_scrubber(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest, count=4)

    first = record_formats.query_collection(
        tmp_path, manifest, expand_child="measurements", limit=2
    )
    cleaned, blocked = scrubber.scrub_value({"continuation": first.continuation})

    assert blocked is False
    assert cleaned == {"continuation": first.continuation}


def test_markdown_log_explicit_selector_matches_boolean_and_rejects_other_fields(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")

    explicit = record_formats.query_collection(tmp_path, manifest, expand_child="movements", limit=20)
    boolean = record_formats.query_collection(tmp_path, manifest, expand_children=True, limit=20)

    assert explicit.rows == boolean.rows
    assert explicit.rows and all(row["child_field"] == "movements" for row in explicit.rows)
    with pytest.raises(collections.CollectionError, match="not available"):
        record_formats.query_collection(tmp_path, manifest, expand_child="unknown")


def json_dump(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True)
