"""`evermatter setup --migrate` moves an install made before the rename to the new names.

Every test runs through injected seams (`claude_home`, `codex_home`, `which_fn`, `run_fn`):
none touches the real client configuration or spawns the real `claude` CLI.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from exomem import client_config, doctor, install_hook, rename_migration
from exomem.__main__ import main
from exomem._hooks import exomem_retrieve_nudge as retrieve

LEGACY_BLOCK = (
    f"{client_config.LEGACY_LEARNING_BEGIN}\nold wording\n{client_config.LEGACY_LEARNING_END}\n"
)

CODEX_CONFIG = """\
model = "gpt-5"

[mcp_servers.exomem]
command = "exomem"
args = ["--transport", "stdio"]

[mcp_servers.exomem.env]
EXOMEM_VAULT_PATH = "/vault"

[mcp_servers.keeper]
command = "keep-me"
"""


class FakeClaude:
    """Stands in for `claude mcp remove` and `claude mcp add-json` over a `.claude.json`.

    The user scope is the top-level `mcpServers`; the local scope is the one under `projects`
    for ``project``.
    """

    def __init__(self, config: Path, project: Path, *, fail_add: bool = False) -> None:
        self.config = config
        self.project = project
        self.fail_add = fail_add
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        data = json.loads(self.config.read_text(encoding="utf-8"))
        scope = argv[argv.index("--scope") + 1]
        servers = (
            data["projects"][str(self.project)]["mcpServers"]
            if scope == "local"
            else data["mcpServers"]
        )
        if argv[2] == "remove":
            del servers[argv[3]]
        elif argv[2] == "add-json":
            if self.fail_add:
                return subprocess.CompletedProcess(argv, 1, "", "add refused")
            servers[argv[-2]] = json.loads(argv[-1])
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


STDIO = {"type": "stdio", "command": "exomem", "args": [], "env": {"A": "1"}}
HTTP = {"type": "http", "url": "https://memory.example/mcp"}
CODEX_HTTP_CONFIG = '[mcp_servers.exomem]\nurl = "https://memory.example/mcp"\n'
KEPT = (
    "kept `exomem` for https://memory.example/mcp: renaming it would sign you out; "
    "a later release moves it with a sign-in notice"
)


class Install:
    """A client home for each client, holding the configuration an old install left."""

    def __init__(
        self,
        root: Path,
        *,
        claude: dict | None = None,
        codex: str = CODEX_CONFIG,
        local: dict | None = None,
    ) -> None:
        self.claude_home = root / "claude"
        self.codex_home = root / "codex"
        self.project = root / "project"
        self.config = root / ".claude.json"
        for folder in (self.claude_home, self.codex_home, self.project):
            folder.mkdir()
        config: dict = {
            "mcpServers": {
                "exomem": claude if claude is not None else STDIO,
                "other": {"type": "http", "url": "https://other.example/mcp"},
            }
        }
        if local is not None:
            config["projects"] = {str(self.project): {"mcpServers": {"exomem": local}}}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        (self.codex_home / "config.toml").write_text(codex, encoding="utf-8")

    def migrate(self, claude: FakeClaude | None = None) -> tuple[int, str]:
        lines: list[str] = []
        code = rename_migration.migrate(
            claude_home=self.claude_home,
            codex_home=self.codex_home,
            claude_config_path=self.config,
            project_dir=self.project,
            which_fn=lambda name: f"/fake/{name}" if name == "claude" else None,
            run_fn=claude or FakeClaude(self.config, self.project),
            print_fn=lines.append,
        )
        return code, "\n".join(lines)


def test_migrate_renames_the_server_key_in_claude_and_codex_with_backups(tmp_path: Path) -> None:
    install = Install(tmp_path)
    claude_before = install.config.read_text(encoding="utf-8")

    code, out = install.migrate()

    assert code == 0
    assert "Restart Claude Code and Codex to load the changes." in out
    servers = json.loads(install.config.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"evermatter", "other"}
    assert servers["evermatter"]["env"] == {"A": "1"}
    assert install.config.with_suffix(".json.exomem-bak").read_text(encoding="utf-8") == claude_before
    config = tomllib.loads((install.codex_home / "config.toml").read_text(encoding="utf-8"))
    assert set(config["mcp_servers"]) == {"evermatter", "keeper"}
    assert config["mcp_servers"]["evermatter"]["env"] == {"EXOMEM_VAULT_PATH": "/vault"}
    assert (install.codex_home / "config.toml.exomem-bak").read_text(encoding="utf-8") == CODEX_CONFIG


def test_migrate_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    install = Install(tmp_path)
    install.migrate()
    claude = FakeClaude(install.config, install.project)
    after_first = (
        install.config.read_bytes(),
        (install.codex_home / "config.toml").read_bytes(),
        (install.codex_home / "config.toml.exomem-bak").read_bytes(),
    )

    code, out = install.migrate(claude)

    assert code == 0
    assert "Restart Claude Code" not in out
    assert claude.calls == []
    assert after_first == (
        install.config.read_bytes(),
        (install.codex_home / "config.toml").read_bytes(),
        (install.codex_home / "config.toml.exomem-bak").read_bytes(),
    )


def test_migrate_keeps_a_claude_url_server_under_its_key_and_renames_the_stdio_codex_one(
    tmp_path: Path,
) -> None:
    install = Install(tmp_path, claude=HTTP)
    claude = FakeClaude(install.config, install.project)
    claude_before = install.config.read_bytes()

    code, out = install.migrate(claude)

    assert code == 0
    assert claude.calls == []
    assert install.config.read_bytes() == claude_before
    assert KEPT in out
    codex = tomllib.loads((install.codex_home / "config.toml").read_text(encoding="utf-8"))
    assert set(codex["mcp_servers"]) == {"evermatter", "keeper"}


def test_migrate_keeps_a_codex_url_server_under_its_key_and_renames_the_stdio_claude_one(
    tmp_path: Path,
) -> None:
    install = Install(tmp_path, codex=CODEX_HTTP_CONFIG)
    codex_before = (install.codex_home / "config.toml").read_bytes()

    code, out = install.migrate()

    assert code == 0
    assert (install.codex_home / "config.toml").read_bytes() == codex_before
    assert not (install.codex_home / "config.toml.exomem-bak").exists()
    assert KEPT in out
    servers = json.loads(install.config.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"evermatter", "other"}


def test_migrating_two_scopes_leaves_the_backup_holding_the_original_file(tmp_path: Path) -> None:
    install = Install(tmp_path, local=STDIO)
    original = install.config.read_bytes()

    code, _out = install.migrate()

    assert code == 0
    assert install.config.with_suffix(".json.exomem-bak").read_bytes() == original
    config = json.loads(install.config.read_text(encoding="utf-8"))
    assert set(config["mcpServers"]) == {"evermatter", "other"}
    assert set(config["projects"][str(install.project)]["mcpServers"]) == {"evermatter"}


def test_migrate_reports_a_scope_with_both_keys_and_other_projects_it_did_not_touch(
    tmp_path: Path,
) -> None:
    install = Install(tmp_path)
    config = json.loads(install.config.read_text(encoding="utf-8"))
    config["mcpServers"]["evermatter"] = STDIO
    config["projects"] = {"/elsewhere": {"mcpServers": {"exomem": STDIO}}}
    install.config.write_text(json.dumps(config), encoding="utf-8")
    before = install.config.read_bytes()

    code, out = install.migrate()

    assert code == 0
    assert install.config.read_bytes() == before
    assert "holds both `exomem` and `evermatter`" in out
    assert "/elsewhere" in out and "other project(s) keep a local `exomem` registration" in out


def test_a_refused_claude_add_restores_the_registration_the_user_had(tmp_path: Path) -> None:
    install = Install(tmp_path)
    before = install.config.read_bytes()

    code, out = install.migrate(FakeClaude(install.config, install.project, fail_add=True))

    assert code == 1
    assert install.config.read_bytes() == before
    assert "restored the previous registration" in out


def test_migrate_moves_the_managed_instruction_block_and_leaves_the_rest(tmp_path: Path) -> None:
    install = Install(tmp_path)
    claude_md = install.claude_home / "CLAUDE.md"
    claude_md.write_text(f"# My rules\n\n{LEGACY_BLOCK}\nMine below.\n", encoding="utf-8")

    install.migrate()

    text = claude_md.read_text(encoding="utf-8")
    assert client_config.LEARNING_BEGIN in text and client_config.LEARNING_END in text
    assert client_config.LEGACY_LEARNING_BEGIN not in text
    assert text.startswith("# My rules\n\n") and text.endswith("\nMine below.\n")
    assert claude_md.with_suffix(".md.exomem-bak").is_file()


def test_migrate_leaves_the_rules_and_hooks_the_user_wrote_and_lists_them(tmp_path: Path) -> None:
    install = Install(tmp_path)
    settings = install.claude_home / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["mcp__exomem__recall_context", "Bash(ls)"]},
                "hooks": {"PreToolUse": [{"matcher": "mcp__exomem__.*", "hooks": []}]},
            }
        ),
        encoding="utf-8",
    )
    before = settings.read_bytes()

    code, out = install.migrate()

    assert code == 0
    assert settings.read_bytes() == before
    assert f"{settings}: permission rule mcp__exomem__recall_context" in out
    assert f"{settings}: hook mcp__exomem__.*" in out
    assert "Bash(ls)" not in out


def test_migrate_adds_the_new_prefix_beside_the_old_one_in_checkpoint_bindings(
    tmp_path: Path,
) -> None:
    install = Install(tmp_path)
    install_hook.install_hook(
        client="claude",
        activation_mode="mcp",
        hook_dir=install.claude_home / "hooks",
        settings_path=install.claude_home / "settings.json",
        refresh_bindings=("exomem",),
    )

    code, _out = install.migrate()

    assert code == 0
    metadata, status = retrieve.refresh_metadata(
        install.claude_home / "hooks", "claude", install.claude_home
    )
    assert metadata is not None, status
    assert metadata["bindings"] == ["mcp__exomem__", "mcp__evermatter__"]


def test_setup_migrate_runs_through_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.chdir(tmp_path)

    code = main(["setup", "--migrate"])

    assert code == 0
    assert "evermatter setup --migrate" in capsys.readouterr().out


def _use_install_for_doctor(install: Install, monkeypatch: pytest.MonkeyPatch, *, server: str) -> Path:
    """Point doctor at ``install``: Claude Code and Codex each register ``server``, and a
    project rule names the `mcp__exomem__` prefix. Returns the rule's file."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(install.claude_home))
    monkeypatch.setenv("CODEX_HOME", str(install.codex_home))
    monkeypatch.chdir(install.project)
    (install.claude_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {server: HTTP}}), encoding="utf-8"
    )
    (install.codex_home / "config.toml").write_text(
        f'[mcp_servers.{server}]\nurl = "https://memory.example/mcp"\n', encoding="utf-8"
    )
    rule = install.project / ".claude" / "settings.local.json"
    rule.parent.mkdir()
    rule.write_text(
        json.dumps({"permissions": {"allow": ["mcp__exomem__save_episode"]}}), encoding="utf-8"
    )
    return rule


def test_doctor_passes_a_rule_that_names_the_old_prefix_while_an_exomem_server_answers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = Install(tmp_path)
    _use_install_for_doctor(install, monkeypatch, server="exomem")

    check = doctor._check_legacy_tool_references()  # noqa: SLF001

    assert check.status == "pass"
    assert "still names it matches" in check.message


def test_doctor_warns_about_a_rule_that_names_the_old_prefix_once_only_an_evermatter_server_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = Install(tmp_path)
    rule = _use_install_for_doctor(install, monkeypatch, server="evermatter")

    check = doctor._check_legacy_tool_references()  # noqa: SLF001

    assert check.status == "warn"
    assert "mcp__exomem__save_episode" in check.message and str(rule) in check.message
    assert "`mcp__evermatter__`" in (check.remediation or "")
