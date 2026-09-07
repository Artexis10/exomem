"""Application correlation is finite metadata, never a generic execution route."""

import pytest

from exomem import commands


@pytest.mark.parametrize("name", ["schema_memory", "connect_memory", "maintain_memory"])
def test_application_metadata_is_declared_on_canonical_schema(name):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    assert {parameter.name for parameter in command.params} >= {
        "vocabulary_ref", "vocabulary_fingerprint",
    }


@pytest.mark.parametrize("function,args", [
    (commands.op_schema_memory, {"operation": "infer"}),
    (commands.op_connect_memory, {"operation": "resolve-entity", "query": "Group"}),
    (commands.op_maintain_memory, {"mode": "audit"}),
])
def test_correlation_cannot_attach_to_unrelated_variants(tmp_path, function, args):
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        function(tmp_path, **args, vocabulary_ref="exomem://review/vocabulary/example",
                 vocabulary_fingerprint="a" * 64)


@pytest.mark.parametrize("metadata", [
    {"vocabulary_ref": "exomem://review/vocabulary/example"},
    {"vocabulary_fingerprint": "a" * 64},
    {"vocabulary_ref": "", "vocabulary_fingerprint": "a" * 64},
    {"vocabulary_ref": "exomem://review/vocabulary/example", "vocabulary_fingerprint": ""},
])
def test_partial_binding_refuses_before_entity_write(tmp_path, metadata):
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        commands.op_connect_memory(tmp_path, operation="create-entity", **metadata)


@pytest.mark.parametrize("metadata", [{}, {
    "vocabulary_ref": None, "vocabulary_fingerprint": None,
}], ids=["omitted", "adapter-null-defaults"])
def test_legacy_entity_creation_accepts_absent_correlation(tmp_path, metadata):
    from exomem import init, writer_lease
    from exomem.governance.principal import library_scope

    vault = tmp_path / "vault"
    init.init_vault(vault)
    command = next(entry for entry in commands.PRODUCT_COMMANDS if entry.name == "connect_memory")
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with library_scope():
        result = manager.invoke(
            command,
            (vault,),
            {
                "operation": "create-entity",
                "entity_type": "organization",
                "name": "Field Station",
                "slug": "field-station",
                "summary": "A permanent research organization.",
                **metadata,
            },
            idempotency_key="legacy-entity-create",
            read_only=False,
        )
    assert result["state"] == "committed"
    assert result["receipt_id"]
    target = vault / "Knowledge Base/Entities/Organizations/field-station.md"
    assert "A permanent research organization." in target.read_text()
