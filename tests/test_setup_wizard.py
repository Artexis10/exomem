"""`exomem setup` — the guided onboarding wizard.

Every test runs through injected seams (input_fn / run_fn / which_fn / home /
print_fn): no test touches the real `~/.claude` or spawns a real `claude`.
"""

from __future__ import annotations

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
        return subprocess.CompletedProcess(argv, 0, "", "")


def _messy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    daily = vault / "Daily" / "2026-1"
    daily.mkdir(parents=True)
    (daily / "2026-01-05.md").write_text("- 09:00 log\n", encoding="utf-8")
    (vault / "floating.md").write_text("note\n", encoding="utf-8")
    return vault


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
    # pre-init scan surfaced the existing content, likely packs, and write contract
    assert "2 files" in out
    assert "Likely packs:" in out
    assert "Adoption: run `exomem adopt`" in out
    assert "compile planning" in out
    assert "writes only under 'Knowledge Base/'" in out
    # registration argv shape
    (reg,) = [c for c in recorder.calls if "add" in c]
    assert reg[0].endswith("claude.CMD")
    assert reg[1:4] == ["mcp", "add", "exomem"]
    assert ["--scope", "user"] == reg[4:6]
    assert f"EXOMEM_VAULT_PATH={vault}" in reg
    assert "EXOMEM_DISABLE_EMBEDDINGS=1" in reg  # lean profile
    assert "--" in reg


def test_rerun_converges_to_skips(tmp_path: Path) -> None:
    vault, home = _messy_vault(tmp_path), tmp_path / "home"
    code, _ = _setup(vault, home, Recorder())
    assert code == 0
    rerun = Recorder(results={"mcp add": (1, "", "MCP server exomem already exists")})
    code, out = _setup(vault, home, rerun)
    assert code == 0
    assert "[skipped: Knowledge Base/ already exists]" in out
    assert "[skipped: already installed]" in out
    assert "[skipped: already registered]" in out


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
    code = main(["setup", "--vault", str(tmp_path), "--yes", "--hybrid", "--scope", "local"])
    assert code == 0
    assert called["vault"] == str(tmp_path)
    assert called["profile"] == "hybrid"
    assert called["scope"] == "local"


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
