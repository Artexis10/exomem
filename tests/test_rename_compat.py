"""Legacy-name compatibility: kb_mcp imports and KB_MCP_* env vars keep working."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings

import pytest

from exomem import env_compat


@pytest.fixture(autouse=True)
def _isolated_settings():
    """Promotion writes `os.environ`: remove the names it creates for these probes."""
    yield
    for name in [name for name in os.environ if "_RENAME_PROBE" in name]:
        os.environ.pop(name)


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
    monkeypatch.delenv("EXOMEM_RENAME_PROBE", raising=False)
    monkeypatch.setenv("KB_MCP_RENAME_PROBE", "legacy-value")

    promoted = env_compat.promote_legacy()

    assert "EXOMEM_RENAME_PROBE" in promoted
    assert os.environ["EXOMEM_RENAME_PROBE"] == "legacy-value"


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


def test_evermatter_prefix_alone_fills_the_internal_name_and_leaves_no_alias(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE3", raising=False)
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE3", "product-value")

    promoted = env_compat.promote_legacy()

    assert "EXOMEM_RENAME_PROBE3" in promoted
    assert os.environ["EXOMEM_RENAME_PROBE3"] == "product-value"
    assert "EVERMATTER_RENAME_PROBE3" not in os.environ


def test_evermatter_prefix_beats_the_legacy_prefix(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE4", raising=False)
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE4", "product-value")
    monkeypatch.setenv("KB_MCP_RENAME_PROBE4", "legacy-value")

    env_compat.promote_legacy()

    assert os.environ["EXOMEM_RENAME_PROBE4"] == "product-value"


def test_a_later_write_of_the_internal_name_wins_and_a_child_process_sees_one_name(
    monkeypatch,
) -> None:
    # The failure: an alias left in the environment beats a value that code writes after import,
    # or a child process inherits two names for one setting and picks the wrong one.
    monkeypatch.delenv("EXOMEM_RENAME_PROBE8", raising=False)
    monkeypatch.setenv("EVERMATTER_RENAME_PROBE8", "from-alias")
    env_compat.promote_legacy()
    os.environ["EXOMEM_RENAME_PROBE8"] = "written-later"
    env_compat.promote_legacy()  # a later loader maps the environment again

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; print(sorted((k, v) for k, v in os.environ.items() if k.endswith('RENAME_PROBE8')))",
        ],
        capture_output=True, text=True, timeout=60, check=True,
    )

    assert child.stdout.strip() == "[('EXOMEM_RENAME_PROBE8', 'written-later')]"


def test_a_conflict_in_a_non_placement_setting_warns_naming_both_variables_and_exomem_wins(
    monkeypatch, caplog
) -> None:
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


def test_a_conflict_in_a_placement_setting_refuses_the_command_with_exit_code_2(tmp_path) -> None:
    # The failure: the CLI acts on the wrong vault because two names disagree about where it is.
    env = _subprocess_env()
    env["EXOMEM_VAULT_PATH"] = str(tmp_path / "explicit-vault")
    env["EVERMATTER_VAULT_PATH"] = str(tmp_path / "alias-vault")

    out = subprocess.run(
        [sys.executable, "-m", "exomem", "doctor"],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )

    assert out.returncode == 2, out.stderr
    assert "EVERMATTER_VAULT_PATH" in out.stderr and "EXOMEM_VAULT_PATH" in out.stderr
    assert "explicit-vault" not in out.stderr and "alias-vault" not in out.stderr


def test_a_dotenv_that_sets_only_evermatter_gives_the_internal_name(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_RENAME_PROBE9", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("EVERMATTER_RENAME_PROBE9=dotenv-value\n", encoding="utf-8")

    env_compat.load_dotenv(env_file)

    assert os.environ["EXOMEM_RENAME_PROBE9"] == "dotenv-value"
    assert "EVERMATTER_RENAME_PROBE9" not in os.environ


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


def test_a_dotenv_that_disagrees_with_itself_about_a_placement_setting_is_refused(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EVERMATTER_STATE_ROOT=/alias-root\nEXOMEM_STATE_ROOT=/explicit-root\n", encoding="utf-8"
    )

    with pytest.raises(env_compat.SettingsConflict) as refused:
        env_compat.load_dotenv(env_file)

    assert "EVERMATTER_STATE_ROOT" in str(refused.value) and "EXOMEM_STATE_ROOT" in str(refused.value)
    assert "alias-root" not in str(refused.value) and "explicit-root" not in str(refused.value)


def _run_module(tmp_path, module_arguments: list[str], **conflict) -> subprocess.CompletedProcess[str]:
    env = _subprocess_env()
    env.update(conflict)
    return subprocess.run(
        [sys.executable, "-m", *module_arguments],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )


@pytest.mark.parametrize(
    "module_arguments",
    [
        # Port 0 asks the OS for a free port: a regression that starts the supervisor cannot take the default 8765.
        ["exomem.service_manager", "serve", "--runtime-dir", "managed", "--worker-python", sys.executable,
         "--unit-name", "exomem-probe.service", "--port", "0"],
        ["exomem.catalogue_prewarm"],
    ],
    ids=["the managed supervisor", "a module run as a job"],
)
def test_a_module_run_as_a_program_refuses_a_placement_conflict_in_its_environment(
    tmp_path, module_arguments
) -> None:
    # The failure: the supervisor and its children run on without a refusal, because only the CLI had one.
    out = _run_module(
        tmp_path, module_arguments,
        EXOMEM_STATE_ROOT=str(tmp_path / "explicit-root"),
        EVERMATTER_STATE_ROOT=str(tmp_path / "alias-root"),
    )

    assert out.returncode == 2, out.stderr
    assert "not run" in out.stderr  # a warning that names both variables and runs on is no refusal
    assert "EVERMATTER_STATE_ROOT" in out.stderr and "EXOMEM_STATE_ROOT" in out.stderr
    assert "explicit-root" not in out.stderr and "alias-root" not in out.stderr


@pytest.mark.parametrize(
    ("orig_argv", "refused"),
    [
        (["python", "-m", "exomem.catalogue_prewarm"], True),
        (["python", "-mexomem.catalogue_prewarm"], True),
        (["python", "-Im", "exomem.catalogue_prewarm"], True),
        (["python", "-Imexomem.catalogue_prewarm"], True),
        (["python", "-W", "ignore", "-m", "exomem.catalogue_prewarm"], True),
        (["python", "-m", "exomem.catalogue_prewarm", "--help"], False),
        (["python", "-m", "exomem"], False),
        (["python", "-m", "pytest", "exomem.catalogue_prewarm"], False),
        (["python", "-c", "import exomem", "-m", "exomem.catalogue_prewarm"], False),
        (["python", "script.py", "-m", "exomem.catalogue_prewarm"], False),
    ],
)
def test_the_program_entry_refusal_reads_every_way_the_interpreter_takes_a_module(
    orig_argv, refused, monkeypatch
) -> None:
    """The failure: `python -Im exomem.<module>` runs a module past the refusal that `-m` meets."""
    conflict = env_compat.Conflict("EVERMATTER_STATE_ROOT", "EXOMEM_STATE_ROOT")
    monkeypatch.setattr(env_compat, "_process_conflicts", [conflict])

    if refused:
        with pytest.raises(SystemExit) as exit_:
            env_compat.refuse_program_entry_conflicts(orig_argv)
        assert exit_.value.code == 2
    else:
        env_compat.refuse_program_entry_conflicts(orig_argv)


def test_help_runs_under_a_placement_conflict(tmp_path) -> None:
    for module_arguments in (["exomem", "--help"], ["exomem.service_manager", "--help"]):
        out = _run_module(
            tmp_path, module_arguments,
            EXOMEM_STATE_ROOT=str(tmp_path / "explicit-root"),
            EVERMATTER_STATE_ROOT=str(tmp_path / "alias-root"),
        )

        assert out.returncode == 0, (module_arguments, out.stderr)
        assert "usage:" in out.stdout.lower()


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


def test_the_credential_hook_sends_no_key_when_service_env_names_two_different_ones(
    monkeypatch, tmp_path
) -> None:
    from exomem._hooks import exomem_local_credentials

    service_env = tmp_path / "service.env"
    service_env.write_text("EVERMATTER_REST_API_KEY=alias-key\nEXOMEM_REST_API_KEY=explicit-key\n")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    monkeypatch.delenv("EVERMATTER_REST_API_KEY", raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(service_env))

    assert exomem_local_credentials.rest_key() == ("", "")
