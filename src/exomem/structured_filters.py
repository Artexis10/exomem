"""Pure, bounded structured-filter compiler and evaluator.

The compiler accepts only the namespaced JSON filter language described by the
semantic-unit retrieval contract.  It produces a deterministic typed AST before
any backend or candidate work; the evaluator is shared by every retrieval lane.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, TypeAlias, cast

from . import semantic_language_registry, semantic_units

MAX_PLAN_BYTES = 16 * 1024
MAX_POINTER_BYTES = 512
MAX_POINTER_SEGMENTS = 16
MAX_STRING_CODEPOINTS = 1024
MAX_STRING_BYTES = 4096
MAX_LIST_VALUES = 64
MAX_TOTAL_VALUES = 256
MAX_NUMERIC_CHARS = 64
MAX_LOGICAL_DEPTH = 4
MAX_LEAF_PREDICATES = 32

_LOGICAL = frozenset({"$and", "$or", "$not"})
_OPERATORS = frozenset(
    {
        "$eq",
        "$ne",
        "$in",
        "$all",
        "$contains",
        "$exists",
        "$gt",
        "$gte",
        "$lt",
        "$lte",
        "$between",
    }
)
_ORDERED = frozenset({"$gt", "$gte", "$lt", "$lte", "$between"})
_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_KNOWN_ARRAY_FIELDS = frozenset(
    {"page.project", "page.tags", "page.speakers", "unit.tags"}
)
_KNOWN_STRING_FIELDS = frozenset(
    {
        "page.status",
        "page.type",
        "page.file_type",
        "unit.category",
        "unit.category_key",
        "unit.kind",
        "unit.context",
        "unit.form",
    }
)
_KNOWN_SCALAR_FIELDS = _KNOWN_STRING_FIELDS | {"page.updated"}
_PAGE_FIELDS = frozenset(
    {
        "page.status",
        "page.type",
        "page.project",
        "page.tags",
        "page.speakers",
        "page.file_type",
        "page.updated",
    }
)
_UNIT_FIELDS = frozenset(
    {
        "unit.category",
        "unit.category_key",
        "unit.kind",
        "unit.tags",
        "unit.context",
        "unit.form",
    }
)

ScalarKind = Literal["null", "boolean", "number", "string", "date", "datetime"]
CategoryResolver = Callable[[str], str]
KindResolver = Callable[[str], str]


class FilterError(ValueError):
    """Stable path-addressed validation failure."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        expected: str,
        remediation: str,
    ) -> None:
        self.code = code
        self.path = path
        self.message = message
        self.expected = expected
        self.remediation = remediation
        super().__init__(f"{code} at {path}: {message}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "expected": self.expected,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class FilterShortcuts:
    types: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    speakers: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()
    exclude_file_types: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    updated_after: str | None = None
    updated_before: str | None = None
    recency_days: int | None = None


@dataclass(frozen=True, slots=True)
class TypedScalar:
    kind: ScalarKind
    value: None | bool | int | float | str | date | datetime

    def json_value(self) -> Any:
        if self.kind == "date":
            assert isinstance(self.value, date) and not isinstance(self.value, datetime)
            return self.value.isoformat()
        if self.kind == "datetime":
            assert isinstance(self.value, datetime)
            value = self.value.astimezone(UTC).isoformat()
            return value.replace("+00:00", "Z")
        return self.value


@dataclass(frozen=True, slots=True)
class FieldReference:
    name: str
    namespace: Literal["page", "unit"]
    path: str
    pointer: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Predicate:
    field: FieldReference
    operators: tuple[tuple[str, TypedScalar | tuple[TypedScalar, ...] | bool], ...]


@dataclass(frozen=True, slots=True)
class AllOf:
    children: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    children: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class Not:
    child: FilterNode


FilterNode: TypeAlias = Predicate | AllOf | AnyOf | Not


@dataclass(frozen=True, slots=True)
class FilterPlan:
    root: FilterNode | None
    has_unit_predicate: bool
    leaf_count: int
    collection_value_count: int

    def to_dict(self) -> dict[str, Any]:
        return _node_to_dict(self.root) if self.root is not None else {}


@dataclass(slots=True)
class _ParseState:
    leaf_count: int = 0
    collection_value_count: int = 0


class _Missing:
    __slots__ = ()


MISSING = _Missing()


def compile_filter(
    expression: Any,
    *,
    shortcuts: FilterShortcuts | None = None,
    resolve_category: CategoryResolver | None = None,
    resolve_kind: KindResolver | None = None,
) -> FilterPlan:
    """Validate and normalize a generic expression plus legacy shortcuts."""
    generic = {} if expression is None else expression
    if not isinstance(generic, Mapping):
        raise _error(
            "INVALID_FILTER_SHAPE",
            "$",
            "filters must be a JSON object",
            "object",
            "Pass a namespaced field expression or logical object.",
        )
    _bounded_json(generic, path="$", code="FILTER_TOO_LARGE")

    active_shortcuts = shortcuts or FilterShortcuts()
    _bounded_json(
        {
            key: value
            for key, value in {
                "types": active_shortcuts.types,
                "projects": active_shortcuts.projects,
                "tags": active_shortcuts.tags,
                "speakers": active_shortcuts.speakers,
                "file_types": active_shortcuts.file_types,
                "exclude_file_types": active_shortcuts.exclude_file_types,
                "categories": active_shortcuts.categories,
                "kinds": active_shortcuts.kinds,
                "updated_after": active_shortcuts.updated_after,
                "updated_before": active_shortcuts.updated_before,
                "recency_days": active_shortcuts.recency_days,
            }.items()
            if value not in ((), None)
        },
        path="$.shortcuts",
        code="FILTER_TOO_LARGE",
    )

    state = _ParseState()
    root = _parse_expression(generic, path="$", depth=0, state=state)
    shortcut_nodes = _compile_shortcuts(active_shortcuts, state=state)
    children = ([root] if root is not None else []) + shortcut_nodes
    structural = _normalize_node(_all_of(children))
    if state.collection_value_count > MAX_TOTAL_VALUES:
        raise _error(
            "FILTER_TOO_COMPLEX",
            "$.shortcuts",
            f"combined filter contains {state.collection_value_count} collection values",
            f"at most {MAX_TOTAL_VALUES} collection and shortcut values",
            "Reduce collection operands or shortcut values.",
        )
    _validate_combined_structure(structural)
    _validate_temporal_conjunctions(structural)
    _bounded_json(
        _node_to_dict(structural) if structural is not None else {},
        path="$",
        code="FILTER_TOO_LARGE",
    )

    category_resolver = resolve_category or (lambda value: value)
    kind_resolver = resolve_kind or (lambda value: value)
    resolved = _normalize_node(
        _resolve_language_aliases(structural, category_resolver, kind_resolver)
    )
    _bounded_json(
        _node_to_dict(resolved) if resolved is not None else {},
        path="$",
        code="FILTER_TOO_LARGE",
    )
    return FilterPlan(
        root=resolved,
        has_unit_predicate=_has_unit_predicate(resolved),
        leaf_count=state.leaf_count,
        collection_value_count=state.collection_value_count,
    )


def evaluate_filter(
    plan: FilterPlan,
    *,
    page: Mapping[str, Any],
    unit: Mapping[str, Any] | None = None,
) -> bool:
    """Evaluate one normalized plan against one parent/unit pair."""
    if plan.root is None:
        return True
    return _evaluate_node(plan.root, page=page, unit=unit) is True


def resolve_result_level(requested: str, plan: FilterPlan) -> str:
    """Resolve ``auto`` without inspecting candidates or backend state."""
    if requested not in {"auto", "page", "unit", "mixed"}:
        raise _error(
            "INVALID_RESULT_LEVEL",
            "$.result_level",
            f"unknown result level {requested!r}",
            "auto, page, unit, or mixed",
            "Choose one of the documented recall result levels.",
        )
    if requested == "auto":
        return "unit" if plan.has_unit_predicate else "page"
    return requested


@dataclass(frozen=True, slots=True)
class IndexCandidateClause:
    """One conjunctive candidate constraint over the category/kind axes.

    Each axis is ``None`` when unconstrained or a (possibly empty) frozenset of
    canonical positive values the index can serve directly.
    """

    category_seeds: frozenset[str] | None
    kind_seeds: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class IndexCandidateAlgebra:
    """Candidate-first classification of a compiled plan.

    ``status`` is ``complete`` when the positive category/kind seeds fully
    describe the candidate set the index can serve (post-evaluation may still
    narrow via page / NOT / unsupported predicates), or ``unsupported`` when no
    positive complete seed exists and recall must fall back to the ordinary
    keyword path.  A ``complete`` result may carry an empty seed set: two exact
    positive equals that intersect to nothing prove ``definitely_empty`` without
    a scope walk.
    """

    status: Literal["complete", "unsupported"]
    definitely_empty: bool
    clauses: tuple[IndexCandidateClause, ...]
    category_seeds: frozenset[str] | None
    kind_seeds: frozenset[str] | None
    post_filter_required: bool

    @property
    def state(self) -> str:
        return self.status

    @property
    def seed_groups(self) -> tuple[tuple[str, frozenset[str]], ...]:
        groups: list[tuple[str, frozenset[str]]] = []
        if self.category_seeds is not None:
            groups.append(("unit.category", self.category_seeds))
        if self.kind_seeds is not None:
            groups.append(("unit.kind", self.kind_seeds))
        return tuple(groups)


@dataclass(frozen=True, slots=True)
class _Seed:
    """Intermediate per-subtree candidate constraint.

    ``clauses`` is the deduplicated disjunction of conjunctive candidate clauses
    the subtree can serve.  ``has_seed`` marks whether the subtree contributes at
    least one positive complete seed; an empty ``clauses`` with ``has_seed`` set
    records a proven contradiction.  ``post`` marks whether the subtree also
    carries predicates left for post-evaluation.
    """

    clauses: tuple[IndexCandidateClause, ...]
    has_seed: bool
    post: bool


_SEEDLESS = _Seed((), False, True)
_EMPTY_SUBTREE = _Seed((), False, False)


def plan_index_candidates(plan: FilterPlan) -> IndexCandidateAlgebra:
    """Classify a compiled plan as a complete or unsupported candidate seed set.

    Exact positive ``unit.category`` / ``unit.kind`` ``$eq`` and ``$in`` provide
    complete seed clauses.  AND takes the Cartesian product of the branch clauses
    and intersects each shared axis, leaving page / NOT / unsupported predicates
    for canonical post-evaluation; OR is complete only when every branch carries
    a complete seed and unions the branch clauses.  A top-level NOT, a page-only
    expression, and an empty plan are unsupported.
    """
    seed = _analyze_index_node(plan.root)
    if not seed.has_seed:
        return IndexCandidateAlgebra(
            status="unsupported",
            definitely_empty=False,
            clauses=(),
            category_seeds=None,
            kind_seeds=None,
            post_filter_required=False,
        )
    category_seeds, kind_seeds, post_multi = _clause_compatibility(seed.clauses)
    return IndexCandidateAlgebra(
        status="complete",
        definitely_empty=not seed.clauses,
        clauses=seed.clauses,
        category_seeds=category_seeds,
        kind_seeds=kind_seeds,
        post_filter_required=seed.post or post_multi,
    )


def _analyze_index_node(node: FilterNode | None) -> _Seed:
    if node is None:
        return _EMPTY_SUBTREE
    if isinstance(node, Predicate):
        return _predicate_seed(node)
    if isinstance(node, AllOf):
        clauses: tuple[IndexCandidateClause, ...] | None = None
        has_seed = False
        post = False
        for child in node.children:
            child_seed = _analyze_index_node(child)
            if not child_seed.has_seed:
                # Page / NOT / unsupported children only post-filter.
                post = True
                continue
            has_seed = True
            post = post or child_seed.post
            if clauses is None:
                clauses = child_seed.clauses
            else:
                clauses = _merge_clause_sets(clauses, child_seed.clauses)
        # Keep ``has_seed`` even when contradiction dropped every clause.
        return _Seed(clauses or (), has_seed, post)
    if isinstance(node, AnyOf):
        collected: list[IndexCandidateClause] = []
        post = False
        for child in node.children:
            child_seed = _analyze_index_node(child)
            if not child_seed.has_seed:
                # A branch without a complete seed makes the whole OR unsupported.
                return _SEEDLESS
            # A seedful empty branch is valid and contributes no clauses.
            collected.extend(child_seed.clauses)
            post = post or child_seed.post
        return _Seed(_dedupe_clauses(collected), True, post)
    # Top-level or nested NOT contributes no positive seed; post-evaluate it.
    return _SEEDLESS


def _predicate_seed(predicate: Predicate) -> _Seed:
    field = predicate.field.name
    if field not in {"unit.category", "unit.kind"}:
        return _SEEDLESS
    positive: frozenset[str] | None = None
    extra = False
    for operator, operand in predicate.operators:
        if operator == "$eq" and isinstance(operand, TypedScalar) and operand.kind == "string":
            assert isinstance(operand.value, str)
            values: frozenset[str] = frozenset({operand.value})
        elif operator == "$in" and isinstance(operand, tuple):
            values = frozenset(
                item.value
                for item in operand
                if isinstance(item, TypedScalar)
                and item.kind == "string"
                and isinstance(item.value, str)
            )
        else:
            # $ne / $contains / $exists on the seed field post-evaluate.
            extra = True
            continue
        positive = values if positive is None else (positive & values)
    if positive is None:
        return _SEEDLESS
    if field == "unit.category":
        clause = IndexCandidateClause(positive, None)
    else:
        clause = IndexCandidateClause(None, positive)
    return _Seed((clause,), True, extra)


def _merge_clause_sets(
    left: tuple[IndexCandidateClause, ...],
    right: tuple[IndexCandidateClause, ...],
) -> tuple[IndexCandidateClause, ...]:
    merged: list[IndexCandidateClause] = []
    for first in left:
        for second in right:
            clause = _merge_clause(first, second)
            if clause is not None:
                merged.append(clause)
    return _dedupe_clauses(merged)


def _merge_clause(
    left: IndexCandidateClause, right: IndexCandidateClause
) -> IndexCandidateClause | None:
    category, category_empty = _merge_axis(left.category_seeds, right.category_seeds)
    kind, kind_empty = _merge_axis(left.kind_seeds, right.kind_seeds)
    if category_empty or kind_empty:
        # A dropped clause is a proven per-clause contradiction.
        return None
    return IndexCandidateClause(category, kind)


def _merge_axis(
    left: frozenset[str] | None, right: frozenset[str] | None
) -> tuple[frozenset[str] | None, bool]:
    if left is None:
        return right, False
    if right is None:
        return left, False
    intersection = left & right
    return intersection, not intersection


def _dedupe_clauses(
    clauses: Sequence[IndexCandidateClause],
) -> tuple[IndexCandidateClause, ...]:
    unique: dict[_ClauseKey, IndexCandidateClause] = {}
    for clause in clauses:
        unique[_clause_sort_key(clause)] = clause
    return tuple(unique[key] for key in sorted(unique))


_AxisKey = tuple[int, tuple[str, ...]]
_ClauseKey = tuple[_AxisKey, _AxisKey]


def _clause_sort_key(clause: IndexCandidateClause) -> _ClauseKey:
    return (_axis_sort_key(clause.category_seeds), _axis_sort_key(clause.kind_seeds))


def _axis_sort_key(axis: frozenset[str] | None) -> _AxisKey:
    # ``None`` sorts distinctly from an empty (contradiction) constraint.
    if axis is None:
        return (0, ())
    return (1, tuple(sorted(axis)))


def _clause_compatibility(
    clauses: tuple[IndexCandidateClause, ...],
) -> tuple[frozenset[str] | None, frozenset[str] | None, bool]:
    if len(clauses) == 1:
        clause = clauses[0]
        return clause.category_seeds, clause.kind_seeds, False
    if not clauses:
        return None, None, False
    category = _union_axis([clause.category_seeds for clause in clauses])
    kind = _union_axis([clause.kind_seeds for clause in clauses])
    return category, kind, True


def _union_axis(axes: Sequence[frozenset[str] | None]) -> frozenset[str] | None:
    result: frozenset[str] = frozenset()
    for axis in axes:
        if axis is None:
            # An unconstrained clause means the axis cannot be narrowed by union.
            return None
        result = result | axis
    return result


def matching_units(
    plan: FilterPlan,
    *,
    page: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Return units that satisfy the whole expression with the same parent.

    A page-only logical branch is also evaluated once with the missing-unit
    sentinel, which lets a page match an OR expression without fabricating a
    child unit.  Callers can distinguish that case through the empty tuple.
    """
    if not plan.has_unit_predicate:
        return tuple(units) if evaluate_filter(plan, page=page) else ()
    matched = tuple(
        unit for unit in units if evaluate_filter(plan, page=page, unit=unit)
    )
    if matched:
        return matched
    # The parent may still satisfy a page-only branch of an OR.
    if evaluate_filter(plan, page=page, unit=None):
        return ()
    return ()


def page_matches(
    plan: FilterPlan,
    *,
    page: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """Evaluate page eligibility with same-unit existential grouping."""
    if not plan.has_unit_predicate:
        return evaluate_filter(plan, page=page)
    return evaluate_filter(plan, page=page, unit=None) or any(
        evaluate_filter(plan, page=page, unit=unit) for unit in units
    )


def page_view(page: Any) -> dict[str, Any]:
    """Adapt a find ``ParsedPage`` without changing YAML runtime types."""
    frontmatter = page.frontmatter
    out: dict[str, Any] = {
        "frontmatter": frontmatter,
        "file_type": page.file_kind,
    }
    for source_key, target_key in (
        ("status", "status"),
        ("type", "type"),
        ("tags", "tags"),
        ("speakers", "speakers"),
    ):
        if source_key in frontmatter:
            out[target_key] = frontmatter[source_key]
    projects: list[Any] = []
    project_declared = "project" in frontmatter or "projects" in frontmatter
    if "project" in frontmatter:
        project = frontmatter["project"]
        if project is not None:
            projects.append(project)
    if "projects" in frontmatter:
        attached = frontmatter["projects"]
        if isinstance(attached, list):
            projects.extend(attached)
        elif attached is not None:
            projects.append(attached)
    # `project` and legacy `projects` are one query field. Preserve declaration
    # separately from membership so $exists distinguishes absent metadata from
    # an explicitly null/empty declaration while collection operators still
    # see the union of all non-null values.
    if project_declared:
        out["project"] = projects
    if "updated" in frontmatter:
        out["updated"] = frontmatter["updated"]
    elif "captured" in frontmatter:
        out["updated"] = frontmatter["captured"]
    return out


def unit_view(unit: Any) -> dict[str, Any]:
    """Adapt one normalized ``SemanticUnit`` for predicate evaluation."""
    return {
        "category": unit.category,
        "category_key": unit.category_key,
        "kind": unit.kind,
        "tags": list(unit.tags),
        "context": unit.context,
        "form": unit.form,
    }


def _parse_expression(
    value: Mapping[Any, Any],
    *,
    path: str,
    depth: int,
    state: _ParseState,
) -> FilterNode | None:
    if not value:
        return None
    if any(not isinstance(key, str) for key in value):
        raise _error(
            "INVALID_FILTER_FIELD",
            path,
            "filter object keys must be strings",
            "namespaced string fields or logical operators",
            "Use page.*, unit.*, $and, $or, or $not keys.",
        )
    logical = [key for key in value if key in _LOGICAL]
    if logical:
        if len(value) != 1:
            raise _error(
                "INVALID_FILTER_SHAPE",
                path,
                "a logical expression cannot be mixed with sibling fields",
                "one $and, $or, or $not key",
                "Wrap sibling predicates inside the logical expression.",
            )
        operator = logical[0]
        if depth >= MAX_LOGICAL_DEPTH:
            raise _error(
                "FILTER_TOO_COMPLEX",
                f"{path}.{operator}",
                "logical nesting exceeds four levels",
                f"at most {MAX_LOGICAL_DEPTH} logical levels",
                "Flatten or split the filter expression.",
            )
        operand = value[operator]
        if operator in {"$and", "$or"}:
            if not isinstance(operand, list) or not operand:
                raise _error(
                    "INVALID_FILTER_VALUE",
                    f"{path}.{operator}",
                    f"{operator} requires a non-empty array",
                    "non-empty expression array",
                    "Provide at least one child expression.",
                )
            children: list[FilterNode] = []
            for index, child in enumerate(operand):
                child_path = f"{path}.{operator}[{index}]"
                if not isinstance(child, Mapping) or not child:
                    raise _error(
                        "INVALID_FILTER_SHAPE",
                        child_path,
                        "logical children must be non-empty objects",
                        "filter expression object",
                        "Provide a field predicate or nested logical expression.",
                    )
                parsed = _parse_expression(
                    child, path=child_path, depth=depth + 1, state=state
                )
                assert parsed is not None
                children.append(parsed)
            return AllOf(tuple(children)) if operator == "$and" else AnyOf(tuple(children))
        if not isinstance(operand, Mapping) or not operand:
            raise _error(
                "INVALID_FILTER_VALUE",
                f"{path}.$not",
                "$not requires exactly one expression object",
                "one non-empty expression object",
                "Pass the expression directly as the $not value.",
            )
        child = _parse_expression(
            operand, path=f"{path}.$not", depth=depth + 1, state=state
        )
        assert child is not None
        return Not(child)

    predicates = [
        _parse_predicate(str(field), operators, path=path, state=state)
        for field, operators in value.items()
    ]
    return _all_of(predicates)


def _parse_predicate(
    field_name: str,
    operators: Any,
    *,
    path: str,
    state: _ParseState,
) -> Predicate:
    field_path = f"{path}.{field_name}"
    field = _parse_field(field_name, field_path)
    if not isinstance(operators, Mapping) or not operators:
        raise _error(
            "INVALID_FILTER_SHAPE",
            field_path,
            "field predicates must be non-empty operator objects",
            "object containing supported $ operators",
            "Wrap the comparison value in an operator such as {$eq: value}.",
        )
    if any(not isinstance(operator, str) for operator in operators):
        raise _error(
            "INVALID_FILTER_OPERATOR",
            field_path,
            "operator names must be strings",
            "supported $ operator",
            "Use a documented structured-filter operator.",
        )
    _register_leaf(state, path=field_path)
    parsed: list[tuple[str, TypedScalar | tuple[TypedScalar, ...] | bool]] = []
    for operator, operand in operators.items():
        operator_path = f"{field_path}.{operator}"
        if operator not in _OPERATORS:
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                f"unknown operator {operator!r}",
                "one of $eq, $ne, $in, $all, $contains, $exists, comparison operators, or $between",
                "Use a documented bounded operator; regex, SQL, and scripts are unsupported.",
            )
        if field.name in _KNOWN_ARRAY_FIELDS and operator in {"$eq", "$ne"}:
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                f"{operator} cannot compare known array field {field.name}",
                "$in, $all, or $contains",
                "Use an array operator for terminal collection fields.",
            )
        if field.name in _KNOWN_ARRAY_FIELDS and operator in _ORDERED:
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                f"{operator} cannot order known array field {field.name}",
                "$in, $all, or $contains",
                "Use an array operator for terminal collection fields.",
            )
        if field.name in _KNOWN_SCALAR_FIELDS and operator == "$all":
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                "$all requires a terminal array field",
                "terminal array field",
                "Use $eq, $in, or $contains for scalar strings.",
            )
        if field.name in _KNOWN_STRING_FIELDS and operator in _ORDERED:
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                f"{operator} cannot order string field {field.name}",
                "$eq, $ne, $in, or $contains",
                "Use exact or substring comparison for string fields.",
            )
        if field.name == "page.updated" and operator == "$contains":
            raise _error(
                "INVALID_FILTER_OPERATOR",
                operator_path,
                "$contains cannot compare the typed date field page.updated",
                "$eq, $ne, $in, or an ordered date comparison",
                "Use an ISO date or timezone-qualified date-time comparison.",
            )
        parsed_operand = _parse_operator_operand(
            field,
            operator,
            operand,
            path=operator_path,
            state=state,
        )
        _validate_closed_field_operand(
            field,
            operator,
            parsed_operand,
            path=operator_path,
        )
        parsed.append((operator, parsed_operand))
    temporal_kinds = {
        scalar.kind
        for _operator, operand in parsed
        for scalar in (
            operand if isinstance(operand, tuple) else (operand,)
        )
        if isinstance(scalar, TypedScalar)
        and scalar.kind in {"date", "datetime"}
    }
    if len(temporal_kinds) > 1:
        raise _invalid_value(
            field_path,
            "one field predicate cannot mix dates and date-times",
            "all temporal operands resolved to dates or all resolved to date-times",
        )
    return Predicate(field=field, operators=tuple(sorted(parsed, key=lambda item: item[0])))


def _parse_field(name: str, path: str) -> FieldReference:
    if name in _PAGE_FIELDS:
        return FieldReference(name=name, namespace="page", path=path)
    if name in _UNIT_FIELDS:
        return FieldReference(name=name, namespace="unit", path=path)
    prefix = "page.frontmatter:"
    if name.startswith(prefix):
        pointer = name[len(prefix) :]
        return FieldReference(
            name=name,
            namespace="page",
            path=path,
            pointer=_decode_pointer(pointer, path=path),
        )
    raise _error(
        "INVALID_FILTER_FIELD",
        path,
        f"unknown filter field {name!r}",
        "reserved page.*, page.frontmatter:/pointer, or closed unit.* field",
        "Correct the field name or address custom metadata through page.frontmatter:/... .",
    )


def _decode_pointer(pointer: str, *, path: str) -> tuple[str, ...]:
    if len(pointer.encode("utf-8")) > MAX_POINTER_BYTES:
        raise _error(
            "FILTER_TOO_LARGE",
            path,
            "frontmatter pointer exceeds 512 UTF-8 bytes",
            f"at most {MAX_POINTER_BYTES} UTF-8 bytes",
            "Use a shorter frontmatter pointer.",
        )
    if not pointer.startswith("/"):
        raise _error(
            "INVALID_FILTER_POINTER",
            path,
            "frontmatter pointer must start with '/'",
            "RFC 6901 pointer beginning with /",
            "Prefix the frontmatter key path with '/'.",
        )
    raw_segments = pointer[1:].split("/")
    if len(raw_segments) > MAX_POINTER_SEGMENTS:
        raise _error(
            "FILTER_TOO_COMPLEX",
            path,
            "frontmatter pointer exceeds 16 decoded segments",
            f"at most {MAX_POINTER_SEGMENTS} segments",
            "Use a shallower metadata path.",
        )
    decoded: list[str] = []
    for segment in raw_segments:
        out: list[str] = []
        index = 0
        while index < len(segment):
            char = segment[index]
            if char != "~":
                out.append(char)
                index += 1
                continue
            if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
                raise _error(
                    "INVALID_FILTER_POINTER",
                    path,
                    "frontmatter pointer contains an invalid '~' escape",
                    "RFC 6901 ~0 or ~1 escape",
                    "Escape '~' as '~0' and '/' inside a key as '~1'.",
                )
            out.append("~" if segment[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(out))
    return tuple(decoded)


def _parse_operator_operand(
    field: FieldReference,
    operator: str,
    operand: Any,
    *,
    path: str,
    state: _ParseState,
) -> TypedScalar | tuple[TypedScalar, ...] | bool:
    if operator == "$exists":
        if type(operand) is not bool:
            raise _invalid_value(path, "$exists requires a boolean", "boolean")
        return operand
    if operator in {"$in", "$all"}:
        if not isinstance(operand, list) or not operand:
            raise _invalid_value(path, f"{operator} requires a non-empty array", "non-empty scalar array")
        _check_list_length(operand, path=path, state=state)
        values = tuple(
            _parse_scalar(item, field=field, ordered=False, path=f"{path}[{index}]")
            for index, item in enumerate(operand)
        )
        return _dedupe_scalars(values)
    if operator == "$between":
        if not isinstance(operand, list) or len(operand) != 2:
            raise _invalid_value(path, "$between requires exactly two values", "two ordered values")
        _check_list_length(operand, path=path, state=state)
        values = tuple(
            _parse_scalar(item, field=field, ordered=True, path=f"{path}[{index}]")
            for index, item in enumerate(operand)
        )
        if values[0].kind != values[1].kind:
            raise _invalid_value(path, "$between bounds must have the same resolved type", "two numbers, dates, or date-times of one type")
        assert values[0].value is not None and values[1].value is not None
        if cast(Any, values[0].value) > cast(Any, values[1].value):
            raise _invalid_value(path, "$between lower bound exceeds upper bound", "ordered inclusive bounds")
        return values
    if operator in _ORDERED:
        return _parse_scalar(operand, field=field, ordered=True, path=path)
    if operator in {"$eq", "$ne", "$contains"}:
        if operator == "$contains" and operand is None:
            raise _invalid_value(path, "$contains requires a non-null scalar", "string, number, or boolean scalar")
        return _parse_scalar(operand, field=field, ordered=False, path=path)
    raise AssertionError(f"unhandled operator: {operator}")


def _parse_scalar(
    value: Any,
    *,
    field: FieldReference,
    ordered: bool,
    path: str,
) -> TypedScalar:
    if value is None:
        if ordered:
            raise _invalid_value(path, "ordered comparison does not accept null", "number, ISO date, or timezone-qualified date-time")
        return TypedScalar("null", None)
    if type(value) is bool:
        if ordered:
            raise _invalid_value(path, "ordered comparison does not accept booleans", "number, ISO date, or timezone-qualified date-time")
        return TypedScalar("boolean", value)
    if type(value) in {int, float}:
        assert isinstance(value, (int, float))
        if not math.isfinite(value):
            raise _invalid_value(path, "numeric operands must be finite", "finite JSON number")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > MAX_NUMERIC_CHARS:
            raise _error(
                "FILTER_TOO_LARGE",
                path,
                "numeric operand exceeds 64 encoded characters",
                f"at most {MAX_NUMERIC_CHARS} encoded characters",
                "Use a shorter finite JSON number.",
            )
        return TypedScalar("number", value)
    if not isinstance(value, str):
        raise _invalid_value(path, "filter operands must be JSON scalars", "string, number, boolean, or null")
    _check_string(value, path=path)
    if field.name == "page.updated" or ordered:
        parsed_temporal = _parse_temporal(value)
        if parsed_temporal is None:
            if ordered:
                raise _invalid_value(path, "ordered string operands must be ISO dates or timezone-qualified RFC 3339 date-times", "ISO date or timezone-qualified RFC 3339 date-time")
            raise _invalid_value(path, "page.updated requires an ISO date or timezone-qualified RFC 3339 date-time", "ISO date or timezone-qualified RFC 3339 date-time")
        return parsed_temporal
    return TypedScalar("string", _canonicalize_string(field.name, value, path=path))


def _validate_closed_field_operand(
    field: FieldReference,
    operator: str,
    operand: TypedScalar | tuple[TypedScalar, ...] | bool,
    *,
    path: str,
) -> None:
    if operator == "$exists" or field.pointer is not None:
        return
    values = operand if isinstance(operand, tuple) else (operand,)
    scalars = tuple(value for value in values if isinstance(value, TypedScalar))
    if field.name == "page.updated":
        allowed = {"date", "datetime"}
        if operator in {"$eq", "$ne"}:
            allowed.add("null")
        if any(value.kind not in allowed for value in scalars):
            raise _invalid_value(
                path,
                "page.updated accepts only typed ISO dates or timezone-qualified date-times",
                "date/date-time operand (or null for $eq/$ne)",
            )
        return
    if field.name in _KNOWN_STRING_FIELDS:
        allowed = {"string"}
        if operator in {"$eq", "$ne"}:
            allowed.add("null")
        if any(value.kind not in allowed for value in scalars):
            raise _invalid_value(
                path,
                f"{field.name} accepts only string operands",
                "string operand (or null for $eq/$ne)",
            )
        return
    if field.name in _KNOWN_ARRAY_FIELDS and any(
        value.kind != "string" for value in scalars
    ):
        raise _invalid_value(
            path,
            f"{field.name} accepts only string collection operands",
            "string scalar or string array operand",
        )


def _parse_temporal(value: str) -> TypedScalar | None:
    try:
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            if parsed_date.isoformat() == value:
                return TypedScalar("date", parsed_date)
        if _RFC3339_DATETIME_RE.fullmatch(value) is None:
            return None
        candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return TypedScalar("datetime", parsed.astimezone(UTC))
    except (TypeError, ValueError):
        return None


def _canonicalize_string(field: str, value: str, *, path: str = "$") -> str:
    if field in {"page.status", "page.type", "page.file_type", "unit.form"}:
        return value.strip().casefold()
    if field in {"page.tags", "page.speakers", "unit.tags"}:
        return value.casefold()
    if field in {"unit.category", "unit.category_key"}:
        try:
            return semantic_units.canonicalize_category(value)
        except ValueError as error:
            raise _invalid_value(path, str(error), "valid semantic-unit category") from error
    if field == "unit.kind":
        normalized = semantic_language_registry.normalize_label(value)
        if not normalized:
            raise _invalid_value(
                path,
                "kind cannot be empty",
                "non-empty governed kind",
            )
        return normalized
    return value


def _check_string(value: str, *, path: str) -> None:
    if len(value) > MAX_STRING_CODEPOINTS or len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise _error(
            "FILTER_TOO_LARGE",
            path,
            "string operand exceeds the codepoint or UTF-8 byte limit",
            f"at most {MAX_STRING_CODEPOINTS} codepoints and {MAX_STRING_BYTES} UTF-8 bytes",
            "Use a shorter string value.",
        )


def _check_list_length(values: list[Any], *, path: str, state: _ParseState) -> None:
    if len(values) > MAX_LIST_VALUES:
        raise _error(
            "FILTER_TOO_COMPLEX",
            path,
            f"collection contains {len(values)} raw values",
            f"at most {MAX_LIST_VALUES} values before deduplication",
            "Reduce the collection operand.",
        )
    state.collection_value_count += len(values)


def _compile_shortcuts(shortcuts: FilterShortcuts, *, state: _ParseState) -> list[FilterNode]:
    nodes: list[FilterNode] = []
    axes = (
        ("types", "page.type", shortcuts.types, False),
        ("projects", "page.project", shortcuts.projects, False),
        ("tags", "page.tags", shortcuts.tags, False),
        ("speakers", "page.speakers", shortcuts.speakers, False),
        ("file_types", "page.file_type", shortcuts.file_types, False),
        ("exclude_file_types", "page.file_type", shortcuts.exclude_file_types, True),
        ("categories", "unit.category", shortcuts.categories, False),
        ("kinds", "unit.kind", shortcuts.kinds, False),
    )
    for shortcut_name, field_name, raw_values, negate in axes:
        if not raw_values:
            continue
        path = f"$.shortcuts.{shortcut_name}"
        values = list(raw_values)
        _check_list_length(values, path=path, state=state)
        _register_leaf(state, path=path)
        field = _parse_field(field_name, path)
        typed = tuple(
            _parse_scalar(value, field=field, ordered=False, path=f"{path}[{index}]")
            for index, value in enumerate(values)
        )
        predicate: FilterNode = Predicate(field, (("$in", _dedupe_scalars(typed)),))
        nodes.append(Not(predicate) if negate else predicate)

    date_ops: list[tuple[str, str, str]] = []
    if shortcuts.updated_after is not None:
        date_ops.append(("$gte", shortcuts.updated_after, "updated_after"))
    if shortcuts.updated_before is not None:
        date_ops.append(("$lte", shortcuts.updated_before, "updated_before"))
    if shortcuts.recency_days is not None:
        if type(shortcuts.recency_days) is not int or shortcuts.recency_days < 0:
            raise _invalid_value(
                "$.shortcuts.recency_days",
                "recency_days must be a non-negative integer",
                "non-negative integer",
            )
        try:
            recency_lower_bound = date.today() - timedelta(
                days=shortcuts.recency_days
            )
        except OverflowError as error:
            raise _invalid_value(
                "$.shortcuts.recency_days",
                "recency_days exceeds the supported calendar range",
                "non-negative integer with a lower bound on or after 0001-01-01",
            ) from error
        date_ops.append(
            ("$gte", recency_lower_bound.isoformat(), "recency_days")
        )
    for operator, value, shortcut_name in date_ops:
        path = f"$.shortcuts.{shortcut_name}"
        state.collection_value_count += 1
        _register_leaf(state, path=path)
        field = _parse_field("page.updated", "$.shortcuts.dates")
        nodes.append(
            Predicate(
                field,
                (
                    (
                        operator,
                        _parse_scalar(
                            value,
                            field=field,
                            ordered=True,
                            path=path,
                        ),
                    ),
                ),
            )
        )
    return nodes


def _resolve_language_aliases(
    node: FilterNode | None,
    category_resolver: CategoryResolver,
    kind_resolver: KindResolver,
) -> FilterNode | None:
    if node is None:
        return None
    if isinstance(node, Predicate):
        if node.field.name not in {"unit.category", "unit.kind"}:
            return node
        resolver = (
            category_resolver if node.field.name == "unit.category" else kind_resolver
        )
        resolved_ops: list[tuple[str, TypedScalar | tuple[TypedScalar, ...] | bool]] = []
        for operator, operand in node.operators:
            if isinstance(operand, TypedScalar):
                resolved_ops.append(
                    (
                        operator,
                        _resolve_scalar_language(
                            operand,
                            resolver,
                            category=node.field.name == "unit.category",
                            path=node.field.path,
                        ),
                    )
                )
            elif isinstance(operand, tuple):
                resolved_ops.append(
                    (
                        operator,
                        _dedupe_scalars(
                            tuple(
                                _resolve_scalar_language(
                                    item,
                                    resolver,
                                    category=node.field.name == "unit.category",
                                    path=node.field.path,
                                )
                                for item in operand
                            )
                        ),
                    )
                )
            else:
                resolved_ops.append((operator, operand))
        return Predicate(node.field, tuple(resolved_ops))
    if isinstance(node, AllOf):
        return AllOf(
            tuple(
                _require_node(
                    _resolve_language_aliases(
                        child, category_resolver, kind_resolver
                    )
                )
                for child in node.children
            )
        )
    if isinstance(node, AnyOf):
        return AnyOf(
            tuple(
                _require_node(
                    _resolve_language_aliases(
                        child, category_resolver, kind_resolver
                    )
                )
                for child in node.children
            )
        )
    return Not(
        _require_node(
            _resolve_language_aliases(
                node.child, category_resolver, kind_resolver
            )
        )
    )


def _resolve_scalar_language(
    value: TypedScalar,
    resolver: Callable[[str], str],
    *,
    category: bool,
    path: str,
) -> TypedScalar:
    if value.kind != "string":
        return value
    assert isinstance(value.value, str)
    try:
        resolved = resolver(value.value)
    except FilterError as error:
        raise _error(
            error.code,
            path,
            error.message,
            error.expected,
            error.remediation,
        ) from error
    if not isinstance(resolved, str) or not resolved:
        raise _error(
            "INVALID_FILTER_VALUE",
            path,
            f"{'category' if category else 'kind'} resolver returned an invalid canonical value",
            "non-empty canonical category key" if category else "non-empty governed kind",
            "Repair the semantic-language registry alias.",
        )
    try:
        canonical = (
            semantic_units.canonicalize_category(resolved)
            if category
            else semantic_language_registry.normalize_label(resolved)
        )
        if not canonical:
            raise ValueError("kind cannot be empty")
    except ValueError as error:
        raise _error(
            "INVALID_FILTER_VALUE",
            path,
            str(error),
            "valid canonical category" if category else "valid governed kind",
            "Repair the semantic-language registry alias.",
        ) from error
    return TypedScalar("string", canonical)


def _evaluate_node(
    node: FilterNode,
    *,
    page: Mapping[str, Any],
    unit: Mapping[str, Any] | None,
) -> bool | None:
    if isinstance(node, Predicate):
        if node.field.namespace == "unit" and unit is None:
            return None
        runtime = _resolve_runtime_field(node.field, page=page, unit=unit)
        return all(
            _evaluate_operator(node.field, runtime, operator, operand)
            for operator, operand in node.operators
        )
    if isinstance(node, AllOf):
        values = [_evaluate_node(child, page=page, unit=unit) for child in node.children]
        if False in values:
            return False
        return None if None in values else True
    if isinstance(node, AnyOf):
        values = [_evaluate_node(child, page=page, unit=unit) for child in node.children]
        if True in values:
            return True
        return None if None in values else False
    value = _evaluate_node(node.child, page=page, unit=unit)
    return None if value is None else not value


def _resolve_runtime_field(
    field: FieldReference,
    *,
    page: Mapping[str, Any],
    unit: Mapping[str, Any] | None,
) -> Any:
    if field.pointer is not None:
        current: Any = page.get("frontmatter", MISSING)
        for segment in field.pointer:
            if not isinstance(current, Mapping):
                return MISSING
            if segment not in current:
                return MISSING
            current = current[segment]
        return current
    if field.namespace == "unit":
        if unit is None:
            return MISSING
        return unit.get(field.name.removeprefix("unit."), MISSING)
    return page.get(field.name.removeprefix("page."), MISSING)


def _evaluate_operator(
    field: FieldReference,
    runtime: Any,
    operator: str,
    operand: TypedScalar | tuple[TypedScalar, ...] | bool,
) -> bool:
    if operator == "$exists":
        assert isinstance(operand, bool)
        return (runtime is not MISSING) is operand
    if runtime is MISSING:
        return False
    if field.name in _KNOWN_ARRAY_FIELDS and not isinstance(runtime, (list, tuple)):
        return False
    if operator in {"$in", "$all", "$between"}:
        assert isinstance(operand, tuple)
        if operator == "$between":
            scalar = _runtime_scalar(runtime, field=field)
            return (
                scalar is not None
                and scalar.kind == operand[0].kind
                and operand[0].value <= scalar.value <= operand[1].value  # type: ignore[operator]
            )
        if isinstance(runtime, (list, tuple)):
            runtime_values = [
                scalar
                for item in runtime
                if (scalar := _runtime_scalar(item, field=field)) is not None
            ]
            if operator == "$in":
                return any(_same_scalar(actual, wanted) for actual in runtime_values for wanted in operand)
            return all(any(_same_scalar(actual, wanted) for actual in runtime_values) for wanted in operand)
        if operator == "$all":
            return False
        scalar = _runtime_scalar(runtime, field=field)
        return scalar is not None and any(_same_scalar(scalar, wanted) for wanted in operand)
    assert isinstance(operand, TypedScalar)
    if operator == "$contains":
        if isinstance(runtime, (list, tuple)):
            return any(
                scalar is not None and _same_scalar(scalar, operand)
                for item in runtime
                if (scalar := _runtime_scalar(item, field=field)) is not None
            )
        if isinstance(runtime, str) and operand.kind == "string":
            actual = _runtime_scalar(runtime, field=field)
            return (
                actual is not None
                and isinstance(actual.value, str)
                and isinstance(operand.value, str)
                and operand.value in actual.value
            )
        return False
    scalar = _runtime_scalar(runtime, field=field)
    if scalar is None:
        return False
    if operator == "$eq":
        return _same_scalar(scalar, operand)
    if operator == "$ne":
        return scalar.kind == operand.kind and scalar.value != operand.value
    if scalar.kind != operand.kind or scalar.kind not in {"number", "date", "datetime"}:
        return False
    if operator == "$gt":
        return scalar.value > operand.value  # type: ignore[operator]
    if operator == "$gte":
        return scalar.value >= operand.value  # type: ignore[operator]
    if operator == "$lt":
        return scalar.value < operand.value  # type: ignore[operator]
    if operator == "$lte":
        return scalar.value <= operand.value  # type: ignore[operator]
    raise AssertionError(f"unhandled operator: {operator}")


def _runtime_scalar(value: Any, *, field: FieldReference) -> TypedScalar | None:
    if value is None:
        return TypedScalar("null", None)
    if type(value) is bool:
        return TypedScalar("boolean", value)
    if type(value) is int:
        return TypedScalar("number", value)
    if type(value) is float:
        return TypedScalar("number", value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return TypedScalar("datetime", value.astimezone(UTC))
    if isinstance(value, date):
        return TypedScalar("date", value)
    if isinstance(value, str):
        return TypedScalar("string", _canonicalize_string(field.name, value))
    return None


def _same_scalar(left: TypedScalar, right: TypedScalar) -> bool:
    return left.kind == right.kind and left.value == right.value


def _all_of(children: Sequence[FilterNode | None]) -> FilterNode | None:
    concrete = tuple(child for child in children if child is not None)
    if not concrete:
        return None
    if len(concrete) == 1:
        return concrete[0]
    return AllOf(concrete)


def _normalize_node(node: FilterNode | None) -> FilterNode | None:
    if node is None or isinstance(node, Predicate):
        return node
    if isinstance(node, Not):
        child = _normalize_node(node.child)
        assert child is not None
        return Not(child)
    normalized_children: list[FilterNode] = []
    same_type = AllOf if isinstance(node, AllOf) else AnyOf
    for child in node.children:
        normalized = _normalize_node(child)
        assert normalized is not None
        if isinstance(normalized, same_type):
            normalized_children.extend(normalized.children)
        else:
            normalized_children.append(normalized)
    unique = {_canonical_node(child): child for child in normalized_children}
    ordered = tuple(unique[key] for key in sorted(unique))
    if len(ordered) == 1:
        return ordered[0]
    return AllOf(ordered) if isinstance(node, AllOf) else AnyOf(ordered)


def _node_to_dict(node: FilterNode | None) -> dict[str, Any]:
    if node is None:
        return {}
    if isinstance(node, Predicate):
        return {
            node.field.name: {
                operator: (
                    [item.json_value() for item in operand]
                    if isinstance(operand, tuple)
                    else operand.json_value()
                    if isinstance(operand, TypedScalar)
                    else operand
                )
                for operator, operand in node.operators
            }
        }
    if isinstance(node, AllOf):
        return {"$and": [_node_to_dict(child) for child in node.children]}
    if isinstance(node, AnyOf):
        return {"$or": [_node_to_dict(child) for child in node.children]}
    return {"$not": _node_to_dict(node.child)}


def _canonical_node(node: FilterNode) -> str:
    return json.dumps(
        _node_to_dict(node),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dedupe_scalars(values: tuple[TypedScalar, ...]) -> tuple[TypedScalar, ...]:
    unique = {
        json.dumps(
            [value.kind, value.json_value()],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ): value
        for value in values
    }
    return tuple(unique[key] for key in sorted(unique))


def _has_unit_predicate(node: FilterNode | None) -> bool:
    if node is None:
        return False
    if isinstance(node, Predicate):
        return node.field.namespace == "unit"
    if isinstance(node, Not):
        return _has_unit_predicate(node.child)
    return any(_has_unit_predicate(child) for child in node.children)


def _register_leaf(state: _ParseState, *, path: str) -> None:
    state.leaf_count += 1
    if state.leaf_count > MAX_LEAF_PREDICATES:
        raise _error(
            "FILTER_TOO_COMPLEX",
            path,
            "combined filter contains more than 32 leaf predicates",
            f"at most {MAX_LEAF_PREDICATES} leaf predicates",
            "Reduce or split the filter expression or its shortcuts.",
        )


def _validate_combined_structure(node: FilterNode | None, *, depth: int = 0) -> int:
    if node is None:
        return 0
    if isinstance(node, Predicate):
        return 1
    if depth >= MAX_LOGICAL_DEPTH:
        raise _error(
            "FILTER_TOO_COMPLEX",
            "$",
            "combined generic and shortcut plan exceeds four logical levels",
            f"at most {MAX_LOGICAL_DEPTH} logical levels",
            "Flatten the generic expression or remove independent shortcuts.",
        )
    if isinstance(node, Not):
        return _validate_combined_structure(node.child, depth=depth + 1)
    leaves = sum(
        _validate_combined_structure(child, depth=depth + 1)
        for child in node.children
    )
    if leaves > MAX_LEAF_PREDICATES:
        raise _error(
            "FILTER_TOO_COMPLEX",
            "$",
            "combined normalized plan exceeds 32 leaf predicates",
            f"at most {MAX_LEAF_PREDICATES} leaf predicates",
            "Reduce or split the filter expression or its shortcuts.",
        )
    return leaves


def _validate_temporal_conjunctions(
    node: FilterNode | None,
) -> dict[str, tuple[set[int], str]]:
    """Track date/date-time kinds across every possible conjunctive branch.

    Bit 1 represents a date and bit 2 a date-time.  Each field has at most
    four possible masks, so OR branches stay bounded instead of expanding to
    disjunctive normal form.  A mask of 3 means one executable conjunction
    mixes the two temporal types and is rejected.  A bare OR may retain
    separate masks {1, 2} because its alternatives are not conjunctive.
    """
    if node is None:
        return {}
    if isinstance(node, Predicate):
        mask = 0
        for _operator, operand in node.operators:
            scalars = operand if isinstance(operand, tuple) else (operand,)
            for scalar in scalars:
                if not isinstance(scalar, TypedScalar):
                    continue
                if scalar.kind == "date":
                    mask |= 1
                elif scalar.kind == "datetime":
                    mask |= 2
        if mask == 3:
            raise _invalid_value(
                node.field.path,
                "one field predicate cannot mix dates and date-times",
                "all temporal operands resolved to dates or all resolved to date-times",
            )
        return {node.field.name: ({mask}, node.field.path)} if mask else {}
    if isinstance(node, Not):
        _validate_temporal_conjunctions(node.child)
        # A negated predicate does not impose its runtime type on siblings.
        return {}

    child_states = [_validate_temporal_conjunctions(child) for child in node.children]
    fields = set().union(*(set(states) for states in child_states))
    if isinstance(node, AnyOf):
        alternatives: dict[str, tuple[set[int], str]] = {}
        for field_name in fields:
            states: set[int] = set()
            paths: list[str] = []
            for child in child_states:
                if field_name in child:
                    child_masks, child_path = child[field_name]
                    states.update(child_masks)
                    paths.append(child_path)
                else:
                    states.add(0)
            alternatives[field_name] = (states, min(paths))
        return alternatives

    combined: dict[str, tuple[set[int], str]] = {}
    for child in child_states:
        for field_name in set(combined) | set(child):
            prior_masks, prior_path = combined.get(field_name, ({0}, ""))
            next_masks, next_path = child.get(field_name, ({0}, prior_path))
            merged = {prior | following for prior in prior_masks for following in next_masks}
            if 3 in merged:
                raise _invalid_value(
                    next_path or prior_path,
                    f"conjunctive predicates for {field_name} mix dates and date-times",
                    "all temporal operands for one conjunctive field resolved to one temporal type",
                )
            combined[field_name] = (merged, next_path or prior_path)
    return combined


def _bounded_json(value: Any, *, path: str, code: str) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _error(
            "INVALID_FILTER_VALUE",
            path,
            "filters must contain finite JSON values",
            "JSON object containing only finite JSON values",
            "Remove non-JSON or non-finite values.",
        ) from error
    if len(encoded) > MAX_PLAN_BYTES:
        raise _error(
            code,
            path,
            f"encoded filter plan is {len(encoded)} bytes",
            f"at most {MAX_PLAN_BYTES} UTF-8 bytes",
            "Reduce the filter expression or shortcut values.",
        )


def _invalid_value(path: str, message: str, expected: str) -> FilterError:
    return _error(
        "INVALID_FILTER_VALUE",
        path,
        message,
        expected,
        "Use a value with the documented operator type and arity.",
    )


def _error(
    code: str,
    path: str,
    message: str,
    expected: str,
    remediation: str,
) -> FilterError:
    return FilterError(
        code,
        path,
        message,
        expected=expected,
        remediation=remediation,
    )


def _require_node(node: FilterNode | None) -> FilterNode:
    assert node is not None
    return node
