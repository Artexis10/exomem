"""The disclosure diff: who may see which files before the relayout, and who may after it.

Governance decides who sees a file from the file's path, its page metadata and the page it
inherits from. The relayout changes paths, and a path selector that names the old path stops
selecting. This module evaluates today's policy at today's paths and a translated copy of the
policy at the new paths, each with the membership kernel that serves the store itself
(`governance.membership`), and reports what differs. The translated copy runs every `paths`,
`exclude_paths`, `refs` and `exclude_refs` value through `layout.translate_path`. A selector
that names a page which the plan renames cannot be translated, and the owner converts it to an
id ref. The `_access.yaml` tiers go through the same translation.

The diff reads the policy files, `_access.yaml` and the registries, and writes nothing. A policy
or `_access.yaml` that does not load is the state `unknown`, never an empty diff. Page metadata
comes from the plan's first pass (`PageFact.subject`), so no page is read again.
"""

from __future__ import annotations

import bisect
import dataclasses
import posixpath
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .. import (
    access,
    document_containers,
    homes,
    kbdir,
    layout,
    project_keys,
    topic_carriers,
)
from ..governance import connector_boundary, membership
from ..governance import policy as governance_policy
from ..held_fs import vault_name_key
from ..vocabulary import instances
from .facts import COMPANION_FIELDS, PARENT_MEDIA, PageFact
from .sampled import SAMPLE_LIMIT, Sampled

if TYPE_CHECKING:
    from .plan import RelayoutPlan

#: The states of a figure. The loader's own results, named for the report; `unknown` is never zero.
STATE_EVALUATED = "evaluated"
STATE_NONE = "none"
STATE_UNKNOWN = "unknown"

#: The two inheritance edges that membership reads, named by the kernel that fixes them.
FRAME = membership.FRAME_EDGE
PART = membership.PART_EDGE

#: Why a file's scopes are not evaluated here: the kernel reads them from a source that the plan does not read.
FROM_CONTAINER = "scopes_from_a_document_container"
FROM_COMPANION = "scopes_from_a_companion"

#: Why an edge breaks and what that does to the page's scopes. These are the plan's own report values.
FRAME_LEAVES_FRAMES_FOLDER = "frame_leaves_its_frames_folder"
FRAME_POINTER_NOT_REPOINTED = "frame_pointer_not_repointed"
PART_LEAVES_PART_FOLDER = "part_leaves_its_part_folder"
PARENT_PAGE_ABSENT = "parent_page_absent_after_the_move"
PARENT_PAGE_REPLACED = "another_page_sits_at_the_parent_path"
EFFECT_OWN_SCOPES_ONLY = "page_takes_its_own_scopes_only"
EFFECT_UNRESOLVED = "membership_unresolved_so_access_is_refused"
EFFECT_OTHER_PARENT = "page_inherits_from_another_page"

#: How a path value held by a binding fares: it holds the same files after the move, other files, or none today.
MAPS = "maps"
DOES_NOT_MAP = "does_not_map"
HOLDS_NO_FILES = "holds_no_files"

#: The `Scope` fields that hold path text, each with the kernel rule that reads it. The scope schema fixes them.
_SELECTOR_FIELDS: tuple[tuple[str, Callable[[tuple[str, ...], str], bool]], ...] = (
    ("paths", membership.path_selector_matches),
    ("exclude_paths", membership.path_selector_matches),
    ("refs", membership.ref_selector_matches),
    ("exclude_refs", membership.ref_selector_matches),
)


@dataclass(frozen=True, slots=True)
class Inherit:
    """The page whose scopes a page joins, as membership reads it. `unresolved` says that page is missing."""

    kind: str | None = None
    target: str | None = None
    unresolved: bool = False


NO_EDGE = Inherit()


@dataclass(frozen=True, slots=True)
class BrokenEdge:
    """An inheritance edge that holds today and not after the move."""

    kind: str
    page: str
    new_page: str
    parent: str
    reason: str
    effect: str


@dataclass(frozen=True, slots=True)
class Untranslatable:
    """A selector value that matches renamed pages today and cannot match them after, with examples."""

    scope: str
    field: str
    value: str
    translated: str
    pages: int
    examples: tuple[str, ...]


@dataclass(slots=True)
class ScopeDiff:
    """A single scope's member counts today and after, who enters and leaves, and what flattening would add."""

    scope: str
    name: str | None
    before: int = 0
    after: int = 0
    entering: int = 0
    leaving: int = 0
    entering_sample: list[dict[str, str]] = field(default_factory=list)
    leaving_sample: list[dict[str, str]] = field(default_factory=list)
    under_flatten: int = 0
    under_flatten_sample: list[dict[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.entering or self.leaving)


@dataclass(frozen=True, slots=True)
class BoundValue:
    """A path value that a registry or `_access.yaml` holds, and whether `translate_path` carries it."""

    owner: str
    value: str
    translated: str
    files_today: int
    files_after: int
    state: str


@dataclass(slots=True)
class Group:
    """The values of a single binding source. `state` is unknown when the source did not load."""

    state: str
    detail: str = ""
    values: list[BoundValue] = field(default_factory=list)


@dataclass(slots=True)
class TierChange:
    """The files that move from a single tier to another: all counted, a bounded few named."""

    files: int = 0
    sample: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class Tiers:
    """The `_access.yaml` tiers of every file today and after, and the files whose tier changes."""

    state: str
    detail: str = ""
    before: Counter[str] = field(default_factory=Counter)
    after: Counter[str] = field(default_factory=Counter)
    changes: dict[tuple[str, str], TierChange] = field(default_factory=dict)
    changed_files: int = 0
    prefixes: Group = field(default_factory=lambda: Group(STATE_NONE))

    @property
    def tiers_changed(self) -> int:
        """The tiers whose file set changes: a tier that a file leaves or joins."""
        return len({tier for pair in self.changes for tier in pair})


@dataclass(slots=True)
class Disclosure:
    """What the relayout changes about who may see which file. See the module docstring."""

    policy_state: str
    policy_detail: str
    scopes: list[ScopeDiff]
    tiers: Tiers
    broken_edges: list[BrokenEdge]
    untranslatable: list[Untranslatable] | None
    bindings: dict[str, Group]
    not_evaluated: dict[str, int]
    items_evaluated: int
    flatten_scopes_changed: int | None

    @property
    def scopes_changed(self) -> int | None:
        if self.policy_state == STATE_UNKNOWN:
            return None
        return sum(1 for scope in self.scopes if scope.changed)

    @property
    def differences(self) -> int | None:
        """Scopes and tiers whose member sets change, broken edges and untranslatable selectors; None when a source is unknown."""
        if self.policy_state == STATE_UNKNOWN or self.tiers.state == STATE_UNKNOWN:
            return None
        return (
            (self.scopes_changed or 0)
            + self.tiers.tiers_changed
            + len(self.broken_edges)
            + len(self.untranslatable or ())
        )

    def unevaluated_governed(self) -> int:
        """Governed files whose members the plan could not compare, so a zero difference is partial.

        Files that no policy governs, files outside the managed folder and files with no new path
        (listed as unmappable) are left out of this count on purpose.
        """
        skipped = self.not_evaluated
        return (
            skipped[FROM_CONTAINER]
            + skipped[FROM_COMPANION]
            + skipped["registry_bound"]
            + skipped["membership_unresolved_today"]
            + skipped["membership_unresolved_after"]
        )

    def figures(self) -> dict[str, int | None]:
        """The headline figures as counts; None marks a figure whose source did not load."""
        return {
            "differences": self.differences,
            "scopes_changed": self.scopes_changed,
            "tiers_changed": None
            if self.tiers.state == STATE_UNKNOWN
            else self.tiers.tiers_changed,
            "broken_inheritance_edges": len(self.broken_edges),
            "untranslatable_selectors": None
            if self.untranslatable is None
            else len(self.untranslatable),
        }


# --------------------------------------------------------------------------- #
# Policy and access config
# --------------------------------------------------------------------------- #


def _load_policy(root: Path) -> tuple[str, governance_policy.Policy | None, str]:
    """The loaded policy with its state: evaluated, none (no `_Governance/`), or unknown."""
    try:
        loaded = governance_policy.load(root)
    except Exception as error:  # noqa: BLE001 -- any failure to read the policy is the visible state unknown, not an empty diff
        return STATE_UNKNOWN, None, f"the policy could not be read: {type(error).__name__}"
    if loaded.blocked:
        findings = sorted(
            {f["code"] for f in loaded.findings if f.get("severity", "error") != "warning"}
        )
        return (
            STATE_UNKNOWN,
            None,
            "the policy did not compile: " + (", ".join(findings) or "blocked"),
        )
    if loaded.empty or not loaded.scopes:
        return STATE_NONE, loaded, "no scope is defined"
    return STATE_EVALUATED, loaded, f"{len(loaded.scopes)} scopes"


def _translate(values: Iterable[str], registry: layout.Registry) -> tuple[str, ...]:
    return tuple(layout.translate_path(value, layout.Layout.ROOT, registry) for value in values)


def _translated_policy(
    policy: governance_policy.Policy, registry: layout.Registry
) -> governance_policy.Policy:
    """The policy as the root layout would read it: every path selector value translated."""
    scopes = {
        scope_id: dataclasses.replace(
            scope,
            paths=_translate(scope.paths, registry),
            exclude_paths=_translate(scope.exclude_paths, registry),
            refs=_translate(scope.refs, registry),
            exclude_refs=_translate(scope.exclude_refs, registry),
        )
        for scope_id, scope in policy.scopes.items()
    }
    return dataclasses.replace(policy, scopes=scopes)


def _load_access(root: Path) -> tuple[str, dict[str, list[str]], str]:
    """The `_access.yaml` tiers, read from the bytes on disk, with their state."""
    nothing: dict[str, list[str]] = {"readonly": [], "excluded": []}
    try:
        raw = access.access_config_path(root).read_bytes()
    except FileNotFoundError:
        return STATE_NONE, nothing, "no _access.yaml"
    except OSError as error:
        return STATE_UNKNOWN, nothing, f"_access.yaml could not be read: {type(error).__name__}"
    try:
        return STATE_EVALUATED, access.parse_config(raw), "_access.yaml"
    except (UnicodeError, yaml.YAMLError) as error:
        return STATE_UNKNOWN, nothing, f"_access.yaml did not parse: {type(error).__name__}"


# --------------------------------------------------------------------------- #
# Membership under a single layout
# --------------------------------------------------------------------------- #


class _World:
    """The scopes of every file under a single policy at a single set of paths, with the inheritance edges it has.

    `paths` maps each file's current path to its path in this world; None is today's own paths.
    """

    def __init__(
        self,
        policy: governance_policy.Policy,
        facts: Mapping[str, PageFact],
        paths: Mapping[str, str] | None,
        edges: Mapping[str, Inherit],
    ) -> None:
        self._policy = policy
        self._facts = facts
        self._paths = paths
        self._edges = edges

    def _own(self, item: str) -> frozenset[str] | None:
        """The scopes of the file's own path and metadata, by the kernel; None when membership is unresolved."""
        path = item if self._paths is None else self._paths[item]
        fact = self._facts.get(item)
        carried = fact.subject if fact is not None else None
        subject = (
            dataclasses.replace(carried, path=path)
            if carried is not None
            else membership.MarkdownSubject(path=path)
        )
        try:
            # No root and no registry assignment: this evaluates path, ref and metadata selectors and the residual scope only.
            return membership.evaluate_subject(None, subject, self._policy, None)
        except membership.MembershipUnresolved:
            return None

    def scopes(self, item: str) -> frozenset[str] | None:
        """The file's scopes as `membership.evaluate` reads them: a part takes its Source's, a frame adds its video's."""
        edge = self._edges.get(item, NO_EDGE)
        if edge.unresolved or (edge.kind is not None and edge.target is None):
            return None
        if edge.kind == PART and edge.target is not None:
            return self._own(edge.target)
        own = self._own(item)
        if edge.kind == FRAME and edge.target is not None:
            inherited = self._own(edge.target)
            return None if own is None or inherited is None else own | inherited
        return own


def _edge(
    fact: PageFact,
    path: str,
    pointer: Callable[[str, str], str],
    resolve: Callable[[str], str | None],
) -> Inherit:
    """The page that the file at `path` inherits scopes from, by the kernel's own edge function.

    `pointer(field, value)` is a pointer field's value in the layout read; `resolve(page)` is the
    current path of the file that sits at `page` in that layout, or None.
    """
    media = fact.parent_media
    found = membership.inheritance_edge(
        path,
        part_of=fact.declares_part_of,
        parent_media=None if media is None else pointer(PARENT_MEDIA, media),
    )
    if found is None:
        return NO_EDGE
    target = resolve(found.parent)
    return Inherit(found.kind, target, unresolved=target is None)


def _edges_after(
    candidates: Mapping[str, PageFact],
    paths: Mapping[str, str],
    edits: Mapping[tuple[str, str], str],
) -> dict[str, Inherit]:
    """The inheritance edges of `candidates` once every file sits at `paths` and the planned pointer edits are applied."""
    at = {new: current for current, new in paths.items()}
    found: dict[str, Inherit] = {}
    for item, fact in candidates.items():
        if item not in paths:
            continue
        edge = _edge(
            fact,
            paths[item],
            lambda name, value, item=item: edits.get((item, name), value),
            at.get,
        )
        if edge is not NO_EDGE:
            found[item] = edge
    return found


def _edges_before(
    candidates: Mapping[str, PageFact], facts: Mapping[str, PageFact]
) -> dict[str, Inherit]:
    found: dict[str, Inherit] = {}
    for item, fact in candidates.items():
        edge = _edge(
            fact,
            item,
            lambda _name, value: value,
            lambda page: page if page in facts else None,
        )
        if edge is not NO_EDGE:
            found[item] = edge
    return found


def _breaks(
    item: str, today: Inherit, after: Inherit, new_page: str, *, pointer_stale: bool
) -> BrokenEdge | None:
    """The broken edge of a file whose edge holds today and not after, with the reason and the effect on its scopes.

    `pointer_stale` says the frame's `parent_media` still names a path below today's managed
    folder because the plan has no new path for it.
    """
    if today.kind is None or today.unresolved:
        return None
    if after.kind == today.kind and not after.unresolved and after.target == today.target:
        return None
    if after.unresolved:
        reason, effect = PARENT_PAGE_ABSENT, EFFECT_UNRESOLVED
    elif after.kind is None:
        effect = EFFECT_OWN_SCOPES_ONLY
        if today.kind == PART:
            reason = PART_LEAVES_PART_FOLDER
        else:
            reason = FRAME_POINTER_NOT_REPOINTED if pointer_stale else FRAME_LEAVES_FRAMES_FOLDER
    else:
        reason, effect = PARENT_PAGE_REPLACED, EFFECT_OTHER_PARENT
    return BrokenEdge(today.kind, item, new_page, today.target or "", reason, effect)


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def _untranslatable(
    policy: governance_policy.Policy, registry: layout.Registry, moved: Mapping[str, str]
) -> list[Untranslatable]:
    """Selector values that match a renamed file today and cannot match it after the move."""
    renamed = [
        (current, new)
        for current, new in sorted(moved.items())
        if posixpath.basename(current) != posixpath.basename(new)
    ]
    found: list[Untranslatable] = []
    for scope_id, scope in sorted(policy.scopes.items()):
        for field_name, matches in _SELECTOR_FIELDS:
            for value in getattr(scope, field_name):
                translated = layout.translate_path(value, layout.Layout.ROOT, registry)
                lost = [
                    current
                    for current, new in renamed
                    if matches((value,), current) and not matches((translated,), new)
                ]
                if lost:
                    found.append(
                        Untranslatable(
                            scope_id,
                            field_name,
                            value,
                            translated,
                            len(lost),
                            tuple(lost[:SAMPLE_LIMIT]),
                        )
                    )
    return found


# --------------------------------------------------------------------------- #
# Values that registries and _access.yaml hold
# --------------------------------------------------------------------------- #


class _Tree:
    """The files of a single layout, to count what a folder value holds under the vault's name comparison."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._keys = sorted(vault_name_key(path) for path in paths)

    def holds(self, value: str) -> int:
        """The files at `value` or below it; a value that names no path holds none."""
        key = vault_name_key(value.strip("/"))
        if not key:
            return 0
        below = bisect.bisect_left(self._keys, key + "0") - bisect.bisect_left(
            self._keys, key + "/"
        )
        return below + bisect.bisect_right(self._keys, key) - bisect.bisect_left(self._keys, key)


def _bound(
    owner: str, value: str, registry: layout.Registry, today: _Tree, after: _Tree
) -> BoundValue:
    translated = layout.translate_path(value, layout.Layout.ROOT, registry)
    files_today = today.holds(kbdir.managed_form(value))
    files_after = after.holds(translated)
    if files_today == 0:
        state = HOLDS_NO_FILES
    else:
        state = MAPS if files_after == files_today else DOES_NOT_MAP
    return BoundValue(owner, value, translated, files_today, files_after, state)


def _vocabulary_paths(document: Mapping[str, object]) -> list[str]:
    """The path text of a version 2 vocabulary binding: each instance's storage and every page binding."""
    paths: list[str] = []
    bindings = [document.get("public"), *dict(document.get("private") or {}).values()]
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        paths += [str(binding[name]) for name in ("namespace", "history") if name in binding]
        for override in dict(binding.get("overrides") or {}).values():
            paths += [str(value) for value in dict(override).values()]
    for name in ("destinations", "selections"):
        paths += [str(path) for path in dict(document.get(name) or {})]
    return paths


def _bindings(
    root: Path, registry: layout.Registry, today: _Tree, after: _Tree
) -> tuple[dict[str, Group], bool]:
    """The path values of the registries, with whether each translates; and whether a version 2 vocabulary binding exists."""
    groups: dict[str, Group] = {}
    armed = False
    try:
        requirement = connector_boundary.read_requirement(root)
    except connector_boundary.BoundaryUnavailable:
        groups["vocabulary_instances"] = Group(
            STATE_UNKNOWN, "the connector requirement could not be read"
        )
    else:
        armed = requirement is not None and requirement.get("version") == 2
        group = Group(STATE_EVALUATED if armed else STATE_NONE)
        if armed and requirement is not None:
            group.values = [
                _bound("vocabulary_instances", value, registry, today, after)
                for value in _vocabulary_paths(requirement["vocabulary"])
            ]
        groups["vocabulary_instances"] = group

    carriers = topic_carriers.load(root)
    group = Group(STATE_UNKNOWN if carriers.findings else STATE_EVALUATED)
    if carriers.findings:
        group.detail = "the topic-carriers overlay is invalid"
    group.values = [
        _bound("topic_carrier_folders", carrier.match, registry, today, after)
        for carrier in carriers.of_kind(topic_carriers.KIND_FOLDER)
    ]
    groups["topic_carrier_folders"] = group

    home_set = homes.load(root)
    group = Group(STATE_UNKNOWN if home_set.findings else STATE_EVALUATED)
    if home_set.findings:
        group.detail = "the homes overlay is invalid"
    group.values = [
        _bound("homes", home.folder, registry, today, after)
        for home in home_set.homes
        if home.active
    ]
    groups["homes"] = group

    # A project key names a folder by its name; only a value that holds a path can need a translation.
    folders = project_keys.load_project_registry(root).project_to_folder.values()
    groups["project_key_folders"] = Group(
        STATE_EVALUATED,
        f"{sum(1 for folder in folders if '/' not in folder)} folders are names that hold no path",
        [
            _bound("project_key_folders", folder, registry, today, after)
            for folder in folders
            if "/" in folder
        ],
    )
    return groups, armed


# --------------------------------------------------------------------------- #
# The diff
# --------------------------------------------------------------------------- #


def _identity(fact: PageFact | None, path: str) -> dict[str, str]:
    """A file as the report names it: its `exomem_id` when it has an identity, else its path, and always its current path."""
    if fact is not None and fact.exomem_id is not None:
        return {"id": fact.exomem_id, "path": path}
    return {"id": path, "path": path}


def _note(sample: list[dict[str, str]], entry: dict[str, str]) -> None:
    if len(sample) < SAMPLE_LIMIT:
        sample.append(entry)


def _tiers(
    cfg_state: str,
    cfg_detail: str,
    cfg: dict[str, list[str]],
    registry: layout.Registry,
    moved: Mapping[str, str],
    today: _Tree,
    after: _Tree,
) -> Tiers:
    result = Tiers(cfg_state, cfg_detail)
    if cfg_state == STATE_UNKNOWN:
        return result
    translated = {name: list(_translate(cfg[name], registry)) for name in ("readonly", "excluded")}
    for current, new in sorted(moved.items()):
        before_tier = access.tier_in(cfg, current)
        after_tier = access.tier_in(translated, new)
        result.before[before_tier] += 1
        result.after[after_tier] += 1
        if before_tier != after_tier:
            result.changed_files += 1
            change = result.changes.setdefault((before_tier, after_tier), TierChange())
            change.files += 1
            _note(change.sample, {"path": current, "new_path": new})
    result.prefixes = Group(
        STATE_EVALUATED,
        values=[
            _bound(name, prefix, registry, today, after)
            for name in ("readonly", "excluded")
            for prefix in cfg[name]
        ],
    )
    return result


@dataclass(frozen=True, slots=True)
class _Reading:
    """What the plan and the vault give the diff: the plan, the policy and the inheritance edges today and after."""

    plan: RelayoutPlan
    root: Path
    policy: governance_policy.Policy
    candidates: Mapping[str, PageFact]
    edits: Mapping[tuple[str, str], str]
    edges_today: Mapping[str, Inherit]
    edges_after: Mapping[str, Inherit]
    armed: bool


def build(plan: RelayoutPlan, vault_root: Path) -> Disclosure:
    """The disclosure diff of a built `plan` over the vault at `vault_root`. Nothing is written."""
    root = Path(vault_root)
    facts, moved, registry = plan.facts, plan.moved, plan.registry
    policy_state, policy, policy_detail = _load_policy(root)
    cfg_state, cfg, cfg_detail = _load_access(root)

    today_tree = _Tree([*moved, *(item.current_path for item in plan.unmappable)])
    after_tree = _Tree(moved.values())
    bindings, armed = _bindings(root, registry, today_tree, after_tree)
    tiers = _tiers(cfg_state, cfg_detail, cfg, registry, moved, today_tree, after_tree)

    candidates = {
        path: fact
        for path, fact in facts.items()
        if fact.declares_part_of or fact.parent_media is not None
    }
    edits = {(edit.page, edit.field): edit.new for edit in plan.pointers.edits}
    edges_today = _edges_before(candidates, facts)
    edges_after = _edges_after(candidates, moved, edits)
    broken = [
        edge
        for item, today in sorted(edges_today.items())
        if item in moved
        and (
            edge := _breaks(
                item,
                today,
                edges_after.get(item, NO_EDGE),
                moved[item],
                pointer_stale=(item, PARENT_MEDIA) not in edits,
            )
        )
    ]

    not_evaluated = {
        "no_new_path": len(plan.unmappable),
        "not_governed_content": 0,
        FROM_CONTAINER: 0,
        FROM_COMPANION: 0,
        "registry_bound": 0,
        "membership_unresolved_today": 0,
        "membership_unresolved_after": 0,
        "outside_the_managed_folder": sum(entry.files for entry in plan.outside),
    }
    scopes: list[ScopeDiff] = []
    evaluated = 0
    untranslatable: list[Untranslatable] | None = None
    flatten_changed: int | None = None
    if policy is not None:
        untranslatable = _untranslatable(policy, registry, moved)
        flatten_changed = 0
    if policy_state == STATE_EVALUATED and policy is not None:
        reading = _Reading(plan, root, policy, candidates, edits, edges_today, edges_after, armed)
        evaluated, scopes = _count_members(reading, not_evaluated)
        flatten_changed = sum(1 for scope in scopes if scope.under_flatten)
    return Disclosure(
        policy_state,
        policy_detail,
        scopes,
        tiers,
        broken,
        untranslatable,
        bindings,
        not_evaluated,
        evaluated,
        flatten_changed,
    )


def _count_members(reading: _Reading, not_evaluated: dict[str, int]) -> tuple[int, list[ScopeDiff]]:
    """Each scope's members today, after the move and under O1's flat paths; and the files evaluated."""
    plan, policy = reading.plan, reading.policy
    facts, moved = plan.facts, plan.moved
    translated = _translated_policy(policy, plan.registry)
    today = _World(policy, facts, None, reading.edges_today)
    after = _World(translated, facts, moved, reading.edges_after)
    flat_paths = {**moved, **plan.flattened}
    flat = (
        _World(
            translated,
            facts,
            flat_paths,
            _edges_after(reading.candidates, flat_paths, reading.edits),
        )
        if plan.flattened
        else None
    )
    diffs = {scope_id: ScopeDiff(scope_id, scope.name) for scope_id, scope in policy.scopes.items()}
    evaluated = 0
    elsewhere = _scoped_elsewhere(moved, facts, reading.edges_today)
    for item in sorted(moved):
        if governance_policy.is_governance_path(item) or governance_policy.is_workflow_state_path(
            item
        ):
            not_evaluated["not_governed_content"] += 1
            continue
        if item in elsewhere:
            not_evaluated[elsewhere[item]] += 1
            continue
        if reading.armed:
            bound = _registry_bound(reading.root, item, policy)
            if bound is None or bound:
                not_evaluated[
                    "membership_unresolved_today" if bound is None else "registry_bound"
                ] += 1
                continue
        before, then = today.scopes(item), after.scopes(item)
        if before is None or then is None:
            unresolved = "membership_unresolved_" + ("today" if before is None else "after")
            not_evaluated[unresolved] += 1
            continue
        evaluated += 1
        who = _identity(facts.get(item), item)
        for scope_id in before:
            diffs[scope_id].before += 1
        for scope_id in then:
            diffs[scope_id].after += 1
        for scope_id in then - before:
            diffs[scope_id].entering += 1
            _note(diffs[scope_id].entering_sample, who)
        for scope_id in before - then:
            diffs[scope_id].leaving += 1
            _note(diffs[scope_id].leaving_sample, who)
        flatter = flat.scopes(item) if flat is not None else then
        for scope_id in (flatter ^ then) if flatter is not None else ():
            diffs[scope_id].under_flatten += 1
            _note(diffs[scope_id].under_flatten_sample, who)
    return evaluated, list(diffs.values())


def _scoped_elsewhere(
    moved: Mapping[str, str], facts: Mapping[str, PageFact], edges: Mapping[str, Inherit]
) -> dict[str, str]:
    """The files whose scopes the kernel reads from a container binding or a companion page, each with its reason.

    A document container's member takes its parent's scopes, and so does everything that sits
    in the payload tree. A binary that a page names as a companion's artifact or as a dataset
    card's data file takes the semantic scopes of that page. A part or a frame whose parent is
    such a file takes them too. The plan reads none of those bindings, so it counts these
    files apart rather than evaluating their path alone.
    """
    companion_files = {
        value
        for fact in facts.values()
        for name, value in fact.pointers
        if name in COMPANION_FIELDS
    }
    found: dict[str, str] = {}
    for item in moved:
        fact = facts.get(item)
        if (fact is not None and fact.container_member) or document_containers.in_payload_tree(
            item
        ):
            found[item] = FROM_CONTAINER
        elif fact is None and item in companion_files:
            found[item] = FROM_COMPANION
    for item, edge in edges.items():
        if item in moved and item not in found and edge.target in found:
            found[item] = found[edge.target]
    return found


def _registry_bound(root: Path, item: str, policy: governance_policy.Policy) -> bool | None:
    """Whether a version 2 vocabulary binding assigns the file its scope, which no path selector decides.

    None when the binding cannot be read: the kernel then leaves the file's membership unresolved.
    """
    try:
        return instances.bound_scopes(root, item, policy) is not None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _group_report(group: Group) -> dict[str, object]:
    held = Counter(value.state for value in group.values)
    return {
        "state": group.state,
        "detail": group.detail,
        "values": len(group.values),
        MAPS: held[MAPS],
        DOES_NOT_MAP: held[DOES_NOT_MAP],
        HOLDS_NO_FILES: held[HOLDS_NO_FILES],
        "values_that_do_not_map": Sampled(
            [dataclasses.asdict(value) for value in group.values if value.state == DOES_NOT_MAP]
        ),
    }


def _scope_report(scope: ScopeDiff) -> dict[str, object]:
    return {
        "scope": scope.scope,
        "name": scope.name,
        "members_before": scope.before,
        "members_after": scope.after,
        "entering": scope.entering,
        "leaving": scope.leaving,
        "sample_limit": SAMPLE_LIMIT,
        "entering_sample": scope.entering_sample,
        "leaving_sample": scope.leaving_sample,
    }


def report(disclosure: Disclosure) -> dict[str, object]:
    """The disclosure as a JSON-ready section. A figure whose source did not load reads "unknown", never 0."""
    unknown = STATE_UNKNOWN
    figures = {
        name: unknown if value is None else value for name, value in disclosure.figures().items()
    }
    tiers = disclosure.tiers
    changed_first = sorted(disclosure.scopes, key=lambda scope: (not scope.changed, scope.scope))
    scope_section: dict[str, object] = {
        "state": disclosure.policy_state,
        "detail": disclosure.policy_detail,
    }
    if disclosure.policy_state != unknown:
        scope_section |= {
            "changed": figures["scopes_changed"],
            "evaluated": len(disclosure.scopes),
            "items": Sampled([_scope_report(scope) for scope in changed_first]),
        }
    tier_section: dict[str, object] = {"state": tiers.state, "detail": tiers.detail}
    if tiers.state != unknown:
        tier_section |= {
            "files_by_tier_today": dict(sorted(tiers.before.items())),
            "files_by_tier_after": dict(sorted(tiers.after.items())),
            "files_whose_tier_changes": tiers.changed_files,
            "tiers_changed": tiers.tiers_changed,
            "changes": [
                {
                    "from": before,
                    "to": after,
                    "files": change.files,
                    "sample_limit": SAMPLE_LIMIT,
                    "sample": change.sample,
                }
                for (before, after), change in sorted(tiers.changes.items())
            ],
            "prefixes": _group_report(tiers.prefixes),
        }
    return {
        "differences": figures["differences"],
        "counted": "scopes and tiers whose member sets change, broken inheritance edges and untranslatable selectors",
        "figures": figures,
        "scopes": scope_section,
        "access_tiers": tier_section,
        "broken_inheritance_edges": Sampled(
            [dataclasses.asdict(edge) for edge in disclosure.broken_edges]
        ),
        "untranslatable_selectors": unknown
        if disclosure.untranslatable is None
        else Sampled([dataclasses.asdict(item) for item in disclosure.untranslatable]),
        "bindings": {name: _group_report(group) for name, group in disclosure.bindings.items()},
        "files_evaluated": disclosure.items_evaluated,
        "files_not_evaluated": dict(disclosure.not_evaluated),
        "limits": [
            "A file that is not a page is evaluated by its path and refs, unless a companion page or a document container gives it scopes; those files are counted under files_not_evaluated, because the plan does not read the bindings.",
            "Files outside the managed folder carry no scopes today and are governed content in the root layout; they are counted, not evaluated.",
            "A version 2 vocabulary binding assigns scopes to its storage files; those files are counted, not evaluated, and the binding's own path values are listed under bindings.",
            "The O1 count reads the translated policy at the flattened paths, with the pointer edits of the primary plan.",
        ],
    }


def flatten_report(disclosure: Disclosure) -> dict[str, object] | str:
    """The scopes whose members change if the partition folders are flattened (O1); "unknown" when the policy did not load."""
    if disclosure.flatten_scopes_changed is None:
        return STATE_UNKNOWN
    return {
        "count": disclosure.flatten_scopes_changed,
        "reads": "the translated policy at the flattened paths, against the primary plan's paths",
        "scopes": Sampled(
            [
                {
                    "scope": scope.scope,
                    "files_that_change_membership": scope.under_flatten,
                    "sample": scope.under_flatten_sample,
                }
                for scope in disclosure.scopes
                if scope.under_flatten
            ]
        ),
    }
