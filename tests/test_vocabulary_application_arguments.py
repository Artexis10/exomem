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
