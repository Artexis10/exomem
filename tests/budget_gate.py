"""One byte-budget gate: fail at the ceiling, warn in the band below it."""

from __future__ import annotations

import warnings


def check_budget(size: int, *, ceiling: int, band: int, label: str) -> None:
    """Fail above `ceiling`; warn (never fail) when fewer than `band` bytes remain."""
    assert size <= ceiling, (
        f"{label} is {size:,} bytes, over the {ceiling:,} ceiling by {size - ceiling:,}"
    )
    headroom = ceiling - size
    if headroom < band:
        warnings.warn(
            f"{label} is {size:,} bytes with only {headroom:,} bytes under the "
            f"{ceiling:,} ceiling. The next addition of any size will trip it: trim, "
            "or raise the ceiling with the argument its constant requires, but decide "
            "it deliberately rather than discovering it as a red CI run.",
            UserWarning,
            stacklevel=3,
        )
