"""Tests for the local-only private-vault snapshot builder (task 1.3).

``scripts/private_vault_snapshot.py`` is loaded by path, matching
``tests/test_referent_resolution_benchmark.py``'s convention for a ``scripts/``
module that is not on ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "private_vault_snapshot.py"


def _module():
    spec = importlib.util.spec_from_file_location("private_vault_snapshot", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module's own frozen dataclass needs to resolve its (postponed,
    # string) annotations via `sys.modules[cls.__module__]`, so it must be
    # registered before `exec_module` runs the class body -- unlike
    # `referent_resolution_benchmark.py`'s equivalent helper, which never
    # needed this because that script has no dataclasses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_vault(root: Path) -> None:
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "clean.md").write_text("# Clean\n\nNothing sensitive here.\n", encoding="utf-8")
    (root / "notes" / "quotes-fixture.md").write_text(
        "# Meta note\n\nAs discussed: I keep hitting my AI usage limits again this week.\n",
        encoding="utf-8",
    )
    (root / "notes" / "binary.md").write_bytes(b"\xff\xfe\x00\x01")


def test_refuses_a_destination_inside_a_git_checkout(tmp_path: Path) -> None:
    module = _module()
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / ".git").mkdir(parents=True)
    dest = fake_repo / "snapshot-out"
    with pytest.raises(module.SnapshotError):
        module.refuse_unsafe_destination(dest)


def test_accepts_a_destination_outside_any_checkout(tmp_path: Path) -> None:
    module = _module()
    dest = tmp_path / "outside" / "snapshot-out"
    resolved = module.refuse_unsafe_destination(dest)
    assert resolved == dest.expanduser().resolve()


def test_excludes_a_page_that_quotes_a_fixture_turn_verbatim(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    _seed_vault(source)
    dest = tmp_path / "snapshot"
    manifest = module.build_snapshot(
        source,
        dest,
        fixture_turns=("I keep hitting my AI usage limits again this week.",),
        taken_at="2026-09-16T00:00:00Z",
    )
    assert "notes/quotes-fixture.md" in manifest.excluded
    assert not (dest / "notes" / "quotes-fixture.md").exists()
    assert (dest / "notes" / "clean.md").exists()


def test_excludes_an_undecodable_page_without_raising(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    _seed_vault(source)
    dest = tmp_path / "snapshot"
    manifest = module.build_snapshot(source, dest, fixture_turns=(), taken_at="2026-09-16T00:00:00Z")
    assert "notes/binary.md" in manifest.excluded
    assert manifest.file_count == 2  # clean.md + quotes-fixture.md (no turns supplied to match)


def test_manifest_never_quotes_excluded_page_content(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    _seed_vault(source)
    dest = tmp_path / "snapshot"
    module.build_snapshot(
        source,
        dest,
        fixture_turns=("I keep hitting my AI usage limits again this week.",),
        taken_at="2026-09-16T00:00:00Z",
    )
    written = json.loads((dest / "SNAPSHOT_MANIFEST.json").read_text(encoding="utf-8"))
    blob = json.dumps(written)
    assert "AI usage limits" not in blob  # paths only, never quoted body text


def test_digest_is_stable_for_the_same_content(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    _seed_vault(source)
    first = module.build_snapshot(source, tmp_path / "snap-a", fixture_turns=(), taken_at="t")
    second = module.build_snapshot(source, tmp_path / "snap-b", fixture_turns=(), taken_at="t")
    assert first.digest == second.digest


def test_refuses_a_missing_source(tmp_path: Path) -> None:
    module = _module()
    with pytest.raises(module.SnapshotError):
        module.build_snapshot(tmp_path / "does-not-exist", tmp_path / "snapshot", fixture_turns=(), taken_at="t")


# -- N3: contamination matching survives line-wrapping and typographic variants --


def test_excludes_a_page_that_line_wraps_a_fixture_turn(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    (source / "notes").mkdir(parents=True)
    (source / "notes" / "clean.md").write_text("# Clean\n\nNothing sensitive here.\n", encoding="utf-8")
    (source / "notes" / "wrapped.md").write_text(
        "# Meta note\n\nAs discussed: I keep hitting my AI usage\nlimits again this week.\n",
        encoding="utf-8",
    )
    dest = tmp_path / "snapshot"
    manifest = module.build_snapshot(
        source, dest, fixture_turns=("I keep hitting my AI usage limits again this week.",), taken_at="t"
    )
    assert "notes/wrapped.md" in manifest.excluded


def test_excludes_a_page_that_renders_a_fixture_turn_with_an_em_dash_and_curly_quotes(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    (source / "notes").mkdir(parents=True)
    (source / "notes" / "typographic.md").write_text(
        "# Meta note\n\nAs discussed—“I keep hitting my AI usage limits again this week.”\n",
        encoding="utf-8",
    )
    dest = tmp_path / "snapshot"
    manifest = module.build_snapshot(
        source, dest, fixture_turns=("I keep hitting my AI usage limits again this week.",), taken_at="t"
    )
    assert "notes/typographic.md" in manifest.excluded


def test_clean_page_is_not_excluded_by_the_normalized_check(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "vault"
    _seed_vault(source)
    dest = tmp_path / "snapshot"
    manifest = module.build_snapshot(
        source, dest, fixture_turns=("I keep hitting my AI usage limits again this week.",), taken_at="t"
    )
    assert "notes/clean.md" not in manifest.excluded


# -- N4: a bare repository is exactly as unsafe as a normal checkout -------


def test_refuses_a_destination_inside_a_bare_repository(tmp_path: Path) -> None:
    module = _module()
    bare_repo = tmp_path / "fake-bare-repo.git"
    (bare_repo / "objects").mkdir(parents=True)
    (bare_repo / "refs").mkdir(parents=True)
    (bare_repo / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    dest = bare_repo / "snapshot-out"
    with pytest.raises(module.SnapshotError):
        module.refuse_unsafe_destination(dest)


def test_accepts_a_directory_that_merely_looks_like_a_bare_repo_by_name(tmp_path: Path) -> None:
    # Only the actual HEAD/objects/refs shape marks a bare repo -- a
    # directory named "*.git" with none of that structure is not one.
    almost_bare = tmp_path / "just-a-folder.git"
    almost_bare.mkdir(parents=True)
    dest = almost_bare / "snapshot-out"
    module = _module()
    resolved = module.refuse_unsafe_destination(dest)
    assert resolved == dest.expanduser().resolve()
