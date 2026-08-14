"""`exomem setup` — the guided onboarding wizard.

Every test runs through injected seams (input_fn / run_fn / which_fn / home /
print_fn): no test touches the real `~/.claude` or spawns a real `claude`.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import setup_wizard
from exomem.__main__ import main


class Recorder:
    """Fake subprocess.run: records argv, answers by substring match."""

    def __init__(self, results: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        for key, (rc, out, err) in self.results.items():
            if key in joined:
                return subprocess.CompletedProcess(argv, rc, out, err)
        if "plugin list --json" in joined:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _messy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    daily = vault / "Daily" / "2026-1"
    daily.mkdir(parents=True)
    (daily / "2026-01-05.md").write_text("- 09:00 log\n", encoding="utf-8")
    (vault / "floating.md").write_text("note\n", encoding="utf-8")
    return vault


def test_default_claude_config_path_honors_relocated_config_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "relocated-claude"

    assert setup_wizard._default_claude_config_path(  # noqa: SLF001
        {"CLAUDE_CONFIG_DIR": str(config_dir)}
    ) == config_dir / ".claude.json"


def _setup(vault: Path, home: Path, recorder: Recorder, **overrides):
    lines: list[str] = []
    kwargs = dict(
        vault=str(vault),
        yes=True,
        profile="lean",
        with_hooks=False,
        skip_claude_register=False,
        scope="user",
        input_fn=lambda prompt="": pytest.fail(f"unexpected prompt: {prompt}"),
        run_fn=recorder,
        which_fn=lambda name: f"C:/fake/{name}.CMD",
        home=home,
        claude_config_path=home.parent / ".claude.json",
        project_dir=vault.parent,
        environ={},
        persist_profile_fn=lambda _profile: None,
        # Always sandboxed: without this the wizard would resolve the REAL
        # ~/.codex and a test run could rewrite the developer's own config.toml.
        codex_home=home.parent / "codex",
        print_fn=lines.append,
    )
    kwargs.update(overrides)
    code = setup_wizard.run_setup(**kwargs)
    return code, "\n".join(lines)


def test_fresh_vault_happy_path(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()
    code, out = _setup(vault, home, recorder)
    assert code == 0
    # KB scaffold landed; skill installed under the injected home
    assert (vault / "Knowledge Base" / "index.md").is_file()
    assert (home / "skills" / "exomem" / "SKILL.md").is_file()
    selected = json.loads((vault / "Knowledge Base" / "_Packs" / "selected-packs.json").read_text(encoding="utf-8"))
    assert selected["selected_pack_ids"] == ["personal-records"]
    # pre-init scan surfaced the existing content, likely packs, and write contract
    assert "2 files" in out
    assert "Likely packs:" in out
    assert "Selected packs: Personal records" in out
    assert "Adoption: run `exomem adopt`" in out
    assert "compile planning" in out
    assert "writes only under 'Knowledge Base/'" in out
    # registration argv shape (Codex is registered too; pick the Claude call)
    (reg,) = [
        c
        for c in recorder.calls
        if c[1:3] == ["mcp", "add-json"] and "claude" in c[0]
    ]
    assert reg[0].endswith("claude.CMD")
    assert reg[1:6] == ["mcp", "add-json", "--scope", "user", "exomem"]
    registration = json.loads(reg[6])
    assert registration["type"] == "stdio"
    assert registration["args"][-2:] == ["--transport", "stdio"]
    assert registration["env"]["EXOMEM_VAULT_PATH"] == str(vault)
    assert registration["env"]["EXOMEM_DISABLE_EMBEDDINGS"] == "1"


def test_interactive_setup_can_select_multiple_packs(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()
    answers = iter(["technical, creative", "n"])
    lines: list[str] = []

    code = setup_wizard.run_setup(
        vault=str(vault),
        yes=False,
        profile="lean",
        with_hooks=False,
        skip_claude_register=True,
        scope="user",
        input_fn=lambda prompt="": next(answers),
        run_fn=recorder,
        which_fn=lambda name: None,
        home=home,
        environ={},
        project_dir=tmp_path,
        persist_profile_fn=lambda _profile: None,
        print_fn=lines.append,
    )

    selected = json.loads((vault / "Knowledge Base" / "_Packs" / "selected-packs.json").read_text(encoding="utf-8"))
    assert code == 0
    assert selected["selected_pack_ids"] == ["technical", "creative"]
    assert "Choose starter knowledge packs" in "\n".join(lines)
    assert "Selected packs: Technical, Creative" in "\n".join(lines)

def test_rerun_converges_to_skips(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    code, _ = _setup(vault, home, Recorder())
    assert code == 0
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "exomem": {
                        "command": "uv",
                        "args": setup_wizard._server_command(
                            lambda name: f"C:/fake/{name}.CMD"
                        )[1:],
                        "env": {
                            "EXOMEM_VAULT_PATH": str(vault),
                            "EXOMEM_DISABLE_EMBEDDINGS": "1",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rerun = Recorder()
    code, out = _setup(vault, home, rerun)
    assert code == 0
    assert "[skipped: Knowledge Base/ already exists]" in out
    assert "[skipped: already installed]" in out
    assert "[skipped: already registered in user]" in out


def test_foreign_skill_is_preserved(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    target = home / "skills" / "exomem"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: my-custom-skill\n---\n", encoding="utf-8")
    code, out = _setup(vault, home, Recorder())
    assert code == 0
    assert "not the bundled skill" in out
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: my-custom-skill")


def test_legacy_skill_is_migrated(tmp_path: Path) -> None:
    """A pre-rename install at skills/knowledge-base is retired once the renamed skill
    lands at skills/exomem."""
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    legacy = home / "skills" / "knowledge-base"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: knowledge-base\n---\n\nThis skill is the Exomem contract.\n",
        encoding="utf-8",
    )
    code, out = _setup(vault, home, Recorder())
    assert code == 0
    assert (home / "skills" / "exomem" / "SKILL.md").is_file()
    assert not legacy.exists()
    assert "removed stale" in out


def test_hook_step_permission_error_reports_a_step_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#478: the trusted-directory guard raises PermissionError, an OSError.

    Only FileNotFoundError was caught, so the guard's rejection escaped as a raw
    stack trace — after register, register-codex and skill had already applied.
    scripts/install.sh keeps failures in plain language precisely so users never
    see a traceback; the wizard has to hold the same line, and has to say the
    earlier steps stuck so a user does not assume nothing happened.
    """
    vault, home = _messy_vault(tmp_path), tmp_path / "home"

    def _guard_rejects(**_kwargs):
        raise PermissionError(1, "unsafe writable or foreign-owned directory: /home/u/.claude")

    monkeypatch.setattr(setup_wizard.hook_module, "install_hook", _guard_rejects)

    code, out = _setup(vault, home, Recorder(), with_hooks=True)

    assert code == 1
    assert "Traceback" not in out
    assert "hooks: [failed:" in out
    assert "unsafe writable" in out
    # The steps that did apply are named, so the user knows the state they are in.
    assert "skill" in out
    assert "idempotent" in out


def test_setup_generates_access_policy(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    code, out = _setup(vault, home, Recorder())
    assert code == 0
    # `Daily/` is a markdown sibling → classified read-only in a generated _access.yaml.
    access_yaml = vault / "Knowledge Base" / "_access.yaml"
    assert access_yaml.is_file()
    assert "Daily" in access_yaml.read_text(encoding="utf-8")
    assert "personalize" in out
    # Re-run converges — nothing left to govern.
    code2, out2 = _setup(vault, home, Recorder(results={"mcp add": (1, "", "already exists")}))
    assert code2 == 0
    assert "[skipped: no sibling folders need governing]" in out2


def test_setup_skips_registration_when_exomem_exists_in_another_scope(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        json.dumps(
            {
                "projects": {
                    project_dir.resolve().as_posix(): {
                        "mcpServers": {
                            "exomem": {
                                "command": "exomem",
                                "args": ["--transport", "stdio"],
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    recorder = Recorder()

    code, out = _setup(
        vault,
        home,
        recorder,
        scope="user",
        project_dir=project_dir,
        claude_config_path=config_path,
    )

    assert code == 0
    assert "[skipped: already registered in local]" in out
    # An existing Claude registration must be left alone. Codex is a separate
    # client with its own registration, so it is still wired.
    assert not [c for c in recorder.calls if "add" in c and "claude" in c[0]]


def test_client_route_prefers_explicit_then_dotenv_then_inherited_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "EXOMEM_BASE_URL=https://dotenv.example.com\n", encoding="utf-8"
    )
    common = dict(
        force_stdio=False,
        cwd=tmp_path,
        environ={"EXOMEM_BASE_URL": "https://inherited.example.com"},
        which_fn=lambda name: f"/fake/{name}",
        vault_path=tmp_path / "vault",
        profile="hybrid",
    )

    explicit = setup_wizard._resolve_client_route(
        mcp_url="https://explicit.example.com/mcp", **common
    )
    dotenv = setup_wizard._resolve_client_route(mcp_url=None, **common)
    inherited = setup_wizard._resolve_client_route(
        mcp_url=None, **(common | {"cwd": tmp_path / "no-dotenv"})
    )

    assert explicit.url == "https://explicit.example.com/mcp"
    assert dotenv.url == "https://dotenv.example.com/mcp"
    assert inherited.url == "https://inherited.example.com/mcp"


def test_client_route_force_stdio_ignores_configured_service(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "EXOMEM_BASE_URL=https://dotenv.example.com\n", encoding="utf-8"
    )

    route = setup_wizard._resolve_client_route(
        mcp_url=None,
        force_stdio=True,
        cwd=tmp_path,
        environ={"EXOMEM_BASE_URL": "https://inherited.example.com"},
        which_fn=lambda name: f"/fake/{name}",
        vault_path=tmp_path / "vault",
        profile="lean",
    )

    assert route.transport == "stdio"
    assert route.command[-2:] == ("--transport", "stdio")
    assert route.env["EXOMEM_DISABLE_EMBEDDINGS"] == "1"


def test_present_invalid_dotenv_service_url_fails_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "EXOMEM_BASE_URL=http://public.example.com\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="MCP URL"):
        setup_wizard._resolve_client_route(
            mcp_url=None,
            force_stdio=False,
            cwd=tmp_path,
            environ={},
            which_fn=lambda name: f"/fake/{name}",
            vault_path=tmp_path / "vault",
            profile="lean",
        )


def test_invalid_service_url_fails_before_setup_mutates_any_configuration(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "new-vault"
    config_path = tmp_path / ".claude.json"
    original = b'{"mcpServers":{"exomem":{"command":"old"}}}\n'
    config_path.write_bytes(original)
    lines: list[str] = []

    code = setup_wizard.run_setup(
        vault=str(vault),
        yes=True,
        profile="lean",
        mcp_url="http://public.example.com",
        run_fn=Recorder(),
        which_fn=lambda name: f"C:/fake/{name}.CMD",
        claude_config_path=config_path,
        project_dir=tmp_path,
        environ={},
        persist_profile_fn=lambda _profile: pytest.fail("profile was persisted"),
        print_fn=lines.append,
    )

    assert code == 2
    assert not vault.exists()
    assert config_path.read_bytes() == original
    assert "MCP URL" in "\n".join(lines)


def test_http_route_registers_native_claude_and_codex_clients(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()

    code, out = _setup(
        vault,
        home,
        recorder,
        mcp_url="https://kb.example.com",
    )

    assert code == 0
    (claude_add,) = [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "claude" in c[0]]
    (codex_add,) = [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "codex" in c[0]]
    assert claude_add == [
        "C:/fake/claude.CMD",
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        "user",
        "exomem",
        "https://kb.example.com/mcp",
    ]
    assert codex_add == [
        "C:/fake/codex.CMD",
        "mcp",
        "add",
        "exomem",
        "--url",
        "https://kb.example.com/mcp",
    ]
    assert "claude mcp" in out and "/mcp" in out
    assert "codex mcp login exomem" in out


def _write_shadowing_claude_configs(config_path: Path, project_dir: Path) -> None:
    stdio = {"command": "exomem", "args": ["--transport", "stdio"]}
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {"exomem": stdio},
                "projects": {
                    project_dir.resolve().as_posix(): {"mcpServers": {"exomem": stdio}}
                },
                "unrelated": {"keep": True},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"exomem": stdio}, "keep": True}, indent=2),
        encoding="utf-8",
    )


def test_explicit_replacement_removes_every_shadowing_claude_scope(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    config_path = tmp_path / ".claude.json"
    _write_shadowing_claude_configs(config_path, project_dir)
    recorder = Recorder()

    code, out = _setup(
        vault,
        home,
        recorder,
        project_dir=project_dir,
        claude_config_path=config_path,
        mcp_url="https://kb.example.com/mcp",
        replace_client_registration=True,
    )

    assert code == 0
    removals = [c for c in recorder.calls if c[1:3] == ["mcp", "remove"]]
    assert {c[c.index("--scope") + 1] for c in removals} == {"local", "project", "user"}
    assert "local, project, user" in out
    assert [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "claude" in c[0]]


def test_failed_claude_replacement_restores_every_snapshotted_file(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    config_path = tmp_path / ".claude.json"
    project_path = project_dir / ".mcp.json"
    _write_shadowing_claude_configs(config_path, project_dir)
    original_user = config_path.read_bytes()
    original_project = project_path.read_bytes()
    calls: list[list[str]] = []

    def failing_run(argv, **_kwargs):
        calls.append(list(argv))
        joined = " ".join(str(value) for value in argv)
        if "plugin list --json" in joined:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[1:3] == ["mcp", "remove"]:
            config_path.write_text('{"mutated": true}\n', encoding="utf-8")
            project_path.write_text('{"mutated": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["mcp", "add"] and "claude" in argv[0]:
            return subprocess.CompletedProcess(argv, 1, "", "synthetic add failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    code, out = _setup(
        vault,
        home,
        Recorder(),
        run_fn=failing_run,
        project_dir=project_dir,
        claude_config_path=config_path,
        mcp_url="https://kb.example.com/mcp",
        replace_client_registration=True,
    )

    assert code == 1
    assert "synthetic add failure" in out
    assert "restored previous registration" in out
    assert config_path.read_bytes() == original_user
    assert project_path.read_bytes() == original_project


def test_configuration_snapshot_restore_preserves_restrictive_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"secret": true}\n', encoding="utf-8")
    path.chmod(0o444)
    expected_mode = stat.S_IMODE(path.stat().st_mode)
    snapshots = setup_wizard._snapshot_files((path,))  # noqa: SLF001

    path.chmod(0o666)
    path.write_text('{"mutated": true}\n', encoding="utf-8")
    try:
        setup_wizard._restore_files(snapshots)  # noqa: SLF001

        assert path.read_text(encoding="utf-8") == '{"secret": true}\n'
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    finally:
        path.chmod(0o666)


def test_enabled_legacy_stdio_plugin_blocks_false_shared_route_claim(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    legacy = [
        {
            "id": "exomem@exomem",
            "enabled": True,
            "mcpServers": {
                "exomem": {
                    "command": "uvx",
                    "args": ["exomem", "--transport", "stdio"],
                }
            },
        }
    ]
    recorder = Recorder(results={"plugin list --json": (0, json.dumps(legacy), "")})

    code, out = _setup(
        vault,
        home,
        recorder,
        mcp_url="https://kb.example.com/mcp",
    )

    assert code == 1
    assert "claude plugin update exomem@exomem" in out
    assert "/reload-plugins" in out
    assert not [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "claude" in c[0]]


def test_unverifiable_plugin_inventory_blocks_a_convergence_claim(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder(results={"plugin list --json": (1, "", "inventory unavailable")})

    code, out = _setup(vault, home, recorder, mcp_url="https://kb.example.com/mcp")

    assert code == 1
    assert "could not verify Claude plugin state" in out
    assert not [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "claude" in c[0]]


@pytest.mark.parametrize(
    "plugin",
    [
        {"id": "exomem@exomem", "enabled": True},
        {"id": "exomem@exomem", "enabled": True, "mcpServers": []},
        {
            "id": "exomem@exomem",
            "enabled": True,
            "mcpServers": {"another-server": {"type": "http", "url": "https://example.com/mcp"}},
        },
        {"id": "exomem@exomem", "enabled": True, "mcpServers": {"exomem": {}}},
    ],
)
def test_incomplete_enabled_exomem_plugin_inventory_fails_closed(
    tmp_path: Path, plugin: dict
) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder(
        results={"plugin list --json": (0, json.dumps([plugin]), "")}
    )

    code, out = _setup(vault, home, recorder, mcp_url="https://kb.example.com/mcp")

    assert code == 1
    assert "could not verify Claude plugin state" in out
    assert not [c for c in recorder.calls if c[1:3] == ["mcp", "add"] and "claude" in c[0]]


def test_failed_codex_replacement_restores_its_config(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    original = '[mcp_servers.exomem]\ncommand = "old"\nargs = []\n'
    config_path.write_text(original, encoding="utf-8")

    def failing_run(argv, **_kwargs):
        joined = " ".join(str(value) for value in argv)
        if "plugin list --json" in joined:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if "codex" in argv[0] and argv[1:3] == ["mcp", "remove"]:
            config_path.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "codex" in argv[0] and argv[1:3] == ["mcp", "add"]:
            return subprocess.CompletedProcess(argv, 1, "", "synthetic codex failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    code, out = _setup(
        vault,
        home,
        Recorder(),
        run_fn=failing_run,
        codex_home=codex_home,
        mcp_url="https://kb.example.com/mcp",
        replace_client_registration=True,
    )

    assert code == 1
    assert "synthetic codex failure" in out
    assert "restored previous Codex registration" in out
    assert config_path.read_text(encoding="utf-8") == original



def test_no_claude_cli_prints_snippet(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()
    code, out = _setup(vault, home, recorder, which_fn=lambda name: None)
    assert code == 0
    assert recorder.calls == []  # nothing spawned
    assert '"mcpServers"' in out
    assert "[skipped: no claude CLI" in out


def test_doctor_failure_aborts_yes_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    monkeypatch.setattr(
        setup_wizard.doctor_module,
        "doctor",
        lambda **kw: SimpleNamespace(success=False, checks=[]),
    )
    monkeypatch.setattr(setup_wizard.doctor_module, "render_human", lambda r: "DOCTOR FAIL")
    recorder = Recorder()
    code, out = _setup(vault, home, recorder)
    assert code == 1
    assert "DOCTOR FAIL" in out
    assert recorder.calls == []  # aborted before registration
    assert not (home / "skills").exists()  # …and before skill install


def test_yes_without_vault_is_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as e:
        main(["setup", "--yes"])
    assert e.value.code == 2


def test_setup_dispatches_from_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    called: dict = {}
    monkeypatch.setattr(setup_wizard, "run_setup", lambda **kw: called.update(kw) or 0)
    code = main(
        [
            "setup",
            "--vault",
            str(tmp_path),
            "--yes",
            "--hybrid",
            "--scope",
            "local",
            "--mcp-url",
            "https://kb.example.com/mcp",
            "--replace-client-registration",
        ]
    )
    assert code == 0
    assert called["vault"] == str(tmp_path)
    assert called["profile"] == "hybrid"
    assert called["scope"] == "local"
    assert called["mcp_url"] == "https://kb.example.com/mcp"
    assert called["force_stdio"] is False
    assert called["replace_client_registration"] is True


# ============================================================================
# _server_command — launch command preference order: uv in a repo checkout,
# then the durable `exomem` console script, then `uvx exomem` as the
# transient-install fallback.
# ============================================================================


def _no_repo_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fake setup_wizard.__file__ so its derived repo-root has no
    pyproject.toml — this test worktree IS a real checkout, so branch 1
    (uv) would otherwise always fire regardless of which_fn."""
    fake_file = tmp_path / "elsewhere" / "src" / "exomem" / "setup_wizard.py"
    monkeypatch.setattr(setup_wizard, "__file__", str(fake_file))


def test_server_command_prefers_uv_in_a_repo_checkout() -> None:
    """This worktree IS a real repo checkout, so branch 1 fires whenever
    which_fn('uv') is truthy — unchanged behavior from before the branch
    order was introduced."""
    repo_root = Path(setup_wizard.__file__).resolve().parents[2]
    cmd = setup_wizard._server_command(lambda name: "C:/fake/uv.CMD" if name == "uv" else None)
    assert cmd == [
        "uv", "--directory", str(repo_root),
        "run", "python", "-m", "exomem", "--transport", "stdio",
    ]


def test_server_command_falls_back_to_console_script_outside_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_repo_checkout(monkeypatch, tmp_path)
    cmd = setup_wizard._server_command(
        lambda name: "/usr/local/bin/exomem" if name == "exomem" else None
    )
    assert cmd == ["/usr/local/bin/exomem", "--transport", "stdio"]


def test_server_command_falls_back_to_uvx_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_repo_checkout(monkeypatch, tmp_path)
    cmd = setup_wizard._server_command(lambda name: None)
    assert cmd == ["uvx", "exomem", "--transport", "stdio"]


# ---- Codex registration: symmetric with Claude Code ----
#
# Codex reads skills from disk and speaks MCP just like Claude Code, but the
# wizard used to only ever print a snippet for it, so Codex users ended up with
# tools and no working registration.


def test_codex_is_registered_through_its_cli_when_available(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()

    code, out = _setup(vault, home, recorder)

    assert code == 0
    (reg,) = [c for c in recorder.calls if "add" in c and "codex" in c[0]]
    assert reg[1:4] == ["mcp", "add", "exomem"]
    assert f"EXOMEM_VAULT_PATH={vault}" in reg
    # --transport stdio must be explicit: the server defaults to http.
    assert "--transport" in reg and "stdio" in reg
    assert "[done] registered with Codex" in out


def test_codex_is_skipped_when_not_installed(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    recorder = Recorder()

    code, out = _setup(
        vault,
        home,
        recorder,
        which_fn=lambda name: None if name == "codex" else f"C:/fake/{name}.CMD",
    )

    assert code == 0
    assert "[skipped: Codex not detected on this machine]" in out
    assert not [c for c in recorder.calls if "codex" in str(c[0])]


def test_codex_falls_back_to_config_toml_when_its_cli_cannot_register(
    tmp_path: Path,
) -> None:
    """Older Codex builds have no `mcp add`; the user must still end up wired."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    # Key on the executable, not a bare "codex": Recorder matches substrings across
    # the whole argv, and pytest's tmp_path is named after this test - so "codex"
    # alone also matches the Claude call's vault path.
    recorder = Recorder(results={"fake/codex.CMD": (1, "", "unknown subcommand 'mcp'")})

    code, out = _setup(vault, home, recorder)

    assert code == 0
    written = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.exomem]" in written
    assert 'model = "gpt-5"' in written, "must not clobber the user's other settings"
    assert "config.toml" in out
