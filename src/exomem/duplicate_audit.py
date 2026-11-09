"""The audit's two duplicate checks: identical bytes (`duplicate_content`) and one URL with differing bytes (`duplicate_source_url`).

The lexical catalogue's payload and URL columns only propose groups. Every member of a group of two or more
is read again through the caller's admission, re-derived, and its original re-hashed, so a stale or damaged
index entry costs a missed finding and never a false one. An index that cannot be proven complete reads as
unchecked, never as zero findings.

A `duplicate_content` finding carries the exact `fold-duplicate` step for each copy, with the oldest filing as
the suggested canonical page. The audit only suggests: the owner confirms which filing stays (`duplicate_fold`).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    adoption_plan,
    duplicate_fold,
    duplicates,
    evidence_reuse,
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


def _member(vault_root: Path, path: str) -> Member | None:
    """The page as the caller may read it, with its original re-hashed, or None when it no longer qualifies."""
    payload = duplicates.readable_page(vault_root, path)
    if payload is None or payload.ref is None:
        return None
    size = None
    if payload.artifact_path is not None:
        size = duplicates.original_size(vault_root, path, payload)
        if size is None:
            return None
    return Member(path, payload, str(payload.frontmatter.get("captured") or ""), size)


def _content_finding(vault_root: Path, members: list[Member], sha256: str) -> tuple[str, AuditFinding]:
    """One finding for a group of identical pages in one protection class. Returns its lane label and the finding."""
    from .audit import AuditFinding

    ordered = sorted(members, key=lambda member: member.filing_order)
    canonical, copies = ordered[0], ordered[1:]
    lanes = {member.payload.lane for member in members}
    lane = SPANNING if len(lanes) > 1 else canonical.payload.lane
    freed = sum(member.size or 0 for member in copies)
    why = None
    if len(lanes) > 1:
        why = "the identical bytes sit in both lanes, and a fold never crosses lanes"
    elif canonical.payload.protected:
        why = "the pages are raw-protected, a release grant binds to each page's content hash, and a fold never rewrites a protected page"
    steps = []
    if why is None:
        steps = [
            duplicate_fold.suggested_step(
                f"fold-{number}",
                canonical=(canonical.path, vault.content_hash(canonical.payload.text)),
                copy=(copy.path, vault.content_hash(copy.payload.text)),
                why="An exact duplicate of the oldest filing, folded into it.",
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
    payload_groups = lexstore.payload_groups(vault_root) if wanted_content else {}
    url_groups = lexstore.source_url_groups(vault_root) if wanted_url else {}
    if payload_groups is None or url_groups is None:
        return [], {"index": "unchecked"}, "unknown"
    findings: list[AuditFinding] = []
    content: dict[str, dict[str, Any]] = {}
    stale = 0
    if wanted_content:
        for sha256, paths in payload_groups.items():
            request_budget.checkpoint()
            held = [member for path in paths if (member := _member(vault_root, path)) and member.payload.sha256 == sha256]
            # Two protection classes never answer for each other, so each class is its own group.
            classes = {protected: [m for m in held if m.payload.protected == protected] for protected in (False, True)}
            groups = [members for members in classes.values() if len(members) > 1]
            if not groups:
                stale += 1
            for members in groups:
                lane, finding = _content_finding(vault_root, members, sha256)
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
                and (member := _member(vault_root, path))
                and member.payload.source_url == url
            ]
            # One URL with identical payloads is a `duplicate_content` group, which appears once, there.
            if len({member.payload.sha256 for member in held}) > 1:
                findings.append(_url_finding(vault_root, held, url))
                urls["groups"] += 1
                urls["sources"] += len(held)
    findings.sort(key=lambda finding: (finding.category, finding.path))
    report: dict[str, Any] = {"index": "complete", "stale_index_groups": stale}
    if wanted_content:
        report["content"] = content
    if wanted_url:
        report["source_url"] = urls
    return findings, report, "complete"
