"""Legacy-name compatibility: kb_mcp imports and KB_MCP_* env vars keep working."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings

from exomem import env_compat


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "src" + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_kb_mcp_alias_is_the_same_module_object() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import exomem.find
        import kb_mcp.find
    assert kb_mcp.find is exomem.find  # single module state, not a re-import
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from exomem import embeddings as canonical
        from kb_mcp import embeddings as legacy
    assert legacy is canonical


def test_kb_mcp_import_warns_deprecation() -> None:
    # First import must fire the warning — guarantee "first" via a subprocess.
    out = subprocess.run(
        [sys.executable, "-W", "always::DeprecationWarning", "-c", "import kb_mcp"],
        capture_output=True, text=True, env=_subprocess_env(), timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "renamed to 'exomem'" in out.stderr


def test_python_dash_m_kb_mcp_still_runs() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "kb_mcp", "--help"],
        capture_output=True, text=True, env=_subprocess_env(), timeout=120,
    )
    assert out.returncode == 0, out.stderr


def test_promote_legacy_fills_unset_canonical(monkeypatch) -> None:
    # promote_legacy writes os.environ directly (that's its job) — pop what it
    # creates ourselves; monkeypatch can't restore keys that were absent.
    monkeypatch.delenv("EXOMEM_RENAME_PROBE", raising=False)
    monkeypatch.setenv("KB_MCP_RENAME_PROBE", "legacy-value")
    try:
        promoted = env_compat.promote_legacy()
        assert "EXOMEM_RENAME_PROBE" in promoted
        assert os.environ["EXOMEM_RENAME_PROBE"] == "legacy-value"
    finally:
        os.environ.pop("EXOMEM_RENAME_PROBE", None)


def test_promote_legacy_never_clobbers_explicit_new_name(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_RENAME_PROBE2", "new-wins")
    monkeypatch.setenv("KB_MCP_RENAME_PROBE2", "legacy-loses")
    env_compat.promote_legacy()
    assert os.environ["EXOMEM_RENAME_PROBE2"] == "new-wins"


def test_dotenv_legacy_vars_promoted_at_server_build(vault, monkeypatch) -> None:
    """A pre-rename .env (KB_MCP_* keys) loads inside build_server, after the
    import-time promotion — the post-dotenv re-promotion must cover it."""
    from exomem import server as server_mod

    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)
    monkeypatch.delenv("KB_MCP_VAULT_PATH", raising=False)

    def fake_load_dotenv(*args, **kwargs):
        os.environ["KB_MCP_VAULT_PATH"] = str(vault)  # what an old .env supplies

    monkeypatch.setattr(server_mod, "load_dotenv", fake_load_dotenv)
    try:
        srv = server_mod.build_server(require_auth=False)
        assert srv is not None
        assert os.environ["EXOMEM_VAULT_PATH"] == str(vault)
    finally:
        os.environ.pop("EXOMEM_VAULT_PATH", None)
        os.environ.pop("KB_MCP_VAULT_PATH", None)


def test_legacy_env_reaches_a_real_reader(monkeypatch) -> None:
    from exomem import embeddings

    monkeypatch.delenv("EXOMEM_VIDEO_SCENE_FRAMES", raising=False)
    monkeypatch.delenv("KB_MCP_VIDEO_SCENE_FRAMES", raising=False)
    assert not embeddings.scene_frames_enabled()
    monkeypatch.setenv("KB_MCP_VIDEO_SCENE_FRAMES", "1")  # old-style .env
    try:
        env_compat.promote_legacy()  # what any late env loading would call
        assert embeddings.scene_frames_enabled()
    finally:
        os.environ.pop("EXOMEM_VIDEO_SCENE_FRAMES", None)
