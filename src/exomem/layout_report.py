"""What `exomem status` and `exomem doctor` say about a vault's layout.

`report` reads the layout through `layout.inspect` and turns every outcome into a result that
leads with a short sentence: a resolved layout, a layout that the record and folders cannot yet
decide, or a layout that could not be read at all. It never raises and never refuses. The
refusal that vault resolution and service start raise for a vault-root schema folder without a
record is a finding here, and its remedy names the record to write.

The report reads the disk through `layout.inspect` each time and keeps no cache: it must show
what the vault holds now, which `layout.of` (the cached answer that service start uses) may not.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import layout

#: The report's `layout` value when the record and folders cannot decide a layout, or when
#: the read failed: both are states to show, never a guess at `nested` or `root`. A closed
#: pair that this report fixes; the other values come from `layout.Layout`.
UNRESOLVED = "unresolved"
UNKNOWN = "unknown"


def _leftover_remedy(folders: tuple[str, ...], record: layout.LayoutRecord) -> str:
    """What to do about a second schema folder, which depends on whether the record sits where its layout keeps it."""
    holder = record.path.rpartition("/")[0]
    fitting = layout.schema_folder(record.layout)
    if holder != fitting:
        # The record sits in the other layout's folder, so the folder that its own layout keeps is the leftover.
        return (
            f"The record in {holder}/ says {record.layout.value}, and a {record.layout.value} vault keeps "
            f"its schema folder at {fitting}/. Move {record.path} to {fitting}/{layout.RECORD_NAME}. "
            f"Then check that {holder}/ holds nothing that {fitting}/ lacks, and remove {holder}/. "
            "A relayout in progress leaves both until it finishes."
        )
    where = ", ".join(f"{folder}/" for folder in folders)
    return (
        f"The record in {holder}/ decides the layout, so {where} changes nothing. A relayout "
        "in progress leaves it until it finishes. Afterwards, check that it holds nothing "
        f"that {holder}/ lacks, then remove it."
    )


def settled(found: dict[str, Any]) -> bool:
    """Whether a `report` result shows a layout that nothing leaves in doubt: resolved, with no second schema folder."""
    return found["layout"] not in (UNRESOLVED, UNKNOWN) and not found["leftover_schema_folders"]


def _unknown_remedy(error: Exception, reason: str) -> str:
    """What to do about a read that failed: name the store only when the store failed."""
    from .collection_store import connection

    # What a failed store read raises: the collection store's own errors, the database's and the filesystem's.
    store_failures = (
        connection.CollectionStoreError,
        connection.CollectionStoreUnavailable,
        sqlite3.Error,
        OSError,
    )
    if isinstance(error, store_failures):
        return f"Repeat the check once the vault's store answers ({reason})."
    return f"Repeat the check. If {reason} comes back, report it: the layout code does not handle it."


def _unknown(outcome: str, remedy: str | None, diagnostics: dict[str, Any]) -> dict[str, Any]:
    """The result for a layout that could not be read: shown as unknown, never as `nested` or `root`."""
    return {
        "outcome": outcome,
        "layout": UNKNOWN,
        "record": None,
        "leftover_schema_folders": [],
        "remedy": remedy,
        "diagnostics": diagnostics,
    }


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
    except Exception as error:  # noqa: BLE001 - a failure that the layout code does not classify is unknown, never a guess
        reason = getattr(error, "code", None) or type(error).__name__
        return _unknown(
            f"The layout of this vault could not be read ({reason}).",
            _unknown_remedy(error, str(reason)),
            {"code": str(reason)},
        )
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
        remedy = _leftover_remedy(leftover, record)
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
