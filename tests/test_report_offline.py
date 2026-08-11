"""RM5 / RM8: report honesty, the variant axis, and a real offline socket guard."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest import mock

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


def _started_manifest(run_dir: Path) -> None:
    from protocol.manifest import start_manifest

    start_manifest(
        run_dir, run_id="started",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 0},
        started_at="2026-01-01T00:00:00Z",
    )


def _run(out: Path, run_id: str, provider: str = "hybrid-rag-control"):
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        return execute_run(
            RunConfig(dataset=FIXTURE, out=out, reader_name="stub", run_id=run_id, provider=provider),
            reader=StubReader(),
        )


def test_report_refuses_a_non_terminal_manifest(tmp_path: Path) -> None:
    from lme.report import render_run_report

    _started_manifest(tmp_path)
    with pytest.raises(ValueError, match="non-terminal"):
        render_run_report(tmp_path, offline=True)


def test_the_offline_guard_refuses_a_real_socket_connect() -> None:
    """RM8: a genuine socket.connect attempt, not a stand-in call on the patch."""

    from lme.report import offline_guard
    from protocol.offline import offline_guard as shared_offline_guard

    assert offline_guard is shared_offline_guard

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        with offline_guard():
            with pytest.raises(OSError, match="offline report generation forbids socket.connect"):
                probe.connect(("127.0.0.1", 9))
    # Outside the guard, the real stack is restored: a closed port refuses with
    # its own error, never the guard's message.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        with pytest.raises(OSError) as failure:
            probe.connect(("127.0.0.1", 9))
        assert "offline report generation" not in str(failure.value)


def test_a_valid_run_renders_identically_from_both_entry_points(tmp_path: Path) -> None:
    """RM5: the runner's report.md and artifact-only regeneration agree, variant included."""

    from lme.report import render_run_report

    result = _run(tmp_path, "variant-axis")
    written = (result.run_dir / "report.md").read_text(encoding="utf-8")
    regenerated = render_run_report(result.run_dir, offline=True)
    assert regenerated == written
    assert "| Ability | Variant | Questions |" in regenerated
    assert "| multi-session | hybrid-rag-fixture |" in regenerated
    assert "awaiting official judge" in regenerated
    assert "False" not in regenerated, "an unjudged row must never render a fabricated verdict"
    assert "Aggregate" not in regenerated and "aggregate" not in regenerated


def test_the_false_green_manifest_shape_renders_invalid(tmp_path: Path) -> None:
    """RM5 red-shape: contaminated-but-VALID must not regenerate as a healthy report."""

    from lme.report import render_run_report

    result = _run(tmp_path, "false-green-source")
    healthy = render_run_report(result.run_dir, offline=True)
    assert "INVALID" not in healthy

    false_green = tmp_path / "false-green"
    false_green.mkdir()
    for name in ("dataset.json", "hypotheses.jsonl", "failures.jsonl"):
        (false_green / name).write_bytes((result.run_dir / name).read_bytes())
    (false_green / "bounds").mkdir()
    for name in ("gold-evidence-ceiling.jsonl", "null-abstain-floor.jsonl"):
        (false_green / "bounds" / name).write_bytes((result.run_dir / "bounds" / name).read_bytes())
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["contamination"] = "contaminated"
    (false_green / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rendered = render_run_report(false_green, offline=True)
    assert "INVALID" in rendered
    assert "contamination=contaminated" in rendered
    assert "invalid environment" in rendered


def test_a_readiness_unverifiable_manifest_renders_its_own_status(tmp_path: Path) -> None:
    from lme.report import render_run_report

    result = _run(tmp_path, "unverifiable-source")
    target = tmp_path / "unverifiable"
    target.mkdir()
    for name in ("dataset.json", "hypotheses.jsonl", "failures.jsonl"):
        (target / name).write_bytes((result.run_dir / name).read_bytes())
    (target / "bounds").mkdir()
    for name in ("gold-evidence-ceiling.jsonl", "null-abstain-floor.jsonl"):
        (target / "bounds" / name).write_bytes((result.run_dir / "bounds" / name).read_bytes())
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "READINESS_UNVERIFIABLE"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rendered = render_run_report(target, offline=True)
    assert "READINESS_UNVERIFIABLE" in rendered


def test_the_cli_report_command_regenerates_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from lme import cli

    result = _run(tmp_path, "cli-report")
    assert cli.main(["report", "--run-dir", str(result.run_dir), "--offline"]) == 0
    assert "| Ability | Variant | Questions |" in capsys.readouterr().out
