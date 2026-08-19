"""`install-hook --uninstall`: undo the wiring, including where it hides.

Installing wired entries into the client's config and copied scripts into its
hook directory; until now the only way back out was hand-editing JSON. That is
bad on its own, and on a yadm-managed machine it is worse than it looks: the
deployed config is regenerated from `settings.json##os.*` sources whenever
alternate selection runs, which ordinary commands like `yadm status` trigger.
Hooks removed from the deployed file therefore come back with no visible cause,
and stay gone only once every source is edited too (#580).

So these pin three things: uninstall removes what install added and leaves
everything else alone, it reaches the alternate sources rather than only the
file they overwrite, and where it cannot finish the job it says so instead of
reporting success.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import install_hook as hook_module

FOREIGN = {
    "type": "command",
    "command": "bash ~/.claude/hooks/somebody-elses-hook.sh",
    "timeout": 7,
}


def _install(tmp_path: Path, client: str = "claude") -> tuple[Path, Path]:
    hook_dir = tmp_path / "hooks"
    settings = tmp_path / ("hooks.json" if client == "codex" else "settings.json")
    hook_module.install_hook(hook_dir=hook_dir, settings_path=settings, client=client)
    return hook_dir, settings


def _entries(data: dict) -> list[dict]:
    return [
        hook
        for groups in data.get("hooks", {}).values()
        for group in groups
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
    ]


def _ours(data: dict) -> list[dict]:
    return [hook for hook in _entries(data) if hook_module._is_exomem_entry(hook)]


def _add_foreign(settings: Path) -> None:
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["Stop"][0]["hooks"].append(FOREIGN)
    data["hooks"]["Notification"] = [{"matcher": "*", "hooks": [FOREIGN]}]
    data["theme"] = "dark"
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_uninstall_removes_our_entries_and_leaves_the_rest_of_the_config(
    tmp_path: Path,
) -> None:
    """The whole risk of an automated uninstall is that it takes too much.

    A user's own hook shares the group we wired ours into, so removing the
    group wholesale -- the obvious implementation -- would delete a hook exomem
    never installed and cannot restore.
    """
    hook_dir, settings = _install(tmp_path)
    _add_foreign(settings)
    assert _ours(json.loads(settings.read_text(encoding="utf-8")))

    report = hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["config_changed"] is True
    assert report["removed_entries"] == 5
    assert _ours(data) == []
    assert FOREIGN in _entries(data)
    assert data["hooks"]["Notification"] == [{"matcher": "*", "hooks": [FOREIGN]}]
    assert data["theme"] == "dark"


def test_an_event_left_holding_nothing_of_ours_goes_with_the_entry(
    tmp_path: Path,
) -> None:
    """Our own containers are ours to remove; a user's empty one is not.

    `UserPromptSubmit` exists in the config only because we wired the retrieval
    nudge into it, so leaving `"UserPromptSubmit": [{"matcher": ..., "hooks":
    []}]` behind is residue of an uninstall, not configuration. An empty group
    the user wrote is a different thing and stays.
    """
    hook_dir, settings = _install(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": []}]
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "UserPromptSubmit" not in data["hooks"]
    assert "Stop" not in data["hooks"]
    assert data["hooks"]["PreToolUse"] == [{"matcher": "Bash", "hooks": []}]


def test_uninstall_removes_the_scripts_it_deployed_and_only_those(
    tmp_path: Path,
) -> None:
    """The hook directory belongs to every hook the user runs, not to us."""
    hook_dir, settings = _install(tmp_path)
    stranger = hook_dir / "somebody-elses-hook.sh"
    stranger.write_text("echo hi\n", encoding="utf-8")

    report = hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    assert not (hook_dir / "exomem_capture_nudge.py").exists()
    assert not (hook_dir / "exomem-retrieve-nudge.sh").exists()
    assert not (hook_dir / "exomem_continuation_checkpoint.py").exists()
    assert stranger.exists()
    assert all(row["removed"] for row in report["scripts"])
    assert hook_dir.is_dir()


def test_keep_scripts_unwires_without_deleting_a_synced_hook_tree(
    tmp_path: Path,
) -> None:
    """The hook tree is deliberately yadm-synced, so deleting it is a choice.

    A user turning the hooks off on one machine may well want the scripts to
    stay -- they are shared across machines by design -- and re-wiring later
    should not need the package again.
    """
    hook_dir, settings = _install(tmp_path)

    report = hook_module.uninstall_hook(
        hook_dir=hook_dir, settings_path=settings, remove_scripts=False
    )

    assert report["scripts"] == []
    assert (hook_dir / "exomem_capture_nudge.py").exists()
    assert _ours(json.loads(settings.read_text(encoding="utf-8"))) == []


def test_a_second_uninstall_changes_nothing_and_writes_no_backup(
    tmp_path: Path,
) -> None:
    """Idempotent in the same sense install is, and quiet about it.

    A no-op that still rewrites the file would churn a yadm-tracked config into
    a permanent diff, which is precisely the noise this feature exists to end.
    """
    hook_dir, settings = _install(tmp_path)
    hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)
    before = settings.read_bytes()

    report = hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    assert report["config_changed"] is False
    assert report["removed_entries"] == 0
    assert report["backup"] is None
    assert settings.read_bytes() == before
    assert len(list(tmp_path.glob("settings.json.backup-*"))) == 1


def test_an_absent_config_is_nothing_to_do_rather_than_an_error(
    tmp_path: Path,
) -> None:
    """Uninstalling a client that was never installed must not fail the run."""
    report = hook_module.uninstall_hook(
        hook_dir=tmp_path / "hooks", settings_path=tmp_path / "settings.json"
    )

    assert report["success"] is True
    assert report["config_changed"] is False
    assert report["scripts"] == []


def test_entries_from_the_pre_rename_install_are_removed_too(tmp_path: Path) -> None:
    """The machines most in need of an uninstall are the oldest ones.

    A config wired before the kb -> exomem rename carries `kb-capture-nudge.sh`
    and a `SessionEnd` continuation entry this build no longer installs. Both
    are ours, both are invisible to a user hunting by name, and an uninstall
    scoped to the events the current specs happen to name would walk past them.
    """
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ~/.claude/hooks/kb-capture-nudge.sh",
                                    "timeout": 10,
                                }
                            ],
                        }
                    ],
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "bash ~/.claude/hooks/kb-continuation-checkpoint.sh"
                                    ),
                                    "timeout": 5,
                                }
                            ]
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    report = hook_module.uninstall_hook(
        hook_dir=tmp_path / "hooks", settings_path=settings
    )

    assert report["removed_entries"] == 2
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": {}}


def test_a_yadm_alternate_source_is_pruned_beside_the_deployed_file(
    tmp_path: Path,
) -> None:
    """The edit that actually sticks.

    Editing only the deployed file is undone the next time alternate selection
    runs, and that run needs no user intent -- `yadm status` is enough. The
    reported incident ended exactly here: the hooks stayed removed only once
    both the Msys and WSL sources were edited directly.
    """
    hook_dir, settings = _install(tmp_path)
    deployed = settings.read_bytes()
    msys = tmp_path / "settings.json##os.Msys"
    wsl = tmp_path / "settings.json##os.WSL"
    msys.write_bytes(deployed)
    wsl.write_bytes(deployed)

    report = hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    assert report["success"] is True
    assert {Path(row["path"]).name for row in report["alternates"]} == {
        msys.name,
        wsl.name,
    }
    assert all(row["changed"] for row in report["alternates"])
    for source in (msys, wsl):
        assert _ours(json.loads(source.read_text(encoding="utf-8"))) == []
    assert "yadm alternate source" in hook_module.render_uninstall_human(report)
    assert "committed copy" in hook_module.render_uninstall_human(report)


def test_a_templated_alternate_is_named_rather_than_rewritten(tmp_path: Path) -> None:
    """A source we cannot parse is an unfinished uninstall, and must read as one.

    yadm templates are not JSON, so pruning them is not on the table -- but
    reporting success would tell the user the hooks are gone when the next
    render puts them straight back. Naming the file is the whole remedy
    available, so it has to survive into the exit status.
    """
    hook_dir, settings = _install(tmp_path)
    template = tmp_path / "settings.json##template.j2"
    template.write_text("{% if yadm.os %}{ not json {% endif %}\n", encoding="utf-8")
    before = template.read_bytes()

    report = hook_module.uninstall_hook(hook_dir=hook_dir, settings_path=settings)

    assert report["config_changed"] is True
    assert report["success"] is False
    assert len(report["alternates"]) == 1
    assert report["alternates"][0]["error"]
    assert template.read_bytes() == before
    assert "! yadm alternate" in hook_module.render_uninstall_human(report)


def test_codex_hooks_json_is_uninstalled_the_same_way(tmp_path: Path) -> None:
    """Codex wires `command` + `commandWindows` into a different file name."""
    hook_dir, settings = _install(tmp_path, client="codex")
    assert _ours(json.loads(settings.read_text(encoding="utf-8")))

    report = hook_module.uninstall_hook(
        hook_dir=hook_dir, settings_path=settings, client="codex"
    )

    assert report["success"] is True
    assert _ours(json.loads(settings.read_text(encoding="utf-8"))) == []
    assert not (hook_dir / "exomem_capture_nudge.py").exists()


def test_the_cli_uninstalls_and_reports_json(tmp_path: Path) -> None:
    """The surface the issue asked for, exercised end to end."""
    hook_dir, settings = _install(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "exomem",
            "install-hook",
            "--uninstall",
            "--hook-dir",
            str(hook_dir),
            "--settings",
            str(settings),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["config_changed"] is True
    assert _ours(json.loads(settings.read_text(encoding="utf-8"))) == []


def test_the_cli_refuses_uninstall_combined_with_check(tmp_path: Path) -> None:
    """Two opposite intents in one invocation is a typo, not a request."""
    completed = subprocess.run(
        [sys.executable, "-m", "exomem", "install-hook", "--uninstall", "--check"],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 2
    assert "--uninstall cannot be combined" in completed.stderr


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_install_after_uninstall_wires_a_clean_config(
    tmp_path: Path, client: str
) -> None:
    """Uninstall must leave a shape install can build on, not just an empty one."""
    hook_dir, settings = _install(tmp_path, client=client)
    hook_module.uninstall_hook(
        hook_dir=hook_dir, settings_path=settings, client=client
    )

    hook_module.install_hook(hook_dir=hook_dir, settings_path=settings, client=client)

    data = json.loads(settings.read_text(encoding="utf-8"))
    expected = len(hook_module._HOOK_SPECS) + len(hook_module._CONTINUATION_EVENTS[client])
    assert len(_ours(data)) == expected
