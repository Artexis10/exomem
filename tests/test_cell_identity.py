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
    assert cell_identity.recall_space_state(vault) == {"state": "unknown", "id": None}


def _publish_space(vault):
    from contextlib import closing

    from exomem import embedding_index, recall_space

    index = embedding_index.EmbeddingIndex(vault)
    with closing(index._connect()) as connection, connection:
        space = recall_space.SpaceIdentity("cell-model", "cell-weights", 2)
        recall_space.write_identity(connection, space)
    return embedding_index.read_published_identity(vault).identity_digest


def _hold_store(vault, seconds):
    """Hold the store lock in another thread, as a concurrent reader or a capture does."""
    import threading

    from exomem import reserved_paths

    held = threading.Event()

    def hold():
        with reserved_paths._subsystem_authority_scope("embedding_index"), \
                reserved_paths._identity_coordination_scope(
                    vault, descriptor_ids=("embeddings-store",), identity_may_change=False):
            held.set()
            threading.Event().wait(seconds)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    return holder


def test_discovery_waits_out_a_short_store_hold_instead_of_reporting_no_space(vault):
    """Four concurrent discovery calls reported a published space unavailable 3 times in 4."""
    space = _publish_space(vault)
    holder = _hold_store(vault, 0.05)
    try:
        assert cell_identity.recall_space_state(vault) == {"state": "observed", "id": space}
    finally:
        holder.join()


def test_a_store_held_past_the_wait_reports_an_unknown_recall_space(vault, monkeypatch):
    """A busy store must not read as a cell with no published recall space."""
    _publish_space(vault)
    monkeypatch.setattr(cell_identity, "RECALL_SPACE_WAIT_SECONDS", 0.01)
    holder = _hold_store(vault, 0.5)
    try:
        assert cell_identity.recall_space_state(vault) == {"state": "unknown", "id": None}
    finally:
        holder.join()


def test_a_cell_without_a_published_space_reports_it_unavailable(vault):
    """An empty cell must stay distinguishable from a busy one."""
    assert cell_identity.recall_space_state(vault) == {"state": "unavailable", "id": None}


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
