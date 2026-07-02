"""Aging/triage on the unprocessed_source audit check (Pillar 3)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import add as add_module
from exomem import audit as audit_module
from exomem import schema as schema_module


def test_parse_fm_date_coerces_forms() -> None:
    assert audit_module._parse_fm_date("2026-05-04") == dt.date(2026, 5, 4)
    assert audit_module._parse_fm_date(dt.date(2026, 5, 4)) == dt.date(2026, 5, 4)
    assert audit_module._parse_fm_date(dt.datetime(2026, 5, 4, 9, 0)) == dt.date(2026, 5, 4)
    assert audit_module._parse_fm_date(None) is None
    assert audit_module._parse_fm_date("not-a-date") is None


def test_unprocessed_sources_aged_bucketed_and_oldest_first(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    # Two fresh-off-add sources (ingested_into: []), with controlled capture dates.
    add_module.add(
        vault, source_schema, content="old capture body", source_type="other",
        title="Aging Old One", today=dt.date(2026, 1, 1),
    )
    add_module.add(
        vault, source_schema, content="recent capture body", source_type="other",
        title="Aging Recent One", today=dt.date(2026, 5, 20),
    )

    report = audit_module.audit(
        vault, categories=["unprocessed_source"], today=dt.date(2026, 5, 29)
    )
    findings = report.findings
    old_f = next(f for f in findings if "aging-old-one" in f.path)
    rec_f = next(f for f in findings if "aging-recent-one" in f.path)

    # Aging meta.
    assert old_f.meta["age_days"] == (dt.date(2026, 5, 29) - dt.date(2026, 1, 1)).days
    assert old_f.meta["age_bucket"] == "stale"
    assert old_f.severity == "warn"
    assert old_f.meta["captured"] == "2026-01-01"

    assert rec_f.meta["age_days"] == 9
    assert rec_f.meta["age_bucket"] == "fresh"
    assert rec_f.severity == "info"

    # Oldest first.
    paths = [f.path for f in findings]
    assert paths.index(old_f.path) < paths.index(rec_f.path)

    # proposed_fix points at the new drain path.
    assert "propose_compilation" in old_f.proposed_fix


def test_unprocessed_source_finding_omits_meta_when_no_date(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    # as_dict should not carry a meta key when capture date is unknown.
    # (add always sets captured, so synthesize a finding directly.)
    f = audit_module.AuditFinding(
        category="unprocessed_source", severity="info", path="x", detail="d",
    )
    assert "meta" not in f.as_dict()
    assert "paths" not in f.as_dict()
