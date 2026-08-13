from __future__ import annotations

from pathlib import Path

import pytest
from record_presentation_fixtures import ITEM_KEY, setup_collection, values

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


def test_inspection_reports_only_noncurrent_states_counts_remedies_and_guards(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    appended = _append(tmp_path, manifest)
    item_path = tmp_path / appended["affected_paths"][0]
    current = item_path.read_text(encoding="utf-8")
    item_path.write_text(current.replace("Below threshold", "tampered display", 1), encoding="utf-8")

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
    missing = record_formats.inspect_collection(tmp_path, collections.load_manifest(tmp_path, manifest.path))
    assert missing.presentation[0]["state"] == "missing"

    item_path.write_text(text + "\n<!-- /exomem-record-presentation -->", encoding="utf-8")
    malformed = record_formats.inspect_collection(tmp_path, collections.load_manifest(tmp_path, manifest.path))
    assert malformed.presentation[0]["state"] == "malformed"

    item_path.write_text(text.replace("value: <5", "value:\n    unexpected: object", 1), encoding="utf-8")
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
