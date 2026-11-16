"""The second pass: every wikilink of a page, rewritten to the text that resolves after the relayout.

Today's resolver is built once. A link that it resolves to a page the plan moves gets the
shortest link text that Obsidian's rule resolves to that page's new path
(`obsidian_links.link_form`). A link text that ends in a file extension other than `.md`
names a file by its full name, so it resolves against the files that are not Markdown by the
same rule, and its new text comes from the same link-form rule over those files after the move.
A link that it leaves unresolved or ambiguous keeps its bytes and is counted. Each planned text
is then resolved again under Obsidian's rule over the vault as it will be, and a text that does
not reach its page is reported as a plan failure with what the rule made of it. Pages are
scanned singly: a body is read, scanned and dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .. import kbdir, layout, obsidian_links, provenance, vault

# The wikilink grammar fixes the brackets that wrap a link's text.
_OPEN, _CLOSE = "[[", "]]"


class Outcome(StrEnum):
    """What today's resolver makes of a link. The resolver's own three results, and the folder link."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    #: A link to a folder (`[[Folder/]]`) names no page; the plan counts it and leaves it.
    FOLDER = "folder"


@dataclass(frozen=True, slots=True)
class Target:
    """The page a link text names today: its current path when the outcome is RESOLVED."""

    outcome: Outcome
    path: str | None = None
    #: The path names a file that is not Markdown, which a link reads by its full file name.
    attachment: bool = False


@dataclass(frozen=True, slots=True)
class LinkEdit:
    """A link whose text changes. `old` and `new` are the text between the brackets, alias included.

    `region` is `body` or `frontmatter`; `full_path` says the new text is a full vault path
    because no shorter text is unique.
    """

    page: str
    region: str
    old: str
    new: str
    full_path: bool = False
    attachment: bool = False


@dataclass(frozen=True, slots=True)
class LinkNote:
    """A link that the plan leaves as written, with the page that holds it."""

    page: str
    text: str


@dataclass(frozen=True, slots=True)
class LinkMiss:
    """A link whose planned text does not reach its page, with what an owner needs to act on it.

    `text` is the link text as written and `target` the page it resolves to today; `new_target`
    is that page's new path and `planned_text` the text the plan gives it. `rule_result` is what
    Obsidian's rule makes of `planned_text` and `matches` the files it names: none for an
    unresolved text, several for an ambiguous text, and another page for a text that resolves
    to the wrong page.
    """

    page: str
    text: str
    target: str
    new_target: str
    planned_text: str
    rule_result: obsidian_links.Resolution
    matches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OutsideLink:
    """A link from a page outside the managed root to a page inside it, and whether it breaks."""

    page: str
    text: str
    target: str
    new_target: str | None
    breaks: bool


@dataclass(slots=True)
class LinkCounts:
    """What the scan found, by outcome. `links` excludes links in code and in origin carriers.

    `attachment_links` and `attachment_edits` are the links that resolve to a file that is not
    Markdown, and the edits among them; both are part of `links` and `edits`, not apart from them.
    """

    pages_scanned: int = 0
    links: int = 0
    carrier_links: int = 0
    attachment_links: int = 0
    attachment_edits: int = 0
    edits: int = 0
    unchanged: int = 0
    to_outside: int = 0
    to_unmapped: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    folder: int = 0
    to_retired_collection_paths: int = 0
    full_path_forms: int = 0
    edit_pages: int = 0
    append_only_edit_pages: int = 0
    frontmatter_edits: int = 0
    unreadable_pages: int = 0


@dataclass(slots=True)
class LinkScan:
    """The result of the second pass over every page, ready for a plan to hold."""

    counts: LinkCounts = field(default_factory=LinkCounts)
    edits: list[LinkEdit] = field(default_factory=list)
    unresolved: list[LinkNote] = field(default_factory=list)
    ambiguous: list[LinkNote] = field(default_factory=list)
    folder: list[LinkNote] = field(default_factory=list)
    retired_collection: list[LinkNote] = field(default_factory=list)
    outside: list[OutsideLink] = field(default_factory=list)
    outside_pages_scanned: int = 0
    failures: list[LinkMiss] = field(default_factory=list)


def _names_collection_path(inner: str) -> bool:
    """Whether the link text names a path inside a collection layer, with or without the managed folder."""
    page = vault.split_wikilink_target(inner)[0]
    below = kbdir.managed_split(page)
    return layout.in_collection_layer(page if below is None else below)


def _inner(text: str, start: int, end: int) -> str:
    return text[start + len(_OPEN) : end - len(_CLOSE)]


class LinkRewriter:
    """Rewrites the links of pages, against a single resolver and a single post-relayout index.

    `moved` maps the current path of each file the plan moves to its new path; `post_index` is
    Obsidian's basename index of the Markdown files of the vault as it will be, and
    `attachments_today` and `attachments_after` are the same index of the other files before and
    after the move. The resolver answers for today's Markdown files. `retired` is not consulted,
    because it asks the collection store, which a plan never opens: a link that resolves to no
    page and names a path inside a collection layer is counted as a link to a retired collection
    path instead of an unresolved link.
    """

    def __init__(
        self,
        resolver: vault.WikilinkResolver,
        vault_root: Path,
        moved: Mapping[str, str],
        post_index: obsidian_links.Index,
        path_of_key: Mapping[str, str],
        attachments_today: obsidian_links.Index,
        attachments_after: obsidian_links.Index,
    ) -> None:
        self._resolver = resolver
        self._vault_root = vault_root
        self._moved = moved
        self._forms = obsidian_links.link_forms(post_index)
        self._post_resolver = obsidian_links.IndexResolver(post_index)
        self._attachment_forms = obsidian_links.link_forms(attachments_after)
        self._attachments_today = obsidian_links.IndexResolver(attachments_today)
        self._post_attachments = obsidian_links.IndexResolver(attachments_after)
        self._path_of_key = path_of_key
        self._targets: dict[str, Target] = {}
        self._checked: dict[tuple[str, bool], obsidian_links.LinkMatch] = {}

    def target(self, inner: str) -> Target:
        """What today's resolver makes of the link text `inner`, answered once per page part."""
        page, _anchor = vault.split_wikilink_target(inner)
        found = self._targets.get(page)
        if found is None:
            found = self._targets[page] = self._resolve(inner, page)
        return found

    def _resolve(self, inner: str, page: str) -> Target:
        if page.endswith("/"):
            return Target(Outcome.FOLDER)
        if obsidian_links.names_attachment(page):
            found = self._attachments_today.resolve(page)
            if found.outcome is obsidian_links.Resolution.RESOLVED:
                return Target(Outcome.RESOLVED, found.path, attachment=True)
            if found.outcome is obsidian_links.Resolution.AMBIGUOUS:
                return Target(Outcome.AMBIGUOUS)
        try:
            canonical, _warning = vault.normalize_wikilink(
                inner,
                self._vault_root,
                resolver=self._resolver,
                strict=True,
                retired=lambda _path: None,
            )
        except vault.AmbiguousWikilinkError:
            return Target(Outcome.AMBIGUOUS)
        except vault.UnresolvedWikilinkError:
            return Target(Outcome.UNRESOLVED)
        path = self._path_of_key.get(vault.split_wikilink_target(canonical)[0])
        return Target(Outcome.RESOLVED, path) if path is not None else Target(Outcome.UNRESOLVED)

    def new_text(self, inner: str, target: Target) -> tuple[str, bool] | None:
        """The link text after the relayout and whether it is a full path, or None for a page that stays put.

        None when the target page has no new path or no link form: the link keeps its bytes.
        """
        new_path = self._moved.get(target.path or "")
        form = (self._attachment_forms if target.attachment else self._forms).get(new_path or "")
        if form is None:
            return None
        return obsidian_links.link_text(inner, form.text), form.full_path

    def _match(self, text: str, *, attachment: bool) -> obsidian_links.LinkMatch:
        """What Obsidian's rule over the post-relayout vault makes of the link text, answered once per page part."""
        page, _anchor = vault.split_wikilink_target(text)
        key = (page, attachment)
        found = self._checked.get(key)
        if found is None:
            resolver = self._post_attachments if attachment else self._post_resolver
            found = self._checked[key] = resolver.resolve(page)
        return found

    def reaches(self, text: str, new_path: str, *, attachment: bool = False) -> bool:
        """Whether Obsidian's rule over the post-relayout vault resolves the link text to `new_path`."""
        return self._match(text, attachment=attachment).path == new_path

    def _entries(
        self, text: str, *, skip: frozenset[str] = frozenset(), body: bool
    ) -> tuple[list[tuple[str, bool]], int]:
        """The live links of `text` as `(inner, in_carrier)`, with the number of those that sit in carriers.

        A link in code is not live. A body link inside an origin carrier keeps its recorded
        bytes, so it is returned with its flag set and counted apart.
        """
        inside = provenance.in_carrier(text) if body else None
        found: list[tuple[str, bool]] = []
        carried = 0
        for match in vault.iter_body_wikilinks(text):
            whole = text[match.start() : match.end()]
            if whole in skip:
                continue
            in_carrier = bool(inside and inside(match.start(), match.end()))
            carried += in_carrier
            found.append((_inner(text, match.start(), match.end()), in_carrier))
        return found, carried

    def scan_managed(self, scan: LinkScan, page: str, data: bytes, skip: frozenset[str]) -> None:
        """Count and plan the links of a page inside the managed root."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            scan.counts.unreadable_pages += 1
            return
        counts = scan.counts
        counts.pages_scanned += 1
        _frontmatter, body, block = vault.parse_frontmatter(text)
        regions: list[tuple[str, str]] = [("body", body)]
        if block is not None:
            regions.append(("frontmatter", block))
        before = counts.edits
        for region, region_text in regions:
            links, carried = self._entries(
                region_text,
                skip=skip if region == "frontmatter" else frozenset(),
                body=region == "body",
            )
            counts.carrier_links += carried
            for inner, in_carrier in links:
                if in_carrier:
                    continue
                counts.links += 1
                self._plan_link(scan, page, region, inner)
        if counts.edits > before:
            counts.edit_pages += 1
            counts.append_only_edit_pages += vault.in_append_only_tree(page) is not None

    def _plan_link(self, scan: LinkScan, page: str, region: str, inner: str) -> None:
        counts = scan.counts
        target = self.target(inner)
        note = LinkNote(page, inner)
        counts.attachment_links += target.attachment
        if target.outcome is Outcome.UNRESOLVED and _names_collection_path(inner):
            counts.to_retired_collection_paths += 1
            scan.retired_collection.append(note)
        elif target.outcome is Outcome.UNRESOLVED:
            counts.unresolved += 1
            scan.unresolved.append(note)
        elif target.outcome is Outcome.AMBIGUOUS:
            counts.ambiguous += 1
            scan.ambiguous.append(note)
        elif target.outcome is Outcome.FOLDER:
            counts.folder += 1
            scan.folder.append(note)
        elif target.path not in self._moved:
            # The page sits outside the managed root, or the plan gives it no new path.
            if vault.managed_split(target.path or "") is None:
                counts.to_outside += 1
            else:
                counts.to_unmapped += 1
        else:
            planned = self.new_text(inner, target)
            if planned is None:
                counts.to_unmapped += 1
                return
            new, full_path = planned
            if new == inner:
                counts.unchanged += 1
                return
            counts.edits += 1
            counts.frontmatter_edits += region == "frontmatter"
            counts.full_path_forms += full_path
            counts.attachment_edits += target.attachment
            scan.edits.append(LinkEdit(page, region, inner, new, full_path, target.attachment))
            self._self_check(scan, page, inner, target, new)

    def _self_check(self, scan: LinkScan, page: str, inner: str, target: Target, new: str) -> None:
        """Resolve a planned text again over the vault as it will be, and record it when it misses its page."""
        new_path = self._moved[target.path or ""]
        match = self._match(new, attachment=target.attachment)
        if match.path == new_path:
            return
        scan.failures.append(
            LinkMiss(page, inner, target.path or "", new_path, new, match.outcome, match.paths)
        )

    def scan_outside(self, scan: LinkScan, page: str, data: bytes) -> None:
        """List the links of a page outside the managed root that resolve into it, and which of them break."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        scan.outside_pages_scanned += 1
        for inner, _carrier in self._entries(text, body=False)[0]:
            target = self.target(inner)
            if target.outcome is not Outcome.RESOLVED or target.path not in self._moved:
                continue
            new_path = self._moved[target.path or ""]
            scan.outside.append(
                OutsideLink(
                    page,
                    inner,
                    target.path or "",
                    new_path,
                    not self.reaches(inner, new_path, attachment=target.attachment),
                )
            )
