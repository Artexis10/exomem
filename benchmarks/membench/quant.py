"""Unit-aware deterministic arithmetic evaluator over TypedValue quantities.

Pure module (stdlib ``decimal`` + the schema types, nothing else): the
quantitative family's oracle computes expected answers here at generation
time. All arithmetic is :class:`decimal.Decimal` under an explicit local
context — float values are rejected outright, and float equality is never
used anywhere.

Rules:

- Units come from a small **closed table** over three dimensions
  (mass ``g``/``kg``, length ``m``/``km``, time ``min``/``h``). Converting
  between different units requires both to be table entries of the same
  dimension; identical unit strings never need the table, so same-unit
  arithmetic works for any unit label (for example ``points``).
- Every derivation returns a :class:`DerivedQuantity` — canonical string
  value, unit, and a ``Decimal`` tolerance — ready for an
  ``ExpectedAnswer``. With ``places=None`` the result must be **exact**
  (the ``Inexact`` signal is trapped; a non-terminating result raises) and
  the tolerance is ``0``. With ``places=k`` the result is quantized to
  ``k`` decimal places (ROUND_HALF_EVEN) and the tolerance is the
  half-quantum rounding allowance ``0.5 * 10**-k``.
- :func:`within_tolerance` is the matching rule: a canonical candidate
  string is accepted iff ``abs(candidate - value) <= tolerance`` in exact
  Decimal arithmetic (the boundary is inclusive).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    localcontext,
)

from membench.schema import TypedValue

_PRECISION = 28

# unit -> (dimension, factor to the dimension's base unit)
_UNIT_TABLE: dict[str, tuple[str, Decimal]] = {
    "g": ("mass", Decimal(1)),
    "kg": ("mass", Decimal(1000)),
    "m": ("length", Decimal(1)),
    "km": ("length", Decimal(1000)),
    "min": ("time", Decimal(1)),
    "h": ("time", Decimal(60)),
}

SUPPORTED_UNITS = frozenset(_UNIT_TABLE)


class QuantityError(ValueError):
    """Non-quantity input, unknown unit, cross-dimension arithmetic,
    float input, or a non-terminating result without an explicit ``places``."""


@dataclass(frozen=True)
class DerivedQuantity:
    """One oracle-computed derivation, ready for an ``ExpectedAnswer``."""

    value: str  # canonical decimal string (no exponent notation)
    unit: str | None  # None = dimensionless (ratio)
    tolerance: Decimal  # 0 when exact; half-quantum when quantized


def _guard_not_float(raw: object, what: str) -> None:
    if isinstance(raw, float):
        raise QuantityError(f"{what} must not be a float (got {raw!r}); use str/int/Decimal")


def _decimal(raw: str | int | Decimal, what: str) -> Decimal:
    _guard_not_float(raw, what)
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise QuantityError(f"{what} {raw!r} is not a decimal number") from exc


def _table_entry(unit: str) -> tuple[str, Decimal]:
    entry = _UNIT_TABLE.get(unit)
    if entry is None:
        raise QuantityError(
            f"unknown unit {unit!r}; the closed unit table supports "
            f"{sorted(SUPPORTED_UNITS)}"
        )
    return entry


def _canonical(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return str(value.normalize())


def _compute(operation, *, places: int | None) -> tuple[Decimal, Decimal]:
    """Run ``operation`` deterministically; return (result, tolerance)."""

    if places is not None and (not isinstance(places, int) or places < 0):
        raise QuantityError(f"places must be a non-negative int, got {places!r}")
    with localcontext(Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)) as ctx:
        if places is None:
            ctx.traps[Inexact] = True
            try:
                return operation(), Decimal(0)
            except Inexact:
                raise QuantityError(
                    "result is not exactly representable; pass places= to "
                    "quantize with a rounding tolerance"
                ) from None
        quantum = Decimal(1).scaleb(-places)
        result = operation().quantize(quantum, rounding=ROUND_HALF_EVEN)
        return result, quantum / 2


def parse_quantity(value: TypedValue) -> tuple[Decimal, str | None]:
    """The (magnitude, unit) of a ``kind="quantity"`` TypedValue."""

    if value.kind != "quantity":
        raise QuantityError(f"expected a quantity TypedValue, got kind {value.kind!r}")
    return _decimal(value.value, "quantity value"), value.unit


def convert_value(
    value: str | int | Decimal, unit: str, to_unit: str, *, places: int | None = None
) -> Decimal:
    """Convert a magnitude between two closed-table units (same dimension)."""

    magnitude = _decimal(value, "value")
    if unit == to_unit:
        return magnitude
    from_dimension, from_factor = _table_entry(unit)
    to_dimension, to_factor = _table_entry(to_unit)
    if from_dimension != to_dimension:
        raise QuantityError(
            f"cannot convert {unit!r} ({from_dimension}) to {to_unit!r} "
            f"({to_dimension}): different dimensions"
        )
    result, _ = _compute(lambda: magnitude * from_factor / to_factor, places=places)
    return result


def _base_magnitudes(
    unit: str | None, *values: TypedValue
) -> tuple[list[Decimal], Decimal, str]:
    """Operand magnitudes in base units plus the target unit's factor.

    When every operand already carries the target unit, factors are 1 and the
    closed table is never consulted — same-unit arithmetic works for any unit
    label. Differing units must all be table entries of one dimension.
    """

    parsed = [parse_quantity(value) for value in values]
    target = unit if unit is not None else parsed[0][1]
    if target is None:
        raise QuantityError("operands need a unit (or pass unit= explicitly)")
    units = {operand_unit for _, operand_unit in parsed}
    if units == {target}:
        return [magnitude for magnitude, _ in parsed], Decimal(1), target
    target_dimension, target_factor = _table_entry(target)
    magnitudes = []
    for magnitude, operand_unit in parsed:
        if operand_unit is None:
            raise QuantityError("every operand quantity needs a unit")
        dimension, factor = _table_entry(operand_unit)
        if dimension != target_dimension:
            raise QuantityError(
                f"cannot combine {operand_unit!r} ({dimension}) with "
                f"{target!r} ({target_dimension}): different dimensions"
            )
        magnitudes.append(magnitude * factor)
    return magnitudes, target_factor, target


def derive_sum(
    a: TypedValue, b: TypedValue, *, unit: str | None = None, places: int | None = None
) -> DerivedQuantity:
    """``a + b`` expressed in ``unit`` (default: a's unit)."""

    (left, right), factor, target = _base_magnitudes(unit, a, b)
    result, tolerance = _compute(lambda: (left + right) / factor, places=places)
    return DerivedQuantity(value=_canonical(result), unit=target, tolerance=tolerance)


def derive_difference(
    a: TypedValue, b: TypedValue, *, unit: str | None = None, places: int | None = None
) -> DerivedQuantity:
    """``a - b`` expressed in ``unit`` (default: a's unit)."""

    (left, right), factor, target = _base_magnitudes(unit, a, b)
    result, tolerance = _compute(lambda: (left - right) / factor, places=places)
    return DerivedQuantity(value=_canonical(result), unit=target, tolerance=tolerance)


def derive_ratio(
    a: TypedValue, b: TypedValue, *, places: int | None = None
) -> DerivedQuantity:
    """Dimensionless ``a / b`` after aligning ``b`` with ``a``'s unit."""

    (left, right), _, _ = _base_magnitudes(None, a, b)
    if right == 0:
        raise QuantityError("ratio divisor is zero")
    result, tolerance = _compute(lambda: left / right, places=places)
    return DerivedQuantity(value=_canonical(result), unit=None, tolerance=tolerance)


def derive_scale(
    a: TypedValue,
    factor: str | int | Decimal,
    *,
    unit: str | None = None,
    places: int | None = None,
) -> DerivedQuantity:
    """``a * factor`` expressed in ``unit`` (default: a's unit)."""

    (left,), unit_factor, target = _base_magnitudes(unit, a)
    multiplier = _decimal(factor, "scale factor")
    result, tolerance = _compute(lambda: left * multiplier / unit_factor, places=places)
    return DerivedQuantity(value=_canonical(result), unit=target, tolerance=tolerance)


_OPERATIONS = {
    "sum": derive_sum,
    "difference": derive_difference,
    "ratio": derive_ratio,
}


def derive(
    op: str,
    a: TypedValue,
    b: TypedValue,
    *,
    unit: str | None = None,
    places: int | None = None,
) -> DerivedQuantity:
    """Dispatch a two-operand derivation (``sum``/``difference``/``ratio``)."""

    operation = _OPERATIONS.get(op)
    if operation is None:
        raise QuantityError(f"unknown operation {op!r}; expected one of {sorted(_OPERATIONS)}")
    if op == "ratio":
        if unit is not None:
            raise QuantityError("ratio is dimensionless; unit= is not accepted")
        return derive_ratio(a, b, places=places)
    return operation(a, b, unit=unit, places=places)


def within_tolerance(derived: DerivedQuantity, candidate: str) -> bool:
    """Inclusive tolerance check in exact Decimal arithmetic (never float)."""

    _guard_not_float(candidate, "candidate")
    if not isinstance(candidate, str):
        raise QuantityError(f"candidate must be a canonical string, got {candidate!r}")
    try:
        offered = Decimal(candidate)
    except InvalidOperation as exc:
        raise QuantityError(f"candidate {candidate!r} is not a decimal number") from exc
    return abs(offered - Decimal(derived.value)) <= derived.tolerance
