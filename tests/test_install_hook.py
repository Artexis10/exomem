"""install-hook (installer) + the capture (Stop) and retrieval (UserPromptSubmit)
nudge hooks.

The hooks are the reliability fix for the KB loop: skill prose is passive, so Stop
re-arms "capture this stepping-stone" (write) and UserPromptSubmit re-arms "consult
the KB first" (read). Both are language-agnostic (structural gate + cooldown). The
registered command is machine-agnostic (`bash ~/.claude/hooks/<name>.sh`) so a
yadm-synced ~/.claude works across Windows / WSL / Linux / macOS. Codex installs
the same bundled Python scripts into ~/.codex/hooks with command + commandWindows
wiring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import exomem
from exomem import install_hook as hook_module
from exomem._hooks import exomem_continuation_checkpoint as checkpoint

_HOOKS = Path(exomem.__file__).parent / "_hooks"
CAPTURE_SCRIPT = _HOOKS / "exomem_capture_nudge.py"
RETRIEVE_SCRIPT = _HOOKS / "exomem_retrieve_nudge.py"


def _stop_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("Stop", []) for h in g["hooks"]]


def _ups_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("UserPromptSubmit", []) for h in g["hooks"]]


def _hook_entries(data: dict, event: str) -> list[dict]:
    return [h for g in data["hooks"].get(event, []) for h in g["hooks"]]


def _checkpoint_groups(data: dict, event: str, client: str) -> list[dict]:
    marker = (
        "exomem_continuation_checkpoint.py"
        if client == "codex"
        else "exomem-continuation-checkpoint.sh"
    )
    return [
        group
        for group in data["hooks"].get(event, [])
        if any(marker in hook.get("command", "") for hook in group.get("hooks", []))
    ]


# --- install_hook: the installer (both hooks, py + wrapper) ----------------------


def test_install_hook_copies_scripts_and_wrappers_and_wires_both(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp)
    for f in (
        "exomem_capture_nudge.py",
        "exomem-capture-nudge.sh",
        "exomem_retrieve_nudge.py",
        "exomem-retrieve-nudge.sh",
    ):
        assert (hd / f).exists(), f
    assert r["wired"] is True
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert any("exomem-capture-nudge.sh" in c for c in _stop_cmds(data))
    assert any("exomem-retrieve-nudge.sh" in c for c in _ups_cmds(data))


def test_command_is_machine_agnostic(tmp_path: Path) -> None:
    # Default location -> ~-relative bash command (no abs path / interpreter / backslash),
    # so the same settings.json works on every machine after a yadm sync.
    cmd = hook_module._command_for("exomem-capture-nudge.sh", hook_module.DEFAULT_HOOK_DIR)
    assert cmd == "bash ~/.claude/hooks/exomem-capture-nudge.sh"
    assert "\\" not in cmd and "python" not in cmd.lower() and ":" not in cmd
    # Custom dir -> POSIX (forward-slash) bash command, never Windows backslashes.
    custom = hook_module._command_for("exomem-capture-nudge.sh", tmp_path / "hooks")
    assert custom.startswith('bash "') and custom.endswith('.sh"') and "\\" not in custom

    codex = hook_module._command_for(
        "exomem-retrieve-nudge.sh",
        hook_module.DEFAULT_CODEX_HOOK_DIR,
        client="codex",
        script="exomem_retrieve_nudge.py",
    )
    assert codex == "python3 ~/.codex/hooks/exomem_retrieve_nudge.py"


def test_install_hook_idempotent(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert sum("exomem-capture-nudge" in c for c in _stop_cmds(data)) == 1
    assert sum("exomem-retrieve-nudge" in c for c in _ups_cmds(data)) == 1


def test_install_hook_supersedes_prior_absolute_path_entry(tmp_path: Path) -> None:
    # The old (buggy) form baked an absolute Windows python path; re-running must
    # replace it with the machine-agnostic wrapper command, exactly once.
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        '"C:\\Python\\python.exe" "C:\\Users\\x\\.claude\\hooks\\'
                                        'kb_capture_nudge.py"'
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    stop = _stop_cmds(data)
    assert not any("python.exe" in c for c in stop)  # absolute-path form gone
    assert sum("exomem-capture-nudge" in c for c in stop) == 1  # exactly one, the wrapper


def test_install_hook_migrates_old_kb_entry(tmp_path: Path) -> None:
    # A machine that installed the pre-rename hook has a `kb-capture-nudge.sh`
    # wrapper command wired in. Re-running install-hook must STRIP that legacy entry
    # (via the retained old _MARKERS) and leave only the new `exomem-capture-nudge`
    # one — a clean migration, not a duplicate.
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ~/.claude/hooks/kb-capture-nudge.sh",
                                }
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ~/.claude/hooks/kb-retrieve-nudge.sh",
                                }
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    stop, ups = _stop_cmds(data), _ups_cmds(data)
    assert not any("kb-capture-nudge" in c for c in stop)  # legacy entry gone
    assert not any("kb-retrieve-nudge" in c for c in ups)
    assert sum("exomem-capture-nudge" in c for c in stop) == 1  # new entry present, once
    assert sum("exomem-retrieve-nudge" in c for c in ups) == 1


def test_install_hook_codex_migrates_old_kb_entries_and_preserves_other_hooks(
    tmp_path: Path,
) -> None:
    hd, sp = tmp_path / "codex-hooks", tmp_path / "hooks.json"
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "python guard.py"}],
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.codex/hooks/kb_retrieve_nudge.py",
                                }
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "python3 ~/.codex/hooks/zellij_tab_context_rename.py"
                                    ),
                                }
                            ]
                        },
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.codex/hooks/kb_capture_nudge.py",
                                }
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    assert r["client"] == "codex"
    for f in (
        "exomem_capture_nudge.py",
        "exomem-capture-nudge.sh",
        "exomem_retrieve_nudge.py",
        "exomem-retrieve-nudge.sh",
    ):
        assert (hd / f).exists(), f
    data = json.loads(sp.read_text(encoding="utf-8"))
    capture_command = 'python3 "' + (hd / "exomem_capture_nudge.py").as_posix() + '"'
    assert _stop_cmds(data).count(capture_command) == 1
    ups = _ups_cmds(data)
    assert sum("exomem_retrieve_nudge.py" in c for c in ups) == 1
    assert not any("kb_retrieve_nudge" in c for c in ups)
    assert not any("kb_capture_nudge" in c for c in _stop_cmds(data))
    assert "exomem_retrieve_nudge.py" in ups[0]
    assert "zellij_tab_context_rename.py" in ups[1]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python guard.py"
    retrieve = [
        h
        for h in _hook_entries(data, "UserPromptSubmit")
        if "exomem_retrieve_nudge.py" in h["command"]
    ][0]
    assert retrieve["commandWindows"].endswith('exomem_retrieve_nudge.py"')


def test_install_hook_preserves_other_hooks_and_keys(tmp_path: Path) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "bash guard.sh"}],
                        }
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "bash other-stop.sh"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "bash guard.sh"
    assert "bash other-stop.sh" in _stop_cmds(data)  # unrelated Stop hook kept
    assert any("exomem-capture-nudge" in c for c in _stop_cmds(data))  # ours added
    assert any("exomem-retrieve-nudge" in c for c in _ups_cmds(data))


def test_install_hook_print_only_leaves_settings(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, wire=False)
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem-capture-nudge.sh").exists()
    assert r["wired"] is False
    assert not sp.exists()
    snip = hook_module.snippet(r["installed"])
    assert "Stop" in snip and "UserPromptSubmit" in snip


def test_install_hook_via_cli(tmp_path: Path) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    assert main(["install-hook", "--hook-dir", str(hd), "--settings", str(sp)]) == 0
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem-retrieve-nudge.sh").exists()
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"] and data["hooks"]["UserPromptSubmit"]


def test_install_hook_via_cli_for_codex(tmp_path: Path) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    assert (
        main(
            [
                "install-hook",
                "--client",
                "codex",
                "--hook-dir",
                str(hd),
                "--settings",
                str(sp),
            ]
        )
        == 0
    )
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem_retrieve_nudge.py").exists()
    data = json.loads(sp.read_text(encoding="utf-8"))
    entries = _hook_entries(data, "UserPromptSubmit")
    assert any(
        "exomem_retrieve_nudge.py" in h["command"] and h.get("commandWindows") for h in entries
    )


def test_install_hook_check_reports_healthy_codex_install(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)

    assert report["success"] is True
    client = report["clients"][0]
    assert client["client"] == "codex"
    assert any(
        c["id"] == "config.UserPromptSubmit" and c["status"] == "pass" for c in client["checks"]
    )
    assert any(c["id"] == "scripts.Stop" and c["status"] == "pass" for c in client["checks"])
    assert client["logs"]["retrieve"]["path"].startswith(str(home / ".codex"))


@pytest.mark.parametrize(
    "drift",
    ["command", "commandWindows", "timeout", "matcher", "duplicate"],
)
def test_install_hook_check_requires_one_exact_continuation_registration(
    tmp_path: Path,
    drift: str,
) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    data = json.loads(sp.read_text(encoding="utf-8"))
    groups = data["hooks"]["PreCompact"]
    group = _checkpoint_groups(data, "PreCompact", "codex")[0]
    entry = group["hooks"][0]
    if drift == "command":
        entry["command"] = entry["command"].replace(str(hd), str(tmp_path / "wrong"))
    elif drift == "commandWindows":
        entry["commandWindows"] += ".wrong"
    elif drift == "timeout":
        entry["timeout"] = 6
    elif drift == "matcher":
        group["matcher"] = "manual"
    else:
        groups.append(json.loads(json.dumps(group)))
    groups.append(
        {
            "matcher": "anything",
            "hooks": [
                {
                    "type": "command",
                    "command": "python unrelated.py",
                    "timeout": 99,
                }
            ],
        }
    )
    sp.write_text(json.dumps(data), encoding="utf-8")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)

    assert report["success"] is False
    check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.PreCompact")
    assert check["status"] == "fail"


@pytest.mark.parametrize(
    ("client", "alternate_basename", "preferred_basename"),
    [
        ("claude", "exomem_continuation_checkpoint.py", "exomem-continuation-checkpoint.sh"),
        ("codex", "exomem-continuation-checkpoint.sh", "exomem_continuation_checkpoint.py"),
    ],
)
def test_reinstall_normalizes_same_client_alternate_continuation_basename(
    tmp_path: Path,
    client: str,
    alternate_basename: str,
    preferred_basename: str,
) -> None:
    hd = tmp_path / "hooks"
    sp = tmp_path / ("settings.json" if client == "claude" else "hooks.json")
    hooks: dict[str, list[dict]] = {}
    for event, matcher in hook_module._CONTINUATION_EVENTS[client]:
        group: dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": f"python {alternate_basename} --client {client}",
                    "timeout": 5,
                },
                {
                    "type": "command",
                    "command": f"python {alternate_basename} --client wrong-client",
                    "timeout": 5,
                },
                {
                    "type": "command",
                    "command": f"python {alternate_basename}.backup --client {client}",
                    "timeout": 5,
                },
            ]
        }
        if matcher is not None:
            group["matcher"] = matcher
        hooks[event] = [group]
    sp.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    hook_module.install_hook(hook_dir=hd, settings_path=sp, client=client)
    data = json.loads(sp.read_text(encoding="utf-8"))

    for event, _matcher in hook_module._CONTINUATION_EVENTS[client]:
        commands = [entry["command"] for entry in _hook_entries(data, event)]
        assert (
            sum(
                preferred_basename in command and f"--client {client}" in command
                for command in commands
            )
            == 1
        )
        assert not any(
            alternate_basename in command
            and f"--client {client}" in command
            and ".backup" not in command
            for command in commands
        )
        assert any("--client wrong-client" in command for command in commands)
        assert any(".backup" in command for command in commands)

    report = hook_module.check_hooks(clients=(client,), hook_dir=hd, settings_path=sp)
    assert report["success"] is True


def test_install_hook_check_rejects_unsupported_codex_session_end(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    data = json.loads(sp.read_text(encoding="utf-8"))
    entry = json.loads(json.dumps(_checkpoint_groups(data, "PreCompact", "codex")[0]["hooks"][0]))
    data["hooks"]["SessionEnd"] = [{"hooks": [entry]}]
    sp.write_text(json.dumps(data), encoding="utf-8")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)

    check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.SessionEnd")
    assert check["status"] == "fail"


def test_install_hook_check_flags_stale_deployed_copy(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    (hd / "exomem_retrieve_nudge.py").write_text("# stale\n", encoding="utf-8")

    report = hook_module.check_hooks(clients=("claude",), hook_dir=hd, settings_path=sp)

    assert report["success"] is False
    checks = report["clients"][0]["checks"]
    stale = [c for c in checks if c["id"] == "scripts.UserPromptSubmit"][0]
    assert stale["status"] == "fail"
    assert "differ from bundled source" in stale["message"]


def test_install_hook_check_flags_legacy_kb_config(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    data["hooks"]["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/kb-retrieve-nudge.sh"}]}
    ]
    sp.write_text(json.dumps(data), encoding="utf-8")

    report = hook_module.check_hooks(clients=("claude",), hook_dir=hd, settings_path=sp)

    assert report["success"] is False
    checks = report["clients"][0]["checks"]
    assert [c for c in checks if c["id"] == "config.legacy"][0]["status"] == "fail"
    assert [c for c in checks if c["id"] == "config.UserPromptSubmit"][0]["status"] == "fail"


def test_install_hook_check_via_cli_json(tmp_path: Path, capsys) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    assert (
        main(
            [
                "install-hook",
                "--check",
                "--client",
                "codex",
                "--hook-dir",
                str(hd),
                "--settings",
                str(sp),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["clients"][0]["client"] == "codex"


def test_hook_check_skips_absent_client_in_multi_client_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_hooks = tmp_path / ".claude" / "hooks"
    claude_settings = tmp_path / ".claude" / "settings.json"
    hook_module.install_hook(
        hook_dir=claude_hooks,
        settings_path=claude_settings,
        client="claude",
    )

    monkeypatch.setattr(
        hook_module,
        "_default_hook_dir",
        lambda client: claude_hooks if client == "claude" else tmp_path / ".codex" / "hooks",
    )
    monkeypatch.setattr(
        hook_module,
        "_default_settings",
        lambda client: (
            claude_settings if client == "claude" else tmp_path / ".codex" / "hooks.json"
        ),
    )

    report = hook_module.check_hooks(clients=("claude", "codex"))
    assert report["success"] is True
    codex = next(item for item in report["clients"] if item["client"] == "codex")
    assert codex["status"] == "skipped"
    assert codex["success"] is True


def test_hook_check_explicit_absent_client_remains_strict(tmp_path: Path) -> None:
    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=tmp_path / "hooks",
        settings_path=tmp_path / "hooks.json",
    )
    assert report["success"] is False
    assert report["clients"][0]["status"] == "failed"


# --- continuation checkpoint installer -----------------------------------------


@pytest.mark.parametrize(
    ("client", "events"),
    [
        ("claude", ("PreCompact", "SessionEnd", "SessionStart")),
        ("codex", ("PreCompact", "SessionStart")),
    ],
)
def test_install_hook_wires_pinned_continuation_matrix(
    tmp_path: Path, client: str, events: tuple[str, ...]
) -> None:
    hd = tmp_path / client / "hooks"
    sp = tmp_path / client / ("hooks.json" if client == "codex" else "settings.json")

    result = hook_module.install_hook(hook_dir=hd, settings_path=sp, client=client)
    data = json.loads(sp.read_text(encoding="utf-8"))

    assert (hd / "exomem_continuation_checkpoint.py").is_file()
    assert (hd / "exomem-continuation-checkpoint.sh").is_file()
    assert set(event for event in events if _checkpoint_groups(data, event, client)) == set(events)
    assert not _checkpoint_groups(data, "SessionEnd", "codex")
    assert _checkpoint_groups(data, "PreCompact", client)[0]["matcher"] == "manual|auto"
    assert _checkpoint_groups(data, "SessionStart", client)[0]["matcher"] == "compact|resume"
    if client == "claude":
        assert "matcher" not in _checkpoint_groups(data, "SessionEnd", client)[0]
    installed = [row for row in result["installed"] if row.get("kind") == "continuation"]
    assert {row["event"] for row in installed} == set(events)
    assert all(f"--client {client}" in row["command"] for row in installed)
    if client == "codex":
        assert all(f"--client {client}" in row["commandWindows"] for row in installed)
    else:
        assert all(row["commandWindows"] is None for row in installed)


def test_codex_preserves_unrelated_session_end_and_exact_marker_neighbors(tmp_path: Path) -> None:
    sp = tmp_path / "hooks.json"
    unrelated = {"hooks": [{"type": "command", "command": "python user-session-end.py"}]}
    neighbor = {
        "matcher": "manual|auto",
        "hooks": [
            {
                "type": "command",
                "command": "python exomem_continuation_checkpoint.py.backup --client codex",
            }
        ],
    }
    wrong_client = {
        "matcher": "manual|auto",
        "hooks": [
            {
                "type": "command",
                "command": "python exomem_continuation_checkpoint.py --client claude",
            }
        ],
    }
    legacy = {
        "matcher": "manual|auto",
        "hooks": [{"type": "command", "command": "python kb_continuation_checkpoint.py"}],
    }
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [unrelated],
                    "PreCompact": [neighbor, wrong_client, legacy],
                }
            }
        ),
        encoding="utf-8",
    )

    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp, client="codex")
    data = json.loads(sp.read_text(encoding="utf-8"))

    assert data["hooks"]["SessionEnd"] == [unrelated]
    commands = [hook["command"] for group in data["hooks"]["PreCompact"] for hook in group["hooks"]]
    assert commands[0].endswith(".backup --client codex")
    assert commands[1].endswith("--client claude")
    assert not any("kb_continuation_checkpoint.py" in command for command in commands)
    assert (
        sum(
            "exomem_continuation_checkpoint.py" in command
            and "--client codex" in command
            and not command.endswith(".backup --client codex")
            for command in commands
        )
        == 1
    )


@pytest.mark.parametrize(
    ("client", "variable", "config_name"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", "settings.json"),
        ("codex", "CODEX_HOME", "hooks.json"),
    ],
)
def test_client_specific_and_shared_home_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    variable: str,
    config_name: str,
) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv(variable, str(configured))
    result = hook_module.install_hook(client=client)
    assert Path(result["settings"]) == configured / config_name
    assert (configured / "hooks" / "exomem_continuation_checkpoint.py").exists()

    shared = tmp_path / "shared"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(shared))
    result = hook_module.install_hook(client=client)
    assert Path(result["settings"]) == shared / config_name
    assert (shared / "hooks" / "exomem_continuation_checkpoint.py").exists()


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"null"])
def test_config_parse_fails_closed_without_replacement_or_backup(
    tmp_path: Path,
    raw: bytes,
) -> None:
    sp = tmp_path / "settings.json"
    sp.write_bytes(raw)

    with pytest.raises(ValueError):
        hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)

    assert sp.read_bytes() == raw
    assert not list(tmp_path.glob("settings.json.backup-*"))


def test_normalized_reinstall_does_not_rewrite_or_backup(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    before = (sp.read_bytes(), sp.stat().st_mtime_ns, sp.stat().st_ino)

    result = hook_module.install_hook(hook_dir=hd, settings_path=sp)

    assert (sp.read_bytes(), sp.stat().st_mtime_ns, sp.stat().st_ino) == before
    assert result["config_changed"] is False
    assert result["backup"] is None
    assert not list(tmp_path.glob("settings.json.backup-*"))


def test_real_config_change_creates_unique_mode_preserving_backup(tmp_path: Path) -> None:
    sp = tmp_path / "settings.json"
    original = b'{"theme":"dark","hooks":{}}\n'
    sp.write_bytes(original)
    sp.chmod(0o640)

    first = hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    backup = Path(first["backup"])

    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o640
    assert sp.stat().st_mode & 0o777 == 0o640
    assert backup.parent == sp.parent
    assert first["config_changed"] is True


def test_postcommit_replace_error_retains_backup_and_committed_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem._hooks import exomem_continuation_checkpoint as safe

    sp = tmp_path / "settings.json"
    original = b'{"owner":"original","hooks":{}}\n'
    sp.write_bytes(original)
    installed = hook_module._continuation_items(tmp_path / "hooks", "codex")
    real_replace = safe._replace_at

    def replace_then_raise(directory, source: str, destination: str) -> None:
        real_replace(directory, source, destination)
        raise OSError("simulated directory fsync failure after commit")

    monkeypatch.setattr(safe, "_replace_at", replace_then_raise)
    with pytest.raises(OSError, match="after commit"):
        hook_module._merge_hooks(sp, installed, timeout=10)

    committed = json.loads(sp.read_text(encoding="utf-8"))
    assert committed["owner"] == "original"
    assert _checkpoint_groups(committed, "PreCompact", "codex")
    backups = list(tmp_path.glob("settings.json.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not list(tmp_path.glob(".settings.json.tmp-*"))


def test_observed_config_drift_retries_from_fresh_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text('{"hooks":{}}\n', encoding="utf-8")
    real = hook_module._snapshot_config_at
    calls = 0

    def drift_once(directory, name: str, display_path: Path):
        nonlocal calls
        snapshot = real(directory, name, display_path)
        calls += 1
        if calls == 1:
            sp.write_text('{"theme":"newer","hooks":{}}\n', encoding="utf-8")
        return snapshot

    monkeypatch.setattr(hook_module, "_snapshot_config_at", drift_once)

    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    assert json.loads(sp.read_text(encoding="utf-8"))["theme"] == "newer"
    assert calls >= 4


def test_persistent_config_drift_fails_without_stale_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text('{"counter":0,"hooks":{}}\n', encoding="utf-8")
    real = hook_module._snapshot_config_at
    calls = 0

    def always_drift(directory, name: str, display_path: Path):
        nonlocal calls
        snapshot = real(directory, name, display_path)
        calls += 1
        if calls % 2:
            sp.write_text(json.dumps({"counter": calls, "hooks": {}}), encoding="utf-8")
        return snapshot

    monkeypatch.setattr(hook_module, "_snapshot_config_at", always_drift)

    with pytest.raises(RuntimeError, match="concurrent"):
        hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    assert json.loads(sp.read_text(encoding="utf-8"))["counter"] > 0
    assert not list(tmp_path.glob("settings.json.backup-*"))
    assert not list(tmp_path.glob(".settings.json.tmp-*"))


def test_drift_observed_after_backup_cleans_attempt_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text('{"counter":0,"hooks":{}}\n', encoding="utf-8")
    real = hook_module._snapshot_config_at
    calls = 0

    def drift_before_final(directory, name: str, display_path: Path):
        nonlocal calls
        calls += 1
        if calls % 4 == 0:
            sp.write_text(json.dumps({"counter": calls, "hooks": {}}), encoding="utf-8")
        return real(directory, name, display_path)

    monkeypatch.setattr(hook_module, "_snapshot_config_at", drift_before_final)

    with pytest.raises(RuntimeError, match="concurrent"):
        hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    assert not list(tmp_path.glob("settings.json.backup-*"))
    assert not list(tmp_path.glob(".settings.json.tmp-*"))


@pytest.mark.skipif(os.name == "nt", reason="Windows parent handles prevent the test rename")
def test_config_merge_stays_bound_to_opened_parent_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "config-parent"
    parent.mkdir()
    config = parent / "hooks.json"
    config.write_text('{"owner":"original","hooks":{}}\n', encoding="utf-8")
    moved = tmp_path / "original-moved"
    attacker_raw = b'{"owner":"attacker","hooks":{}}\n'
    real_merge = hook_module._merged_config
    swapped = False

    def swap_parent(source: dict, installed: list[dict], timeout: int) -> dict:
        nonlocal swapped
        result = real_merge(source, installed, timeout)
        if not swapped:
            swapped = True
            parent.rename(moved)
            parent.mkdir()
            (parent / "hooks.json").write_bytes(attacker_raw)
        return result

    monkeypatch.setattr(hook_module, "_merged_config", swap_parent)
    installed = hook_module._continuation_items(tmp_path / "hooks", "codex")

    result = hook_module._merge_hooks(config, installed, timeout=10)

    assert result["changed"] is True
    assert (parent / "hooks.json").read_bytes() == attacker_raw
    original = json.loads((moved / "hooks.json").read_text(encoding="utf-8"))
    assert original["owner"] == "original"
    assert _checkpoint_groups(original, "PreCompact", "codex")


def test_symlinked_config_is_refused_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"untouched":true}', encoding="utf-8")
    link = tmp_path / "settings.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(OSError):
        hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=link)
    assert target.read_text(encoding="utf-8") == '{"untouched":true}'


def test_all_client_cli_isolated_partial_failure_and_override_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exomem.__main__ import main

    shared = tmp_path / "homes"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(shared))
    # all-client shared overrides are deliberately rejected before any write
    with pytest.raises(SystemExit):
        main(["install-hook", "--client", "all", "--hook-dir", str(tmp_path / "hooks")])

    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    monkeypatch.delenv("EXOMEM_HOOK_HOME")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    codex.mkdir()
    (codex / "hooks.json").write_text("{broken", encoding="utf-8")

    assert main(["install-hook", "--client", "all", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert next(row for row in payload["clients"] if row["client"] == "claude")["success"] is True
    assert next(row for row in payload["clients"] if row["client"] == "codex")["success"] is False
    assert (claude / "settings.json").exists()
    assert (codex / "hooks.json").read_text(encoding="utf-8") == "{broken"


def test_health_check_reports_capability_matrix_hashes_and_first_run(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
    checks = report["clients"][0]["checks"]

    assert report["success"] is True
    assert any(row["id"] == "config.PreCompact" and row["status"] == "pass" for row in checks)
    assert any(row["id"] == "config.SessionStart" and row["status"] == "pass" for row in checks)
    assert any(row["id"] == "config.SessionEnd" and row["status"] == "pass" for row in checks)
    assert any(row["id"] == "scripts.continuation" and row["status"] == "pass" for row in checks)
    assert any(row["id"] == "runtime.continuation" and row["status"] == "warn" for row in checks)


def test_installer_capability_matrix_matches_pinned_adapter_provenance() -> None:
    for client, provenance in checkpoint.ADAPTER_PROVENANCE.items():
        assert (
            tuple(event for event, _matcher in hook_module._CONTINUATION_EVENTS[client])
            == provenance["events"]
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX deployment modes")
def test_install_deploys_restrictive_regular_hook_files(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    for deployed in hd.iterdir():
        info = deployed.lstat()
        assert not deployed.is_symlink()
        assert deployed.is_file()
        assert info.st_mode & 0o077 == 0


def test_health_rejects_matching_deployed_hook_symlink(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    deployed = hd / "exomem_continuation_checkpoint.py"
    deployed.unlink()
    try:
        deployed.symlink_to(_HOOKS / "exomem_continuation_checkpoint.py")
    except OSError:
        pytest.skip("symlinks unavailable")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "scripts.continuation"
    )

    assert check["status"] == "fail"
    assert check["details"]["exomem_continuation_checkpoint.py"]["safe_regular"] is False


def test_health_rejects_deployed_hook_directory_with_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    hd, sp = real_parent / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    alias_parent = tmp_path / "alias"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=alias_parent / "hooks",
        settings_path=sp,
    )
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "scripts.continuation"
    )

    assert check["status"] == "fail"
    assert all(not item["safe_regular"] for item in check["details"].values())


def test_health_rejects_symlinked_config_leaf(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    target = tmp_path / "real-hooks.json"
    sp.replace(target)
    try:
        sp.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
    check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.file")

    assert check["status"] == "fail"
    assert "unsafe" in check["message"].lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode trust")
def test_install_and_health_reject_group_writable_hook_directory(tmp_path: Path) -> None:
    hd = tmp_path / "hooks"
    hd.mkdir(mode=0o700)
    hd.chmod(0o777)

    with pytest.raises(OSError, match="trusted|writable|permissions"):
        hook_module.install_hook(
            hook_dir=hd,
            settings_path=tmp_path / "hooks.json",
            client="codex",
            wire=False,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode trust")
def test_health_rejects_group_writable_hook_directory(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    hd.chmod(0o777)

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "scripts.continuation"
    )

    assert check["status"] == "fail"
    assert all(not item["safe_regular"] for item in check["details"].values())


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor ownership and mode trust")
def test_install_rejects_replaceable_nonsticky_ancestor_with_safe_child(
    tmp_path: Path,
) -> None:
    broad = tmp_path / "replaceable"
    broad.mkdir(mode=0o700)
    broad.chmod(0o777)
    hook_dir = broad / "hooks"
    hook_dir.mkdir(mode=0o700)
    config_dir = broad / "config"
    config_dir.mkdir(mode=0o700)

    with pytest.raises(OSError, match="unsafe|writable|trusted"):
        hook_module.install_hook(
            hook_dir=hook_dir,
            settings_path=tmp_path / "safe-settings.json",
            client="codex",
            wire=False,
        )
    with pytest.raises(OSError, match="unsafe|writable|trusted"):
        hook_module.install_hook(
            hook_dir=tmp_path / "safe-hooks",
            settings_path=config_dir / "hooks.json",
            client="codex",
        )

    safe_root = tmp_path / "safe-isolated"
    result = hook_module.install_hook(
        hook_dir=safe_root / "hooks",
        settings_path=safe_root / "hooks.json",
        client="codex",
    )
    assert Path(result["settings"]).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor ownership and mode trust")
def test_health_rejects_replaceable_nonsticky_ancestor_after_install(
    tmp_path: Path,
) -> None:
    container = tmp_path / "container"
    hook_dir = container / "hooks"
    settings = container / "config" / "hooks.json"
    hook_module.install_hook(hook_dir=hook_dir, settings_path=settings, client="codex")
    container.chmod(0o777)

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=hook_dir,
        settings_path=settings,
    )
    config_check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.file")
    script_check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "scripts.continuation"
    )

    assert config_check["status"] == "fail"
    assert script_check["status"] == "fail"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode trust")
def test_install_rejects_group_writable_config_parent_and_leaf(tmp_path: Path) -> None:
    hd = tmp_path / "hooks"
    broad_parent = tmp_path / "broad-config"
    broad_parent.mkdir(mode=0o700)
    broad_parent.chmod(0o777)
    with pytest.raises(OSError, match="trusted|writable|permissions"):
        hook_module.install_hook(
            hook_dir=hd,
            settings_path=broad_parent / "hooks.json",
            client="codex",
        )

    safe_parent = tmp_path / "safe-config"
    safe_parent.mkdir(mode=0o700)
    config = safe_parent / "hooks.json"
    config.write_text("{}\n", encoding="utf-8")
    config.chmod(0o666)
    with pytest.raises(OSError, match="writable|permissions"):
        hook_module.install_hook(
            hook_dir=hd,
            settings_path=config,
            client="codex",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX config mode trust")
def test_health_rejects_group_writable_config_leaf(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")
    sp.chmod(0o666)

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
    check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.file")

    assert check["status"] == "fail"
    assert "writable" in check["message"].lower()

    sp.chmod(0o644)
    tmp_path.chmod(0o777)
    try:
        report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)
        check = next(row for row in report["clients"][0]["checks"] if row["id"] == "config.file")
        assert check["status"] == "fail"
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode diagnostics")
def test_health_reports_broad_state_directory_and_lock_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    result = hook_module.install_hook(client="codex")
    checkpoint.write_checkpoint(
        {
            "contract_version": 1,
            "client": "codex",
            "event": "PreCompact",
            "session_id": "broad",
            "turn_id": None,
            "trigger": "manual",
            "source": None,
            "cwd": None,
            "transcript_path": None,
            "model": None,
        },
        home,
    )
    root = checkpoint.client_state_root(home, "codex")
    state = checkpoint.session_state_dir(home, "codex", "broad")
    root.chmod(0o777)
    state.chmod(0o777)
    (root / ".root.lock").chmod(0o666)
    (state / ".lock").chmod(0o666)

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=Path(result["installed"][0]["script"]).parent,
        settings_path=Path(result["settings"]),
    )
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "runtime.continuation"
    )

    assert check["status"] == "fail"
    assert check["details"]["permissions_ok"] is False
    assert {"root", ".root.lock", "session", ".lock"}.issubset(
        set(check["details"]["permission_violations"])
    )


def test_health_decodes_current_and_reports_valid_previous_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    result = hook_module.install_hook(client="codex")
    event = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "diagnostic",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    checkpoint.write_checkpoint(
        event,
        home,
        observed_at_ns=time.time_ns() - 10 * 1_000_000_000,
    )
    checkpoint.write_checkpoint(
        {**event, "trigger": "auto"}, home, observed_at_ns=time.time_ns() - 1
    )
    state = checkpoint.session_state_dir(home, "codex", "diagnostic")
    (state / "current.json").write_bytes(b"not-json")

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=home / "hooks",
        settings_path=Path(result["settings"]),
    )
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "runtime.continuation"
    )
    session = check["details"]["session_states"][0]

    assert check["status"] == "fail"
    assert session["current"] == "corrupt"
    assert session["previous"] == "valid"
    assert session["selection"] == "rollback_previous"
    assert check["details"]["latest_age"] != "0s ago"


def test_health_rejects_checkpoint_copied_into_another_session_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    result = hook_module.install_hook(client="codex")
    base = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    checkpoint.write_checkpoint(
        {**base, "session_id": "alpha"}, home, observed_at_ns=time.time_ns()
    )
    checkpoint.write_checkpoint({**base, "session_id": "beta"}, home, observed_at_ns=time.time_ns())
    alpha = checkpoint.session_state_dir(home, "codex", "alpha")
    beta = checkpoint.session_state_dir(home, "codex", "beta")
    (beta / "current.json").write_bytes((alpha / "current.json").read_bytes())

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=home / "hooks",
        settings_path=Path(result["settings"]),
    )
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "runtime.continuation"
    )
    beta_status = next(
        row for row in check["details"]["session_states"] if row["name"] == beta.name
    )

    assert check["status"] == "fail"
    assert beta_status["current"] == "binding_invalid"
    assert beta_status["selection"] == "corrupt"


def test_health_and_live_selection_reject_inverted_generation_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    observed = time.time_ns()
    event = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "health-inverted",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    checkpoint.write_checkpoint(event, home, observed_at_ns=observed - 2)
    checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=observed - 1)
    state = checkpoint.session_state_dir(home, "codex", "health-inverted")
    current = (state / "current.json").read_bytes()
    previous = (state / "previous.json").read_bytes()
    (state / "current.json").write_bytes(previous)
    (state / "previous.json").write_bytes(current)
    start = {
        **event,
        "event": "SessionStart",
        "trigger": None,
        "source": "resume",
    }

    runtime = hook_module._continuation_runtime_summary("codex")

    assert runtime["session_states"][0]["current"] == "generation_invalid"
    assert runtime["session_states"][0]["selection"] == "corrupt"
    assert checkpoint.select_checkpoint(start, home, now_ns=observed) is None


def test_health_rejects_duplicate_current_and_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    observed = time.time_ns()
    event = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "health-duplicate",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    checkpoint.write_checkpoint(event, home, observed_at_ns=observed)
    state = checkpoint.session_state_dir(home, "codex", "health-duplicate")
    shutil.copy2(state / "current.json", state / "previous.json")

    runtime = hook_module._continuation_runtime_summary("codex")

    assert runtime["session_states"][0]["current"] == "generation_invalid"
    assert runtime["session_states"][0]["selection"] == "corrupt"


def test_health_surfaces_stale_previous_behind_valid_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    event = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "health-stale-history",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    now = time.time_ns()
    checkpoint.write_checkpoint(event, home, observed_at_ns=now - checkpoint.RETENTION_NS - 2)
    checkpoint.write_checkpoint(
        {**event, "trigger": "auto"},
        home,
        observed_at_ns=now - checkpoint.RETENTION_NS - 1,
    )
    checkpoint.write_checkpoint({**event, "turn_id": "fresh"}, home, observed_at_ns=now)

    runtime = hook_module._continuation_runtime_summary("codex")

    assert runtime["session_states"][0]["selection"] == "valid_current"
    assert runtime["session_states"][0]["history"] == "stale_previous"


def test_health_identifies_authorized_stale_interrupted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    with checkpoint._session_lock(
        home,
        "codex",
        "health-interrupted",
        create=True,
        created_at_ns=time.time_ns() - checkpoint.RETENTION_NS - 1,
    ):
        pass

    runtime = hook_module._continuation_runtime_summary("codex")

    assert runtime["session_states"][0]["selection"] == "stale_incomplete"
    assert runtime["session_states"][0]["manifest"] == "valid"


@pytest.mark.parametrize(
    "row",
    [
        {
            "event": "PreCompact/SECRET",
            "status": "written",
            "duration_ms": 0,
            "checkpoint_id": "a" * 64,
        },
        {
            "event": "PreCompact",
            "status": "written /tmp/SECRET",
            "duration_ms": 0,
            "checkpoint_id": "a" * 64,
        },
        {"event": "PreCompact", "status": "written", "duration_ms": -1, "checkpoint_id": "a" * 64},
        {
            "event": "PreCompact",
            "status": "written",
            "duration_ms": 60_001,
            "checkpoint_id": "a" * 64,
        },
        {
            "event": "PreCompact",
            "status": "written",
            "duration_ms": 0,
            "checkpoint_id": "SECRET/path",
        },
        {
            "event": "PreCompact",
            "status": "error",
            "duration_ms": 0,
            "error_class": "Bearer /tmp/SECRET",
        },
        {
            "event": ["PreCompact"],
            "status": "written",
            "duration_ms": 0,
            "checkpoint_id": "a" * 64,
        },
        {"event": "SessionStart", "status": ["empty"], "duration_ms": 0},
    ],
)
def test_metadata_log_health_rejects_content_path_and_invalid_domains(
    tmp_path: Path,
    row: dict,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    log = root / "events.log"
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    log.chmod(0o600)

    summary = hook_module._metadata_log_runtime_summary(root)

    assert summary["status"] == "corrupt"


def test_health_reports_stale_missing_and_invalid_metadata_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    result = hook_module.install_hook(client="codex")
    event = {
        "contract_version": 1,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "stale",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
    }
    checkpoint.write_checkpoint(
        event,
        home,
        observed_at_ns=time.time_ns() - checkpoint.RETENTION_NS - 1,
    )
    root = checkpoint.client_state_root(home, "codex")
    missing = checkpoint.session_state_dir(home, "codex", "missing")
    missing.mkdir(mode=0o700)
    (missing / ".lock").write_bytes(b"\0")
    (missing / ".lock").chmod(0o600)
    log = root / "events.log"
    log.write_bytes(b"{bad\n")
    log.chmod(0o600)

    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=home / "hooks",
        settings_path=Path(result["settings"]),
    )
    check = next(
        row for row in report["clients"][0]["checks"] if row["id"] == "runtime.continuation"
    )
    states = {row["selection"] for row in check["details"]["session_states"]}

    assert check["status"] == "fail"
    assert {"stale", "missing"}.issubset(states)
    assert check["details"]["metadata_log"]["status"] == "corrupt"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_isolated_installed_continuation_adapter_and_config_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
) -> None:
    home = tmp_path / f"{client} home"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    result = hook_module.install_hook(client=client)
    config = json.loads(Path(result["settings"]).read_text(encoding="utf-8"))
    script = home / "hooks" / "exomem_continuation_checkpoint.py"
    wrapper = home / "hooks" / "exomem-continuation-checkpoint.sh"
    command = [sys.executable, str(script), "--client", client]
    if client == "claude":
        command = ["bash", str(wrapper), "--client", client]

    assert _checkpoint_groups(config, "PreCompact", client)
    assert _checkpoint_groups(config, "SessionStart", client)
    assert bool(_checkpoint_groups(config, "SessionEnd", client)) == (client == "claude")

    transcript = tmp_path / f"{client}.jsonl"
    transcript.write_text("private installed-adapter content", encoding="utf-8")
    env = {**os.environ, "EXOMEM_HOOK_HOME": str(home), "EXOMEM_VAULT_PATH": ""}
    written = subprocess.run(
        command,
        input=json.dumps(
            {
                "hook_event_name": "PreCompact",
                "session_id": "installed-session",
                "trigger": "manual",
                "transcript_path": str(transcript),
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=True,
    )
    resumed = subprocess.run(
        command,
        input=json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": "installed-session",
                "source": "resume",
                "transcript_path": str(transcript),
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
        check=True,
    )

    assert written.stdout == ""
    assert "additionalContext" in resumed.stdout
    assert "private installed-adapter content" not in resumed.stdout
    if client == "claude":
        ended = subprocess.run(
            command,
            input=json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "installed-session-end",
                }
            ),
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
            check=True,
        )
        assert ended.stdout == ""


# --- shared subprocess helper ---------------------------------------------------


def _run(script: Path, event: dict, home: Path, extra_env: dict | None = None):
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        **(extra_env or {}),
    }  # redirect Path.home()
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )


def _transcript(
    tmp_path: Path,
    user_text: str,
    assistant_text: str | None = None,
    assistant_tool: str | None = None,
    assistant_tool_input: dict | None = None,
    assistant_tool_result: dict | None = None,
) -> Path:
    content: list[dict] = []
    if assistant_tool:
        content.append(
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": assistant_tool,
                "input": assistant_tool_input or {},
            }
        )
    if assistant_text is not None:
        content.append({"type": "text", "text": assistant_text})
    lines = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": content}},
    ]
    if assistant_tool_result is not None:
        lines.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            **assistant_tool_result,
                        }
                    ],
                },
            }
        )
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return p


# --- capture (Stop) gate --------------------------------------------------------


def test_capture_fires_on_substantial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(tmp_path, "q?", "We landed on a clear decision. " + "x" * 450)
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s1"}, home)
    assert '"decision": "block"' in r.stdout


def test_capture_fires_language_agnostic_japanese(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    jp = "これは重要な結論です。" * 40
    t = _transcript(tmp_path, "質問", jp)
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "jp"}, home)
    assert '"decision": "block"' in r.stdout


def test_capture_silent_on_trivial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(tmp_path, "q?", "Done.")
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s2"}, home)
    assert r.stdout.strip() == ""


def test_capture_silent_when_already_saved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # Real post-rename connector tool name is Exomem (not Knowledge_Base) — the guard
    # regex must recognise it, or the nudge misfires after every real capture.
    t = _transcript(
        tmp_path,
        "q?",
        "Big conclusion. " + "x" * 450,
        assistant_tool="mcp__claude_ai_Exomem__note",
    )
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s3"}, home)
    assert r.stdout.strip() == ""


def test_capture_silent_after_modern_create_entity_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(
        tmp_path,
        "q?",
        "A durable recurring entity was captured. " + "x" * 450,
        assistant_tool="mcp__claude_ai_Exomem__connect_memory",
        assistant_tool_input={"operation": "create-entity"},
    )

    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "entity"}, home)

    assert r.stdout.strip() == ""


def test_capture_still_fires_after_validate_only_edit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(
        tmp_path,
        "q?",
        "The validation landed on a durable conclusion. " + "x" * 450,
        assistant_tool="mcp__claude_ai_Exomem__edit_memory",
        assistant_tool_input={"validate_only": True},
    )

    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "preview"}, home)

    assert '"decision": "block"' in r.stdout


def test_capture_still_fires_after_failed_edit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(
        tmp_path,
        "q?",
        "The failed write still left a durable conclusion. " + "x" * 450,
        assistant_tool="mcp__claude_ai_Exomem__edit_memory",
        assistant_tool_input={"validate_only": False},
        assistant_tool_result={"is_error": True, "content": "STALE_EDIT"},
    )

    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "failed"}, home)

    assert '"decision": "block"' in r.stdout


def test_capture_reminder_routes_entities_conservatively() -> None:
    script = CAPTURE_SCRIPT.read_text(encoding="utf-8")

    assert "active entity registry" in script
    assert "selected knowledge packs" in script
    assert 'connect_memory(operation="resolve-entity"' in script
    assert "edit_memory" in script
    assert 'connect_memory(operation="create-entity")' in script
    assert "single incidental mention" in script
    assert "person, organization" not in script


def test_capture_silent_when_stop_hook_active(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    r = _run(
        CAPTURE_SCRIPT,
        {"transcript_path": str(t), "session_id": "s4", "stop_hook_active": True},
        home,
    )
    assert r.stdout.strip() == ""


def test_capture_cooldown_suppresses_second_fire(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    ev = {"transcript_path": str(t), "session_id": "cd"}
    first = _run(CAPTURE_SCRIPT, ev, home)
    second = _run(CAPTURE_SCRIPT, ev, home)
    assert '"decision": "block"' in first.stdout
    assert second.stdout.strip() == ""


def test_capture_codex_client_accepts_camel_case_and_uses_codex_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    t = _transcript(tmp_path, "q?", "We landed on a clear decision. " + "x" * 450)
    r = _run(
        CAPTURE_SCRIPT,
        {"transcriptPath": str(t), "sessionId": "codex-cap"},
        home,
        {"EXOMEM_HOOK_CLIENT": "codex"},
    )
    assert '"decision": "block"' in r.stdout
    assert (home / ".codex" / ".cache" / "exomem-nudge" / "codex-cap").exists()
    assert (home / ".codex" / "exomem-capture-nudge.log").exists()
    assert not (home / ".claude" / "exomem-capture-nudge.log").exists()


# --- retrieval (UserPromptSubmit) gate ------------------------------------------


def test_retrieve_fires_on_substantial_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run(
        RETRIEVE_SCRIPT,
        {
            "prompt": "what did I conclude about the kb hook design earlier?",
            "session_id": "r1",
        },
        home,
    )
    assert "additionalContext" in r.stdout


def test_retrieve_fires_language_agnostic_japanese(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    jp = "去年このプロジェクトについて何を結論づけましたか？詳しく教えてください。"
    r = _run(RETRIEVE_SCRIPT, {"prompt": jp, "session_id": "rjp"}, home)
    assert "additionalContext" in r.stdout


def test_retrieve_silent_on_short_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run(RETRIEVE_SCRIPT, {"prompt": "yes go", "session_id": "r2"}, home)
    assert r.stdout.strip() == ""


def test_retrieve_cooldown_suppresses_second_fire(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ev = {
        "prompt": "what did I conclude about the architecture decisions here?",
        "session_id": "rc",
    }
    first = _run(RETRIEVE_SCRIPT, ev, home)
    second = _run(RETRIEVE_SCRIPT, ev, home)
    assert "additionalContext" in first.stdout
    assert second.stdout.strip() == ""


def test_retrieve_codex_client_accepts_camel_case_and_uses_codex_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run(
        RETRIEVE_SCRIPT,
        {
            "userPrompt": "what did I conclude about the Codex hook setup earlier?",
            "sessionId": "codex-ret",
        },
        home,
        {"EXOMEM_HOOK_CLIENT": "codex"},
    )
    assert "additionalContext" in r.stdout
    assert (home / ".codex" / ".cache" / "exomem-nudge" / "retrieve_codex-ret").exists()
    assert (home / ".codex" / "exomem-retrieve-nudge.log").exists()
    assert not (home / ".claude" / "exomem-retrieve-nudge.log").exists()
