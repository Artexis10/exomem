"""What `exomem status` and `exomem doctor` say about a vault's layout.

`report` reads the layout through `layout.inspect` and turns every outcome into a result that
leads with a short sentence: a resolved layout, a layout that the record and folders cannot yet
decide, or a layout that could not be read at all. It never raises and never refuses. The
refusal that `layout.resolve` raises for a vault-root schema folder without a record is a
finding here, and its remedy names the record to write.

Vault resolution and service start call neither this module nor `layout.resolve` yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import layout

#: The report's `layout` value when the record and folders cannot decide a layout, or when
#: the read failed: both are states to show, never a guess at `nested` or `root`. A closed
#: pair that this report fixes; the other values come from `layout.Layout`.
UNRESOLVED = "unresolved"
UNKNOWN = "unknown"


def _leftover_remedy(folders: tuple[str, ...], holder: str) -> str:
    where = ", ".join(f"{folder}/" for folder in folders)
    return (
        f"The record in {holder}/ decides the layout, so {where} changes nothing. A relayout "
        "in progress leaves it until it finishes. Afterwards, check that it holds nothing "
        f"that {holder}/ lacks, then remove it."
    )


def report(vault_root: Path) -> dict[str, Any]:
    """The layout of the vault at `vault_root` as a JSON-ready result, outcome first.

    Keys: `outcome` (a sentence to repeat as it is), `layout` (`nested`, `root`, `unresolved`
    or `unknown`), `record` (its `path`, `layout` and `unmanaged`, or None when the vault has
    none), `leftover_schema_folders` (a second schema folder beside a record), `remedy` (what
    to change, or None when nothing needs a person) and `diagnostics` (the error's code, with
    its message when the layout is unresolved, else None).
    """
    try:
        resolved = layout.inspect(vault_root)
    except layout.LayoutError as error:
        return {
            "outcome": f"The layout of this vault is not resolved. {error.message}",
            "layout": UNRESOLVED,
            "record": None,
            "leftover_schema_folders": [],
            "remedy": error.remediation,
            "diagnostics": {"code": error.code, "message": error.message},
        }
    except Exception as error:  # noqa: BLE001 - an unreadable store is unknown, as in `store_mode.status`
        reason = str(getattr(error, "code", type(error).__name__))
        return {
            "outcome": f"The layout of this vault could not be read ({reason}).",
            "layout": UNKNOWN,
            "record": None,
            "leftover_schema_folders": [],
            "remedy": "Repeat the check once the vault's store answers.",
            "diagnostics": {"code": reason},
        }
    record = resolved.record
    leftover = resolved.leftover_schema_folders
    outcome = (
        f"This vault uses the {resolved.layout.value} layout. It has no layout record, "
        "which is how a vault reads until a record is written."
        if record is None
        else f"This vault uses the {resolved.layout.value} layout, as its record at {record.path} says."
    )
    remedy = None
    if record is not None and leftover:
        outcome += f" A second schema folder remains at {', '.join(f'{folder}/' for folder in leftover)}."
        remedy = _leftover_remedy(leftover, record.path.rpartition("/")[0])
    return {
        "outcome": outcome,
        "layout": resolved.layout.value,
        "record": (
            None
            if record is None
            else {"path": record.path, "layout": record.layout.value, "unmanaged": list(record.unmanaged)}
        ),
        "leftover_schema_folders": list(leftover),
        "remedy": remedy,
        "diagnostics": None,
    }
