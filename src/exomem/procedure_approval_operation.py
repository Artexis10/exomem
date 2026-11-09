"""The `approve_procedure` operation and the `procedure_approvals` review listing.

Both are adapters over `procedure_approval`, which owns the ledger, the key and the one
verdict. An adapter checks that the caller is the vault owner, resolves a stable ref to its
page, calls the ledger module and shapes one answer. Each answer leads with a sentence that
the agent can repeat to the owner as it stands. Entry ids, versions and file names stay in a
`diagnostics` part, and nothing here returns the key or a path to it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import get_page, memory_refs, procedure_approval
from .cli_ops import OpError
from .governance import egress
from .governance.principal import RequestPrincipal, effective_principal

OPERATION = "approve_procedure"
LISTING = "procedure_approvals"
QUARANTINE = "quarantine"
ACTIONS = (procedure_approval.APPROVE, procedure_approval.REVOKE, QUARANTINE)

#: One refusal code per ledger refusal; the module's own message and remediation name the cause and the fix.
_REFUSAL_CODE_PREFIX = "APPROVE_PROCEDURE_"
#: Bounds for the change shown in an approve result, so one call cannot return a whole page twice.
_DIFF_LINES = 120
_DIFF_CHARS = 8000
#: Retained history is read newest first, a page of this many entries at a time, a few pages deep.
_HISTORY_PAGES = 4
_DEFAULT_LISTING_LIMIT = 25
#: Each listed ref reads its page, so one listing call stays bounded.
_MAX_LISTING_LIMIT = 100


def _invalid(message: str, remediation: str) -> OpError:
    return OpError("APPROVE_PROCEDURE_INVALID", message, remediation)


def check_arguments(action: str, ref: str | None, expected_version: str | None) -> str | None:
    """Refuse an input that no action accepts, and return the canonical ref (None for none).

    The MCP rendering, the generated CLI and the REST route all reach this one check, so each
    refuses what the others refuse. It reads nothing and writes nothing.
    """
    if action not in ACTIONS:
        raise _invalid(
            f"action {action!r} is not an approval action.",
            "Use action approve, revoke or quarantine.",
        )
    canonical = None
    if ref is not None:
        identifier = memory_refs.parse_memory_ref(ref)
        if identifier is None:
            raise _invalid(
                "ref is not a stable memory ref.",
                "Pass the page's ref as exomem://memory/<uuid>, as read_memory returns it.",
            )
        canonical = memory_refs.memory_ref(identifier)
    if action != QUARANTINE and canonical is None:
        raise _invalid(f"{action} needs a ref.", "Pass the procedure page's ref.")
    if action == QUARANTINE and expected_version is not None:
        raise _invalid(
            "quarantine takes no expected_version.",
            "Remove expected_version, or use action approve or revoke.",
        )
    if action == procedure_approval.APPROVE and not expected_version:
        raise _invalid(
            "approve needs expected_version.",
            "Read the page with read_memory and pass its content_hash as expected_version.",
        )
    if expected_version is not None and not procedure_approval.is_version(expected_version):
        raise _invalid(
            "expected_version is not a page content_hash, which is 64 lowercase hex characters.",
            "Read the page with read_memory and pass its content_hash as expected_version, "
            "or omit expected_version on a revoke.",
        )
    return canonical


def _require_owner(vault_root: Path, who: RequestPrincipal) -> None:
    if not procedure_approval.owner_view(vault_root, who):
        raise OpError(
            "APPROVE_PROCEDURE_OWNER_ONLY",
            "Only the vault owner, on a connection that reads the whole vault, can approve, revoke "
            "or list procedure approvals.",
            "Ask the vault owner to do this from the local CLI or from an owner connection that "
            "no connector boundary limits.",
        )


def _refusal(error: procedure_approval.ApprovalRefused) -> OpError:
    return OpError(
        f"{_REFUSAL_CODE_PREFIX}{error.code.upper()}",
        error.message,
        error.remediation,
        details={"condition": error.condition} if error.condition else None,
    )


@dataclass(frozen=True, slots=True)
class _Page:
    """The one page that holds a ref, when exactly one readable page does."""

    path: str | None = None
    facts: procedure_approval.PageFacts | None = None
    text: str | None = None
    #: False when two pages hold the ref, so no single page version stands for it.
    unique: bool = False


def _holders(vault_root: Path, refs: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Every page that holds each ref, from one read of the reference index."""
    identifiers = {ref: memory_refs.parse_memory_ref(ref) for ref in refs}
    found = memory_refs.paths_for_ids_read_only(
        vault_root, [identifier for identifier in identifiers.values() if identifier is not None]
    )
    return {ref: tuple(sorted(found.get(identifier, ()))) for ref, identifier in identifiers.items()}


def _resolve(vault_root: Path, holders: tuple[str, ...], who: RequestPrincipal) -> _Page:
    """Read the page that holds a ref, given its holders. A page withheld from this caller reads as absent."""
    if not holders:
        return _Page()
    path = holders[0]
    level = egress.release_level_for(vault_root, path, principal=who)
    if level is None or level < egress.LEVEL_FULL:
        return _Page()
    try:
        page = get_page.get_page(vault_root, path=path)
    except get_page.GetError:
        return _Page()
    facts = procedure_approval.page_facts(
        vault_root, path, page.content.encode("utf-8"), holders=holders
    )
    return _Page(path, facts, page.content, len(holders) == 1)


def _client_label(who: RequestPrincipal) -> str:
    from .command_surface import mcp_caller_identity

    return mcp_caller_identity().get("client_name") or who.surface


def _retained_text(vault_root: Path, path: str, version: str) -> str | None:
    """The exact text of an earlier page version, from the page's retained history, if kept."""
    from . import compiled_history, record_history
    from . import structured_collections as collections

    continuation = None
    for _ in range(_HISTORY_PAGES):
        try:
            page = compiled_history.read(
                vault_root, path=path, limit=record_history.PAGE_MAX, continuation=continuation
            )
        except (ValueError, OSError, collections.CollectionError):
            return None
        for revision in page["revisions"]:
            if revision.get("before_hash") == version:
                return revision["prior"]["source"]
        if not page["truncated"] or page["continuation"] is None:
            return None
        continuation = page["continuation"]
    return None


def _changes(
    vault_root: Path, page: _Page, previous_version: str | None
) -> dict[str, Any]:
    """What changed since the last approved version of the ref, or why that cannot be shown."""
    if previous_version is None:
        return {"summary": "This is the first approval of this page."}
    assert page.facts is not None and page.path is not None and page.text is not None
    if previous_version == page.facts.version:
        return {"summary": "The page is unchanged since the version that was approved before."}
    earlier = _retained_text(vault_root, page.path, previous_version)
    if earlier is None:
        return {
            "summary": (
                "The version that was approved before is not in this page's retained history, "
                "so the change cannot be shown."
            )
        }
    lines = list(
        difflib.unified_diff(
            earlier.splitlines(),
            page.text.splitlines(),
            "previously approved",
            "this version",
            lineterm="",
        )
    )
    shown = "\n".join(lines[:_DIFF_LINES])
    cut = len(lines) > _DIFF_LINES or len(shown) > _DIFF_CHARS
    result: dict[str, Any] = {
        "summary": "The page changed since the version that was approved before. The diff follows.",
        "diff": shown[:_DIFF_CHARS],
    }
    if cut:
        result["diff_truncated"] = True
    return result


def _moved_rows(moved: tuple[procedure_approval.Moved, ...]) -> list[dict[str, str]]:
    return [
        {"ref": item.ref, "kind": item.kind, "file": item.name, "moved_to": item.quarantined_as}
        for item in moved
    ]


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _entry_diagnostics(outcome: procedure_approval.Outcome) -> dict[str, Any]:
    entry = outcome.appended or outcome.existing
    return {
        "appended": outcome.appended is not None,
        "entry_id": entry.entry_id if entry is not None else None,
        "recorded_at": entry.recorded_at if entry is not None else None,
        "previous_approved_version": outcome.previous_approved_version,
    }


def run(
    vault_root: Path,
    action: str,
    ref: str | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Approve, revoke or quarantine. A refusal changes nothing and names what to change."""
    canonical = check_arguments(action, ref, expected_version)
    who = effective_principal()
    root = Path(vault_root)
    _require_owner(root, who)
    try:
        if action == QUARANTINE:
            moved = procedure_approval.quarantine(root, canonical)
            return _quarantine_result(canonical, moved)
        assert canonical is not None
        page = _resolve(root, _holders(root, (canonical,))[canonical], who)
        principal, client = who.principal_kind, _client_label(who)
        if action == procedure_approval.APPROVE:
            assert expected_version is not None
            outcome = procedure_approval.approve(
                root, canonical, page.facts,
                expected_version=expected_version, principal=principal, client=client,
            )
        else:
            outcome = procedure_approval.revoke(
                root, canonical,
                current_version=page.facts.version if page.facts is not None and page.unique else None,
                expected_version=expected_version, principal=principal, client=client,
            )
    except procedure_approval.ApprovalRefused as error:
        raise _refusal(error) from error
    sentence = outcome.outcome
    if outcome.quarantined:
        moved = _count(len(outcome.quarantined), "unreadable approval entry", "unreadable approval entries")
        sentence += f" Moved {moved} aside."
    result: dict[str, Any] = {"outcome": sentence, "operation": OPERATION, "action": action}
    if action == procedure_approval.APPROVE and outcome.appended is not None:
        result["changes"] = _changes(root, page, outcome.previous_approved_version)
    result["diagnostics"] = {
        "ref": canonical,
        "page_version": page.facts.version if page.facts is not None else None,
        **_entry_diagnostics(outcome),
        "moved_aside": _moved_rows(outcome.quarantined),
    }
    return result


def _quarantine_result(
    ref: str | None, moved: tuple[procedure_approval.Moved, ...]
) -> dict[str, Any]:
    if not moved:
        sentence = "No approval entries needed moving. Nothing changed."
    else:
        kinds = [item.kind for item in moved]
        unreadable = kinds.count(procedure_approval.UNREADABLE)
        foreign = kinds.count(procedure_approval.FOREIGN)
        parts = []
        if unreadable:
            parts.append(_count(unreadable, "unreadable entry", "unreadable entries"))
        if foreign:
            parts.append(_count(foreign, "foreign or tampered entry", "foreign or tampered entries"))
        sentence = f"Moved {' and '.join(parts)} aside. No approval was added or changed."
    return {
        "outcome": sentence,
        "operation": OPERATION,
        "action": QUARANTINE,
        "diagnostics": {"ref": ref, "moved_aside": _moved_rows(moved)},
    }


def listing(vault_root: Path, limit: int | None = None) -> dict[str, Any]:
    """Every ref with approval entries: its page, its verdict, its latest entry, its findings."""
    who = effective_principal()
    root = Path(vault_root)
    _require_owner(root, who)
    cap = _DEFAULT_LISTING_LIMIT if limit is None else limit
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= _MAX_LISTING_LIMIT:
        raise _invalid(
            f"limit {limit!r} is not a whole number from 1 to {_MAX_LISTING_LIMIT}.",
            f"Pass a limit from 1 to {_MAX_LISTING_LIMIT}, or omit it to list {_DEFAULT_LISTING_LIMIT}.",
        )
    try:
        refs = tuple(sorted(procedure_approval.listed_refs(root)))
    except procedure_approval.ApprovalRefused as error:
        raise _refusal(error) from error
    shown = refs[:cap]
    ledgers = procedure_approval.ledgers(root, shown)
    holders = _holders(root, shown)
    approvals: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for ref in shown:
        page = _resolve(root, holders[ref], who)
        result = procedure_approval.verdict(
            page.facts, ledgers[ref], owner=True, full_release=page.facts is not None
        )
        row: dict[str, Any] = {
            "ref": ref,
            "path": page.path,
            "verdict": result.value,
            "page_version": page.facts.version if page.facts is not None else None,
        }
        if result.entry is not None:
            row["latest_entry"] = {
                "action": result.entry.action,
                "version": result.entry.version,
                "recorded_at": result.entry.recorded_at,
                "principal": result.entry.principal,
                "client": result.entry.client,
            }
        approvals.append(row)
        findings.extend(item.row() for item in result.findings)
    counts = {
        value: sum(row["verdict"] == value for row in approvals)
        for value in (procedure_approval.APPROVED, procedure_approval.STALE, procedure_approval.UNAPPROVED)
    }
    if not refs:
        sentence = "No procedure has approval entries."
    else:
        sentence = (
            f"{_count(len(refs), 'procedure', 'procedures')} with approval entries"
            + (f", showing {len(shown)}" if len(shown) < len(refs) else "")
            + f": {counts['approved']} approved, {counts['stale']} stale, "
            f"{counts['unapproved']} unapproved."
        )
        if findings:
            sentence += f" {_count(len(findings), 'finding needs', 'findings need')} your attention."
    return {
        "kind": LISTING,
        "outcome": sentence,
        "returned": len(approvals),
        "total": len(refs),
        "truncated": len(shown) < len(refs),
        "approvals": approvals,
        "findings": findings,
    }
