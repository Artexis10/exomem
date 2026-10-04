"""Pure v1 contract tests, with no store, service or real vault state."""

from dataclasses import FrozenInstanceError

import pytest

from exomem.query_engine.validation import normalize_query


@pytest.fixture
def declarations():
    return {
        "runs": {
            "domain": "collections",
            "type": "run",
            "vault": "fixture",
            "fields": {
                "item_key": {"type": "string"},
                "status": {"type": "string"},
                "minutes": {"type": "number"},
                "attempts": {"type": "integer"},
                "started_at": {"type": "datetime"},
                "day": {"type": "date"},
                "metadata": {"type": "object"},
                "metadata.title": {"type": "string"},
                "recipe_key": {"type": "string"},
            },
            "relations": {
                "recipe": {
                    "field": "recipe_key",
                    "target_type": "recipe",
                    "target": "recipes",
                    "cardinality": "many-to-one",
                },
            },
        },
        "recipes": {
            "domain": "collections",
            "type": "recipe",
            "vault": "fixture",
            "fields": {"item_key": {"type": "string"}, "title": {"type": "string"}},
            "relations": {},
        },
        "pages": {
            "domain": "documents",
            "type": "page",
            "vault": "fixture",
            "fields": {"title": {"type": "string"}},
            "relations": {},
        },
        "links": {
            "domain": "graph",
            "type": "graph",
            "vault": "fixture",
            "fields": {},
            "relations": {},
            "relation_types": {"supports": "relation:1"},
        },
    }


def validate(declarations, **query):
    return normalize_query({"version": 1, **query}, declarations=declarations, collection="runs")


def test_normalization_binds_fields_and_keeps_injection_literal_in_immutable_ir(declarations):
    # Catches accidental executable identifier/value handling and mutable caller aliases.
    literal = "'); DROP TABLE items; --"
    request = {
        "version": 1,
        "select": ["metadata.title"],
        "where": {"field": "status", "op": "eq", "value": literal},
    }
    result = normalize_query(request, declarations=declarations, collection="runs")
    assert result is not None, "v1 normalization is absent"
    assert result.findings == ()
    assert result.query.source.ref == "runs"
    assert result.query.where.value == literal
    assert result.query.select.fields[0].path == "metadata.title"
    assert result.query.mode == "execute"
    assert result.query.execution_profile == "interactive"
    assert result.query.page.limit == 50
    assert result.query.order_by.keys[-1].field.path == "item_key"
    request["select"].append("status")
    assert len(result.query.select.fields) == 1
    with pytest.raises(FrozenInstanceError):
        result.query.mode = "preview"
    assert not hasattr(result.query, "admitted")


@pytest.mark.parametrize(
    "query,code,at",
    [
        ({"sql": "SELECT * FROM items"}, "QUERY_KEY_UNKNOWN", "sql"),
        ({"admitted": True}, "QUERY_KEY_UNKNOWN", "admitted"),
        ({"version": True}, "QUERY_VERSION_UNSUPPORTED", "version"),
        (
            {"where": {"field": "minutes", "op": "sql", "value": 1}},
            "QUERY_OPERATOR_UNKNOWN",
            "where.op",
        ),
        ({"select": ["minutes + 1"]}, "QUERY_FIELD_UNKNOWN", "select[0]"),
        (
            {"where": {"field": "status", "op": "gt", "value": 2}},
            "QUERY_VALUE_INVALID",
            "where.value",
        ),
        (
            {"where": {"field": "attempts", "op": "eq", "value": True}},
            "QUERY_VALUE_INVALID",
            "where.value",
        ),
        (
            {"where": {"field": "started_at", "op": "gte", "value": "2026-10-01T12:00:00"}},
            "QUERY_VALUE_INVALID",
            "where.value",
        ),
        (
            {"where": {"field": "minutes", "op": "between", "value": {"lower": 4, "upper": 1}}},
            "QUERY_VALUE_INVALID",
            "where.value",
        ),
        (
            {"where": {"field": "minutes", "op": "is_null", "value": None}},
            "QUERY_KEY_UNKNOWN",
            "where.value",
        ),
        ({"page": {"offset": 5}}, "QUERY_KEY_UNKNOWN", "page.offset"),
        ({"mode": "author"}, "QUERY_VALUE_INVALID", "mode"),
        ({"execution_profile": "unlimited"}, "QUERY_VALUE_INVALID", "execution_profile"),
    ],
)
def test_invalid_request_has_repairable_addressed_findings(declarations, query, code, at):
    result = validate(declarations, **query)
    assert result is not None, "v1 validation is absent"
    assert result.query is None
    finding = result.findings[0].as_dict()
    assert finding["code"] == code
    assert finding["at"] == at
    assert set(finding) == {"code", "at", "expected", "allowed", "repair", "retryable"}
    assert finding["repair"]
    assert finding["retryable"] is False


def test_null_operators_and_nested_booleans_preserve_separate_operations(declarations):
    result = validate(
        declarations,
        where={
            "all": [
                {"field": "minutes", "op": "is_null"},
                {
                    "not": {
                        "any": [
                            {"field": "status", "op": "is_missing"},
                            {"field": "status", "op": "is_not_null"},
                        ]
                    }
                },
            ]
        },
    )
    assert result is not None, "v1 boolean normalization is absent"
    assert result.findings == ()
    assert result.query.where.children[0].op == "is_null"
    assert result.query.where.children[1].children[0].children[0].op == "is_missing"


def test_between_inclusivity_defaults_and_boolean_flags_survive_normalization(declarations):
    predicate = {"field": "minutes", "op": "between", "value": {"lower": 1, "upper": 3}}
    result = validate(declarations, where=predicate)
    assert result.findings == ()
    assert result.query.where.value.include_lower is True
    assert result.query.where.value.include_upper is True
    predicate["value"].update(include_lower=False, include_upper=True)
    result = validate(declarations, where=predicate)
    assert result.findings == (), "Boolean range inclusivity was refused"
    assert result.query.where.value.include_lower is False
    assert result.query.where.value.include_upper is True
    predicate["value"].update(include_lower=True, include_upper=False)
    result = validate(declarations, where=predicate)
    assert result.findings == ()
    assert result.query.where.value.include_lower is True
    assert result.query.where.value.include_upper is False


def test_between_inclusivity_refuses_integer_boolean_coercion(declarations):
    result = validate(
        declarations,
        where={
            "field": "minutes",
            "op": "between",
            "value": {
                "lower": 1,
                "upper": 3,
                "include_upper": 1,
            },
        },
    )
    assert result.query is None
    assert result.findings[0].code == "QUERY_VALUE_INVALID"
    assert result.findings[0].at == "where.value.include_upper"


def test_legacy_shaping_conflicts_but_selector_does_not(declarations):
    result = normalize_query(
        {"version": 1},
        declarations=declarations,
        collection="runs",
        legacy_arguments={"filters": [], "limit": 50},
    )
    assert result is not None, "v1 conflict validation is absent"
    assert result.findings[0].code == "QUERY_ARGUMENT_CONFLICT"


def test_declared_join_binds_target_without_arbitrary_keys(declarations):
    result = validate(
        declarations,
        joins=[{"relation": "recipe", "alias": "r", "kind": "left"}],
        select=["item_key", "r.title"],
        order_by=[{"field": "r.title"}],
    )
    assert result is not None, "v1 relation binding is absent"
    assert result.findings == ()
    assert result.query.joins[0].target.ref == "recipes"
    assert result.query.select.fields[1].source.ref == "recipes"
    assert result.query.select.fields[1].alias == "r"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"cardinality": "one-to-many"}, "QUERY_UNSUPPORTED"),
        ({"target_type": "other"}, "QUERY_RELATION_INVALID"),
        ({"target": "runs"}, "QUERY_RELATION_INVALID"),
    ],
)
def test_relation_contract_rejects_fanout_wrong_type_and_cycles(declarations, change, code):
    declarations["runs"]["relations"]["recipe"].update(change)
    result = validate(declarations, joins=[{"relation": "recipe", "alias": "r", "kind": "inner"}])
    assert result is not None, "v1 relation checks are absent"
    assert result.findings[0].code == code


def test_cross_vault_join_is_refused(declarations):
    declarations["recipes"]["vault"] = "other"
    result = validate(declarations, joins=[{"relation": "recipe", "alias": "r", "kind": "left"}])
    assert result is not None, "v1 vault checks are absent"
    assert result.findings[0].code == "QUERY_RELATION_INVALID"


def test_grouped_query_binds_having_alias_and_group_page(declarations):
    result = validate(
        declarations,
        group_by=[{"field": "day", "bucket": "month"}],
        aggregates={
            "runs": {"op": "count"},
            "typical": {"op": "percentile", "field": "minutes", "p": 0.5},
        },
        having={"field": "runs", "op": "gte", "value": 3},
    )
    assert result is not None, "v1 grouping validation is absent"
    assert result.findings == ()
    assert result.query.having.field.path == "runs"
    assert result.query.page.limit == 50
    assert result.query.aggregate.groups[0].bucket == "month"


def test_datetime_bucket_binds_calendar_date_for_having_projection_and_order(declarations):
    result = validate(
        declarations,
        group_by=[{"field": "started_at", "bucket": "day"}],
        having={"field": "started_at", "op": "eq", "value": "2026-10-02"},
        select=["started_at"],
        order_by=[{"field": "started_at"}],
    )
    assert result.findings == (), "calendar bucket was validated as an input instant"
    assert result.query.aggregate.groups[0].field.value_type == "datetime"
    assert result.query.having.field.value_type == "date"
    assert result.query.select.fields[0].value_type == "date"
    assert [key.field.value_type for key in result.query.order_by.keys] == ["date"]
    result = validate(declarations, group_by=[{"field": "started_at", "bucket": "month"}])
    assert result.findings == ()
    assert result.query.order_by.keys[0].field.value_type == "date"


def test_duplicate_group_paths_refuse_ambiguous_reduction_outputs(declarations):
    result = validate(
        declarations, group_by=[{"field": "day"}, {"field": "day", "bucket": "month"}]
    )
    assert result.query is None, "two reductions shared the same output field identity"
    assert result.findings[0].code == "QUERY_VALUE_INVALID"
    assert result.findings[0].at == "group_by[1].field"


@pytest.mark.parametrize(
    "query,at",
    [
        ({"select": ["status"] * 33}, "select"),
        ({"order_by": [{"field": "status"}] * 5}, "order_by"),
        ({"where": {"all": [{"field": "status", "op": "eq", "value": "a"}] * 65}}, "where.all"),
        ({"where": {"field": "status", "op": "in", "value": ["a"] * 101}}, "where.value"),
        ({"page": {"limit": 1001}}, "page.limit"),
        ({"group_by": [{"field": "status"}] * 5}, "group_by"),
        ({"aggregates": {f"n{i}": {"op": "count"} for i in range(9)}}, "aggregates"),
        ({"where": {"field": "status", "op": "eq", "value": "a" * 17000}}, "query"),
    ],
)
def test_input_caps_refuse_instead_of_silent_truncation(declarations, query, at):
    result = validate(declarations, **query)
    assert result is not None, "v1 input limits are absent"
    assert result.query is None
    assert result.findings[0].code == "QUERY_INPUT_LIMIT"
    assert result.findings[0].at == at


def test_boolean_depth_is_bounded_before_recursion(declarations):
    where = {"field": "status", "op": "eq", "value": "a"}
    for _ in range(9):
        where = {"not": where}
    result = validate(declarations, where=where)
    assert result is not None, "v1 depth limit is absent"
    assert result.findings[0].code == "QUERY_INPUT_LIMIT"


def test_generic_source_respects_facade_domains_and_adapter_capabilities(declarations):
    query = {"version": 1, "source": {"domain": "documents", "ref": "pages"}}
    result = normalize_query(query, declarations=declarations, allowed_domains=("collections",))
    assert result is not None, "v1 source validation is absent"
    assert result.findings[0].code == "QUERY_SOURCE_INVALID"
    result = normalize_query(query, declarations=declarations)
    assert result.findings[0].code == "QUERY_UNSUPPORTED"


def test_open_grammar_is_explicitly_unsupported(declarations):
    result = validate(declarations, text={"all": ["literal phrase"]})
    assert result is not None, "v1 capability findings are absent"
    assert result.findings[0].code == "QUERY_UNSUPPORTED"


def test_traversal_payload_binds_declared_relations_and_defaults(declarations):
    # Catches an unbound relation label reaching future backend compilation.
    from exomem.query_engine import validation

    normalizer = getattr(validation, "normalize_traversal", lambda *args, **kwargs: None)
    result = normalizer(
        {"anchors": ["page:a"], "relations": ["supports"]},
        relation_types=declarations["links"]["relation_types"],
        capabilities=frozenset({"graph"}),
    )
    assert result is not None, "internal traversal normalization is absent"
    assert result.findings == ()
    assert result.node.anchors == ("page:a",)
    assert result.node.relations == ("relation:1",)
    assert (result.node.min_hops, result.node.max_hops) == (1, 3)


@pytest.mark.parametrize(
    "payload,code,at",
    [
        ({"anchors": []}, "QUERY_VALUE_INVALID", "graph.anchors"),
        ({"anchors": ["page:a"] * 9}, "QUERY_INPUT_LIMIT", "graph.anchors"),
        (
            {"anchors": ["page:a"], "min_hops": 4, "max_hops": 3},
            "QUERY_VALUE_INVALID",
            "graph.min_hops",
        ),
        ({"anchors": ["page:a"], "max_hops": 6}, "QUERY_INPUT_LIMIT", "graph.max_hops"),
        (
            {"anchors": ["page:a"], "relations": ["unknown"]},
            "QUERY_RELATION_INVALID",
            "graph.relations[0]",
        ),
        (
            {"anchors": ["page:a"], "relations": ["supports"] * 17},
            "QUERY_INPUT_LIMIT",
            "graph.relations",
        ),
        (
            {"anchors": ["page:a"], "direction": "sideways"},
            "QUERY_VALUE_INVALID",
            "graph.direction",
        ),
        ({"anchors": ["page:a"], "subtypes": "true"}, "QUERY_VALUE_INVALID", "graph.subtypes"),
    ],
)
def test_traversal_payload_refuses_unbounded_or_unbound_steps(declarations, payload, code, at):
    from exomem.query_engine import validation

    normalizer = getattr(validation, "normalize_traversal", lambda *args, **kwargs: None)
    result = normalizer(
        payload,
        relation_types=declarations["links"]["relation_types"],
        capabilities=frozenset({"graph"}),
    )
    assert result is not None, "internal traversal validation is absent"
    assert result.node is None
    assert (result.findings[0].code, result.findings[0].at) == (code, at)


def test_path_payload_is_bounded_and_does_not_grant_adapter_support():
    from exomem.query_engine import validation

    normalizer = getattr(validation, "normalize_path", lambda *args, **kwargs: None)
    payload = {"kind": "shortest", "from": "page:a", "to": "page:b", "max_hops": 5}
    result = normalizer(payload)
    assert result is not None, "internal path normalization is absent"
    assert result.findings[0].code == "QUERY_UNSUPPORTED"
    result = normalizer(payload, capabilities=frozenset({"graph"}))
    assert result.findings == ()
    assert result.node.start == "page:a"
    assert result.node.end == "page:b"
    assert result.node.max_hops == 5


def test_group_pages_cannot_use_row_page_limit(declarations):
    result = validate(declarations, aggregates={"n": {"op": "count"}}, page={"limit": 201})
    assert result.findings[0].code == "QUERY_INPUT_LIMIT"


def test_having_cannot_address_ungrouped_input_values(declarations):
    result = validate(
        declarations,
        aggregates={"n": {"op": "count"}},
        having={"field": "minutes", "op": "gte", "value": 3},
    )
    assert result.findings[0].code == "QUERY_FIELD_UNKNOWN"


def test_typed_date_bounds_normalize_utc_and_relative_values(declarations):
    result = validate(
        declarations,
        as_of="2026-10-02T01:00:00+01:00",
        where={
            "field": "started_at",
            "op": "gte",
            "value": {"relative_to": "as_of", "amount": -30, "unit": "day"},
        },
    )
    assert result.findings == ()
    assert result.query.as_of == "2026-10-02T00:00:00+00:00"
    assert result.query.where.value.amount == -30


def test_external_graph_container_remains_unsupported(declarations):
    result = normalize_query(
        {
            "version": 1,
            "source": {"domain": "graph", "ref": "links"},
            "graph": {"anchors": ["page:a"]},
        },
        declarations=declarations,
        capabilities=frozenset({"graph"}),
    )
    assert result.findings[0].code == "QUERY_UNSUPPORTED"


def test_large_exact_integer_does_not_overflow_float_validation(declarations):
    # JSON integers can exceed a float's range without becoming nonfinite values.
    result = validate(declarations, where={"field": "minutes", "op": "eq", "value": 10**400})
    assert result.findings == ()
    assert result.query.where.value == 10**400


def test_join_alias_cannot_shadow_declared_dotted_field(declarations):
    declarations["runs"]["fields"]["r.title"] = {"type": "string"}
    result = validate(declarations, joins=[{"relation": "recipe", "alias": "r", "kind": "left"}])
    assert result.query is None, "ambiguous alias was accepted"
    assert result.findings[0].code == "QUERY_RELATION_INVALID"


def test_boolean_minimum_is_not_an_ordered_aggregate(declarations):
    declarations["runs"]["fields"]["flag"] = {"type": "boolean"}
    result = validate(declarations, aggregates={"minimum": {"op": "min", "field": "flag"}})
    assert result.query is None, "unordered boolean minimum was accepted"
    assert result.findings[0].code == "QUERY_VALUE_INVALID"


def test_internal_graph_payload_is_also_byte_bounded(declarations):
    from exomem.query_engine.validation import normalize_traversal

    result = normalize_traversal(
        {"anchors": ["a" * 17000]}, relation_types={}, capabilities=frozenset({"graph"})
    )
    assert result.node is None, "oversized traversal payload was accepted"
    assert result.findings[0].code == "QUERY_INPUT_LIMIT"


def test_existing_enum_declaration_binds_scalar_type_without_constraining_query_literals(
    declarations,
):
    declarations["runs"]["fields"]["status"] = {"type": "enum", "enum": ["ready", "done"]}
    result = validate(declarations, where={"field": "status", "op": "eq", "value": "ready"})
    assert result.findings == (), "existing declared enum cannot be queried"
    assert result.query.where.field.value_type == "string"
    assert result.query.where.field.enum == ("ready", "done")
    result = validate(declarations, where={"field": "status", "op": "eq", "value": "other"})
    assert result.findings == ()


def test_declared_link_field_can_bind_a_many_to_one_relation(declarations):
    declarations["runs"]["fields"]["recipe_key"] = {"type": "link", "link_kind": "record"}
    result = validate(declarations, joins=[{"relation": "recipe", "alias": "r", "kind": "left"}])
    assert result.findings == (), "existing declared link cannot be joined"
    assert result.query.joins[0].source_field.value_type == "link"


def test_graph_source_without_settled_container_cannot_become_an_anchor_free_scan(declarations):
    result = normalize_query(
        {"version": 1, "source": {"domain": "graph", "ref": "links"}},
        declarations=declarations,
        capabilities=frozenset({"graph"}),
    )
    assert result.query is None, "anchor-free external graph query was normalized"
    assert result.findings[0].code == "QUERY_UNSUPPORTED"


def test_invalid_unicode_returns_a_finding_instead_of_escaping(declarations):
    result = validate(declarations, where={"field": "status", "op": "eq", "value": "\ud800"})
    assert result.query is None
    assert result.findings[0].code == "QUERY_VALUE_INVALID"


def test_large_percentile_is_a_structured_refusal(declarations):
    result = validate(
        declarations, aggregates={"p": {"op": "percentile", "field": "minutes", "p": 10**400}}
    )
    assert result.query is None
    assert result.findings[0].at == "aggregates.p.p"


def test_count_field_can_count_non_null_structured_values(declarations):
    result = validate(declarations, aggregates={"present": {"op": "count", "field": "metadata"}})
    assert result.findings == (), "count(field) unnecessarily requires scalar values"
    assert result.query.aggregate.values[0].field.value_type == "object"


@pytest.mark.parametrize(
    "declaration,predicate",
    [
        ({"type": "string", "enum": ["open", "complete"]}, {"op": "startswith", "value": "com"}),
        ({"type": "enum", "enum": ["open", "complete"]}, {"op": "contains", "value": "com"}),
        ({"type": "enum", "enum": [1, 2]}, {"op": "between", "value": {"lower": 0.5, "upper": 3}}),
    ],
)
def test_query_predicate_literals_are_not_proposed_enum_values(
    declarations, declaration, predicate
):
    declarations["runs"]["fields"]["status"] = declaration
    result = validate(declarations, where={"field": "status", **predicate})
    assert result.findings == (), "write-only enum membership restricted the query literal"
