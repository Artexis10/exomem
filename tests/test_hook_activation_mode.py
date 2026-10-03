"""Canonical native-MCP activation stays transport-free and profile-local."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from benchmark_capabilities import require_posix_file_modes

from exomem import install_hook as installer
from exomem._hooks import exomem_capture_nudge as capture
from exomem._hooks import exomem_continuation_checkpoint as continuation
from exomem._hooks import exomem_retrieve_nudge as retrieve


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in tuple(os.environ):
        if name.startswith(("EXOMEM_", "KB_", "CODEX_", "CLAUDE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "absent-config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")


def test_mcp_turn_requests_native_activation_without_transport(monkeypatch, capsys):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    calls = []
    monkeypatch.setattr(
        retrieve, "_gather_hits_with_lane", lambda turn: calls.append(turn) or ([], "none")
    )
    turn = 'Check the decision about the adapter. Keep "this" raw.\nSecond line.'
    monkeypatch.setattr(
        retrieve.sys, "stdin", io.StringIO(json.dumps({"prompt": turn, "session_id": "s"}))
    )
    assert retrieve.main() == 0
    assert calls == []
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "bootstrap" in context and "activate_context" in context
    assert "raw user turn" in context and "once" in context
    assert "already" in context and "duplicate" in context


def test_mcp_cadence_survives_session_global_cooldown_and_short_resume(monkeypatch, capsys):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    for session, prompt in (
        ("one", "A substantive question about our adapter decisions"),
        ("one", "continue"),
        ("two", "where were we"),
    ):
        monkeypatch.setattr(
            retrieve.sys,
            "stdin",
            io.StringIO(json.dumps({"prompt": prompt, "session_id": session})),
        )
        assert retrieve.main() == 0
        output = capsys.readouterr().out
        assert output, (session, prompt)


def test_working_set_transport_failure_requests_native_activation(monkeypatch, capsys):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "working_set")
    monkeypatch.setattr(retrieve, "_gather_packet_with_lane", lambda *args: (None, "none"))
    monkeypatch.setattr(
        retrieve.sys,
        "stdin",
        io.StringIO(
            json.dumps({"prompt": "Check the prior decision about the adapter", "session_id": "s"})
        ),
    )
    assert retrieve.main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "activate_context" in context and "native MCP" in context
    assert "already activated" not in context


@pytest.mark.parametrize("branch", ["due", "coverage", "door"])
def test_mcp_capture_does_not_resolve_personal_service_credentials(monkeypatch, branch):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_TURNS", "1")
    reads = []
    monkeypatch.setattr(
        capture, "_resolve_rest_key", lambda: reads.append("service") or ("", "file")
    )
    if branch == "due":
        assert capture._episode_ask("s", [], True, "balanced") is not None
    elif branch == "coverage":
        state = capture._read_episode_state(capture._episode_state_path("s"))
        state["workflow_episode"] = "candidate-episode"
        capture._coverage_ask(state, 10**12, 0)
    else:
        assert capture._episode_door("inspect", "episode") is None
    assert reads == []


@pytest.mark.parametrize("module", [retrieve, capture])
def test_named_codex_profile_binds_nudge_state(monkeypatch, tmp_path, module):
    profile = tmp_path / "named-codex"
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "codex")
    monkeypatch.setenv("CODEX_HOME", str(profile))
    assert module._hook_home() == profile


@pytest.mark.parametrize("module", [retrieve, capture, continuation])
@pytest.mark.parametrize("available", [True, False])
def test_platform_home_binding_never_falls_back_to_an_inherited_home(
    monkeypatch, tmp_path, module, available
):
    """A directory-compatible command must still isolate every hook's state."""
    inherited = tmp_path / "other-profile"
    platform = tmp_path / "plugin state"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(inherited))
    if available:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(platform))
    payload = {"prompt": "continue", "session_id": "platform-binding"}
    observed = []
    if module is continuation:
        monkeypatch.setattr(module.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        monkeypatch.setattr(
            module, "dispatch_event", lambda client, data, *, environ: observed.append(environ)
        )
    else:
        monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))
    args = ["--client", "claude", "--hook-home-env", "CLAUDE_PLUGIN_DATA"]
    if module is not continuation:
        args += ["--activation-mode", "mcp"]
    assert module.main(args) == 0
    assert not inherited.exists()
    if available:
        if module is continuation:
            assert observed, "The bound continuation event was not dispatched"
        env = observed[0] if module is continuation else os.environ
        assert env["EXOMEM_HOOK_HOME"] == str(platform)
    else:
        assert not observed
        assert os.environ["EXOMEM_HOOK_HOME"] == str(inherited)


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.skipif(os.name == "nt", reason="Unix shell quoting and filename bytes")
def test_installed_mcp_commands_bind_shared_scripts_to_each_profile(tmp_path, client):
    shared = tmp_path / 'shared hooks "quoted" $literal `literal`'
    profiles = [tmp_path / 'profile one "quote" $literal', tmp_path / "profile two"]
    for home in profiles:
        settings = home / ("hooks.json" if client == "codex" else "settings.json")
        report = installer.install_hook(
            hook_dir=shared, settings_path=settings, client=client, activation_mode="mcp"
        )
        assert report["hook_home"] == str(home)
        prompt = next(item for item in report["installed"] if item["event"] == "UserPromptSubmit")
        args = shlex.split(prompt["command"])
        assert args[args.index("--hook-home") + 1] == str(home)
        assert args[args.index("--client") + 1] == client
        env = {
            **os.environ,
            "EXOMEM_HOOK_HOME": str(tmp_path / "wrong"),
            "EXOMEM_HOOK_CLIENT": "claude",
        }
        result = subprocess.run(
            prompt["command"],
            shell=True,
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "same-session",
                    "prompt": "continue",
                }
            ),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert "native MCP" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert (home / "exomem-retrieve-nudge.log").is_file()
        health = installer.check_hooks(
            clients=(client,), hook_dir=shared, settings_path=settings, activation_mode="mcp"
        )
        assert health["success"]
        row = health["clients"][0]
        assert row["activation_mode"] == "mcp" and row["runtime_activation"] == "unverified"
        assert row["logs"]["retrieve"]["path"] == str(home / "exomem-retrieve-nudge.log")
    assert not (tmp_path / "wrong").exists()


def test_activation_health_rejects_changed_mode_and_home(tmp_path):
    home = tmp_path / "profile"
    settings, hooks = home / "hooks.json", tmp_path / "shared"
    installer.install_hook(
        hook_dir=hooks, settings_path=settings, client="codex", activation_mode="mcp"
    )
    assert not installer.check_hooks(
        clients=("codex",), hook_dir=hooks, settings_path=settings, activation_mode="working-set"
    )["success"]
    config = json.loads(settings.read_text())
    config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] += " --hook-home wrong"
    settings.write_text(json.dumps(config))
    assert not installer.check_hooks(
        clients=("codex",), hook_dir=hooks, settings_path=settings, activation_mode="mcp"
    )["success"]


def test_mode_cli_installs_and_checks_same_profile(tmp_path):
    from exomem.__main__ import _install_hook_main

    args = [
        "--client",
        "codex",
        "--hook-dir",
        str(tmp_path / "shared"),
        "--settings",
        str(tmp_path / "profile" / "hooks.json"),
        "--activation-mode",
        "mcp",
        "--json",
    ]
    assert _install_hook_main(args) == 0
    assert _install_hook_main([*args, "--check"]) == 0


def test_mode_cli_names_the_bound_profile_log_path(tmp_path, capsys):
    from exomem.__main__ import _install_hook_main

    home = tmp_path / "profile"
    assert (
        _install_hook_main(
            [
                "--client",
                "codex",
                "--settings",
                str(home / "hooks.json"),
                "--activation-mode",
                "mcp",
            ]
        )
        == 0
    )
    assert f"{home}/exomem-retrieve-nudge.log" in capsys.readouterr().out


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_activation_default_config_uses_client_profile_not_shared_hook_home(
    monkeypatch, tmp_path, client
):
    home = tmp_path / "named-profile"
    monkeypatch.setenv("CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path / "wrong-shared"))
    report = installer.install_hook(client=client, activation_mode="mcp")
    assert report["settings"] == str(
        home / ("hooks.json" if client == "codex" else "settings.json")
    )
    assert not (tmp_path / "wrong-shared").exists()
    assert installer.check_hooks(clients=(client,), activation_mode="mcp")["success"]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_installed_capture_and_continuation_keep_same_session_in_separate_homes(tmp_path, client):
    if os.name == "nt" and client == "claude":
        pytest.skip("Claude wrapper subprocess requires a Unix shell")
    hooks = tmp_path / "shared-scripts"
    session = "same-session"
    for home in (tmp_path / "profile-one", tmp_path / "profile-two"):
        report = installer.install_hook(
            client=client,
            hook_dir=hooks,
            settings_path=home / ("hooks.json" if client == "codex" else "settings.json"),
            activation_mode="mcp",
        )
        transcript = home / "turn.jsonl"
        text = "The adapter decision and its follow-up are settled. " * 12
        if client == "codex":
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Review our adapter decision"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "namespace": "mcp__exomem__",
                        "name": "episode_memory",
                        "arguments": json.dumps({"action": "record", "episode": "episode-s"}),
                        "call_id": "record-1",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "record-1",
                        "output": 'Wall time: 0.01 seconds\nOutput: {"success":true}',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    },
                },
            ]
        else:
            rows = [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Review our adapter decision"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "record-1",
                                "name": "mcp__exomem__episode_memory",
                                "input": {"action": "record", "episode": "episode-s"},
                            },
                            {"type": "text", "text": text},
                        ],
                    },
                },
            ]
        transcript.write_text("\n".join(json.dumps(row) for row in rows))
        env = {
            **os.environ,
            "EXOMEM_HOOK_HOME": str(tmp_path / "wrong"),
            "EXOMEM_HOOK_CLIENT": "claude",
            "EXOMEM_RETRIEVE_INJECT": "working_set",
        }
        stop = next(item for item in report["installed"] if item["event"] == "Stop")
        result = subprocess.run(
            stop["commandWindows"] if os.name == "nt" else stop["command"],
            shell=True,
            input=json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": session,
                    "transcript_path": str(transcript),
                    "last_assistant_message": text,
                    "stop_hook_active": False,
                }
            ),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout == ""
        state = home / ".cache" / "exomem-nudge" / f"episode_{session}"
        assert json.loads(state.read_text())["substantive_since_record"] == 0
        compact = next(item for item in report["installed"] if item["event"] == "PreCompact")
        result = subprocess.run(
            compact["commandWindows"] if os.name == "nt" else compact["command"],
            shell=True,
            input=json.dumps(
                {
                    "hook_event_name": "PreCompact",
                    "session_id": session,
                    "trigger": "manual",
                    "cwd": str(home),
                    "transcript_path": str(transcript),
                }
            ),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        root = continuation.client_state_root(home, client)
        assert list(root.glob("*/current.json"))
    assert not (tmp_path / "wrong").exists()
    assert not (tmp_path / ".cache").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "other-write"])
def test_explicit_mode_preserves_untrusted_config_refusal(tmp_path, unsafe):
    if unsafe == "other-write":
        require_posix_file_modes()
    settings = tmp_path / "profile" / "hooks.json"
    settings.parent.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text('{"hooks":{}}')
    if unsafe == "symlink":
        settings.symlink_to(target)
    else:
        settings.write_text(target.read_text())
        settings.chmod(0o666)
    with pytest.raises(OSError):
        installer.install_hook(
            client="codex",
            hook_dir=tmp_path / "scripts",
            settings_path=settings,
            activation_mode="mcp",
        )
    assert target.read_text() == '{"hooks":{}}'


@pytest.mark.parametrize(
    "input_change",
    [
        {"prompt": "ok"},
        {"hook_event_name": "TaskNotification"},
        {"prompt": "Check adapter decisions", "type": "task_notification"},
    ],
)
def test_mcp_keeps_task_control_silent(monkeypatch, capsys, input_change):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    data = {
        "prompt": "Check the adapter decisions in our project",
        "session_id": "s",
        **input_change,
    }
    monkeypatch.setattr(retrieve.sys, "stdin", io.StringIO(json.dumps(data)))
    assert retrieve.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "name,value", [("EXOMEM_RETRIEVE_NUDGE_DISABLE", "1"), ("EXOMEM_PROMINENCE", "off")]
)
def test_mcp_respects_explicit_silence(monkeypatch, capsys, name, value):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        retrieve.sys, "stdin", io.StringIO(json.dumps({"prompt": "continue", "session_id": "s"}))
    )
    assert retrieve.main() == 0
    assert capsys.readouterr().out == ""


def test_windows_command_quotes_shell_metacharacters_without_spaces(tmp_path):
    command = installer._command_windows_for(
        "exomem_retrieve_nudge.py",
        tmp_path / "scripts&more",
        client="codex",
        hook_home=tmp_path / "profile&more",
        activation_mode="mcp",
    )
    assert f'"{tmp_path / "scripts&more" / "exomem_retrieve_nudge.py"}"' in command
    assert f'--hook-home "{tmp_path / "profile&more"}"' in command


@pytest.mark.parametrize("unsafe", ['quote"', "%EXPAND%", "!EXPAND!", "newline\n"])
@pytest.mark.skipif(os.name == "nt", reason="native Windows refuses instead of omitting")
def test_unrepresentable_windows_override_is_omitted_on_unix(tmp_path, unsafe):
    command = installer._command_windows_for(
        "exomem_retrieve_nudge.py",
        tmp_path / unsafe,
        client="codex",
        hook_home=tmp_path,
        activation_mode="mcp",
    )
    assert command is None


@pytest.mark.parametrize("mode", ["mcp", "working-set"])
def test_managed_refresh_preserves_explicit_activation_mode(tmp_path, mode):
    import sys

    home = tmp_path / "home"
    profile = home / ".claude-work"
    settings = profile / "settings.json"
    installer.install_hook(hook_dir=profile / "hooks", settings_path=settings, activation_mode=mode)
    report = installer.refresh_wired_profiles(sys.executable, home=home)
    assert report["success"], report
    assert installer.check_hooks(
        clients=("claude",),
        hook_dir=profile / "hooks",
        settings_path=settings,
        activation_mode=mode,
    )["success"]


def test_managed_refresh_preserves_profile_when_config_is_linked(tmp_path):
    import sys

    home = tmp_path / "home"
    profile = home / ".claude-work"
    settings = profile / "settings.json"
    installer.install_hook(
        hook_dir=profile / "hooks", settings_path=settings, activation_mode="mcp"
    )
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir(mode=0o700)
    settings.rename(target)
    settings.symlink_to(target)
    assert installer.refresh_wired_profiles(sys.executable, home=home)["success"]
    command = json.loads(target.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert shlex.split(command)[shlex.split(command).index("--hook-home") + 1] == str(profile)


def test_managed_refresh_defers_owner_gated_capture(tmp_path):
    import sys

    home = tmp_path / "home"
    profile = home / ".claude-polly"
    settings = profile / "settings.json"
    installer.install_hook(
        hook_dir=profile / "hooks", settings_path=settings, activation_mode="mcp"
    )
    config = json.loads(settings.read_text())
    config["hooks"]["Stop"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{profile / "hooks" / "polly_capture_gate.py"}" --client claude --hook-home "{profile}"',
                }
            ]
        }
    ]
    settings.write_text(json.dumps(config))
    before = settings.read_bytes()
    report = installer.refresh_wired_profiles(sys.executable, home=home)
    assert settings.read_bytes() == before
    assert report["profiles"][0]["skipped"] is True
    assert report["profiles"][0]["reason"] == "owner_managed_capture"
    assert report["skipped_profiles"] == 1 and report["refreshed_profiles"] == 0


def test_managed_refresh_refuses_binding_to_another_profile(tmp_path):
    import sys

    home = tmp_path / "home"
    profile = home / ".claude-work"
    settings = profile / "settings.json"
    installer.install_hook(
        hook_dir=profile / "hooks", settings_path=settings, activation_mode="mcp"
    )
    config = json.loads(settings.read_text())
    entry = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    args = shlex.split(entry["command"])
    args[args.index("--hook-home") + 1] = str(tmp_path / "other-profile")
    entry["command"] = shlex.join(args)
    settings.write_text(json.dumps(config))
    before = settings.read_bytes()
    report = installer.refresh_wired_profiles(sys.executable, home=home)
    assert not report["success"]
    assert settings.read_bytes() == before
    assert not (tmp_path / "other-profile").exists()


def test_repeated_installs_keep_two_named_codex_homes_bound(tmp_path):
    hooks = tmp_path / "shared"
    homes = [tmp_path / "codex-one", tmp_path / "codex-two"]
    for _round in range(2):
        for home in homes:
            settings = home / "hooks.json"
            installer.install_hook(
                client="codex",
                hook_dir=hooks,
                settings_path=settings,
                hook_home=home,
                activation_mode="mcp",
            )
            health = installer.check_hooks(
                clients=("codex",),
                hook_dir=hooks,
                settings_path=settings,
                hook_home=home,
                activation_mode="mcp",
            )
            assert health["success"] and health["clients"][0]["hook_home"] == str(home)


@pytest.mark.parametrize("module", [retrieve, capture])
def test_invalid_runtime_arguments_fail_soft(module):
    assert module.main(["--client", "unknown"]) == 0


@pytest.mark.parametrize("turn", ["why?", "what about the second one?", "なぜ？"])
def test_mcp_activates_short_substantive_followups(monkeypatch, capsys, turn):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")
    monkeypatch.setattr(
        retrieve.sys, "stdin", io.StringIO(json.dumps({"prompt": turn, "session_id": "s"}))
    )
    assert retrieve.main() == 0
    assert "activate_context" in capsys.readouterr().out


@pytest.mark.parametrize("turn", ["why?", "continue"])
def test_mcp_keeps_explicit_length_floor(monkeypatch, capsys, turn):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_MIN_CHARS", "80")
    monkeypatch.setattr(
        retrieve.sys, "stdin", io.StringIO(json.dumps({"prompt": turn, "session_id": "s"}))
    )
    assert retrieve.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("unsafe", ['quote"', "%EXPAND%", "!EXPAND!", "newline\n"])
def test_native_windows_refuses_unrepresentable_binding_before_writes(
    monkeypatch, tmp_path, unsafe
):
    from types import SimpleNamespace

    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt", environ=os.environ))
    hooks, home = tmp_path / "scripts", tmp_path / unsafe
    with pytest.raises(ValueError, match="Windows cmd.exe"):
        installer.install_hook(
            client="codex", hook_dir=hooks, settings_path=home / "hooks.json", activation_mode="mcp"
        )
    assert not hooks.exists() and not home.exists()


@pytest.mark.parametrize("module,kind", [(retrieve, "retrieve"), (capture, "capture")])
def test_hook_logs_do_not_persist_turn_content(monkeypatch, tmp_path, module, kind):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    home = tmp_path / "state"
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    private = "API_KEY=synthetic-hook-secret; private conversation content"
    old = os.umask(0o022)
    try:
        module._log(private)
    finally:
        os.umask(old)
    log = home / f"exomem-{kind}-nudge.log"
    assert private not in log.read_text()
    assert "synthetic-hook-secret" not in log.read_text()
    if os.name != "nt":
        assert log.stat().st_mode & 0o777 == 0o600
        assert home.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "unsafe", ["writable-home", "writable-cache", "linked-cache", "linked-marker"]
)
def test_installer_refuses_unsafe_selected_state_before_deployment(monkeypatch, tmp_path, unsafe):
    require_posix_file_modes()
    home, scripts, settings = (
        tmp_path / "state",
        tmp_path / "scripts",
        tmp_path / "config" / "hooks.json",
    )
    home.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("synthetic-target-unchanged")
    cache = home / ".cache"
    if unsafe == "writable-home":
        home.chmod(0o770)
    elif unsafe == "writable-cache":
        cache.mkdir(mode=0o700)
        cache.chmod(0o770)
    elif unsafe == "linked-cache":
        cache.symlink_to(tmp_path, target_is_directory=True)
    else:
        state = cache / "exomem-nudge"
        state.mkdir(parents=True, mode=0o700)
        (state / "pending-restart").symlink_to(target)
    with pytest.raises(OSError):
        installer.install_hook(
            client="codex",
            hook_dir=scripts,
            settings_path=settings,
            hook_home=home,
            activation_mode="mcp",
        )
    assert not scripts.exists() and not settings.exists()
    assert target.read_text() == "synthetic-target-unchanged"


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
@pytest.mark.parametrize(
    "writer",
    ["retrieve-log", "capture-log", "retrieve-stamp", "capture-stamp", "episode", "restart"],
)
def test_nudge_writers_refuse_redirected_state(monkeypatch, tmp_path, writer, link):
    home = tmp_path / "state"
    state = home / ".cache" / "exomem-nudge"
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    target = tmp_path / "target"
    target.write_text("synthetic-target-unchanged")
    paths = {
        "retrieve-log": home / "exomem-retrieve-nudge.log",
        "capture-log": home / "exomem-capture-nudge.log",
        "retrieve-stamp": state / "retrieve_s",
        "capture-stamp": state / "s",
        "episode": state / "episode_s",
        "restart": state / "pending-restart",
    }
    path = paths[writer]
    if link == "symlink":
        path.symlink_to(target)
    else:
        os.link(target, path)
    if writer == "retrieve-log":
        retrieve._log("synthetic-private-content", "mcp", 0)
    elif writer == "capture-log":
        capture._log("synthetic-private-content")
    elif writer == "retrieve-stamp":
        retrieve._touch(path)
    elif writer == "capture-stamp":
        capture._touch(path)
    elif writer == "episode":
        capture._write_episode_state(path, capture._EPISODE_STATE_DEFAULT)
    else:
        installer._mark_restart_pending(home / "hooks")
    assert target.read_text() == "synthetic-target-unchanged"


def test_fresh_mcp_codex_capture_checks_live_capabilities_without_restart_wait(
    monkeypatch, tmp_path
):
    home, scripts, settings = (
        tmp_path / "state",
        tmp_path / "scripts",
        tmp_path / "config" / "hooks.json",
    )
    report = installer.install_hook(
        client="codex",
        hook_dir=scripts,
        settings_path=settings,
        hook_home=home,
        activation_mode="mcp",
    )
    marker = home / ".cache" / "exomem-nudge" / "pending-restart"
    assert not marker.exists()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("old-marker")
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "c1",
                    "input": 'text(await tools.exec_command({cmd:"git push"}));',
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "c1",
                    "output": '{"exit_code":0,"output":"done"}',
                },
            }
        )
    )
    stop = next(item for item in report["installed"] if item["event"] == "Stop")
    result = subprocess.run(
        stop["commandWindows"] if os.name == "nt" else stop["command"],
        shell=True,
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "s",
                "transcript_path": str(transcript),
                "last_assistant_message": "We settled the adapter contract and its follow-up. "
                * 12,
            }
        ),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=True,
    )
    reason = json.loads(result.stdout)["reason"]
    assert "bootstrap" in reason and "capabilities" in reason
    assert "not connected" in reason and "unavailable" in reason
    assert marker.read_text() == "old-marker"


def test_mcp_installer_preflight_does_not_create_bound_owner_state(tmp_path):
    home = tmp_path / "owner-state"
    stage = tmp_path / "stage"
    installer.install_hook(
        client="codex",
        hook_dir=stage / "hooks",
        settings_path=stage / "hooks.json",
        hook_home=home,
        activation_mode="mcp",
    )
    assert not home.exists()


@pytest.mark.parametrize("mode", [None, "working-set"])
def test_non_mcp_installs_keep_restart_gate(tmp_path, monkeypatch, mode):
    home = tmp_path / "state"
    installer.install_hook(
        client="codex",
        hook_dir=home / "hooks",
        settings_path=home / "hooks.json",
        activation_mode=mode,
    )
    marker = home / ".cache" / "exomem-nudge" / "pending-restart"
    assert marker.exists()
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    if mode:
        monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", mode)
    assert capture._restart_pending([])


@pytest.mark.parametrize("reason", [capture.REMINDER, capture.EPISODE_ASK, capture.COVERAGE_ASK])
def test_all_mcp_capture_reasons_guard_live_capabilities(monkeypatch, capsys, reason):
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setattr(capture, "_episode_ask", lambda *args: reason)
    monkeypatch.setattr(
        capture.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "s",
                    "last_assistant_message": "A substantive conclusion. " * 30,
                }
            )
        ),
    )
    assert capture.main() == 0
    output = json.loads(capsys.readouterr().out)["reason"]
    assert "bootstrap" in output and "live capture capabilities" in output
    assert "not connected" in output and "unavailable" in output
    assert reason in output


@pytest.mark.parametrize("module", [retrieve, capture])
@pytest.mark.parametrize("unsafe", ["writable-home", "writable-cache", "linked-cache"])
def test_runtime_writers_refuse_unsafe_state_chain(monkeypatch, tmp_path, module, unsafe):
    require_posix_file_modes()
    home, target = tmp_path / "state", tmp_path / "target"
    home.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    cache = home / ".cache"
    if unsafe == "writable-home":
        home.chmod(0o770)
    elif unsafe == "writable-cache":
        cache.mkdir(mode=0o700)
        cache.chmod(0o770)
    else:
        cache.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    module._touch(cache / "exomem-nudge" / "s")
    assert not (cache / "exomem-nudge").exists()


def test_log_metadata_does_not_accept_arbitrary_lane_content(monkeypatch, tmp_path):
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path))
    retrieve._log("private turn", "synthetic-secret-lane\n", 2)
    row = (tmp_path / "exomem-retrieve-nudge.log").read_text()
    assert "synthetic-secret" not in row and "private turn" not in row
    assert "lane=unknown hits=2" in row


def test_explicit_health_reports_unsafe_selected_state_home(tmp_path):
    require_posix_file_modes()
    home, stage = tmp_path / "state", tmp_path / "stage"
    installer.install_hook(
        client="codex",
        hook_dir=stage / "hooks",
        settings_path=stage / "hooks.json",
        hook_home=home,
        activation_mode="mcp",
    )
    home.mkdir(mode=0o700)
    home.chmod(0o770)
    report = installer.check_hooks(
        clients=("codex",),
        hook_dir=stage / "hooks",
        settings_path=stage / "hooks.json",
        hook_home=home,
        activation_mode="mcp",
    )
    assert not report["success"]
    row = next(row for row in report["clients"][0]["checks"] if row["id"] == "state.home")
    assert row["status"] == "fail" and "writable" in row["message"]


@pytest.mark.parametrize(
    "unsafe", ["writable-ancestor", "linked-ancestor", "writable-token", "hardlinked-token"]
)
@pytest.mark.parametrize("operation", ["read", "write", "clear"])
def test_activation_tokens_refuse_unsafe_state_chain(monkeypatch, tmp_path, unsafe, operation):
    require_posix_file_modes()
    home = tmp_path / "state"
    path = retrieve.activation_token_path(home, "codex", "s")
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("synthetic-planted-token")
    path.chmod(0o600)
    ancestor = home / ".cache" / "exomem-continuation"
    if unsafe == "writable-ancestor":
        ancestor.chmod(0o770)
    elif unsafe == "linked-ancestor":
        moved = tmp_path / "redirect"
        ancestor.rename(moved)
        ancestor.symlink_to(moved, target_is_directory=True)
    elif unsafe == "writable-token":
        path.chmod(0o660)
    else:
        os.link(path, tmp_path / "linked-token")
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(home))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "codex")
    if operation == "read":
        assert retrieve._read_activation_token("s") == ""
    elif operation == "write":
        retrieve._write_activation_token("s", "replacement-token")
        assert path.read_text() == "synthetic-planted-token"
    else:
        continuation._clear_activation_token(home, "codex", "s")
        assert path.read_text() == "synthetic-planted-token"


@pytest.mark.parametrize("unsafe", ["writable", "linked"])
def test_installer_preflights_existing_continuation_chain_without_writes(tmp_path, unsafe):
    require_posix_file_modes()
    home, stage = tmp_path / "state", tmp_path / "stage"
    root = home / ".cache" / "exomem-continuation" / "codex"
    root.mkdir(parents=True, mode=0o700)
    if unsafe == "writable":
        root.chmod(0o770)
    else:
        moved = tmp_path / "redirect"
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        installer.install_hook(
            client="codex",
            hook_dir=stage / "hooks",
            settings_path=stage / "hooks.json",
            hook_home=home,
            activation_mode="working-set",
        )
    assert not stage.exists()


def test_activation_token_reader_refuses_group_readable_anchor_binding(monkeypatch, tmp_path):
    require_posix_file_modes()
    path = retrieve.activation_token_path(tmp_path, "codex", "s")
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("synthetic-private-anchor-token")
    path.chmod(0o640)
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "codex")
    assert retrieve._read_activation_token("s") == ""
