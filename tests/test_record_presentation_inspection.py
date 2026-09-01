from __future__ import annotations

from pathlib import Path

import pytest
from record_presentation_fixtures import ITEM_KEY, manifest_text, setup_collection, values

from exomem import record_formats, record_governance, records, vault, writer_lease
from exomem import structured_collections as collections


@pytest.fixture(autouse=True)
def _writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    writer_lease.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
    yield
    writer_lease.reset_managers_for_tests()


def _append(vault_root: Path, manifest: collections.CollectionManifest, *, key: str = ITEM_KEY):
    return records.append_record(
        vault_root,
        manifest,
        item=values(),
        item_key=key,
        why="record generic observations",
        body="Authored prose.\n",
    )


def _shared_manifest(
    *, presentation: bool = True, filename_fields: str = "observed_on, subject"
) -> str:
    source = manifest_text(presentation=False).replace(
        "natural_key: [observed_on]", "natural_key: [observed_on, subject]"
    )
    recipe = f"""item_filename:
  version: 1
  fields: [{filename_fields}]
"""
    if presentation:
        recipe += """item_presentation:
  version: 1
  title: subject
  summary: [observed_on]
  long_text: [note, provenance]
"""
    return source.removesuffix("---\n") + recipe + "---\n"


def _shared_collection(
    root: Path, *, presentation: bool = True, filename_fields: str = "observed_on, subject"
) -> collections.CollectionManifest:
    original = setup_collection(root, presentation=False)
    path = root / original.path
    path.write_text(
        _shared_manifest(presentation=presentation, filename_fields=filename_fields),
        encoding="utf-8",
    )
    return collections.load_manifest(root, path)


def test_inspection_reports_only_noncurrent_states_counts_remedies_and_guards(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    item_path = tmp_path / appended["affected_paths"][0]
    current = item_path.read_text(encoding="utf-8")
    item_path.write_text(
        current.replace("Below threshold", "tampered display", 1), encoding="utf-8"
    )

    inspection = record_governance.inspect_collection(tmp_path, manifest.path)

    assert inspection["presentation"]["counts"] == {
        "missing": 0,
        "stale": 1,
        "malformed": 0,
        "unrenderable": 0,
    }
    assert inspection["presentation"]["truncated"] is False
    finding = inspection["presentation"]["items"][0]
    assert finding["item_key"] == ITEM_KEY
    assert finding["state"] == "stale"
    assert finding["remedy"] == "rebaseline_then_refresh"
    assert finding["version"] == collections.source_version(item_path).hash
    assert set(inspection["lifecycle_guards"]) == {
        "expected_manifest_hash",
        "expected_container_hash",
    }


def test_inspection_distinguishes_missing_malformed_and_unrenderable_with_safe_location(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    item_path = tmp_path / appended["affected_paths"][0]
    text = item_path.read_text(encoding="utf-8")
    span = record_formats._presentation_span(text)
    assert span is not None

    item_path.write_text(text[: span[0]] + text[span[1] :], encoding="utf-8")
    missing = record_formats.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest.path)
    )
    assert missing.presentation[0]["state"] == "missing"

    item_path.write_text(text + "\n<!-- /exomem-record-presentation -->", encoding="utf-8")
    malformed = record_formats.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest.path)
    )
    assert malformed.presentation[0]["state"] == "malformed"

    item_path.write_text(
        text.replace("value: <5", "value:\n    unexpected: object", 1), encoding="utf-8"
    )
    unrenderable = record_governance.inspect_collection(tmp_path, manifest.path)
    finding = unrenderable["presentation"]["items"][0]
    assert finding["state"] == "unrenderable"
    assert finding["location"] == {
        "table": "measurements",
        "column": "value",
        "child_index": 0,
    }
    assert finding["remedy"] == "guarded_value_update"


def test_inspection_late_stale_item_is_not_crowded_out_and_reads_do_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = setup_collection(tmp_path)
    for index in range(4):
        item_values = values()
        item_values["observed_on"] = f"2026-08-{index + 10:02d}"
        key = f"{index + 1:08d}-1111-4111-8111-111111111111"
        records.append_record(
            tmp_path, manifest.path, item=item_values, item_key=key, why="seed inspection ordering"
        )
    loaded = collections.load_manifest(tmp_path, manifest.path)
    snapshot = record_formats.load_adapter(tmp_path, loaded).read()
    late = max(snapshot.records, key=lambda record: record.identity.key)
    late_path = tmp_path / late.source.path
    late_path.write_text(
        late_path.read_text(encoding="utf-8").replace("Observation 2", "tampered", 1),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (tmp_path / loaded.storage.source).glob("*.md")}
    monkeypatch.setattr(record_governance, "_PRESENTATION_FINDING_LIMIT", 1, raising=False)

    result = record_governance.inspect_collection(tmp_path, loaded.path)
    after = {path: path.read_bytes() for path in (tmp_path / loaded.storage.source).glob("*.md")}

    assert [item["item_key"] for item in result["presentation"]["items"]] == [late.identity.key]
    assert result["presentation"]["counts"]["stale"] == 1
    assert before == after


def test_inspection_authorizes_each_item_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = setup_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    withheld = appended["affected_paths"][0]
    opened: list[str] = []
    real_read = vault.read_bounded_guarded_bytes

    def observed(root, relative, **kwargs):  # noqa: ANN001, ANN202
        opened.append(relative)
        assert relative != withheld
        return real_read(root, relative, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", observed)
    inspection = record_formats.inspect_collection(
        tmp_path,
        collections.load_manifest(tmp_path, manifest.path),
        authorize_path=lambda path: path != withheld,
    )

    assert inspection.presentation == ()
    assert withheld not in opened


@pytest.mark.parametrize(
    ("damage", "expected_state"),
    [
        ("recipe", "stale_recipe"),
        ("item", "stale_item"),
        ("content", "authored_presentation"),
        ("missing", "missing"),
    ],
)
def test_shared_inspection_distinguishes_owned_presentation_drift(
    tmp_path: Path, damage: str, expected_state: str
) -> None:
    manifest = _shared_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    item_path = tmp_path / appended["affected_paths"][0]
    text = item_path.read_text(encoding="utf-8")
    span = record_formats._item_presentation_span(text)
    assert span is not None
    if damage == "recipe":
        marker = record_formats._ITEM_PRESENTATION_OPEN.search(text, span[0], span[1])
        assert marker is not None
        text = text[: marker.start(1)] + ("0" * 64) + text[marker.end(1) :]
    elif damage == "item":
        marker = record_formats._ITEM_PRESENTATION_OPEN.search(text, span[0], span[1])
        assert marker is not None
        text = text[: marker.start(2)] + ("0" * 64) + text[marker.end(2) :]
    elif damage == "content":
        text = text.replace("# Sample &lt;A&gt;", "# Authored over generated bytes", 1)
    else:
        text = text[: span[0]] + text[span[1] :]
    item_path.write_text(text, encoding="utf-8")

    inspected = record_formats.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest.path)
    )

    assert {finding["state"] for finding in inspected.presentation} == {expected_state}


def test_shared_inspection_reports_filename_drift_and_projected_collision(
    tmp_path: Path,
) -> None:
    manifest = _shared_collection(tmp_path, filename_fields="observed_on")
    first = _append(tmp_path, manifest)
    second_values = values()
    second_values["subject"] = "Sample B"
    second = records.append_record(
        tmp_path,
        manifest.path,
        item=second_values,
        item_key="22222222-2222-4222-8222-222222222222",
        expected_container_hash=first["after_container_hash"],
        why="record a second observation on the same date",
        body="Authored prose.\n",
    )
    first_path = tmp_path / first["affected_paths"][0]
    second_path = tmp_path / second["affected_paths"][0]
    first_path.rename(first_path.with_name("legacy-one.md"))
    second_path.rename(second_path.with_name("legacy-two.md"))

    inspected = record_formats.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest.path)
    )
    states = [finding["state"] for finding in inspected.presentation]

    assert states.count("filename_collision") == 2
    assert states.count("filename_drift") == 2


def test_shared_inspection_finds_an_orphan_marker_without_an_active_recipe(
    tmp_path: Path,
) -> None:
    manifest = _shared_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    manifest_path = tmp_path / manifest.path
    manifest_path.write_text(
        _shared_manifest(presentation=False),
        encoding="utf-8",
    )

    inspected = record_formats.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest.path)
    )

    assert inspected.presentation == (
        {
            "item_key": ITEM_KEY,
            "path": appended["affected_paths"][0],
            "version": collections.source_version(tmp_path / appended["affected_paths"][0]).hash,
            "state": "orphan_presentation",
            "remedy": "guarded_manifest_revision",
        },
    )


def test_governed_inspection_projects_shared_representation_findings(
    tmp_path: Path,
) -> None:
    manifest = _shared_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    item_path = tmp_path / appended["affected_paths"][0]
    item_path.write_text(
        item_path.read_text(encoding="utf-8").replace(
            "# Sample &lt;A&gt;", "# Changed generated heading", 1
        ),
        encoding="utf-8",
    )

    inspected = record_governance.inspect_collection(tmp_path, manifest.path)

    assert inspected["presentation"]["counts"]["authored_presentation"] == 1
    assert inspected["presentation"]["items"][0]["state"] == "authored_presentation"


def test_shared_inspection_resolves_typed_links_and_reports_missing_targets(
    tmp_path: Path,
) -> None:
    setup_collection(tmp_path, presentation=False)
    manifest_path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    source = (
        _shared_manifest()
        .replace(
            "    provenance:\n      type: string\n",
            "    provenance:\n      type: string\n"
            "    related:\n      type: link\n      link_kind: note\n",
        )
        .replace(
            "  long_text: [note, provenance]\n",
            "  long_text: [note, provenance]\n  relationships: [related]\n",
        )
    )
    manifest_path.write_text(source, encoding="utf-8")
    target = tmp_path / "Knowledge Base/Notes/Target.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: Human target\n---\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    linked = values()
    linked["related"] = "[[Knowledge Base/Notes/Target]]"
    appended = records.append_record(
        tmp_path,
        manifest.path,
        item=linked,
        item_key=ITEM_KEY,
        why="record one typed relationship",
    )
    item_path = tmp_path / appended["affected_paths"][0]
    assert "[[Knowledge Base/Notes/Target|Human target]]" in item_path.read_text(encoding="utf-8")
    assert record_formats.inspect_collection(tmp_path, manifest).presentation == ()

    target.unlink()
    inspected = record_formats.inspect_collection(tmp_path, manifest)

    assert inspected.presentation[0]["state"] == "unresolved_relationship"
