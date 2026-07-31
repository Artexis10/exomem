"""Strict-validation and JSON-Schema-export guarantees of the corpus schema."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from membench import schema as mschema

REPO_ROOT = Path(__file__).resolve().parents[1]
MALFORMED_DIR = REPO_ROOT / "tests" / "fixtures" / "membench" / "malformed"
COMMITTED_SCHEMA_DIR = REPO_ROOT / "benchmarks" / "corpus" / "schema"

_MALFORMED_MODEL = {
    "claim-missing-timeline.json": (mschema.ClaimRecord, "claim"),
    "span-inverted-window.json": (mschema.StatusSpan, None),
    "source-unknown-kind.json": (mschema.SourceRecord, "source"),
    "query-extra-field.json": (mschema.QueryRecord, "query"),
}


def _valid_claim() -> mschema.ClaimRecord:
    return mschema.ClaimRecord(
        claim_id="CLM-11111111",
        subject="ENT-00000001",
        predicate="deadline_of",
        object=mschema.TypedValue(kind="date", value="2025-03-14"),
        assertions=[
            mschema.Assertion(
                source_id="SRC-00000001",
                stance=mschema.Stance.SUPPORTS,
                asserted_at=date(2025, 1, 7),
                recorded_week=0,
            )
        ],
        status_timeline=[
            mschema.StatusSpan(
                status=mschema.ClaimStatus.CURRENT,
                valid_from=date(2025, 1, 6),
                recorded_week=0,
                cause=mschema.SpanCause(kind=mschema.SpanCauseKind.INITIAL, by="SRC-00000001"),
            )
        ],
    )


def test_happy_path_records_construct() -> None:
    claim = _valid_claim()
    assert claim.status_timeline[0].status is mschema.ClaimStatus.CURRENT
    source = mschema.SourceRecord(
        source_id="SRC-00000001",
        title="Kickoff note",
        artifact_kind=mschema.ArtifactKind.MARKDOWN,
        path="sources/kickoff.md",
        authority=mschema.AuthorityTier.FIRSTHAND,
        event_time=date(2025, 1, 7),
        recorded_week=0,
    )
    assert source.version == 1
    query = mschema.QueryRecord(
        query_id="QRY-00000001",
        template_id="t00",
        family="query_behavior",
        query_kind="direct_recall",
        prompt_text="What is the deadline?",
        ask=mschema.Ask(knowledge_week=4),
    )
    assert query.should_activate is True


def test_extra_fields_are_rejected_everywhere() -> None:
    with pytest.raises(ValidationError):
        mschema.Ask(knowledge_week=1, extra_knob=True)  # type: ignore[call-arg]


@pytest.mark.parametrize("fixture_name", sorted(_MALFORMED_MODEL))
def test_malformed_fixture_rejected_by_pydantic(fixture_name: str) -> None:
    model, _ = _MALFORMED_MODEL[fixture_name]
    payload = (MALFORMED_DIR / fixture_name).read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        model.model_validate_json(payload)


@pytest.mark.parametrize(
    "fixture_name",
    sorted(name for name, (_, schema_key) in _MALFORMED_MODEL.items() if schema_key),
)
def test_malformed_fixture_rejected_by_exported_jsonschema(fixture_name: str) -> None:
    _, schema_key = _MALFORMED_MODEL[fixture_name]
    exported = json.loads(
        (COMMITTED_SCHEMA_DIR / f"{schema_key}.schema.json").read_text(encoding="utf-8")
    )
    instance = json.loads((MALFORMED_DIR / fixture_name).read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=exported)


def test_committed_schemas_match_export(tmp_path: Path) -> None:
    """Drift gate: re-export and byte-compare against the committed schemas."""

    written = mschema.export_json_schemas(tmp_path)
    assert written, "export produced no schemas"
    fresh = {p.name: p.read_text(encoding="utf-8") for p in written}
    committed = {
        p.name: p.read_text(encoding="utf-8") for p in COMMITTED_SCHEMA_DIR.glob("*.schema.json")
    }
    assert fresh == committed


def test_jsonl_round_trip(tmp_path: Path) -> None:
    claim = _valid_claim()
    target = tmp_path / "claims.jsonl"
    assert mschema.dump_jsonl([claim], target) == 1
    loaded = mschema.load_jsonl(mschema.ClaimRecord, target)
    assert loaded == [claim]


def test_index_by_rejects_duplicates() -> None:
    claim = _valid_claim()
    with pytest.raises(ValueError, match="duplicate"):
        mschema.index_by([claim, claim], "claim_id")
