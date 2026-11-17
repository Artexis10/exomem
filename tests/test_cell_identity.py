"""Discovery must identify the actual service without exposing tenant material."""

from pathlib import Path

from exomem import capabilities, cell_identity, commands, passage_evidence, source_capture


def test_same_version_source_changes_have_distinct_software_identity(tmp_path: Path):
    """A source overlay must not qualify as the build whose code it replaced."""
    package = tmp_path / "package"
    package.mkdir()
    code = package / "engine.py"
    code.write_text("ranker = 'first'\n")
    first = cell_identity.software_identity(package)
    code.write_text("ranker = 'second'\n")
    second = cell_identity.software_identity(package)
    assert first["state"] == second["state"] == "observed"
    assert first["version"] == second["version"]
    assert first["sha256"] != second["sha256"]
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "engine.pyc").write_bytes(b"interpreter-specific cache")
    assert cell_identity.software_identity(package) == second


def test_unreadable_package_identity_remains_unknown(tmp_path: Path):
    """A missing package cannot receive the empty-tree digest as a measured build."""
    observed = cell_identity.software_identity(tmp_path / "missing-package")
    assert observed["state"] == "unknown"
    assert observed["sha256"] is None


def test_discovery_never_reads_a_foreign_sidecar_alias(vault, tmp_path):
    """Unauthenticated discovery must not inspect another vault through a sidecar link."""
    from contextlib import closing

    import pytest

    from exomem import embedding_index, index_paths, recall_space

    foreign = tmp_path / "foreign-vault"
    foreign.mkdir()
    index = embedding_index.EmbeddingIndex(foreign)
    with closing(index._connect()) as connection, connection:
        recall_space.write_identity(connection, recall_space.SpaceIdentity("foreign-model", "foreign-weights", 2))
    alias = index_paths.sidecar_path(vault)
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.unlink(missing_ok=True)
    try:
        alias.symlink_to(index.path)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    assert embedding_index.read_published_identity(vault) is None


def test_discovery_follows_the_filtered_command_registry():
    """A withheld capture command must not advertise capture support or limits."""
    search = next(command for command in commands.PRODUCT_COMMANDS if command.name == "ask_memory")
    capture = next(command for command in commands.PRODUCT_COMMANDS if command.name == "capture_source")
    surface = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product-cloud", tier2_enabled=False,
        product_commands=(search.preferred_name,), contributing_commands=(search,),
    )
    contract = cell_identity.service_contract(surface)
    assert contract["operations"] == ["passage_results"]
    assert contract["limits"] == {"passage_results": passage_evidence.MAX_RESULTS}

    from dataclasses import replace

    with_capture = replace(surface, product_commands=(*surface.product_commands, capture.name),
                           contributing_commands=(search, capture))
    contract = cell_identity.service_contract(with_capture)
    assert "declared_passages" in contract["operations"]
    assert contract["limits"]["passages_per_capture"] == source_capture.MAX_PASSAGES
    assert contract["limits"]["passage_key_utf8_bytes"] == source_capture.MAX_PASSAGE_KEY_BYTES
