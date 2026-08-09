from __future__ import annotations

from pathlib import Path

import pytest


def test_manifest_starts_before_call_and_trace_refuses_unfinished_runs(tmp_path: Path) -> None:
    from protocol.manifest import ManifestError, finalize_manifest, load_manifest, start_manifest
    from protocol.trace import CaseTraceReader, CaseTraceWriter

    start_manifest(tmp_path, run_id="run-1", dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1}, started_at="2026-01-01T00:00:00Z")
    assert (tmp_path / "manifest.json").is_file()
    with pytest.raises(ManifestError, match="non-terminal"):
        load_manifest(tmp_path)
    writer = CaseTraceWriter(tmp_path, "case-1")
    writer.append({"record": "ingest", "session_ordinal": 1, "payload_sha256": "b" * 64, "provider_ids": ["source-1"]})
    assert list(CaseTraceReader(tmp_path, "case-1"))[0].record == "ingest"
    finalize_manifest(tmp_path, status="VALID", finalized_at="2026-01-01T00:00:01Z")
    assert load_manifest(tmp_path).status == "VALID"
