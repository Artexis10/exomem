"""The keyed continuity group of the context-activation benchmark (OpenSpec
change ``add-context-activation-benchmark``, design amendment A6).

Every one of the eighteen pre-registered fixtures is a cold start, and their
scorer leaves ``recent_context`` out. This group measures what they cannot: a
fresh session's first turn after earlier work, with gold for the
recent-context block and for the referent the turn resolves. It is not one of
the eighteen and never enters their verdict.

Pre-registered. The cases, their gold and the scorer's checks are digested by
:func:`continuity_digest`; the digest is pinned in a commit before the group's
first run, and the results are reported whatever they show. Each case is
grounded in a scenario of the close-memory-loop ``memory-loop`` spec:

* K1 -- "A fresh session continues its workspace's thread";
* K2 -- "A recorded episode's page leads until newer work";
* K3 -- "A keyless turn never resolves another conversation's topic";
* K4 -- "A hot page that is not an anchor is carried on recency".

The product path. Each case gets its own unpadded corpus-v4 tree, built,
published and bound exactly as the eighteen are
(:func:`membench.utility.context_activation_product.prepare_tree`). The
earlier session's acts then go through supported doors -- an admitted
``anchor`` pick through ``activate_context``, an ``episode_memory`` record, an
``edit_memory`` commit -- the derived indexes are published again as a live
service would catch up, and the fresh session's turn is one real
``activate_context`` call. Nothing here writes heat, a packet or a page by
hand.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from epistemic.corpora.context_activation import BASE_DISTRACTOR_COUNT, CORPUS_ID

from membench.utility import context_activation_product as product

GROUP_ID = "context-activation-continuity-v1"

#: A ``RecentEntry.page`` naming the recap page the case's own episode record
#: returned, whose path is minted by the writer.
RECAP = "$recap"

#: An ordinary Notes page of corpus v4, named by path: it is not a fixture key.
ORDINARY_NOTE = "Knowledge Base/Notes/Kitchen/sourdough-starter-feeding.md"


@dataclass(frozen=True)
class PriorAct:
    """One act of an earlier session, through a supported door.

    ``kind`` is ``pick`` (``activate_context`` with ``anchor``), ``episode``
    (``episode_memory`` record ``about`` the targets) or ``edit``
    (``edit_memory``). A target is a fixture key or a knowledge-base path.
    """

    kind: str
    targets: tuple[str, ...]
    session: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class RecentEntry:
    page: str
    why: str


@dataclass(frozen=True)
class ContinuityCase:
    case_id: str
    scenario: str
    turn: str
    prior: tuple[PriorAct, ...]
    #: The fresh session's keys; both ``None`` is a keyless caller.
    session: str | None
    workspace: str | None
    #: ``resolved`` (not abstained) or the abstention reason.
    expected_status: str
    #: Pages the packet must serve as ``resolved`` anchors, each with
    #: ``recency`` among its evidence.
    referents: tuple[str, ...]
    #: ``generation.carried_by`` the packet must carry (``None``: absent).
    carried_by: str | None
    #: Pages that must not be a resolved or carried anchor.
    must_not_resolve: tuple[str, ...]
    #: The block's first entry, or ``None`` for no requirement.
    recent_lead: RecentEntry | None
    #: Entries the block must list, anywhere.
    recent_includes: tuple[RecentEntry, ...]
    #: Whether the packet must serve no anchor, unit, pointer or state at all.
    serves_nothing: bool


CASES: tuple[ContinuityCase, ...] = (
    ContinuityCase(
        case_id="K1",
        scenario="A fresh session continues its workspace's thread",
        turn="continue",
        prior=(
            PriorAct("pick", ("c5_resource_profile",), session="k1-earlier", workspace="k1-bench"),
            PriorAct("pick", ("t5_available_resource",), session="k1-other", workspace="k1-elsewhere"),
        ),
        session="k1-fresh",
        workspace="k1-bench",
        expected_status="resolved",
        referents=("c5_resource_profile",),
        carried_by=None,
        must_not_resolve=("t5_available_resource",),
        recent_lead=RecentEntry("c5_resource_profile", "activated"),
        recent_includes=(
            RecentEntry("c5_resource_profile", "activated"),
            RecentEntry("t5_available_resource", "activated"),
        ),
        serves_nothing=False,
    ),
    ContinuityCase(
        case_id="K2",
        scenario="A recorded episode's page leads until newer work",
        turn="where were we",
        prior=(PriorAct("episode", ("c4_entity_profile",)),),
        session="k2-fresh",
        workspace=None,
        expected_status="resolved",
        referents=("c4_entity_profile",),
        carried_by=None,
        must_not_resolve=(),
        recent_lead=RecentEntry(RECAP, "episode"),
        recent_includes=(RecentEntry(RECAP, "episode"),),
        serves_nothing=False,
    ),
    ContinuityCase(
        case_id="K3",
        scenario="A keyless turn never resolves another conversation's topic",
        turn="continue",
        prior=(PriorAct("pick", ("c5_resource_profile",), session="k3-earlier", workspace="k3-bench"),),
        session=None,
        workspace=None,
        expected_status="unresolved",
        referents=(),
        carried_by=None,
        must_not_resolve=("c5_resource_profile",),
        recent_lead=None,
        recent_includes=(RecentEntry("c5_resource_profile", "activated"),),
        serves_nothing=True,
    ),
    ContinuityCase(
        case_id="K4",
        scenario="A hot page that is not an anchor is carried on recency",
        turn="continue",
        prior=(PriorAct("edit", (ORDINARY_NOTE,)),),
        session="k4-fresh",
        workspace=None,
        expected_status="resolved",
        referents=(ORDINARY_NOTE,),
        carried_by="recency",
        must_not_resolve=(),
        recent_lead=RecentEntry(ORDINARY_NOTE, "edited"),
        recent_includes=(RecentEntry(ORDINARY_NOTE, "edited"),),
        serves_nothing=False,
    ),
)

#: The checks :func:`score` applies, in order; part of the digest.
CHECKS: tuple[str, ...] = (
    "status",
    "referents",
    "carried_by",
    "must_not_resolve",
    "serves_nothing",
    "recent_lead",
    "recent_includes",
)


def continuity_digest() -> str:
    """The pre-registered identity of the group: cases, gold and checks."""

    payload = {
        "group_id": GROUP_ID,
        "corpus_id": CORPUS_ID,
        "checks": list(CHECKS),
        "cases": [dataclasses.asdict(case) for case in CASES],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------
# Scoring (pure).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Identities:
    """How one tree spells a case's pages: anchor refs and paths."""

    #: page -> the ref an anchor carries for it.
    refs: Mapping[str, str]
    #: page -> its knowledge-base path, as the block lists it.
    paths: Mapping[str, str]


@dataclass(frozen=True)
class ContinuityScore:
    case_id: str
    observed_status: str
    carried_by: str | None
    resolved: tuple[str, ...]
    recent: tuple[tuple[str, str], ...]
    #: check -> passed, for every check in :data:`CHECKS`.
    checks: tuple[tuple[str, bool], ...]
    failure_reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(ok for _name, ok in self.checks)


def _status(packet: Mapping[str, Any]) -> str:
    if not packet.get("abstained"):
        return "resolved"
    abstention = packet.get("abstention") or {}
    return str(abstention.get("reason") or "abstained")


def score(
    packet: Mapping[str, Any], case: ContinuityCase, identities: Identities
) -> ContinuityScore:
    """Score one fresh-session packet against its pre-registered gold."""

    status = _status(packet)
    anchors = [item for item in packet.get("anchors") or () if isinstance(item, Mapping)]
    served = {
        str(item.get("ref")): item
        for item in anchors
        if item.get("status") in ("resolved", "retrieval_carried")
    }
    resolved = {ref: item for ref, item in served.items() if item.get("status") == "resolved"}
    generation = packet.get("generation") or {}
    carried_by = generation.get("carried_by")
    recent = tuple(
        (str(entry.get("path") or ""), str(entry.get("why") or ""))
        for entry in packet.get("recent_context") or ()
        if isinstance(entry, Mapping)
    )

    def spelled(entry: RecentEntry) -> tuple[str, str]:
        return (identities.paths[entry.page], entry.why)

    names = {path: page for page, path in identities.paths.items()}

    def named(entries: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
        return [(names.get(path, path), why) for path, why in entries]

    reasons: list[str] = []
    checks: dict[str, bool] = {}

    checks["status"] = status == case.expected_status
    if not checks["status"]:
        reasons.append(f"expected status {case.expected_status!r}, observed {status!r}")

    missing = [
        page
        for page in case.referents
        if "recency" not in (resolved.get(identities.refs[page]) or {}).get("evidence", ())
    ]
    checks["referents"] = not missing
    if missing:
        reasons.append(f"referent(s) not resolved on recency: {missing}")

    checks["carried_by"] = carried_by == case.carried_by
    if not checks["carried_by"]:
        reasons.append(f"expected carried_by {case.carried_by!r}, observed {carried_by!r}")

    wrong = [page for page in case.must_not_resolve if identities.refs[page] in served]
    checks["must_not_resolve"] = not wrong
    if wrong:
        reasons.append(f"served another thread's page(s): {wrong}")

    material = bool(
        served or packet.get("units") or packet.get("pointers") or packet.get("current_state")
    )
    checks["serves_nothing"] = not (case.serves_nothing and material)
    if not checks["serves_nothing"]:
        reasons.append("a keyless turn served material")

    if case.recent_lead is None:
        checks["recent_lead"] = True
    else:
        lead = spelled(case.recent_lead)
        checks["recent_lead"] = bool(recent) and recent[0] == lead
        if not checks["recent_lead"]:
            reasons.append(
                f"recent_context leads with {named(recent[:1])}, expected "
                f"{(case.recent_lead.page, case.recent_lead.why)}"
            )

    absent = [
        (entry.page, entry.why) for entry in case.recent_includes if spelled(entry) not in recent
    ]
    checks["recent_includes"] = not absent
    if absent:
        reasons.append(f"recent_context lacks {absent}")

    return ContinuityScore(
        case_id=case.case_id,
        observed_status=status,
        carried_by=carried_by,
        resolved=tuple(sorted(resolved)),
        recent=recent,
        checks=tuple((name, checks[name]) for name in CHECKS),
        failure_reasons=tuple(reasons),
    )


# --------------------------------------------------------------------------
# The product run.
# --------------------------------------------------------------------------


def _identities(tree: product.Tree, case: ContinuityCase) -> Identities:
    bound = dict(tree.binding.key_to_ref)
    pages = {
        *case.referents,
        *case.must_not_resolve,
        *(target for act in case.prior for target in act.targets),
        *(entry.page for entry in (*case.recent_includes, case.recent_lead) if entry),
    }
    refs: dict[str, str] = {}
    paths: dict[str, str] = {}
    for page in pages - {RECAP}:
        if page in tree.corpus.key_to_path:
            refs[page] = bound[page]
            paths[page] = tree.corpus.key_to_path[page]
        else:
            refs[page] = paths[page] = page
    return Identities(refs=refs, paths=paths)


@contextlib.contextmanager
def _vault_env(root: Path) -> Iterator[None]:
    with mock.patch.dict(os.environ, {"EXOMEM_VAULT_PATH": str(root)}):
        yield


def _perform(root: Path, act: PriorAct, identities: Identities) -> dict[str, Any]:
    from exomem import commands, writer_lease
    from exomem import schema as schema_module
    from exomem.governance.principal import owner_principal, request_scope

    if act.kind == "pick":
        (target,) = act.targets
        return commands.op_activate_context(
            root,
            turn="Let's pick this up.",
            anchor=identities.refs[target],
            session=act.session,
            workspace=act.workspace,
        )
    if act.kind == "episode":
        with request_scope(owner_principal(surface="mcp")):
            return commands.op_episode_memory(
                root,
                schema_module.load_source_schema(root),
                action="record",
                subject="Colleague catch-up",
                summary="Went through the open questions with a colleague.",
                worked_on=["Reviewed the colleague's open questions"],
                about=[identities.refs[target] for target in act.targets],
                client="claude-code",
            )
    if act.kind == "edit":
        (target,) = act.targets
        command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "edit_memory")
        return writer_lease.invoke_command(
            command,
            root,
            path=identities.paths[target],
            why="note a detail while working on it",
            operation={
                "kind": "replace_string",
                "old_string": "the night before baking.",
                "new_string": "the night before baking, at room temperature.",
            },
        )
    raise ValueError(f"unknown prior act {act.kind!r}")


@dataclass(frozen=True)
class CaseRun:
    case: ContinuityCase
    identities: Identities
    recap_path: str | None
    packet: dict[str, Any]
    score: ContinuityScore


def run_case(workdir: Path, case: ContinuityCase) -> CaseRun:
    """Build a tree, perform the earlier session's acts, then the fresh turn."""

    from exomem import commands, working_set_heat, working_set_runtime

    working_set_heat.reset_for_tests()
    tree = product.prepare_tree(
        Path(workdir) / case.case_id.lower(), distractor_count=BASE_DISTRACTOR_COUNT
    )
    identities = _identities(tree, case)
    recap_path: str | None = None
    with _vault_env(tree.root):
        for act in case.prior:
            working_set_runtime.reset_caches_for_tests()
            result = _perform(tree.root, act, identities)
            if act.kind == "episode":
                recap_path = str(result["source"]["path"])
        product.publish(tree.root)
        working_set_runtime.reset_caches_for_tests()
        packet = commands.op_activate_context(
            tree.root, turn=case.turn, session=case.session, workspace=case.workspace
        )
    if recap_path is not None:
        identities = Identities(
            refs={**identities.refs, RECAP: recap_path},
            paths={**identities.paths, RECAP: recap_path},
        )
    return CaseRun(case, identities, recap_path, packet, score(packet, case, identities))


def run_group(workdir: Path) -> tuple[CaseRun, ...]:
    return tuple(run_case(workdir, case) for case in CASES)


def recorded_group(runs: tuple[CaseRun, ...]) -> dict[str, Any]:
    """The reproducible part of one group run: every check, never a minted ref."""

    def portable(value: str, run: CaseRun) -> str:
        for page, spelled in (*run.identities.refs.items(), *run.identities.paths.items()):
            if value == spelled:
                return page
        return value if value.startswith("Knowledge Base/") else "<other>"

    return {
        "group_id": GROUP_ID,
        "corpus_id": CORPUS_ID,
        "continuity_digest": continuity_digest(),
        "passed": sum(1 for run in runs if run.score.passed),
        "total": len(runs),
        "per_case": [
            {
                "case_id": run.case.case_id,
                "scenario": run.case.scenario,
                "passed": run.score.passed,
                "observed_status": run.score.observed_status,
                "carried_by": run.score.carried_by,
                "resolved": sorted(portable(ref, run) for ref in run.score.resolved),
                "recent_head": [
                    [portable(path, run), why] for path, why in run.score.recent[:3]
                ],
                "checks": dict(run.score.checks),
                "failure_reasons": list(run.score.failure_reasons),
            }
            for run in runs
        ],
    }


__all__ = [
    "CASES",
    "CHECKS",
    "GROUP_ID",
    "ORDINARY_NOTE",
    "RECAP",
    "CaseRun",
    "ContinuityCase",
    "ContinuityScore",
    "Identities",
    "PriorAct",
    "RecentEntry",
    "continuity_digest",
    "recorded_group",
    "run_case",
    "run_group",
    "score",
]
