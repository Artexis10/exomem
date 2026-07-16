from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from exomem.structured_filters import (
    FilterError,
    FilterShortcuts,
    compile_filter,
    evaluate_filter,
    matching_units,
    page_matches,
    page_view,
    resolve_result_level,
)


def _page(**overrides: object) -> dict[str, object]:
    page: dict[str, object] = {
        "status": "active",
        "type": "insight",
        "project": ["alpha"],
        "tags": ["auth", "oauth"],
        "speakers": ["Alice"],
        "file_type": "note",
        "updated": date(2026, 1, 15),
        "frontmatter": {},
    }
    page.update(overrides)
    return page


def _unit(**overrides: object) -> dict[str, object]:
    unit: dict[str, object] = {
        "category": "config",
        "category_key": "configuration",
        "kind": "decision",
        "tags": ["auth", "sqlite"],
        "context": "Windows rollout",
        "form": "compact",
    }
    unit.update(overrides)
    return unit


def _error(expression: object) -> FilterError:
    with pytest.raises(FilterError) as caught:
        compile_filter(expression)
    return caught.value


def test_namespaces_and_rfc6901_mapping_only_traversal() -> None:
    plan = compile_filter(
        {
            "page.frontmatter:/vendor~1id/~0name": {"$eq": "exact"},
            "page.frontmatter:/numeric/0": {"$eq": "mapping-zero"},
        }
    )
    matching = _page(
        frontmatter={
            "vendor/id": {"~name": "exact"},
            "numeric": {"0": "mapping-zero"},
        }
    )
    array_at_numeric = _page(
        frontmatter={
            "vendor/id": {"~name": "exact"},
            "numeric": ["mapping-zero"],
        }
    )
    assert evaluate_filter(plan, page=matching)
    assert not evaluate_filter(plan, page=array_at_numeric)

    bad_escape = _error({"page.frontmatter:/bad~2escape": {"$exists": True}})
    assert (bad_escape.code, bad_escape.path) == (
        "INVALID_FILTER_POINTER",
        "$.page.frontmatter:/bad~2escape",
    )
    unknown = _error({"unit.categry": {"$eq": "config"}})
    assert (unknown.code, unknown.path) == (
        "INVALID_FILTER_FIELD",
        "$.unit.categry",
    )


def test_missing_null_and_exact_type_equality_are_distinct() -> None:
    eq_null = compile_filter({"page.frontmatter:/owner": {"$eq": None}})
    missing = _page(frontmatter={})
    explicit_null = _page(frontmatter={"owner": None})
    string_value = _page(frontmatter={"owner": "sam"})
    assert not evaluate_filter(eq_null, page=missing)
    assert evaluate_filter(eq_null, page=explicit_null)
    assert not evaluate_filter(eq_null, page=string_value)

    ne_string = compile_filter({"page.frontmatter:/owner": {"$ne": "sam"}})
    assert not evaluate_filter(ne_string, page=missing)
    assert not evaluate_filter(ne_string, page=explicit_null)
    assert not evaluate_filter(ne_string, page=string_value)
    assert evaluate_filter(ne_string, page=_page(frontmatter={"owner": "lee"}))

    exists_false = compile_filter(
        {"page.frontmatter:/owner": {"$exists": False}}
    )
    assert evaluate_filter(exists_false, page=missing)
    assert not evaluate_filter(exists_false, page=explicit_null)

    bool_plan = compile_filter({"page.frontmatter:/flag": {"$eq": True}})
    assert evaluate_filter(bool_plan, page=_page(frontmatter={"flag": True}))
    assert not evaluate_filter(bool_plan, page=_page(frontmatter={"flag": 1}))


def test_array_and_string_operators_remain_distinct() -> None:
    page = _page(frontmatter={"labels": ["auth", "oauth"], "title": "oauth rollout"})
    assert evaluate_filter(
        compile_filter({"page.frontmatter:/labels": {"$contains": "auth"}}),
        page=page,
    )
    assert evaluate_filter(
        compile_filter({"page.frontmatter:/labels": {"$all": ["oauth", "auth"]}}),
        page=page,
    )
    assert not evaluate_filter(
        compile_filter({"page.frontmatter:/labels": {"$all": ["auth", "billing"]}}),
        page=page,
    )
    assert evaluate_filter(
        compile_filter({"page.frontmatter:/labels": {"$in": ["billing", "oauth"]}}),
        page=page,
    )
    assert evaluate_filter(
        compile_filter({"page.frontmatter:/title": {"$contains": "roll"}}),
        page=page,
    )
    assert not evaluate_filter(
        compile_filter({"page.frontmatter:/labels": {"$eq": "auth"}}),
        page=page,
    )

    known_array = _error({"unit.tags": {"$eq": "auth"}})
    assert known_array.code == "INVALID_FILTER_OPERATOR"
    assert known_array.path == "$.unit.tags.$eq"


def test_ordered_numbers_dates_and_datetimes_are_typed_and_inclusive() -> None:
    numeric = compile_filter(
        {"page.frontmatter:/priority": {"$gte": 3, "$lt": 5}}
    )
    assert evaluate_filter(numeric, page=_page(frontmatter={"priority": 3}))
    assert not evaluate_filter(numeric, page=_page(frontmatter={"priority": 5}))
    assert not evaluate_filter(numeric, page=_page(frontmatter={"priority": "3"}))

    dates = compile_filter(
        {"page.updated": {"$between": ["2026-01-01", "2026-01-31"]}}
    )
    assert evaluate_filter(dates, page=_page(updated=date(2026, 1, 1)))
    assert evaluate_filter(dates, page=_page(updated=date(2026, 1, 31)))
    assert not evaluate_filter(dates, page=_page(updated=date(2026, 2, 1)))

    moments = compile_filter(
        {
            "page.frontmatter:/moment": {
                "$gte": "2026-01-01T12:00:00+02:00"
            }
        }
    )
    assert evaluate_filter(
        moments,
        page=_page(
            frontmatter={
                "moment": datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            }
        ),
    )
    assert not evaluate_filter(
        moments,
        page=_page(frontmatter={"moment": "2026-01-01T10:00:00Z"}),
    )

    mixed_dates = _error(
        {
            "page.updated": {
                "$between": ["2026-01-01", "2026-01-31T00:00:00Z"]
            }
        }
    )
    assert mixed_dates.code == "INVALID_FILTER_VALUE"
    assert _error({"page.updated": {"$gte": "yesterday"}}).code == (
        "INVALID_FILTER_VALUE"
    )
    assert _error({"page.frontmatter:/x": {"$gt": True}}).code == (
        "INVALID_FILTER_VALUE"
    )
    assert _error({"page.status": {"$gt": "2026-01-01"}}).code == (
        "INVALID_FILTER_OPERATOR"
    )
    assert _error({"page.tags": {"$gte": 3}}).code == (
        "INVALID_FILTER_OPERATOR"
    )
    assert _error({"page.updated": {"$contains": "2026"}}).code == (
        "INVALID_FILTER_OPERATOR"
    )


@pytest.mark.parametrize(
    "expression",
    [
        {
            "page.updated": {
                "$gte": "2026-01-01",
                "$lte": "2026-01-31T00:00:00Z",
            }
        },
        {
            "page.frontmatter:/moment": {
                "$gte": "2026-01-01",
                "$lte": "2026-01-31T00:00:00Z",
            }
        },
        {
            "page.updated": {
                "$in": ["2026-01-01", "2026-01-31T00:00:00Z"]
            }
        },
    ],
)
def test_one_field_predicate_cannot_mix_dates_and_datetimes(
    expression: object,
) -> None:
    error = _error(expression)
    assert error.code == "INVALID_FILTER_VALUE"


def test_conjunctive_nodes_and_shortcuts_cannot_mix_temporal_kinds() -> None:
    split = {
        "$and": [
            {"page.updated": {"$gte": "2026-01-01"}},
            {"page.updated": {"$lte": "2026-01-31T00:00:00Z"}},
        ]
    }
    assert _error(split).code == "INVALID_FILTER_VALUE"

    with pytest.raises(FilterError) as generic_shortcut:
        compile_filter(
            {"page.updated": {"$gte": "2026-01-01T00:00:00Z"}},
            shortcuts=FilterShortcuts(updated_before="2026-01-31"),
        )
    assert generic_shortcut.value.code == "INVALID_FILTER_VALUE"

    with pytest.raises(FilterError) as shortcuts_only:
        compile_filter(
            None,
            shortcuts=FilterShortcuts(
                updated_after="2026-01-01",
                updated_before="2026-01-31T00:00:00Z",
            ),
        )
    assert shortcuts_only.value.code == "INVALID_FILTER_VALUE"


def test_or_branches_may_use_alternate_temporal_kinds() -> None:
    compile_filter(
        {
            "$or": [
                {"page.updated": {"$gte": "2026-01-01"}},
                {"page.updated": {"$lte": "2026-01-31T00:00:00Z"}},
            ]
        }
    )


def test_logic_same_field_conjunction_and_normalization_are_deterministic() -> None:
    first = compile_filter(
        {
            "$and": [
                {"page.status": {"$eq": "active"}},
                {
                    "$or": [
                        {"page.project": {"$contains": "beta"}},
                        {"page.project": {"$contains": "alpha"}},
                    ]
                },
                {"page.frontmatter:/priority": {"$gte": 2, "$lte": 4}},
            ]
        }
    )
    second = compile_filter(
        {
            "$and": [
                {"page.frontmatter:/priority": {"$lte": 4, "$gte": 2}},
                {
                    "$or": [
                        {"page.project": {"$contains": "alpha"}},
                        {"page.project": {"$contains": "beta"}},
                    ]
                },
                {"page.status": {"$eq": "active"}},
            ]
        }
    )
    assert first.to_dict() == second.to_dict()
    assert evaluate_filter(first, page=_page(frontmatter={"priority": 3}))
    assert not evaluate_filter(
        first, page=_page(status="draft", frontmatter={"priority": 3})
    )

    negated = compile_filter(
        {"$not": {"page.frontmatter:/owner": {"$eq": "sam"}}}
    )
    assert evaluate_filter(negated, page=_page(frontmatter={}))


def test_shortcuts_share_one_plan_and_category_alias_resolution() -> None:
    plan = compile_filter(
        {"page.status": {"$eq": "active"}},
        shortcuts=FilterShortcuts(
            types=("insight",),
            projects=("alpha", "beta"),
            tags=("AUTH",),
            speakers=("ALICE",),
            file_types=("NOTE",),
            categories=("configuration",),
            kinds=("Decision",),
        ),
        resolve_category=lambda value: {
            "configuration": "config"
        }.get(value, value),
    )
    normalized = json.dumps(plan.to_dict(), sort_keys=True)
    assert "configuration" not in normalized
    assert "config" in normalized
    assert plan.has_unit_predicate
    assert evaluate_filter(plan, page=_page(), unit=_unit())
    assert not evaluate_filter(
        plan, page=_page(project=["gamma"]), unit=_unit()
    )
    assert not evaluate_filter(plan, page=_page(), unit=_unit(category="rule"))


def test_page_matching_uses_one_unit_and_keeps_page_only_or_branch() -> None:
    plan = compile_filter(
        {
            "$and": [
                {"unit.category": {"$eq": "config"}},
                {"unit.tags": {"$contains": "auth"}},
            ]
        }
    )
    split_units = [
        _unit(category="config", tags=["sqlite"]),
        _unit(category="rule", tags=["auth"]),
    ]
    assert matching_units(plan, page=_page(), units=split_units) == ()
    together = _unit(category="config", tags=["auth"])
    assert matching_units(plan, page=_page(), units=[*split_units, together]) == (
        together,
    )

    page_or_unit = compile_filter(
        {
            "$or": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "config"}},
            ]
        }
    )
    assert evaluate_filter(page_or_unit, page=_page(), unit=None)
    assert not evaluate_filter(
        page_or_unit, page=_page(status="draft"), unit=None
    )

    not_config = compile_filter(
        {"$not": {"unit.category": {"$eq": "config"}}}
    )
    assert not page_matches(not_config, page=_page(), units=[_unit(category="config")])
    assert page_matches(not_config, page=_page(), units=[_unit(category="rule")])


def test_result_level_auto_is_driven_only_by_unit_predicates() -> None:
    page_plan = compile_filter({"page.status": {"$eq": "active"}})
    unit_plan = compile_filter({"unit.category": {"$eq": "config"}})
    assert resolve_result_level("auto", page_plan) == "page"
    assert resolve_result_level("auto", unit_plan) == "unit"
    assert resolve_result_level("page", unit_plan) == "page"
    assert resolve_result_level("mixed", page_plan) == "mixed"
    with pytest.raises(FilterError) as caught:
        resolve_result_level("units", unit_plan)
    assert caught.value.code == "INVALID_RESULT_LEVEL"


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ({"$and": []}, "INVALID_FILTER_VALUE"),
        ({"$or": []}, "INVALID_FILTER_VALUE"),
        ({"$not": []}, "INVALID_FILTER_VALUE"),
        ({"page.status": {"$regex": "active"}}, "INVALID_FILTER_OPERATOR"),
        ({"page.status": {"$in": []}}, "INVALID_FILTER_VALUE"),
        ({"page.updated": {"$between": ["2026-01-01"]}}, "INVALID_FILTER_VALUE"),
    ],
)
def test_invalid_shapes_fail_closed(expression: object, code: str) -> None:
    assert _error(expression).code == code


def test_complexity_and_operand_limits_apply_before_deduplication() -> None:
    fifth_level = {
        "$not": {
            "$not": {
                "$not": {
                    "$not": {
                        "$not": {"page.status": {"$eq": "active"}}
                    }
                }
            }
        }
    }
    assert _error(fifth_level).code == "FILTER_TOO_COMPLEX"

    leaves = [
        {f"page.frontmatter:/field-{index}": {"$exists": True}}
        for index in range(33)
    ]
    assert _error({"$and": leaves}).code == "FILTER_TOO_COMPLEX"

    duplicate_values = ["same"] * 65
    assert _error({"page.tags": {"$in": duplicate_values}}).code == (
        "FILTER_TOO_COMPLEX"
    )
    assert _error({"page.frontmatter:/x": {"$eq": "x" * 1025}}).code == (
        "FILTER_TOO_LARGE"
    )
    assert _error(
        {"page.frontmatter:/x": {"$eq": "\N{PILE OF POO}" * 1025}}
    ).code == "FILTER_TOO_LARGE"
    assert _error({"page.frontmatter:/x": {"$eq": float("inf")}}).code == (
        "INVALID_FILTER_VALUE"
    )
    assert _error(
        {"page.frontmatter:/" + "/".join(["x"] * 17): {"$exists": True}}
    ).code == "FILTER_TOO_COMPLEX"

    huge_raw = {"page.frontmatter:/x": {"$in": ["x" * 300] * 64}}
    assert _error(huge_raw).code == "FILTER_TOO_LARGE"


def test_shortcut_limits_count_raw_values_before_alias_resolution() -> None:
    with pytest.raises(FilterError) as caught:
        compile_filter(
            None,
            shortcuts=FilterShortcuts(categories=tuple(["alias"] * 65)),
            resolve_category=lambda _value: "config",
        )
    assert caught.value.code == "FILTER_TOO_COMPLEX"
    assert caught.value.path == "$.shortcuts.categories"

    with pytest.raises(FilterError) as oversized:
        compile_filter(
            None,
            shortcuts=FilterShortcuts(tags=tuple(["x" * 300] * 64)),
        )
    assert oversized.value.code == "FILTER_TOO_LARGE"
    assert oversized.value.path == "$.shortcuts"


def test_malformed_runtime_array_field_does_not_gain_scalar_membership() -> None:
    plan = compile_filter(None, shortcuts=FilterShortcuts(tags=("auth",)))
    assert evaluate_filter(plan, page=_page(tags=["auth"]))
    assert not evaluate_filter(plan, page=_page(tags="auth"))


def test_mapping_values_support_exists_but_not_equality() -> None:
    page = _page(frontmatter={"metadata": {"nested": "value"}})
    assert evaluate_filter(
        compile_filter({"page.frontmatter:/metadata": {"$exists": True}}),
        page=page,
    )
    assert not evaluate_filter(
        compile_filter({"page.frontmatter:/metadata": {"$eq": None}}),
        page=page,
    )


def test_independent_pointer_numeric_and_combined_value_bounds() -> None:
    assert _error(
        {"page.frontmatter:/" + ("x" * 513): {"$exists": True}}
    ).code == "FILTER_TOO_LARGE"
    assert _error(
        {"page.frontmatter:/n": {"$eq": 10**64}}
    ).code == "FILTER_TOO_LARGE"

    collections = {
        "$and": [
            {
                f"page.frontmatter:/values-{index}": {
                    "$in": list(range(64))
                }
            }
            for index in range(4)
        ]
    }
    # Exactly 256 collection values is allowed.
    compile_filter(collections)
    collections["$and"].append(
        {"page.frontmatter:/one-more": {"$in": [1]}}
    )
    assert _error(collections).code == "FILTER_TOO_COMPLEX"


def test_combined_shortcut_plan_rechecks_depth_leaves_and_raw_values() -> None:
    leaves = {
        "$and": [
            {f"page.frontmatter:/field-{index}": {"$exists": True}}
            for index in range(32)
        ]
    }
    with pytest.raises(FilterError) as too_many_leaves:
        compile_filter(leaves, shortcuts=FilterShortcuts(types=("insight",)))
    assert too_many_leaves.value.code == "FILTER_TOO_COMPLEX"

    four_deep = {
        "$not": {
            "$not": {
                "$not": {
                    "$not": {"page.status": {"$eq": "active"}}
                }
            }
        }
    }
    compile_filter(four_deep)
    with pytest.raises(FilterError) as too_deep:
        compile_filter(four_deep, shortcuts=FilterShortcuts(types=("insight",)))
    assert too_deep.value.code == "FILTER_TOO_COMPLEX"

    values = {
        "$and": [
            {
                f"page.frontmatter:/values-{index}": {
                    "$in": list(range(64))
                }
            }
            for index in range(4)
        ]
    }
    with pytest.raises(FilterError) as too_many_values:
        compile_filter(
            values,
            shortcuts=FilterShortcuts(updated_after="2026-01-01"),
        )
    assert too_many_values.value.code == "FILTER_TOO_COMPLEX"


def test_date_shortcuts_do_not_crash_when_lower_bounds_overlap() -> None:
    plan = compile_filter(
        None,
        shortcuts=FilterShortcuts(
            updated_after="2026-01-01",
            recency_days=7,
        ),
    )
    assert plan.leaf_count == 2


def test_shortcut_errors_keep_stable_paths_and_bound_recency_overflow() -> None:
    with pytest.raises(FilterError) as empty_kind:
        compile_filter(None, shortcuts=FilterShortcuts(kinds=(" ",)))
    assert empty_kind.value.path == "$.shortcuts.kinds[0]"

    with pytest.raises(FilterError) as huge_recency:
        compile_filter(None, shortcuts=FilterShortcuts(recency_days=10**30))
    assert huge_recency.value.code == "INVALID_FILTER_VALUE"
    assert huge_recency.value.path == "$.shortcuts.recency_days"


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"project": None},
        {"projects": None},
        {"projects": []},
    ],
)
def test_declared_empty_project_metadata_is_present(frontmatter: object) -> None:
    page = page_view(SimpleNamespace(frontmatter=frontmatter, file_kind="note"))
    exists = compile_filter({"page.project": {"$exists": True}})
    contains = compile_filter({"page.project": {"$contains": "alpha"}})
    assert evaluate_filter(exists, page=page)
    assert not evaluate_filter(contains, page=page)


def test_absent_project_metadata_remains_missing() -> None:
    page = page_view(SimpleNamespace(frontmatter={}, file_kind="note"))
    assert evaluate_filter(
        compile_filter({"page.project": {"$exists": False}}),
        page=page,
    )


def test_quoted_updated_string_is_not_coerced_and_rfc3339_is_strict() -> None:
    plan = compile_filter({"page.updated": {"$gte": "2026-01-01"}})
    assert evaluate_filter(plan, page=_page(updated=date(2026, 1, 1)))
    assert not evaluate_filter(plan, page=_page(updated="2026-01-01"))
    assert _error(
        {
            "page.frontmatter:/moment": {
                "$gte": "2026-01-01 10:00:00+00:00"
            }
        }
    ).code == "INVALID_FILTER_VALUE"


def test_runtime_arbitrary_precision_integer_does_not_crash() -> None:
    plan = compile_filter({"page.frontmatter:/x": {"$eq": 1}})
    assert not evaluate_filter(
        plan,
        page=_page(frontmatter={"x": 10**309}),
    )


@pytest.mark.parametrize(
    "expression",
    [
        {"page.updated": {"$gte": 3}},
        {"page.status": {"$eq": 3}},
        {"unit.kind": {"$contains": True}},
        {"page.tags": {"$in": ["auth", 3]}},
    ],
)
def test_closed_fields_reject_impossible_operand_types(expression: object) -> None:
    assert _error(expression).code == "INVALID_FILTER_VALUE"


def test_alias_resolved_plan_rechecks_the_16_kib_bound() -> None:
    expression = {
        "$and": [
            {
                "unit.category": {
                    "$in": [f"a{group}_{index}" for index in range(64)]
                }
            }
            for group in range(4)
        ]
    }

    def expand(value: str) -> str:
        return (value + "_" + ("x" * 64))[:64]

    with pytest.raises(FilterError) as caught:
        compile_filter(expression, resolve_category=expand)
    assert caught.value.code == "FILTER_TOO_LARGE"


@pytest.mark.parametrize(
    "field",
    [
        "types",
        "projects",
        "tags",
        "speakers",
        "file_types",
        "exclude_file_types",
        "categories",
        "kinds",
    ],
)
def test_every_shortcut_list_enforces_the_raw_64_value_limit(field: str) -> None:
    shortcuts = replace(FilterShortcuts(), **{field: tuple(["same"] * 65)})
    with pytest.raises(FilterError) as caught:
        compile_filter(None, shortcuts=shortcuts)
    assert caught.value.code == "FILTER_TOO_COMPLEX"
    assert caught.value.path == f"$.shortcuts.{field}"
