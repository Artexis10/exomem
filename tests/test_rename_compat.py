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


def test_evermatter_prefix_alone_fills_the_internal_name(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE3", raising=False)
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE3", "product-value")

    promoted = env_compat.promote_legacy()

    assert "EXOMEM_RENAME_PROBE3" in promoted
    assert os.environ["EXOMEM_RENAME_PROBE3"] == "product-value"


def test_evermatter_prefix_beats_the_legacy_prefix(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE4", raising=False)
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE4", "product-value")
    monkeypatch.setenv("KB_MCP_RENAME_PROBE4", "legacy-value")

    env_compat.promote_legacy()

    assert os.environ["EXOMEM_RENAME_PROBE4"] == "product-value"


def test_explicit_exomem_value_wins_and_the_warning_names_both_variables_not_values(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(env_compat, "_reported", set())
    monkeypatch.setenv("EXOMEM_RENAME_PROBE5", "explicit-secret")
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE5", "alias-secret")

    with caplog.at_level("WARNING", logger=env_compat.log.name):
        env_compat.promote_legacy()
        env_compat.promote_legacy()  # a later loader maps the same environment again

    assert os.environ["EXOMEM_RENAME_PROBE5"] == "explicit-secret"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "RENAME_PROBE5" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "EXOMEM_RENAME_PROBE5" in warnings[0] and "EVERMATTER_RENAME_PROBE5" in warnings[0]
    assert "explicit-secret" not in warnings[0] and "alias-secret" not in warnings[0]


def test_a_dotenv_that_sets_both_prefixes_keeps_its_own_exomem_value(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE6", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EVERMATTER_RENAME_PROBE6=alias-value\nEXOMEM_RENAME_PROBE6=explicit-value\n",
        encoding="utf-8",
    )

    env_compat.load_dotenv(env_file, override=True)

    assert os.environ["EXOMEM_RENAME_PROBE6"] == "explicit-value"


def test_a_dotenv_that_sets_only_evermatter_beats_an_exported_value_like_its_own_exomem_line(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_RENAME_PROBE7", "exported-value")
    env_file = tmp_path / ".env"
    env_file.write_text("EVERMATTER_RENAME_PROBE7=dotenv-value\n", encoding="utf-8")

    env_compat.load_dotenv(env_file, override=True)

    assert os.environ["EXOMEM_RENAME_PROBE7"] == "dotenv-value"


def test_the_standalone_hooks_map_the_prefix_like_env_compat(monkeypatch, tmp_path) -> None:
    """A hook cannot import env_compat, so it carries a copy of the EVERMATTER_ rule."""
    from exomem._hooks import (
        exomem_capture_nudge,
        exomem_continuation_checkpoint,
        exomem_local_credentials,
        exomem_retrieve_nudge,
    )

    sample = {
        "EVERMATTER_ONLY": "from-product-prefix",
        "EVERMATTER_BOTH": "alias-loses",
        "EXOMEM_BOTH": "explicit-wins",
        "EXOMEM_UNRELATED": "kept",
        "EVERMATTER_EMPTY": "",
        "OTHER_SETTING": "ignored",
    }
    expected = {
        name: value
        for name, value in env_compat.canonical_settings(sample).items()
        if name.startswith(env_compat.CANONICAL_PREFIX)
    }
    assert expected["EXOMEM_ONLY"] == "from-product-prefix"
    assert expected["EXOMEM_BOTH"] == "explicit-wins"

    for hook in (exomem_capture_nudge, exomem_retrieve_nudge):
        monkeypatch.setattr(os, "environ", dict(sample))
        hook._normalize_env_aliases()
        produced = {
            name: value
            for name, value in os.environ.items()
            if name.startswith(env_compat.CANONICAL_PREFIX)
        }
        assert produced == expected, hook.__name__

    mapped = exomem_continuation_checkpoint._with_product_prefix(sample)
    assert {n: v for n, v in mapped.items() if n.startswith("EXOMEM_")} == expected

    # The credential hook reads single names, from the environment and from a file; the file lists
    # the alias first to show that line order does not decide between the two names.
    service_env = tmp_path / "service.env"
    service_env.write_text("".join(f"{name}={value}\n" for name, value in sample.items()))
    monkeypatch.setattr(os, "environ", dict(sample))
    for name, value in expected.items():
        assert exomem_local_credentials.env_value(name) == value, name
        assert exomem_local_credentials.service_env_value(name, service_env) == value, name


def test_the_credential_hook_finds_a_key_that_service_env_names_with_the_product_prefix(
    monkeypatch, tmp_path
) -> None:
    from exomem._hooks import exomem_local_credentials

    service_env = tmp_path / "service.env"
    service_env.write_text("EVERMATTER_REST_API_KEY=synthetic-key\n")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    monkeypatch.delenv("EVERMATTER_REST_API_KEY", raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(service_env))

    assert exomem_local_credentials.rest_key() == ("synthetic-key", "file")
