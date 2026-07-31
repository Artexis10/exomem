"""The synthetic dogfood journey: real backend, real registry, temp vault.

Drives the actual `ExomemBackend` (no fakes) through the UI against a
disposable copy of the fixture vault — capture, packs, ask, citation preview,
contradiction, review triage, status, restart persistence. Never touches a
real user vault: the `vault` fixture copies `tests/fixtures` into tmp and
points `EXOMEM_VAULT_PATH` there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from exomem import writer_lease  # noqa: E402
from exomem.tui.app import ExomemTuiApp  # noqa: E402
from exomem.tui.backend import ExomemBackend  # noqa: E402
from exomem.tui.theme import GLYPHS_UNICODE  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease-state"))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _app() -> ExomemTuiApp:
    return ExomemTuiApp(ExomemBackend(), glyphs=GLYPHS_UNICODE)


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.timeout(180)
async def test_capture_packs_and_persistence(vault: Path):
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"

        # Capture a raw thought through the real governed path.
        app.goto("capture")
        await pilot.pause()
        app.screen.query_one("#capture-content").text = (
            "Sample dogfood thought: bounded retries beat unbounded retries."
        )
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        sources = list((vault / "Knowledge Base" / "Sources").rglob("*.md"))
        assert any("sample-dogfood-thought" in p.name for p in sources), sources

        # Enable two packs; the manifest persists in the vault.
        app.goto("packs")
        await _settle(app, pilot)
        selection_list = app.screen.query_one("#packs-list")
        assert selection_list.option_count >= 6, "built-in catalog expected"
        selection_list.deselect_all()
        for value in ("technical", "business"):
            selection_list.select(value)
        await pilot.press("a")
        await _settle(app, pilot)
        manifest = vault / "Knowledge Base" / "_Packs" / "selected-packs.json"
        assert manifest.exists()
        assert "technical" in manifest.read_text(encoding="utf-8")

    # Restart: a fresh app + backend over the same vault sees the state.
    app2 = _app()
    async with app2.run_test(size=(120, 40)) as pilot:
        await _settle(app2, pilot)
        app2.goto("packs")
        await _settle(app2, pilot)
        selection_list = app2.screen.query_one("#packs-list")
        assert sorted(selection_list.selected) == ["business", "technical"]


@pytest.mark.timeout(180)
async def test_ask_citation_contradiction_review(vault: Path):
    app = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app, pilot)

        # Ask over the real (lean/BM25) retrieval path.
        app.goto("ask")
        await pilot.pause()
        ask_input = app.screen.query_one("#ask-input")
        ask_input.value = "progressive disclosure"
        ask_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        results = app.screen.query_one("#ask-results")
        assert results.display is True and results.option_count > 0

        # Inspect the citation: preview the underlying page (wide side panel).
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.screen.query_one("#ask-detail").has_class("has-content")

        # Introduce a contradictory update via governed write-back. The
        # relation-review contract fires for an unlinked note: the TUI must
        # ASK, and only the user's explicit confirmation commits reviewed-none.
        await pilot.press("w")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "WriteBackModal"
        app.screen.query_one("#writeback-title").value = "Progressive disclosure reversal test"
        app.screen.query_one("#writeback-content").text = (
            "Contrary to the earlier conclusion, progressive disclosure did not "
            "reduce confusion in the sample scenario."
        )
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert app.screen.__class__.__name__ == "ConfirmModal", (
            "an unlinked note must trigger the explicit relation-review question"
        )
        notes = list((vault / "Knowledge Base" / "Notes").rglob("*reversal-test*.md"))
        assert not notes, "nothing may be written before the user confirms"
        await pilot.press("y")
        await _settle(app, pilot)
        notes = list((vault / "Knowledge Base" / "Notes").rglob("*reversal-test*.md"))
        assert notes, "confirmed write-back must land as a governed note"

        # Review the real attention queue and triage one item end-to-end.
        app.goto("review")
        await _settle(app, pilot)
        options = app.screen.query_one("#review-list")
        if options.display and options.option_count:
            await pilot.press("d")
            await _settle(app, pilot)
            state_file = vault / "Knowledge Base" / ".review-state.json"
            assert state_file.exists(), "triage must persist through the governed path"
        else:
            assert app.screen.query_one("#review-empty").display is True

        # Status renders real diagnostics without writing anything.
        app.goto("status")
        await _settle(app, pilot)
        doctor_text = str(app.screen.query_one("#status-doctor").render())
        assert doctor_text.strip(), "doctor section must render"


@pytest.mark.timeout(120)
async def test_adopt_scan_only_writes_nothing(vault: Path, tmp_path: Path):
    folder = tmp_path / "legacy-notes"
    folder.mkdir()
    (folder / "an-old-note.md").write_text("# An old note\nsome text\n", encoding="utf-8")
    before = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))

    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        app.goto("adopt")
        await pilot.pause()
        path_input = app.screen.query_one("#adopt-path")
        path_input.value = str(folder)
        await pilot.press("enter")
        await _settle(app, pilot)
        report = str(app.screen.query_one("#adopt-report").render())
        assert "markdown" in report

    after = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))
    assert before == after, "scan-only must not create or modify anything"


@pytest.mark.timeout(120)
async def test_adopt_write_targets_session_vault_only(vault: Path, tmp_path: Path):
    backend = ExomemBackend()
    backend.resolve_vault()

    # A folder outside the vault is refused, with remediation.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    from exomem.tui.backend import BackendError

    with pytest.raises(BackendError) as excinfo:
        backend.adopt_write(outside, "save-manifest")
    assert excinfo.value.code == "OUTSIDE_VAULT"

    # The vault root itself is a valid target: save-manifest commits only the
    # governed manifest artifact inside the vault.
    before = {str(p) for p in vault.rglob("*manifest*")}
    result = backend.adopt_write(vault, "save-manifest")
    assert isinstance(result, dict)
    assert result.get("ok") is True or result.get("manifest") or result.get("mode"), result
    after = {str(p) for p in vault.rglob("*manifest*")}
    assert after - before, "save-manifest must write a manifest artifact inside the vault"
