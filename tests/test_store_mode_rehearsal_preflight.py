"""The rehearsal driver must be unable to reach the installation that runs beside it.

The realistic failure is a driver that falls back to the live state root, vault or coordinator,
because one setting arrived from the environment or the interpreter. Each refusal and each
isolation setting that stops that is proved here; nothing here starts a service.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "store_mode_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("store_mode_rehearsal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its own module through sys.modules while the class body runs (3.11).
    sys.modules["store_mode_rehearsal"] = module
    spec.loader.exec_module(module)
    return module


rehearsal = _load()


@pytest.fixture
def layout(tmp_path):
    vault = tmp_path / "copy"
    vault.mkdir()
    (vault / "page.md").write_text("A page.\n", encoding="utf-8")
    return rehearsal.Layout(vault=vault, scratch=tmp_path / "scratch", out=tmp_path / "out")


@pytest.fixture
def settings():
    return rehearsal.Settings(rehearsal.free_port(), "writer-token", "operator-token")


@pytest.fixture
def live(tmp_path):
    """A stand-in for the live vault and its state root: a path the run must never touch."""
    path = tmp_path / "live"
    path.mkdir()
    return path


# --- Paths ------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["equal", "inside", "above"])
def test_a_path_that_meets_a_never_path_is_refused(tmp_path, live, where):
    chosen = {"equal": live, "inside": live / "deeper", "above": tmp_path}[where]
    with pytest.raises(rehearsal.Refusal, match="--never"):
        rehearsal.refuse_overlaps({"vault": chosen}, [live])


def test_a_symlink_into_a_never_path_is_refused(tmp_path, live):
    link = tmp_path / "innocent-looking"
    link.symlink_to(live)
    with pytest.raises(rehearsal.Refusal, match="--never"):
        rehearsal.refuse_overlaps({"scratch root": link / "run"}, [live])


def test_a_run_needs_at_least_one_never_path(layout, settings):
    with pytest.raises(rehearsal.Refusal, match="--never"):
        rehearsal.preflight(layout, [], settings)


def test_a_scratch_root_with_leftovers_is_refused(layout, settings, live):
    layout.scratch.mkdir()
    (layout.scratch / "state-from-an-earlier-run").write_text("x", encoding="utf-8")
    with pytest.raises(rehearsal.Refusal, match="not empty"):
        rehearsal.preflight(layout, [live], settings)


def test_a_scratch_root_inside_the_vault_copy_is_refused(layout, settings, live):
    inside = rehearsal.Layout(layout.vault, layout.vault / "scratch", layout.out)
    with pytest.raises(rehearsal.Refusal, match="must not contain each other"):
        rehearsal.preflight(inside, [live], settings)


def test_a_vault_copy_made_with_hard_links_is_refused(layout, settings, live, tmp_path):
    original = tmp_path / "original.md"
    original.write_text("shared\n", encoding="utf-8")
    os.link(original, layout.vault / "linked.md")
    with pytest.raises(rehearsal.Refusal, match="hard link"):
        rehearsal.preflight(layout, [live], settings)


# --- Ports ------------------------------------------------------------------------------------


@pytest.mark.parametrize("port", rehearsal.LIVE_PORTS)
def test_the_ports_of_a_live_installation_are_refused(layout, live, port):
    settings = rehearsal.Settings(port, "writer-token", "operator-token")
    with pytest.raises(rehearsal.Refusal, match="live installation"):
        rehearsal.preflight(layout, [live], settings)


def test_a_port_that_already_listens_is_refused(layout, live):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        settings = rehearsal.Settings(occupied.getsockname()[1], "writer-token", "operator-token")
        with pytest.raises(rehearsal.Refusal, match="already has a listener"):
            rehearsal.preflight(layout, [live], settings)


# --- The children's environment ---------------------------------------------------------------


def test_inherited_exomem_settings_never_reach_a_child(layout, settings, monkeypatch):
    for name in ("EXOMEM_STATE_ROOT", "EXOMEM_VAULT_PATH", "EXOMEM_LOG_DIR", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, "/the/live/installation")
    monkeypatch.setenv("EXOMEM_SOMETHING_NEW", "inherited")
    env = rehearsal.child_env(layout, settings)
    assert "EXOMEM_SOMETHING_NEW" not in env
    scratch = layout.scratch.resolve()
    for name in (
        "EXOMEM_STATE_ROOT",
        "EXOMEM_LOG_DIR",
        "EXOMEM_CONFIG_PATH",
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        "HOME",
        "TMPDIR",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        assert Path(env[name]).resolve().is_relative_to(scratch), name
    assert env["EXOMEM_VAULT_PATH"] == str(layout.vault)
    assert env["EXOMEM_WRITER_LEASE_URL"] == settings.coordinator_url


def test_an_isolated_layout_passes_with_the_real_interpreter(layout, settings, live):
    proved = rehearsal.preflight(layout, [live], settings)
    assert proved["never_paths"] == 1
    assert (
        Path(proved["interpreter"]["state_root"]).resolve().is_relative_to(layout.scratch.resolve())
    )


def test_an_interpreter_that_sets_a_setting_after_the_run_passed_is_refused(
    layout, settings, live, tmp_path
):
    """A `.pth` file or `sitecustomize` runs before the driver's code, and sets what it likes.

    A launcher that exports a live state root before it starts the real interpreter arrives at
    the same place: a setting the run did not pass.
    """
    launcher = tmp_path / "python-with-a-hidden-setting"
    launcher.write_text(
        f'#!/bin/sh\nEXOMEM_STATE_ROOT="{live}/state" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(rehearsal.Refusal, match="EXOMEM_STATE_ROOT"):
        rehearsal.preflight(layout, [live], settings, python=str(launcher))


# --- What a kill is judged on -----------------------------------------------------------------


def test_a_difference_on_a_trashed_page_is_known_and_any_other_is_still_a_finding():
    """A kill must not hide behind the known trash difference: only that row of that page is set
    apart; the same table on another page, and every other table, still count against the kill.
    """
    differences = [
        {"table": "unit_revisions", "keys": ["a/trashed.md", "a/live.md"]},
        {"table": "pages", "keys": ["a/trashed.md"]},
    ]
    known, rest = rehearsal.split_known(differences, {"a/trashed.md"})
    assert known == [{"table": "unit_revisions", "keys": ["a/trashed.md"]}]
    assert rest == [
        {"table": "unit_revisions", "keys": ["a/live.md"]},
        {"table": "pages", "keys": ["a/trashed.md"]},
    ]
