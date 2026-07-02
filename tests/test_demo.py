"""`exomem demo` — the packaged 30-second proof (OpenSpec: add-one-command-onboarding).

Pins the demo.py contract: the bundled sample vault ships inside the package,
`run_demo`/CLI dispatch runs the four timed steps against a temp copy of it
(never the installed package dir), env is fully restored (no bleed for
in-process callers like this suite), and failures propagate a non-zero exit
in both human and `--json` form.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import exomem
from exomem import demo
from exomem.__main__ import _core_op_names
from exomem.__main__ import main as cli_main


def _vault_signature(root: Path) -> list[tuple[str, bytes]]:
    """(relative path, content) for every file under root, for before/after
    equality checks — the packaged sample vault must never be mutated."""
    return sorted(
        (str(f.relative_to(root)), f.read_bytes())
        for f in root.rglob("*")
        if f.is_file()
    )


def test_packaged_sample_vault_present() -> None:
    assert demo.SAMPLE_VAULT.parent == Path(exomem.__file__).resolve().parent
    assert demo.SAMPLE_VAULT.is_dir()
    assert (demo.SAMPLE_VAULT / "Knowledge Base" / "_Schema" / "SKILL.md").is_file()
    assert (demo.SAMPLE_VAULT / demo.TARGET_PATH).is_file()


def test_happy_path_human_output() -> None:
    lines: list[str] = []

    code = demo.run_demo(echo=lines.append)

    assert code == 0
    assert any(line.startswith("1. doctor: PASS") for line in lines)
    assert any(line.startswith('2. find "retrieval": PASS') for line in lines)
    assert any(line.startswith("3. get retrieval insight: PASS") for line in lines)
    assert any(line.startswith("4. audit: PASS") for line in lines)
    assert any("demo PASS" in line for line in lines)
    assert any("exomem setup" in line for line in lines)


def test_cli_dispatch_runs_demo_and_returns_0(capsys: pytest.CaptureFixture) -> None:
    code = cli_main(["demo"])
    out = capsys.readouterr().out

    assert code == 0
    assert "demo PASS" in out


def test_cli_json_envelope_shape_and_ordering(capsys: pytest.CaptureFixture) -> None:
    code = cli_main(["demo", "--json"])
    out = capsys.readouterr().out.strip()

    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 1  # exactly one JSON line
    envelope = json.loads(lines[0])
    assert envelope["success"] is True
    assert [s["name"] for s in envelope["steps"]] == ["doctor", "find", "get", "audit"]
    seconds = [s["seconds"] for s in envelope["steps"]]
    assert all(s >= 0 for s in seconds)
    assert envelope["total_seconds"] >= max(seconds)


def test_temp_isolation_no_leftover_dir_and_sample_vault_unmodified() -> None:
    tmp_root = Path(tempfile.gettempdir())
    before_dirs = set(tmp_root.glob("exomem-demo-*"))
    before_vault = _vault_signature(demo.SAMPLE_VAULT)

    code = demo.run_demo(echo=lambda *_: None)

    after_dirs = set(tmp_root.glob("exomem-demo-*"))
    assert code == 0
    assert after_dirs - before_dirs == set()  # temp copy was removed
    assert _vault_signature(demo.SAMPLE_VAULT) == before_vault  # package copy untouched


def test_keep_flag_keeps_temp_dir_and_prints_path() -> None:
    lines: list[str] = []

    code = demo.run_demo(keep=True, echo=lines.append)

    assert code == 0
    kept_lines = [line for line in lines if line.startswith("kept sample vault at: ")]
    assert len(kept_lines) == 1
    kept_path = Path(kept_lines[0][len("kept sample vault at: "):])
    try:
        assert kept_path.is_dir()
        assert (kept_path / demo.TARGET_PATH).is_file()
    finally:
        shutil.rmtree(kept_path, ignore_errors=True)


def test_env_restore_no_bleed_for_in_process_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sentinel env value set before run_demo() must come back EXACTLY
    afterward, and EXOMEM_VAULT_PATH (unset before this test) must stay
    absent — no bleed into the calling process (this test suite)."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "sentinel-value")
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    code = demo.run_demo(echo=lambda *_: None)

    assert code == 0
    assert os.environ.get("EXOMEM_DISABLE_EMBEDDINGS") == "sentinel-value"
    assert "EXOMEM_VAULT_PATH" not in os.environ


def test_failure_path_empty_vault_exits_1_with_false_json_envelope(tmp_path: Path) -> None:
    """No 'Knowledge Base/' at all -> doctor fails first; both the plain
    exit code and the --json envelope must report the failure."""
    empty = tmp_path / "empty"
    empty.mkdir()
    lines: list[str] = []

    code = demo.run_demo(vault=empty, json_out=True, echo=lines.append)

    assert code == 1
    envelope = json.loads(lines[0])
    assert envelope["success"] is False
    assert envelope["steps"][0]["name"] == "doctor"
    assert envelope["steps"][0]["ok"] is False


def test_corrupted_vault_missing_target_path_fails_at_find(tmp_path: Path) -> None:
    """Sample content present but the retrieval-insight file removed -> doctor
    still passes (Knowledge Base/ exists) but find can no longer locate the
    expected hit, and the demo exits 1."""
    corrupted = tmp_path / "corrupted"
    shutil.copytree(demo.SAMPLE_VAULT / "Knowledge Base", corrupted / "Knowledge Base")
    (corrupted / demo.TARGET_PATH).unlink()
    lines: list[str] = []

    code = demo.run_demo(vault=corrupted, echo=lines.append)

    assert code == 1
    assert any(line.startswith('2. find "retrieval": FAIL') for line in lines)


def test_demo_and_warm_not_registered_as_core_ops() -> None:
    """`demo` and `warm` are special-cased earlier in __main__.main's dispatch
    chain than the registry-driven core-op branch — they must never ALSO be
    core op names, or the two dispatch paths would collide."""
    names = _core_op_names(expose_tier2=True)
    assert "demo" not in names
    assert "warm" not in names


def test_lean_env_names_are_actually_consumed_by_the_source() -> None:
    """Pins the rename-corruption fix for real: the retired repo scripts
    carried misspelled flag variants that nothing read (silent no-ops). Every
    LEAN_ENV name must appear in src/exomem/** source OUTSIDE demo.py itself,
    so a typo or one-sided rename can't reintroduce a dead flag."""
    src = Path(demo.__file__).resolve().parent
    corpus = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in src.glob("*.py")
        if p.name != "demo.py"
    )
    for name in demo.LEAN_ENV:
        assert name in corpus, f"{name} is set by demo but read nowhere in src/exomem"
