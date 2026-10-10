"""Sync-provider evidence is registry data that a vault overlay can only extend.

Each case runs one vault twice and varies only the registry, so a pass means the
overlay decided the custody verdict.
"""

from __future__ import annotations

from pathlib import Path

from exomem import sync_providers
from exomem.collection_store import custody
from exomem.vocabulary import registry


def _vault(base: Path, folder: str, monkeypatch) -> Path:
    root = base / folder / "vault"
    (root / "Knowledge Base" / "_Schema").mkdir(parents=True)
    # The folder sits on a Windows-mounted path, where its name is the only evidence.
    monkeypatch.setattr(custody, "_windows_mounts", lambda: [base])
    return root


def _overlay(root: Path, text: str) -> None:
    sync_providers.registry_path(root).write_text(text, encoding="utf-8")
    registry.invalidate(root)


def test_an_owner_added_sync_folder_withholds_store_custody(tmp_path, monkeypatch):
    """A provider the owner declares is ignored, so the live store lands in its sync root."""
    root = _vault(tmp_path, "Proton Drive", monkeypatch)
    assert custody.verify(root).verified

    _overlay(root, "schema_version: 1\nentries:\n  proton-drive-folder:\n    label: Proton Drive\n"
                   "    attributes: {evidence: windows-folder, value: proton drive}\n")

    verdict = custody.verify(root)
    assert not verdict.verified and "Proton Drive" in verdict.reason


def test_an_overlay_cannot_drop_a_shipped_provider(tmp_path, monkeypatch):
    """An overlay that retires a shipped entry would put the live store back in OneDrive."""
    root = _vault(tmp_path, "OneDrive - Contoso", monkeypatch)
    assert not custody.verify(root).verified

    _overlay(root, "schema_version: 1\nentries:\n  onedrive-folder:\n    status: deprecated\n"
                   "    attributes: {evidence: windows-folder, value: onedrive}\n")

    snapshot = registry.load(sync_providers.SPEC, root)
    assert [finding["code"] for finding in snapshot.findings] == ["invalid_registry_overlay"]
    assert "PACK_ENTRY_FIXED" in snapshot.findings[0]["detail"]
    assert not custody.verify(root).verified
