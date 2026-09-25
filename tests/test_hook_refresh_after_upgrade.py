"""Managed-upgrade Claude Code hook refresh (`install_hook.refresh_wired_profiles`).

A managed upgrade promotes a new release's worker without touching the
operator's Claude Code hooks -- those are wired separately by `exomem
install-hook` and, before this, stayed pinned to whatever release wired them
until someone re-ran the installer by hand. `refresh_wired_profiles` closes
that gap: after promotion it re-runs the NEW release's own `install-hook` for
every local profile that already wires the retrieve nudge, and only those.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from exomem import install_hook as hook_module

pytestmark = pytest.mark.skipif(os.name == "nt", reason="posix permissions only")


def _wired_settings() -> dict:
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash ~/.claude/hooks/exomem-retrieve-nudge.sh",
                            "timeout": 10,
                        }
                    ]
                }
            ]
        }
    }


def _ups_commands(settings_path: Path) -> list[str]:
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in data.get("hooks", {}).get("UserPromptSubmit", [])
        for hook in group.get("hooks", [])
    ]


def test_refresh_wired_profiles_refreshes_a_wired_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    settings = profile / "settings.json"
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)

    assert report["skipped"] is False
    assert report["success"] is True
    assert len(report["profiles"]) == 1
    entry = report["profiles"][0]
    assert entry["success"] is True, entry["error"]
    assert entry["settings_path"] == str(settings)
    assert (profile / "hooks" / "exomem-retrieve-nudge.sh").exists()
    assert any("exomem-retrieve-nudge.sh" in cmd for cmd in _ups_commands(settings))

    persisted = json.loads(
        (profile / ".cache" / "exomem-nudge" / "upgrade-refresh.json").read_text(encoding="utf-8")
    )
    assert persisted == report


def test_refresh_wired_profiles_skips_an_unwired_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default_profile = home / ".claude"
    default_profile.mkdir(parents=True)
    default_settings = default_profile / "settings.json"
    default_settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")

    unwired_profile = home / ".claude-other"
    unwired_profile.mkdir(parents=True)
    unwired_settings = unwired_profile / "settings.json"
    unwired_settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    before = unwired_settings.read_text(encoding="utf-8")

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)

    assert len(report["profiles"]) == 1
    assert report["profiles"][0]["settings_path"] == str(default_settings)
    assert unwired_settings.read_text(encoding="utf-8") == before
    assert not (unwired_profile / "hooks").exists()


def test_refresh_wired_profiles_resolves_a_symlinked_settings_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".claude-work"
    profile.mkdir(parents=True)
    real_settings = tmp_path / "dotfiles" / "work-settings.json"
    real_settings.parent.mkdir(parents=True)
    real_settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    (profile / "settings.json").symlink_to(real_settings)

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)

    assert len(report["profiles"]) == 1
    entry = report["profiles"][0]
    assert entry["success"] is True, entry["error"]
    assert entry["settings_path"] == str(real_settings)
    assert any("exomem-retrieve-nudge.sh" in cmd for cmd in _ups_commands(real_settings))


def test_refresh_wired_profiles_reports_a_hook_error_without_raising(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    settings = profile / "settings.json"
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    os.chmod(profile, 0o775)  # group-writable: install-hook must refuse this
    try:
        report = hook_module.refresh_wired_profiles(sys.executable, home=home)
        # The installer refuses and reports; it never chmods the offending
        # directory itself to work around its own refusal.
        assert profile.stat().st_mode & 0o777 == 0o775
    finally:
        os.chmod(profile, 0o700)

    assert report["skipped"] is False
    assert report["success"] is False
    entry = report["profiles"][0]
    assert entry["success"] is False
    assert entry["error"]
    assert "writable" in entry["error"].lower()


def test_refresh_wired_profiles_honours_the_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_UPGRADE_HOOK_REFRESH", "1")
    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    settings = profile / "settings.json"
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)

    assert report["skipped"] is True
    assert report["profiles"] == []
    assert not (profile / "hooks").exists()


def test_read_last_upgrade_refresh_returns_the_persisted_report(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps(_wired_settings()), encoding="utf-8")

    assert hook_module.read_last_upgrade_refresh(home=home) is None

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert hook_module.read_last_upgrade_refresh(home=home) == report
