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
import subprocess
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
    home.chmod(0o700)
    profile.chmod(0o700)
    settings = profile / "settings.json"
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    settings.chmod(0o600)

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
        hook_module._upgrade_refresh_report_path(home).read_text(encoding="utf-8")
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
    home.chmod(0o700)
    profile.chmod(0o700)
    real_settings = tmp_path / "dotfiles" / "work-settings.json"
    real_settings.parent.mkdir(parents=True)
    real_settings.parent.chmod(0o700)
    real_settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    real_settings.chmod(0o600)
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
    # Other-writable: install-hook must refuse this. (Group-writable alone is
    # accepted when the group is the owner's private group, as on CI runners.)
    os.chmod(profile, 0o777)
    try:
        report = hook_module.refresh_wired_profiles(sys.executable, home=home)
        # The installer refuses and reports; it never chmods the offending
        # directory itself to work around its own refusal.
        assert profile.stat().st_mode & 0o777 == 0o777
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
    home.chmod(0o700)
    profile.chmod(0o700)
    (profile / "settings.json").write_text(json.dumps(_wired_settings()), encoding="utf-8")

    assert hook_module.read_last_upgrade_refresh(home=home) is None

    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert hook_module.read_last_upgrade_refresh(home=home) == report


def test_upgrade_refresh_report_is_private_and_atomic(tmp_path: Path) -> None:
    home = tmp_path / "home"
    report = {"success": False, "profiles": []}
    path = hook_module._upgrade_refresh_report_path(home)

    hook_module._write_upgrade_refresh_report(home, report)
    assert path.stat().st_mode & 0o777 == 0o600
    assert hook_module.read_last_upgrade_refresh(home=home) == report
    hook_module._write_upgrade_refresh_report(home, {"success": True, "profiles": []})
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize("redirect", ["symlink", "hardlink", "parent_symlink"])
def test_upgrade_refresh_report_refuses_untrusted_redirects(
    tmp_path: Path, redirect: str
) -> None:
    home = tmp_path / "home"
    path = hook_module._upgrade_refresh_report_path(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text('{"success":true,"profiles":[]}', encoding="utf-8")
    before = victim.read_bytes()
    if redirect == "parent_symlink":
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(outside, target_is_directory=True)
        (outside / path.name).symlink_to(victim)
    else:
        path.parent.mkdir(parents=True)
        if redirect == "symlink":
            path.symlink_to(victim)
        else:
            os.link(victim, path)

    hook_module._write_upgrade_refresh_report(home, {"success": False, "profiles": []})
    assert victim.read_bytes() == before
    assert hook_module.read_last_upgrade_refresh(home=home) is None


@pytest.mark.parametrize("stream", ["stderr", "stdout", "exception"])
def test_refresh_scrubs_child_diagnostics_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    home.chmod(0o700)
    settings.parent.chmod(0o700)

    def fake_run(argv, **kwargs):
        if stream == "exception":
            raise OSError("passwd=hunter2hunter2 failed")
        return subprocess.CompletedProcess(
            argv, 2,
            "api_key=hunter2hunter2 failed" if stream == "stdout" else "",
            "Authorization: Basic dXNlcjpwYXNz failed" if stream == "stderr" else "",
        )

    monkeypatch.setattr(hook_module.subprocess, "run", fake_run)
    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert report["success"] is False
    assert "failed" in report["profiles"][0]["error"]
    assert "hunter2hunter2" not in json.dumps(report)
    assert "dXNlcjpwYXNz" not in json.dumps(report)
    assert hook_module.read_last_upgrade_refresh(home=home) == report


def test_refresh_limits_profile_attempts_and_reports_deferred_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for number in range(40):
        profile = home / f".claude-{number:02d}"
        profile.mkdir(parents=True)
        (profile / "settings.json").write_text(json.dumps(_wired_settings()), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hook_module.subprocess, "run", fake_run)
    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert len(calls) <= 32
    assert all(0 < timeout <= 30 for timeout in calls)
    assert report["success"] is False
    assert report["deferred"] is True
    assert len(report["profiles"]) == len(calls)


def test_refresh_caps_each_child_at_remaining_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for name in (".claude", ".claude-a", ".claude-b"):
        profile = home / name
        profile.mkdir(parents=True)
        (profile / "settings.json").write_text(json.dumps(_wired_settings()), encoding="utf-8")
    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(hook_module.time, "monotonic", lambda: clock[0])

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] = 29.5 if len(timeouts) == 1 else 31.0
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hook_module.subprocess, "run", fake_run)
    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert timeouts == [30.0, 0.5]
    assert report["success"] is False
    assert report["deferred_reason"] == "time_budget"
    assert len(report["profiles"]) == 2


def test_profile_discovery_stops_at_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for name in (".claude", ".claude-a", ".claude-b"):
        profile = home / name
        profile.mkdir(parents=True)
        (profile / "settings.json").write_text(json.dumps(_wired_settings()), encoding="utf-8")
    clock = [0.0]
    inspected = []
    monkeypatch.setattr(hook_module.time, "monotonic", lambda: clock[0])

    def inspect(settings):
        inspected.append(settings)
        clock[0] = 31.0
        return True

    monkeypatch.setattr(hook_module, "_profile_wires_retrieve_hook", inspect)
    status = {}
    wired = hook_module.discover_wired_profiles(home, deadline=30.0, status=status)
    assert len(inspected) == 1
    assert len(wired) == 1
    assert status["deferred_reason"] == "time_budget"


def test_profile_discovery_bounds_unrelated_home_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    clock = [0.0]
    inspected = []
    original_iterdir = Path.iterdir

    def fake_iterdir(path):
        if path != home:
            yield from original_iterdir(path)
            return
        for number in range(100):
            inspected.append(number)
            clock[0] += 11.0
            yield home / f"unrelated-{number}"

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(hook_module.time, "monotonic", lambda: clock[0])
    status = {}
    assert hook_module.discover_wired_profiles(home, deadline=30.0, status=status) == []
    assert len(inspected) <= 3
    assert status["deferred_reason"] == "time_budget"


def test_malformed_profile_does_not_block_a_wired_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    malformed = home / ".claude"
    malformed.mkdir(parents=True)
    (malformed / "settings.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": 42}]}}), encoding="utf-8"
    )
    good = home / ".claude-good"
    good.mkdir()
    settings = good / "settings.json"
    settings.write_text(json.dumps(_wired_settings()), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hook_module.subprocess, "run", fake_run)
    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert len(calls) == 1
    assert report["profiles"][0]["settings_path"] == str(settings)


def test_report_does_not_create_an_unwired_default_profile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    report = hook_module.refresh_wired_profiles(sys.executable, home=home)
    assert report["profiles"] == []
    assert not (home / ".claude").exists()
    assert hook_module.read_last_upgrade_refresh(home=home) == report
