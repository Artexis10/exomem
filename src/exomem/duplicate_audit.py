"""The audit's two duplicate checks: identical bytes (`duplicate_content`) and one URL with differing bytes (`duplicate_source_url`).

The lexical catalogue's payload and URL columns only propose groups. Every member of a group of two or more
is read again through the caller's admission, re-derived, and its original re-hashed, so a stale or damaged
index entry costs a missed finding and never a false one. An index that cannot be proven complete reads as
unchecked, never as zero findings.

A `duplicate_content` finding carries the exact `fold-duplicate` step for each copy, with the oldest filing as
the suggested canonical page. A page that a fold cannot rewrite may cite a member, and the fold refuses to retire a
cited member, so that member is the canonical page instead; two such members leave the group with no step. A step
is offered only when `duplicate_fold.pair_refusal` accepts the pair, so every step passes proposal unchanged. The
audit only suggests: the owner confirms which filing stays (`duplicate_fold`).

A caller may see only some pages, and a fold needs whole-namespace authority. Such a caller's answer equals the answer
of a vault without the pages it cannot read: it counts and lists only readable members, offers no step, and learns
nothing about which page cites which.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    adoption_plan,
    duplicate_fold,
    duplicates,
    evidence_reuse,
    fold_leaf,
    lexstore,
    request_budget,
    vault,
)
from .governance import raw_protection

if TYPE_CHECKING:
    from .audit import AuditFinding

CONTENT = "duplicate_content"
SOURCE_URL = "duplicate_source_url"
#: How a count was verified: every member of a group was read again and its original re-hashed.
RE_HASHED = "re-hashed"
#: The lane label of a group whose members are in both lanes.
SPANNING = "spanning"


@dataclass(frozen=True, slots=True)
class Member:
    path: str
    payload: duplicates.PagePayload
    captured: str
    size: int | None

    @property
    def filing_order(self) -> tuple[bool, str, str]:
        """Oldest filing first: the recorded capture date, then the path. A page with no recorded date sorts last."""
        return (not self.captured, self.captured, self.path)

    def as_dict(self, vault_root: Path) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ref": self.payload.ref,
            "path": self.path,
            "lane": self.payload.lane,
            "captured": self.captured or None,
        }
        if self.payload.lane == duplicates.EVIDENCE:
            home = evidence_reuse.folder_family(vault_root, self.path)
            families = None if home is None else evidence_reuse.listed_families(self.payload.frontmatter, home)
            entry["families"] = None if families is None else [{"scope": scope, "category": category} for scope, category in families]
        else:
            entry["mappings"] = adoption_plan.page_mappings(self.payload.frontmatter)
        return entry


#: Why a page of an indexed group does not count: the caller may not read it, it no longer holds what the index
#: says, or it is raw-protected without the binding that proves its original.
_WITHHELD, _STALE, _UNPROVEN = "withheld", "stale", "unproven"
_UNPROVEN_BECAUSE = "a raw-protected page has no matching raw-protection binding, so its original cannot be proven"
_NO_AUTHORITY = "a fold needs whole-namespace authority over the vault, which this caller lacks"


def _member(vault_root: Path, path: str) -> Member | str:
    """The page as the caller may read it, with its original re-hashed, or why it does not count."""
    if duplicates.withheld(vault_root, path):
        return _WITHHELD
    payload = duplicates.stored_payload(vault_root, path)
    if payload is None or payload.ref is None:
        return _STALE
    size = None
    if payload.artifact_path is not None:
        if duplicates.binding_unproven(vault_root, path, payload):
            return _UNPROVEN
        size = duplicates.original_size(vault_root, path, payload)
        if size is None:
            return _STALE
    return Member(path, payload, str(payload.frontmatter.get("captured") or ""), size)


def _no_fold_reason(members: Sequence[Member]) -> str | None:
    """Why a group of identical pages gets no fold step whatever its citers, or None when it may."""
    if len({member.payload.lane for member in members}) > 1:
        return "the identical bytes sit in both lanes, and a fold never crosses lanes"
    if members[0].payload.protected:
        return "the pages are raw-protected, a release grant binds to each page's content hash, and a fold never rewrites a protected page"
    return None


def _pinned_members(vault_root: Path, members: Sequence[Member]) -> set[str]:
    """The paths of the members that a page the fold cannot rewrite cites.

    The fold refuses to retire a cited page whose citer it cannot repoint, so such a member can only be canonical.
    This reads the inbound references the way the fold does (`fold_leaf.referencing_pages`, the whole vault), once
    for every group of the audit. It names no citing page: the caller may not be allowed to read it.
    """
    targets = {member.path: member.payload.ref for member in members}
    targets.update({member.payload.artifact_path: None for member in members if member.payload.artifact_path})
    cited = fold_leaf.referencing_pages(vault_root, targets, frozenset())
    return {
        member.path
        for member in members
        for page in {page for name in (member.path, member.payload.artifact_path) if name for page in cited.get(name, ())}
        if page != member.path and duplicate_fold.repoint_refusal(page) is not None
    }


def _content_finding(
    vault_root: Path, members: list[Member], sha256: str, pinned_all: set[str], *, can_fold: bool
) -> tuple[str, AuditFinding]:
    """One finding for a group of identical pages in one protection class. Returns its lane label and the finding."""
    from .audit import AuditFinding

    ordered = sorted(members, key=lambda member: member.filing_order)
    lanes = {member.payload.lane for member in members}
    lane = SPANNING if len(lanes) > 1 else ordered[0].payload.lane
    why = _no_fold_reason(members) if can_fold else _NO_AUTHORITY
    pinned = [member for member in ordered if member.path in pinned_all]
    if why is None and len(pinned) > 1:
        why = (
            "more than one page is cited by a page that the fold cannot rewrite (a Sources or Evidence page, or a page "
            "in another owner's tree), and only one page can stay: "
            + ", ".join(repr(member.path) for member in pinned)
        )
    # The oldest filing, unless exactly one cited page can only be kept: then that page is the canonical one.
    canonical = pinned[0] if len(pinned) == 1 else ordered[0]
    copies = [member for member in ordered if member is not canonical]
    freed = sum(member.size or 0 for member in copies)
    if why is None:
        for copy in copies:
            refusal = duplicate_fold.pair_refusal(
                vault_root, (canonical.path, canonical.payload.text), (copy.path, copy.payload.text)
            )
            if refusal is not None:
                why = f"{copy.path!r} cannot fold into {canonical.path!r}: {refusal.reason}"
                break
    steps = []
    if why is None:
        # The bytes' hash keeps step ids unique across groups, so a batch can carry the steps of several findings.
        steps = [
            duplicate_fold.suggested_step(
                f"fold-{sha256[:12]}-{number}",
                canonical=(canonical.path, vault.content_hash(canonical.payload.text)),
                copy=(copy.path, vault.content_hash(copy.payload.text)),
                why=(
                    "An exact duplicate of the oldest filing, folded into it."
                    if not pinned
                    else "An exact duplicate of a page that a page the fold cannot rewrite cites, folded into that page."
                ),
            )
            for number, copy in enumerate(copies, start=1)
        ]
    finding = AuditFinding(
        category=CONTENT,
        severity="info",
        path=canonical.path,
        detail=(
            f"{len(members)} live pages hold identical bytes ({sha256[:12]}), {lane}: "
            + ", ".join(repr(member.path) for member in ordered)
        ),
        proposed_fix=(
            "Propose the `fold-duplicate` steps in `meta.steps` as one curation plan, keep or change the suggested "
            "canonical page, and confirm the exact plan."
            if not why
            else f"No fold is offered: {why}."
        ),
        paths=[member.path for member in ordered],
        meta={
            "lane": lane,
            "payload_sha256": sha256,
            "protected": canonical.payload.protected,
            "members": [member.as_dict(vault_root) for member in ordered],
            "suggested_canonical": canonical.path,
            "canonical_because": "cited by a page the fold cannot rewrite" if len(pinned) == 1 else "the oldest filing",
            "bytes_freed": freed,
            "steps": steps,
            "no_fold": why,
        },
    )
    return lane, finding


def _url_finding(vault_root: Path, members: list[Member], url: str) -> AuditFinding:
    from .audit import AuditFinding

    ordered = sorted(members, key=lambda member: member.filing_order)
    return AuditFinding(
        category=SOURCE_URL,
        severity="info",
        path=ordered[0].path,
        detail=(
            f"{len(members)} live Sources share the URL {url} but hold different bytes: "
            + ", ".join(repr(member.path) for member in ordered)
        ),
        proposed_fix=(
            "No fold is offered, because the captured bytes differ. Link the Sources or supersede one "
            "through the normal tools if one captures the same page again."
        ),
        paths=[member.path for member in ordered],
        meta={"canonical_url": url, "members": [member.as_dict(vault_root) for member in ordered], "no_fold": "the bytes differ"},
    )


def check(vault_root: Path, categories: Collection[str]) -> tuple[list[AuditFinding], dict[str, Any], str]:
    """The findings of the selected duplicate categories, the report block that counts them, and the coverage.

    Coverage is `complete` or `unknown`: an index that cannot answer is `unknown`, and the report block says `unchecked`.
    """
    wanted_content, wanted_url = CONTENT in categories, SOURCE_URL in categories
    indexed = lexstore.duplicate_groups(vault_root)
    if indexed is None:
        return [], {"index": "unchecked"}, "unknown"
    payload_groups, url_groups = indexed
    findings: list[AuditFinding] = []
    content: dict[str, dict[str, Any]] = {}
    stale = unverifiable = 0
    if wanted_content:
        can_fold = fold_leaf.has_authority(vault_root)
        candidates: list[tuple[str, list[Member]]] = []
        for sha256, paths in payload_groups.items():
            request_budget.checkpoint()
            # A page the caller cannot read does not exist for it: the group is judged as if that page were absent.
            outcomes = [outcome for path in paths if (outcome := _member(vault_root, path)) != _WITHHELD]
            if len(outcomes) < 2:
                continue
            held = [member for member in outcomes if isinstance(member, Member) and member.payload.sha256 == sha256]
            # Two protection classes never answer for each other, so each class is its own group.
            classes = {protected: [m for m in held if m.payload.protected == protected] for protected in (False, True)}
            groups = [members for members in classes.values() if len(members) > 1]
            if _UNPROVEN in outcomes:
                unverifiable += 1
            elif not groups:
                stale += 1
            candidates.extend((sha256, members) for members in groups)
        # One read of the inbound references answers every group that a fold could still be suggested for.
        request_budget.checkpoint()
        foldable = [member for _, members in candidates if _no_fold_reason(members) is None for member in members]
        pinned = _pinned_members(vault_root, foldable) if foldable and can_fold else set()
        for sha256, members in candidates:
            lane, finding = _content_finding(vault_root, members, sha256, pinned, can_fold=can_fold)
            findings.append(finding)
            count = content.setdefault(lane, {"groups": 0, "copies": 0, "verified": RE_HASHED})
            count["groups"] += 1
            count["copies"] += len(members) - 1
    urls = {"groups": 0, "sources": 0, "verified": RE_HASHED}
    if wanted_url:
        for url, paths in url_groups.items():
            request_budget.checkpoint()
            held = [
                member for path in paths
                if not raw_protection.marked(path)
                and isinstance(member := _member(vault_root, path), Member)
                and member.payload.source_url == url
            ]
            # One URL with identical payloads is a `duplicate_content` group, which appears once, there.
            if len({member.payload.sha256 for member in held}) > 1:
                findings.append(_url_finding(vault_root, held, url))
                urls["groups"] += 1
                urls["sources"] += len(held)
    findings.sort(key=lambda finding: (finding.category, finding.path))
    report: dict[str, Any] = {"index": "complete", "stale_index_groups": stale, "unverifiable_groups": unverifiable}
    if unverifiable:
        report["unverifiable_because"] = _UNPROVEN_BECAUSE
    if wanted_content:
        report["content"] = content
    if wanted_url:
        report["source_url"] = urls
    return findings, report, "complete"
