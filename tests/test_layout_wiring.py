"""Vault resolution and service start read the layout once per vault and refuse a vault that cannot say it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from exomem import layout, product_invoke, server_runtime
from exomem import vault as vault_module
from exomem.__main__ import main
from exomem.governance import projection_runtime

KB = "Knowledge Base"
RECORD = f"_Schema/{layout.RECORD_NAME}"
WRAPPER_RECORD = f"{KB}/_Schema/{layout.RECORD_NAME}"


def _write(vault: Path, rel: str, text: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _lay_out_schema_at_the_vault_root(vault: Path) -> None:
    """Move the schema folder out of the wrapper and keep the shipped sentinel reachable, so the folder stays a vault."""
    shutil.move(vault / KB / "_Schema", vault / "_Schema")
    _write(vault, ".exomem/schema/SKILL.md", "---\nname: exomem\n---\n")


def _start_runtime():
    return server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)


def _stub_the_background_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)
    monkeypatch.setattr(server_runtime, "_start_retrieval_runtime", lambda _root, **_kwargs: None)
    monkeypatch.setattr(server_runtime, "_start_derived_drain", lambda _root: None)
    projection_runtime._clear_preactivated_runtimes_for_tests()


class _Spy:
    """Counts how often the layout is read from disk."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads = 0
        real = layout.inspect

        def counted(vault_root: Path):
            self.reads += 1
            return real(vault_root)

        monkeypatch.setattr(layout, "inspect", counted)


def test_the_accessor_reads_the_layout_once_and_again_only_after_the_record_changes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read per call would walk the disk on every operation; a stale answer would hide a relayout."""
    _write(vault, WRAPPER_RECORD, "layout: nested\n")
    spy = _Spy(monkeypatch)

    assert layout.of(vault) is layout.Layout.NESTED
    assert layout.of(vault) is layout.Layout.NESTED
    assert spy.reads == 1

    _write(vault, WRAPPER_RECORD, "layout: root\nunmanaged: [Archive]\n")

    assert layout.of(vault) is layout.Layout.ROOT
    assert layout.of(vault) is layout.Layout.ROOT
    assert spy.reads == 2


def test_the_accessor_sees_a_schema_folder_that_appears_after_the_first_answer(vault: Path) -> None:
    """The first answer was `nested`. A vault-root schema folder with no record must refuse now, not at a restart."""
    assert layout.of(vault) is layout.Layout.NESTED

    _lay_out_schema_at_the_vault_root(vault)

    with pytest.raises(layout.LayoutError) as refused:
        layout.of(vault)
    assert refused.value.code == "LAYOUT_RECORD_MISSING"


def test_the_accessor_does_not_keep_a_refusal_after_the_record_is_written(vault: Path) -> None:
    """The remedy is to write the record. The next call must then answer, not repeat the refusal."""
    _lay_out_schema_at_the_vault_root(vault)
    with pytest.raises(layout.LayoutError):
        layout.of(vault)

    _write(vault, RECORD, "layout: root\n")

    assert layout.of(vault) is layout.Layout.ROOT


def test_service_start_refuses_a_vault_root_schema_folder_with_no_record_and_names_the_remedy(
    vault: Path,
) -> None:
    _lay_out_schema_at_the_vault_root(vault)

    with pytest.raises(layout.LayoutError) as refused:
        _start_runtime()

    assert refused.value.code == "LAYOUT_RECORD_MISSING"
    assert RECORD in refused.value.remediation
    assert "layout: root" in refused.value.remediation


def test_an_explicit_vault_root_is_refused_the_same_way(vault: Path) -> None:
    """`resolve_vault_for` checks a root that a caller hands it without going through the environment."""
    _lay_out_schema_at_the_vault_root(vault)

    with pytest.raises(layout.LayoutError) as refused:
        product_invoke.resolve_vault_for("get_page", {}, vault)

    assert refused.value.code == "LAYOUT_RECORD_MISSING"


def test_a_record_with_a_leftover_second_schema_folder_still_starts_and_status_reports_the_leftover(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A crashed relayout leaves both folders. Refusing here would leave the vault unable to start and to repair."""
    _write(vault, WRAPPER_RECORD, "layout: nested\n")
    _write(vault, "_Schema/SKILL.md")
    _stub_the_background_workers(monkeypatch)

    runtime = _start_runtime()

    assert runtime.vault_root == vault
    assert main(["status", "--vault", str(vault), "--json"]) == 0
    reported = json.loads(capsys.readouterr().out)["layout"]
    assert reported["layout"] == "nested"
    assert reported["leftover_schema_folders"] == ["_Schema"]


def test_serve_prints_the_refusal_code_and_the_remedy(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`str(error)` carries the code and the message only: without the remedy the operator reads a dead end."""
    from exomem import server

    _lay_out_schema_at_the_vault_root(vault)
    monkeypatch.setattr(server, "run", lambda **_kwargs: vault_module.resolve_vault())

    assert main(["--transport", "stdio"]) == 1

    shown = capsys.readouterr().err
    assert "LAYOUT_RECORD_MISSING" in shown
    assert RECORD in shown
