"""Index intent must be bounded before migration can change stored objects."""

import pytest

from exomem.collection_store import types


def declaration(fields, indexes=None):
    result = {"name": "sample", "version": 1, "title": "Sample", "item_type": "sample-item",
              "kind": "observed", "placement": "Records", "default_audience": "policy", "fields": fields}
    if indexes is not None:
        result["indexes"] = indexes
    return result


@pytest.mark.parametrize("flag", ["filterable", "sortable"])
def test_index_flags_do_not_silently_accept_non_boolean_values(flag):
    """A misspelled flag cannot silently publish an unindexed declaration."""
    with pytest.raises(types.CollectionTypeError, match="INVALID_INDEX_DECLARATION"):
        types.parse_declaration(declaration({"amount": {"type": "number", flag: "yes"}}), builtin=False)


def test_flags_coalesce_and_composite_prefix_avoids_duplicate_index():
    """Filter+sort on one field and a covering leading key need no extra trees."""
    data = declaration({"amount": {"type": "number", "filterable": True, "sortable": True},
                        "status": {"type": "string", "sortable": True}},
                       {"by_amount_status": {"keys": [{"field": "amount"}, {"field": "status", "direction": "desc"}]}})
    parsed = types.parse_declaration(data, builtin=False)
    assert [tuple(key.field for key in index.keys) for index in parsed.indexes] == [("amount", "status"), ("status",)]
    assert parsed.indexes[0].keys[1].direction == "desc"


@pytest.mark.parametrize("shape", ["indexes", "keys", "paths", "object", "unknown"])
def test_invalid_or_over_budget_indexes_refuse_before_migration(shape):
    """Independent index/count/path caps and invalid dependencies cannot reach DDL."""
    fields = {f"field_{n}": {"type": "integer"} for n in range(17)}
    indexes = {"sample": {"keys": [{"field": "field_0"}]}}
    if shape == "indexes":
        indexes = {f"index_{n}": {"keys": [{"field": f"field_{n}"}]} for n in range(9)}
    elif shape == "keys":
        indexes["sample"]["keys"] = [{"field": f"field_{n}"} for n in range(5)]
    elif shape == "paths":
        indexes = {f"index_{n}": {"keys": [{"field": f"field_{k}"} for k in range(n * 4, min(17, n * 4 + 4))]}
                   for n in range(5)}
    elif shape == "object":
        fields["field_0"]["type"] = "object"
    else:
        indexes["sample"]["keys"][0]["field"] = "absent"
    code = "INDEX_BUDGET_EXCEEDED" if shape in {"indexes", "keys", "paths"} else "INVALID_INDEX_DECLARATION"
    with pytest.raises(types.CollectionTypeError, match=code):
        types.parse_declaration(declaration(fields, indexes), builtin=False)


def test_logical_names_remain_data_and_parsed_plan_is_immutable():
    """Hostile labels must neither become object identifiers nor mutate a sealed plan."""
    label = 'cost"); DROP TABLE items;--'
    data = declaration({label: {"type": "number"}},
                       {label: {"keys": [{"field": label}]}})
    parsed = types.parse_declaration(data, builtin=False)
    data["indexes"][label]["keys"][0]["field"] = "changed"
    assert parsed.indexes[0].name == parsed.indexes[0].keys[0].field == label
    with pytest.raises(AttributeError):
        parsed.indexes[0].name = "changed"
