"""The binding constraint: a motivation review discloses nothing about a target.

`plan_progress._observe` already states the rule for Records evidence — *a
missing target and a withheld target return the same reason so the review
cannot be used to probe for hidden collections.* Motivation references reach
further, into ordinary knowledge pages, so the rule has to hold across several
distinct failures rather than two: a reference the vault does not hold, one it
holds twice, one whose page a governance ceiling blocks, and one whose page the
access tier excludes.

Written after the implementation, deliberately, so it is adversarial against
what was built rather than co-designed with it. Two forms, and the second is
the one that matters: the structural form shows the entries are the same shape,
while the equality form asserts that the *whole* response is a function of
released state alone — which is what catches count drift, shape drift, and
tally movement that an example can miss.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLANNING_ID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
PLANNING_COLLECTION = "Knowledge Base/Planning/Work/_collection.md"
ITEMS_DIR = "Knowledge Base/Planning/Work/Items"

ABSENT_ID = "4d5e6f70-8192-43a4-95b6-c7d8e9fa0b1c"
BLOCKED_ID = "6f708192-a3b4-45c6-97d8-e9fa0b1c2d3e"
EXCLUDED_ID = "5e6f7081-92a3-44b5-86c7-d8e9fa0b1c2d"
TWIN_ID = "708192a3-b4c5-46d7-a8e9-fa0b1c2d3e4f"
PUBLIC_TWIN_ID = "8192a3b4-c5d6-47e8-b9fa-0b1c2d3e4f50"
LIVE_ID = "92a3b4c5-d6e7-48f9-8a0b-1c2d3e4f5061"

ABSENT_REF = f"exomem://memory/{ABSENT_ID}"
BLOCKED_REF = f"exomem://memory/{BLOCKED_ID}"
EXCLUDED_REF = f"exomem://memory/{EXCLUDED_ID}"
TWIN_REF = f"exomem://memory/{TWIN_ID}"
PUBLIC_TWIN_REF = f"exomem://memory/{PUBLIC_TWIN_ID}"
LIVE_REF = f"exomem://memory/{LIVE_ID}"

PLAN_IDS = {
    "absent": "11111111-1111-4111-8111-111111111111",
    "blocked": "22222222-2222-4222-8222-222222222222",
    "excluded": "33333333-3333-4333-8333-333333333333",
    "duplicated": "44444444-4444-4444-8444-444444444444",
    "released": "55555555-5555-4555-8555-555555555555",
    "twinned": "66666666-6666-4666-8666-666666666666",
    "second": "77777777-7777-4777-8777-777777777777",
}

BLOCKED_PAGE = "Knowledge Base/Notes/Blocked/Blocked Belief.md"
EXCLUDED_PAGE = "Knowledge Base/Notes/Open/Private/Excluded Belief.md"

_PLANNING_MANIFEST = f"""---
type: collection
exomem_id: {PLANNING_ID}
title: Planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
    progress_evidence:
      type: array
      items: {{type: object}}
    motivation:
      type: array
      items: {{type: string}}
---
"""


# --------------------------------------------------------------------------
# Fixture plumbing
# --------------------------------------------------------------------------


def _page(exomem_id: str, title: str, *, status: str = "superseded") -> str:
    return (
        "---\n"
        "type: note\n"
        f"exomem_id: {exomem_id}\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "A belief this vault holds.\n"
    )


def _write_page(root: Path, relative: str, markdown: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")


def _write_governance(root: Path, *, block: bool = True) -> None:
    """Release Planning and `Notes/Open`; block `Notes/Blocked` unless told not to.

    `block=False` releases `Notes/Blocked` at the same ceiling as everything
    else, which is the control every equality assertion below needs: it is the
    only way to show the resolver really reaches the page that the blocked run
    reports as unavailable.

    Note that a governed vault read with no request scope resolves no principal
    at all, so `DISCLOSURE_MIN` applies to everything and the review scans zero
    collections. Every governed fixture here therefore authors its plans before
    the policy lands, and reads under an explicit external scope.
    """
    base = root / "Knowledge Base" / "_Governance"
    (base / "scopes").mkdir(parents=True, exist_ok=True)
    (base / "rules").mkdir(parents=True, exist_ok=True)
    (base / "scopes" / "open.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FD1\n"
        "name: open\n"
        'paths: ["Planning/**", "Notes/Open/**"]\n',
        encoding="utf-8",
    )
    (base / "rules" / "open.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FD2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FD1"]\n'
        "audience: external\nceiling: 6\n",
        encoding="utf-8",
    )
    (base / "scopes" / "blocked.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FC5\n"
        "name: blocked\n"
        'paths: ["Notes/Blocked/**"]\n',
        encoding="utf-8",
    )
    (base / "rules" / "blocked.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FC6\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC5"]\n'
        f"audience: external\nceiling: {0 if block else 6}\n",
        encoding="utf-8",
    )


def _write_access_policy(root: Path, excluded: str) -> None:
    """Exclude one subtree by access tier — a mechanism governance never sees.

    `refuse_if_excluded` is the shared enforcement point every direct-read
    surface consults, and its own contract is that refusal must be rendered
    indistinguishable from a missing path.
    """
    (root / "Knowledge Base" / "_access.yaml").write_text(
        f"excluded:\n  - {excluded}\n", encoding="utf-8"
    )


def _seed_planning(root: Path) -> None:
    from exomem import planning

    (root / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    (root / "Knowledge Base" / "log.md").write_text("# Log\n", encoding="utf-8")
    planning.create_collection(
        root, PLANNING_COLLECTION, _PLANNING_MANIFEST, why="create planning collection"
    )


def _add_plan(root: Path, title: str, motivation: list[str], plan_id: str) -> None:
    from exomem import planning

    planning.add(
        root,
        PLANNING_COLLECTION,
        item={
            "title": title,
            "kind": "outcome",
            "status": "active",
            "commitment": "committed",
            "horizon": "quarter",
            "priority": "high",
            "motivation": motivation,
        },
        plan_id=plan_id,
        why="capture intent",
    )


def _external() -> Any:
    from exomem.governance.principal import RequestPrincipal

    return RequestPrincipal(audience_id="external", surface="mcp")


def _redacted(entry: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """The entry without the caller's own authored reference.

    The reference is the one thing that legitimately differs between these
    entries — the reader authored it, and it is echoed back. Everything else
    must be identical.
    """
    return tuple(
        sorted((key, value) for key, value in entry.items() if key != "memory")
    )


def _frozen(payload: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted(payload.items()))


def _motivation_entry(item: Mapping[str, Any]) -> Mapping[str, Any]:
    [entry] = item["motivation"]
    return entry


def _by_title(result: Mapping[str, Any], title: str) -> Mapping[str, Any]:
    [item] = [entry for entry in result["items"] if entry["intent"]["title"] == title]
    return item


def _walk(payload: Any, key: Any = None) -> Any:
    yield key, payload
    if isinstance(payload, Mapping):
        for name, value in payload.items():
            yield from _walk(value, name)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _walk(value, key)


# --------------------------------------------------------------------------
# Structural form: four failures, one indistinguishable entry
# --------------------------------------------------------------------------


def _four_way_vault(root: Path, *, released: bool = False) -> None:
    _seed_planning(root)
    _write_page(root, BLOCKED_PAGE, _page(BLOCKED_ID, "Blocked Belief"))
    _write_page(root, EXCLUDED_PAGE, _page(EXCLUDED_ID, "Excluded Belief"))
    _write_page(root, "Knowledge Base/Notes/Open/Twin A.md", _page(TWIN_ID, "Twin A"))
    _write_page(root, "Knowledge Base/Notes/Open/Twin B.md", _page(TWIN_ID, "Twin B"))
    _add_plan(root, "Absent", [ABSENT_REF], PLAN_IDS["absent"])
    _add_plan(root, "Blocked", [BLOCKED_REF], PLAN_IDS["blocked"])
    _add_plan(root, "Excluded", [EXCLUDED_REF], PLAN_IDS["excluded"])
    _add_plan(root, "Duplicated", [TWIN_REF], PLAN_IDS["duplicated"])
    if released:
        _write_page(
            root,
            "Knowledge Base/Notes/Open/Live Belief.md",
            _page(LIVE_ID, "Live Belief"),
        )
        _add_plan(root, "Released", [LIVE_REF], PLAN_IDS["released"])
    _write_governance(root)
    _write_access_policy(root, "Notes/Open/Private")


def test_four_unresolvable_motivations_produce_one_indistinguishable_entry(
    tmp_path: Path,
) -> None:
    """A reference not found, one found twice, a blocked page and an
    access-tier refusal all collapse into a single outcome.

    Four different mechanisms, deliberately: a corpus miss, an ambiguity the
    resolver would otherwise raise a counting message for, a governance ceiling
    of zero, and the excluded access tier — which governance never even sees.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    _four_way_vault(tmp_path)

    with request_scope(_external()):
        result = plan_progress.review(tmp_path)

    entries = {item["intent"]["title"]: _motivation_entry(item) for item in result["items"]}
    assert set(entries) == {"Absent", "Blocked", "Excluded", "Duplicated"}

    redacted = {title: _redacted(entry) for title, entry in entries.items()}
    assert len(set(redacted.values())) == 1, redacted
    assert dict(next(iter(redacted.values()))) == {
        "resolved": False,
        "unresolved_reason": "motivation_unavailable",
        "superseded": None,
    }

    divergences = [item["divergence"] for item in result["items"]]
    assert len({_frozen(block) for block in divergences}) == 1, divergences
    assert divergences[0]["motivation_refs"] == 1
    assert divergences[0]["motivation_resolved"] == 0
    assert divergences[0]["motivation_unresolved"] == 1
    assert divergences[0]["motivation_superseded"] == 0

    # The tally counts every entry once and names no case.
    assert result["unavailable"]["motivation_unavailable"] == 4
    assert result["unavailable"]["motivation_budget_exhausted"] == 0

    serialized = str(result)
    for leaked in (
        "Blocked Belief",
        "Excluded Belief",
        "Twin A",
        "Twin B",
        "Notes/Blocked",
        "Notes/Open",
        "appears in",
        "REFERENCE_NOT_FOUND",
        "AMBIGUOUS_REFERENCE",
        "INVALID_REFERENCE",
    ):
        assert leaked not in serialized, leaked


def test_a_released_target_is_the_only_thing_that_resolves(tmp_path: Path) -> None:
    """Non-vacuity: the four entries above are not identical because nothing works.

    Without this, an implementation that reported every motivation reference as
    unresolved would pass the whole disclosure suite.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    _four_way_vault(tmp_path, released=True)

    with request_scope(_external()):
        result = plan_progress.review(tmp_path)

    entry = _motivation_entry(_by_title(result, "Released"))
    assert entry["resolved"] is True
    assert entry["superseded"] is True
    assert _by_title(result, "Released")["divergence"]["motivation_superseded"] == 1


def _twin_vault(root: Path, *, twin: bool) -> None:
    """A released page carrying an identity, optionally shared by a hidden one."""
    _seed_planning(root)
    _write_page(
        root,
        "Knowledge Base/Notes/Open/Public Twin.md",
        _page(PUBLIC_TWIN_ID, "Public Twin"),
    )
    if twin:
        _write_page(
            root,
            "Knowledge Base/Notes/Blocked/Hidden Twin.md",
            _page(PUBLIC_TWIN_ID, "Hidden Twin"),
        )
    _add_plan(root, "Twinned", [PUBLIC_TWIN_REF], PLAN_IDS["twinned"])
    _write_governance(root)


def test_an_unreleased_twin_is_invisible_rather_than_ambiguous(
    tmp_path: Path,
) -> None:
    """Authorization precedes uniqueness, and that ordering is a disclosure decision.

    An earlier draft of this change decided uniqueness over the *unfiltered*
    resolution, reasoning that filtering first would present a duplicated
    identity as a confident unique hit. Measurement showed the reasoning
    backwards: under that ordering the identical reference resolved when no
    hidden page shared the identity and refused when one did, so an unreleased
    page was directly observable to anyone who could cite its id — needing only
    one released page of their own to cite it from.

    Filtering first makes the two vaults indistinguishable. A duplicated
    identity remains an owner-visible repair item through `backfill_ids` and
    `issues()`, which is where it belongs.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    with_twin = tmp_path / "with_twin"
    without_twin = tmp_path / "without_twin"
    _twin_vault(with_twin, twin=True)
    _twin_vault(without_twin, twin=False)

    with request_scope(_external()):
        present = plan_progress.review(with_twin)
    with request_scope(_external()):
        absent = plan_progress.review(without_twin)

    present.pop("generated_at")
    absent.pop("generated_at")

    assert present == absent

    # Non-vacuity: the reference genuinely resolves rather than both vaults
    # refusing it for some unrelated reason.
    entry = _motivation_entry(present["items"][0])
    assert entry["resolved"] is True
    assert entry["superseded"] is True
    assert "Hidden Twin" not in str(present)


def test_a_malformed_stored_reference_refuses_the_collection_without_disclosure(
    tmp_path: Path,
) -> None:
    """`INVALID_REFERENCE` cannot reach a reviewed row, and that is correct.

    A governed Planning collection normalizes every stored record inside
    `query`, so a directly-edited malformed `motivation` entry refuses the
    whole collection before the review sees a row. The review reports that as
    the existing bounded `collections_unavailable` counter and discloses no
    path, title, or reason — which is the same non-answer the per-reference
    outcome would have given.
    """
    from exomem import plan_progress

    _seed_planning(tmp_path)
    _write_page(
        tmp_path, "Knowledge Base/Notes/Open/Live Belief.md", _page(LIVE_ID, "Live Belief")
    )
    _add_plan(tmp_path, "Malformed", [LIVE_REF], PLAN_IDS["absent"])
    item_path = tmp_path / ITEMS_DIR / f"{PLAN_IDS['absent']}.md"
    item_path.write_text(
        item_path.read_text(encoding="utf-8").replace(LIVE_REF, "exomem://memory/not-a-uuid"),
        encoding="utf-8",
    )

    result = plan_progress.review(tmp_path)

    assert result["items"] == []
    assert result["collections_scanned"] == 0
    assert result["collections_unavailable"] == 1
    serialized = str(result)
    assert "not-a-uuid" not in serialized
    assert "Planning/Work" not in serialized
    assert "INVALID_PLAN" not in serialized


# --------------------------------------------------------------------------
# Equality form: the whole response is a function of released state alone
# --------------------------------------------------------------------------


def _hidden_page_vault(root: Path, *, hidden_page: bool, block: bool = True) -> None:
    """Two vaults from one recipe, differing only by one blocked page.

    Deliberately carries no Records evidence: an executed saved view reports
    the collection's snapshot hash, a content digest over records whose
    canonical bytes carry their own capture instants, so two independently
    built vaults would differ there for reasons that have nothing to do with
    disclosure. Every remaining field is a pure function of the authored
    fixture.
    """
    _seed_planning(root)
    _write_page(
        root, "Knowledge Base/Notes/Open/Live Belief.md", _page(LIVE_ID, "Live Belief")
    )
    if hidden_page:
        _write_page(root, BLOCKED_PAGE, _page(BLOCKED_ID, "Blocked Belief"))
    _add_plan(root, "Cites the hidden page", [BLOCKED_REF], PLAN_IDS["blocked"])
    _add_plan(root, "Cites a released page", [LIVE_REF], PLAN_IDS["released"])
    _write_governance(root, block=block)


def _pair(tmp_path: Path, *, block: bool = True) -> tuple[Path, Path]:
    with_page = tmp_path / "with"
    without_page = tmp_path / "without"
    _hidden_page_vault(with_page, hidden_page=True, block=block)
    _hidden_page_vault(without_page, hidden_page=False, block=block)
    return with_page, without_page


def test_the_entire_review_is_equal_whether_or_not_the_hidden_page_exists(
    tmp_path: Path,
) -> None:
    """The faithful statement of "cannot be used to probe for hidden knowledge".

    Not "the entry looks the same" — the whole response, counters, tallies and
    all, is identical. Count drift, shape drift and tally movement all fail
    here even where the example form would still pass.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    with_page, without_page = _pair(tmp_path)

    with request_scope(_external()):
        present = plan_progress.review(with_page)
    with request_scope(_external()):
        absent = plan_progress.review(without_page)

    present.pop("generated_at")
    absent.pop("generated_at")

    assert present == absent


def test_the_two_vaults_really_do_differ_for_a_reader_that_may_see_the_page(
    tmp_path: Path,
) -> None:
    """Non-vacuity for the equality above, and the control that isolates it.

    If the blocked page were simply invisible to the resolver — a stale
    sidecar, a scan that never reached it, a cache — the equality test would
    pass while proving nothing. The identical pair, differing only in that the
    same scope is released rather than blocked, must produce *different*
    responses. So the equality above is the ceiling doing the work.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    with_page, without_page = _pair(tmp_path, block=False)

    with request_scope(_external()):
        present = plan_progress.review(with_page)
    with request_scope(_external()):
        absent = plan_progress.review(without_page)
    present.pop("generated_at")
    absent.pop("generated_at")

    assert present != absent
    visible = _motivation_entry(_by_title(present, "Cites the hidden page"))
    assert visible["resolved"] is True
    assert visible["superseded"] is True
    hidden = _motivation_entry(_by_title(absent, "Cites the hidden page"))
    assert hidden["resolved"] is False


def test_the_budget_verdict_does_not_move_with_hidden_state(tmp_path: Path) -> None:
    """The budget is a counter over authored references, not over targets.

    A budget spent only on references that resolved would report a different
    `motivation_consulted` depending on what exists, which is a probe channel
    the collapsed reason does not close.
    """
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    with_page, without_page = _pair(tmp_path)

    with request_scope(_external()):
        present = plan_progress.review(with_page, motivation_budget=1)
    with request_scope(_external()):
        absent = plan_progress.review(without_page, motivation_budget=1)

    present.pop("generated_at")
    absent.pop("generated_at")

    assert present == absent
    assert present["motivation_consulted"] == 1
    assert present["motivation_truncated"] is True


def test_an_excluded_page_is_equally_invisible_to_the_whole_response(
    tmp_path: Path,
) -> None:
    """The same equality under the other mechanism, and principal-independent.

    The excluded access tier refuses every reader including the owner, so this
    pair needs no request scope at all — a second, independent route to the
    same guarantee.
    """
    from exomem import plan_progress

    with_page = tmp_path / "with"
    without_page = tmp_path / "without"
    for root, present in ((with_page, True), (without_page, False)):
        _seed_planning(root)
        if present:
            _write_page(root, EXCLUDED_PAGE, _page(EXCLUDED_ID, "Excluded Belief"))
        _add_plan(root, "Cites the excluded page", [EXCLUDED_REF], PLAN_IDS["excluded"])
        _write_access_policy(root, "Notes/Open/Private")

    first = plan_progress.review(with_page)
    second = plan_progress.review(without_page)
    first.pop("generated_at")
    second.pop("generated_at")

    assert first == second
    assert _motivation_entry(first["items"][0])["resolved"] is False

    # Control: the same page, excluded somewhere else, resolves — so the
    # equality above is the access tier and not an unreachable page.
    control = tmp_path / "control"
    _seed_planning(control)
    _write_page(control, EXCLUDED_PAGE, _page(EXCLUDED_ID, "Excluded Belief"))
    _add_plan(control, "Cites the excluded page", [EXCLUDED_REF], PLAN_IDS["excluded"])
    _write_access_policy(control, "Notes/Somewhere Else")

    assert _motivation_entry(plan_progress.review(control)["items"][0])["resolved"] is True


def test_no_motivation_key_is_named_ref_and_no_count_is_a_boolean(tmp_path: Path) -> None:
    """Two shipped assertions restated over the surface this change adds."""
    from exomem import plan_progress
    from exomem.governance.principal import request_scope

    _four_way_vault(tmp_path)

    with request_scope(_external()):
        result = plan_progress.review(tmp_path)

    assert not [key for key, _ in _walk(result) if key in {"ref", "fingerprint"}]
    for item in result["items"]:
        assert all(type(value) is int for value in item["divergence"].values())


def test_the_resolution_work_is_equal_whether_or_not_the_hidden_page_exists(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The response is not the only channel; the work performed is one too.

    Scanning only for the ids the sidecar could not answer made the corpus walk
    conditional on whether *any* page — released or not — carried the cited
    identity. Responses stayed byte-identical while a caller who could time the
    call learned whether a hidden page held the id, with no authoring
    prerequisite at all. Keying the scan on sidecar availability instead closed
    that channel and opened a correctness hole — a page written outside a
    governed write read as absent — so the resolver scans unconditionally and
    the work is a function of the batch alone.

    Asserting the call count rather than wall clock, because the leak is the
    decision to scan, and a timing assertion would be flaky under load while
    proving strictly less.
    """
    from exomem import memory_refs, plan_progress
    from exomem.governance.principal import request_scope

    with_page, without_page = _pair(tmp_path)

    def _counted(root: Path) -> list[Any]:
        scans.append(str(root))
        return real(root)

    real = memory_refs._scan_pages
    scans: list[str] = []
    monkeypatch.setattr(memory_refs, "_scan_pages", _counted)

    with request_scope(_external()):
        plan_progress.review(with_page)
    present_scans = len(scans)

    scans.clear()
    with request_scope(_external()):
        plan_progress.review(without_page)
    absent_scans = len(scans)

    assert present_scans == absent_scans
    # Non-vacuity: equality would also hold if neither review resolved anything.
    assert present_scans >= 1
