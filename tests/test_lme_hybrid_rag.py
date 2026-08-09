from __future__ import annotations

import hashlib
from dataclasses import replace


def test_hybrid_rag_is_byte_identical_with_fixture_embedder(monkeypatch) -> None:
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from protocol.models import CaseHandle, DatasetIdentity, EventProvenance, ProtocolEvent

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    identity = DatasetIdentity(id="fixture", variant="mini", source="local", revision="1", sha256="a" * 64, case_count=1)
    content = "The clockwork fox visits every Thursday afternoon."
    event = ProtocolEvent(dataset=identity, case_id="case-1", session_ordinal=1, sequence=0, role="user", turn_ordinal=1, content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(), original_timestamp="2026-01-01T00:00:00Z", timestamp_semantics="event_time_declared_by_dataset", ingestion_ordinal=0, provenance=EventProvenance(dataset_row_index=0, upstream_session_id_sha256="b" * 64, converter="fixture", converter_version="1"))
    provider = HybridRagDirectProvider()
    provider.setup(None)
    provider.ingest_case([event], CaseHandle(case_id="case-1", case_ordinal=1, question_date="2026-01-02T00:00:00Z"))
    first = provider.retrieve("When does the clockwork fox visit?", 3)
    second = provider.retrieve("When does the clockwork fox visit?", 3)
    assert first == second
    assert repr(first).encode() == repr(second).encode()
