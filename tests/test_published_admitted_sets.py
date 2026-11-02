"""A caller's admitted pages are decided once and patched from catalogue deltas.

The activation carry decided every catalogue page against the caller's release
policy on each request, and a restricted caller's structured filter ran the
connector content floors over every candidate. The catalogue now keeps each
caller's page verdicts across requests (`LexicalStore.published_admitted_paths`
and the carry's request hook), keyed by everything a verdict reads except the
page, and a request decides only pages whose catalogue row changed.

These tests pin what that reuse must never change: a page the caller may no
longer see leaves the very next request, another caller never reads a verdict
that was not its own, and a request that cannot name its key decides every
page itself. Invented names only.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, OWNER, call
from test_collection_store_service import service as service  # noqa: F401  (the pytest fixture)
from test_collection_store_writer import manifest_path
from test_connector_boundary import configured_boundary as configured_boundary  # noqa: F401
from test_governance_egress import (
    EXTERNAL,
    SCOPE_ID,
    _gov_dir,
    _reset_caches,
    _verified_external_session,
    write_rule,
    write_scope,
)
from test_private_vocabulary_instances import private_instances as private_instances  # noqa: F401
from test_working_set_carry import _write, seed_ordinary_notes

from exomem import (
    commands,
    lexstore,
    lifecycle_statuses,
    state_paths,
    working_set_index,
    working_set_runtime,
)
from exomem.governance import egress, lifecycle
from exomem.governance.principal import (
    ClientBinding,
    OriginSessionBinding,
    RequestPrincipal,
    owner_principal,
    request_scope,
)
from exomem.published_admission import deciding

SHARED = "Knowledge Base/Notes/Shared/quorvel-tansid-survey.md"
TURN = "What did the quorvel tansid survey conclude?"
ISSUER = "https://memory.example.test"
DENIED_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PRIVATE_PROJECT = "private-project"


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every page the release policy decides, in `egress._decide_path`."""
    decided: list[str] = []
    real = egress._decide_path

    def counting(vault_root, rel_path, *args, **kwargs):
        decided.append(rel_path)
        return real(vault_root, rel_path, *args, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", counting)
    return decided


def _external() -> RequestPrincipal:
    return RequestPrincipal(audience_id=EXTERNAL, surface="mcp")


def _shared_page(project: str = "open-project") -> str:
    return (
        f"---\ntype: research-note\nstatus: active\nupdated: 2026-09-10\nproject: {project}\n---\n\n"
        "# Quorvel tansid survey\n\n## Summary\n\n"
        "- [decision] The quorvel tansid survey closed early.\n"
    )


def _seed_carry(vault: Path, *, grant: bool = True) -> None:
    """A governed page the external audience may carry only while its grant stands."""
    write_scope(vault, paths="Notes/Shared/**", name="Shared", default_deny=True)
    if grant:
        write_rule(vault, ceiling=egress.LEVEL_FULL)
    _write(vault / SHARED, _shared_page())
    seed_ordinary_notes(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()


def _carry(vault: Path, who: RequestPrincipal) -> tuple[list[str], int]:
    """The pages the turn names, and the corpus size rarity is counted over."""
    with request_scope(who), state_paths.resolution_scope():
        basis = lifecycle_statuses.Basis(vault)
        hits, state = working_set_runtime.carry_candidates(vault, TURN, status_basis=basis)
        stems = working_set_runtime.pairable_stems(TURN)
        _rare, pages, rarity_state = working_set_runtime.rare_turn_terms(
            vault, stems, status_basis=basis
        )
    assert (state, rarity_state) == ("available", "available"), "the catalogue must answer"
    return [path for path, _score in hits], pages


def _republish(vault: Path) -> None:
    """The catalogue delta the indexer publishes after a page changes on disk."""
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_runtime.reset_caches_for_tests()


def test_a_removed_grant_leaves_the_next_requests_carry_and_rarity(vault: Path) -> None:
    _seed_carry(vault)
    named, pages = _carry(vault, _external())
    assert named == [SHARED]

    (_gov_dir(vault) / "rules" / "patterns-external.yaml").unlink()
    _reset_caches()
    named_after, pages_after = _carry(vault, _external())

    assert named_after == []
    assert pages_after == pages - 1, "rarity counted a page the caller may no longer see"


def test_a_tombstoned_page_leaves_the_next_requests_carry(vault: Path) -> None:
    _seed_carry(vault)
    assert _carry(vault, _external())[0] == [SHARED]

    lifecycle.begin_deletion(vault, source_rel=SHARED, trash_rel="Knowledge Base/_trash/gone.md")
    named_after, _pages = _carry(vault, _external())

    assert named_after == []


def test_a_page_retagged_into_a_withheld_scope_leaves_the_next_requests_carry(
    vault: Path, decisions: list[str]
) -> None:
    _seed_carry(vault, grant=False)
    # The scope holds pages by project, so a retag moves a page between scopes.
    (_gov_dir(vault) / "scopes" / "patterns.yaml").write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Private\nprojects: [{PRIVATE_PROJECT}]\n"
        "default_deny: true\n",
        encoding="utf-8",
    )
    _reset_caches()
    assert _carry(vault, _external())[0] == [SHARED]

    _write(vault / SHARED, _shared_page(project=PRIVATE_PROJECT))
    _republish(vault)
    decisions.clear()
    named_after, _pages = _carry(vault, _external())

    assert named_after == []
    assert SHARED in decisions, "a retagged page keeps its older verdict"


def test_a_second_request_decides_no_page_and_an_edit_decides_only_that_page(
    vault: Path, decisions: list[str]
) -> None:
    _seed_carry(vault)
    _carry(vault, _external())
    catalogue_pages = len(decisions)
    assert catalogue_pages > 50, "the first request decides every page"

    decisions.clear()
    _carry(vault, _external())
    assert decisions == []

    _write(vault / SHARED, _shared_page().replace("closed early", "closed late"))
    _republish(vault)
    _carry(vault, _external())
    assert set(decisions) == {SHARED}


def test_another_audience_never_reads_a_verdict_decided_for_a_granted_one(vault: Path) -> None:
    _seed_carry(vault, grant=False)
    write_rule(vault, ceiling=egress.LEVEL_FULL, audience="partner")
    _reset_caches()
    partner = RequestPrincipal(audience_id="partner", surface="mcp")

    partner_named, partner_pages = _carry(vault, partner)
    other_named, other_pages = _carry(vault, _external())
    partner_again, _ = _carry(vault, partner)

    assert partner_named == [SHARED] == partner_again
    assert other_named == []
    assert other_pages == partner_pages - 1, "rarity counted a page the audience may not see"


def test_a_verified_authorization_session_decides_every_page_each_request(
    vault: Path, decisions: list[str]
) -> None:
    _seed_carry(vault)
    who, _context = _verified_external_session()
    first, _ = _carry(vault, who)
    first_count = len(decisions)
    decisions.clear()
    second, _ = _carry(vault, who)

    assert first == second
    assert len(decisions) == first_count > 50, (
        "a session grant is time-based, so its verdicts are never kept across requests"
    )


# --------------------------------------------------------------------------
# Structured eligibility: a restricted connector client's content floors
# --------------------------------------------------------------------------

ELIGIBLE = "Knowledge Base/Notes/Eligible"
RETAGGABLE = f"{ELIGIBLE}/harrow-mill-ledger-27.md"
PRIVATE_PAGE = f"{ELIGIBLE}/harrow-mill-ledger-private.md"


def _eligible_page(index: int, project: str = "open-project") -> str:
    return (
        f"---\ntype: insight\nstatus: active\nupdated: 2026-09-{1 + index % 28:02d}\n"
        f"project: {project}\n---\n\n# Harrow mill ledger {index:02d}\n\n## Summary\n\n"
        f"- [decision] The harrow mill ledger {index:02d} was balanced. ^hm-{index}\n"
    )


def _limited_client(
    login: OriginSessionBinding | None = None, client_id: str = "limited"
) -> RequestPrincipal:
    return RequestPrincipal(
        audience_id="owner", surface="mcp", resolved=True, issuer_family="oauth",
        remote_owner=True, client_binding=ClientBinding(ISSUER, client_id),
        origin_session=login or OriginSessionBinding("login-1", "1", "audience"),
    )


def _write_boundary(config: Path, *, denied: list[str], open_client: bool = False) -> None:
    clients = [{"issuer": ISSUER, "client_id": "limited", "denied_scope_ids": denied}]
    if open_client:
        clients.append({"issuer": ISSUER, "client_id": "open", "denied_scope_ids": []})
    config.write_text(json.dumps({
        "version": 1, "default_denied_scope_ids": [DENIED_SCOPE],
        "capture_paths": ["Knowledge Base/Capture"],
        "clients": clients,
    }), encoding="utf-8")


@pytest.fixture
def boundary(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell):
    """Forty open insight pages, one private page, and a client denied the private project."""
    from exomem.activation_manifest import ensure_manifest

    scopes = vault / "Knowledge Base/_Governance/scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "private.yaml").write_text(
        f"governance_version: 1\nid: {DENIED_SCOPE}\nprojects: [{PRIVATE_PROJECT}]\n",
        encoding="utf-8",
    )
    for index in range(40):
        _write(vault / f"{ELIGIBLE}/harrow-mill-ledger-{index:02d}.md", _eligible_page(index))
    _write(vault / PRIVATE_PAGE, _eligible_page(99, PRIVATE_PROJECT))
    with request_scope(owner_principal(surface="library")):
        ensure_manifest(vault)
    config = tmp_path / "host-boundary.json"
    _write_boundary(config, denied=[DENIED_SCOPE])
    monkeypatch.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
    _reset_caches()
    warm_managed_cell(vault)
    return config


@pytest.fixture
def floors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every page the request itself decides for the connector content floors.

    Counted at the admission, not at `egress.content_permits`: releasing the few
    pages a request returns decides them again by design.
    """
    decided: list[str] = []
    real = egress.content_admission

    def counting(vault_root, who):
        predicate = real(vault_root, who)
        decide = predicate.admission.decide

        def counted(rel_path, publish):
            decided.append(rel_path)
            return decide(rel_path, publish)

        predicate.admission.decide = counted
        return predicate

    monkeypatch.setattr(egress, "content_admission", counting)
    return decided


def _insights(vault: Path, who: RequestPrincipal) -> set[str]:
    """The pages a structured `types` filter returns to this caller."""
    from exomem import find as find_module

    # Not `clear_cache`: it hands the published verdicts back with the rest of the idle RAM.
    find_module.unload_ram_caches()
    with request_scope(who):
        result = commands.op_find(
            vault, query="", mode="keyword", scope="kb-only", graph=False,
            include_timings=True, types=["insight"], limit=100,
        )
    return {hit["path"] for hit in result["hits"] if hit["path"].startswith(ELIGIBLE)}


def _edit_and_republish(vault: Path, warm_managed_cell, rel: str, text: str) -> None:
    _write(vault / rel, text)
    warm_managed_cell(vault)


def _published(
    vault: Path, checkpoint=None, *, live: frozenset[str] = frozenset(),
    who: RequestPrincipal | None = None,
):
    """The pages a client's published verdicts admit, as ranking receives them."""
    store = lexstore.get_store(vault)
    current = store.published_recall_checkpoint("kb")
    who = who or _limited_client()
    with request_scope(who):
        admission = egress.content_admission(vault, who).admission
        candidates = store.recall_admitted_paths("kb", current)
        return store.published_admitted_paths(
            "kb", checkpoint or current, admission, set(candidates), live=live
        )


def test_a_page_retagged_into_a_denied_scope_leaves_the_set_the_next_request_ranks(
    vault: Path, boundary: Path, warm_managed_cell
) -> None:
    assert RETAGGABLE in _published(vault)

    _edit_and_republish(vault, warm_managed_cell, RETAGGABLE, _eligible_page(27, PRIVATE_PROJECT))
    admitted = _published(vault)

    assert RETAGGABLE not in admitted
    assert len(admitted) >= 40, "the other pages stay admitted"


def test_a_page_retagged_out_of_a_denied_scope_joins_the_next_filtered_request(
    vault: Path, boundary: Path, warm_managed_cell
) -> None:
    _edit_and_republish(vault, warm_managed_cell, RETAGGABLE, _eligible_page(27, PRIVATE_PROJECT))
    assert RETAGGABLE not in _insights(vault, _limited_client())

    _edit_and_republish(vault, warm_managed_cell, RETAGGABLE, _eligible_page(27))

    assert RETAGGABLE in _insights(vault, _limited_client())


def test_a_second_filtered_request_decides_no_page_and_an_edit_decides_only_that_page(
    vault: Path, boundary: Path, floors: list[str], warm_managed_cell
) -> None:
    _insights(vault, _limited_client())
    assert len(floors) >= 40, "the first request decides every candidate"

    floors.clear()
    _insights(vault, _limited_client())
    assert floors == []

    _edit_and_republish(vault, warm_managed_cell, RETAGGABLE, _eligible_page(27, "another-project"))
    floors.clear()
    _insights(vault, _limited_client())
    assert set(floors) == {RETAGGABLE}


def test_two_logins_of_one_client_share_the_published_verdicts(
    vault: Path, boundary: Path, floors: list[str]
) -> None:
    _insights(vault, _limited_client())
    assert len(floors) >= 40, "the first login decides every candidate"

    floors.clear()
    _insights(vault, _limited_client(OriginSessionBinding("login-2", "7", "audience")))

    assert floors == [], "the floors read the denied scopes, not which login asked"


def test_a_less_restricted_client_never_lends_its_verdicts_to_a_restricted_one(
    vault: Path, boundary: Path
) -> None:
    _write_boundary(boundary, denied=[DENIED_SCOPE], open_client=True)
    assert PRIVATE_PAGE in _published(vault, who=_limited_client(client_id="open"))

    assert PRIVATE_PAGE not in _published(vault), "the open client's verdict reached a denied one"


def test_a_boundary_change_decides_afresh_in_both_directions(vault: Path, boundary: Path) -> None:
    assert PRIVATE_PAGE not in _published(vault)

    _write_boundary(boundary, denied=[])
    assert PRIVATE_PAGE in _published(vault), "a stale withheld verdict hid it"

    _write_boundary(boundary, denied=[DENIED_SCOPE])
    assert PRIVATE_PAGE not in _published(vault), "a stale allowed verdict kept it"


@pytest.mark.parametrize("filter_kind", ["content", "page-release", "walk-release"])
def test_registry_reassignment_retires_verdicts_without_a_page_or_host_config_edit(
    vault: Path, private_instances, configured_boundary, warm_managed_cell, filter_kind: str,
) -> None:
    """An unchanged page can enter a denied scope through the arming record alone."""
    import copy
    from dataclasses import replace

    from test_connector_boundary import PUBLIC, SCOPE

    from exomem.governance import connector_boundary, policy
    from exomem.published_admission import admission_of
    from exomem.vocabulary import instances

    who = private_instances("limited")
    config, _authenticate = configured_boundary
    config_before = config.read_bytes()
    page_before = (vault / PUBLIC).read_bytes()
    warm_managed_cell(vault)
    store = lexstore.get_store(vault)

    def published():
        with request_scope(who), state_paths.resolution_scope():
            if filter_kind == "content":
                keep = egress.content_admission(vault, who)
            else:
                factory = (egress.page_release_filter if filter_kind == "page-release"
                           else egress.release_walk_filter)
                keep = factory(vault, principal=who)
            admission = admission_of(keep)
            assert admission is not None
            checkpoint = store.published_recall_checkpoint("kb")
            return store.published_admitted_paths("kb", checkpoint, admission, {PUBLIC})

    assert published() == {PUBLIC}
    current = connector_boundary.snapshot(vault)
    assignment = copy.deepcopy(current.vocabulary)
    assignment["private"][SCOPE]["namespace"] = PUBLIC
    instances.validate(assignment, policy.load(vault))
    connector_boundary.publish_requirement(vault, replace(current, vocabulary=assignment))

    assert published() == set(), "the older allowed verdict survived a scope reassignment"
    assert (vault / PUBLIC).read_bytes() == page_before
    assert config.read_bytes() == config_before

    connector_boundary.publish_requirement(vault, current)
    assert published() == {PUBLIC}, "the withheld verdict survived the restored assignment"


def test_a_page_under_pending_custody_is_decided_on_its_current_bytes(
    vault: Path, boundary: Path
) -> None:
    assert RETAGGABLE in _published(vault)

    # The writer's own change: on disk, not yet in the catalogue.
    _write(vault / RETAGGABLE, _eligible_page(27, PRIVATE_PROJECT))

    assert RETAGGABLE not in _published(vault, live=frozenset({RETAGGABLE}))


def test_a_checkpoint_the_catalogue_has_moved_past_abstains_instead_of_answering(
    vault: Path, boundary: Path, warm_managed_cell
) -> None:
    old = lexstore.get_store(vault).published_recall_checkpoint("kb")
    assert _published(vault, old)

    _edit_and_republish(vault, warm_managed_cell, RETAGGABLE, _eligible_page(27, "another-project"))

    assert _published(vault, old) is None


# --------------------------------------------------------------------------
# Collection placement: a store-owned page is not decided from its own bytes
# --------------------------------------------------------------------------


def _carried_pages(root: Path, who: RequestPrincipal | None = None) -> tuple[str, set[str]]:
    """The readiness and the pages the caller's rarity counts over (the external audience by default)."""
    with request_scope(who or _external()), state_paths.resolution_scope():
        keep = egress.release_walk_filter(root)
        result = lexstore.carry_term_statistics(
            root, ["work"], visible=keep, status_basis=lifecycle_statuses.Basis(root),
            path_limit=lambda pages: 5,
        )
    return result.readiness.status, set(result.value[3] or ())


def test_a_page_withheld_while_the_collection_store_was_down_returns_when_it_is_back(
    service, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from exomem.collection_store import runtime

    root = service.root
    write_scope(root, paths="Unrelated/**")
    write_rule(root, ceiling=egress.LEVEL_FULL)
    lexstore.ensure_fresh(root)
    manifest = manifest_path()

    with monkeypatch.context() as outage:
        outage.setattr(runtime, "_server", lambda _root: None)
        egress.clear_decision_memo()
        state, during = _carried_pages(root)
    assert state == "available" and manifest not in during, "the outage must withhold the page"

    egress.clear_decision_memo()
    state, after = _carried_pages(root)

    assert state == "available"
    assert manifest in after, "a refusal decided during the outage outlived it"


def _enrol_store_collections(root: Path, count: int) -> list[str]:
    """More collections the store owns; each places its own manifest page in the catalogue."""
    pages = [manifest_path()]
    for index in range(count):
        path = DAILY_PATH.replace("Daily", f"Extra{index}")
        text = DAILY_TEXT.replace(DAILY, f"7777777{index}-7777-4777-8777-777777777777").replace("Daily", f"Extra{index}")
        call(root, "record_memory", action="create", manifest_path=path, manifest_text=text, why="more store pages")
        pages.append(path)
    return pages


@pytest.fixture
def store_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The arguments of every request that reaches the collection-store thread."""
    from exomem.collection_store import runtime

    requests: list[dict] = []
    real = runtime.StoreServer.call

    def counting(self, work, **kwargs):
        requests.append(kwargs)
        return real(self, work, **kwargs)

    monkeypatch.setattr(runtime.StoreServer, "call", counting)
    return requests


def _per_page_verdicts(root: Path, pages: list[str], who: RequestPrincipal | None = None) -> dict[str, bool]:
    """What the caller's release filter says for each page asked on its own, outside any publication."""
    egress.clear_decision_memo()
    with request_scope(who or _external()), state_paths.resolution_scope():
        keep = egress.release_walk_filter(root)
        return {page: keep(page) for page in pages}


def test_a_batched_publication_gives_each_page_the_verdict_it_has_on_its_own(service) -> None:  # noqa: F811
    """The pages decided together must not be released where the page's own decision withholds them."""
    root = service.root
    pages = _enrol_store_collections(root, 4)
    write_scope(root, paths="Records/Extra0/**", default_deny=True)
    write_rule(root, ceiling=0)
    lexstore.ensure_fresh(root)
    expected = _per_page_verdicts(root, pages)
    assert sorted(expected.values()) == [False, True, True, True, True]

    egress.clear_decision_memo()
    state, admitted = _carried_pages(root)

    assert state == "available"
    assert {page: page in admitted for page in pages} == expected


def test_a_caller_that_expects_other_bytes_than_the_page_has_is_refused_as_on_its_own(
    service, store_requests: list[dict]  # noqa: F811
) -> None:
    """A hit indexed from older bytes must not take the verdict decided for the page as it is now."""
    from types import SimpleNamespace

    root = service.root
    pages = _enrol_store_collections(root, 1)
    write_scope(root, paths="Unrelated/**")
    write_rule(root, ceiling=egress.LEVEL_FULL)
    lexstore.ensure_fresh(root)
    current = {page: hashlib.sha256((root / page).read_bytes()).hexdigest() for page in pages}
    stale, fresh = pages[0], pages[1]

    def released(*, together: bool) -> list[str]:
        hits = [SimpleNamespace(path=stale, snapshot_hash="0" * 64), SimpleNamespace(path=fresh, snapshot_hash=current[fresh])]
        egress.clear_decision_memo()
        with request_scope(OWNER), state_paths.resolution_scope():
            with deciding(pages) if together else nullcontext():
                return [hit.path for hit in egress.annotate_hits(root, hits).hits]

    assert released(together=False) == [fresh]
    store_requests.clear()
    assert released(together=True) == [fresh]
    assert len(store_requests) <= 2, "the stale page is decided again on its own, the rest together"


def test_a_page_the_store_refuses_stays_withheld_while_the_others_are_answered(
    service, store_requests: list[dict], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from exomem.cli_ops import OpError

    root = service.root
    pages = _enrol_store_collections(root, 2)
    write_scope(root, paths="Unrelated/**")
    write_rule(root, ceiling=egress.LEVEL_FULL)
    lexstore.ensure_fresh(root)
    refused = pages[1]
    real = egress.projection_decision

    def refusing(vault_root, rel_path, *args, **kwargs):
        if rel_path == refused:
            raise OpError("COLLECTION_STORE_UNAVAILABLE", "the store cannot place this page")
        return real(vault_root, rel_path, *args, **kwargs)

    monkeypatch.setattr(egress, "projection_decision", refusing)
    store_requests.clear()
    expected = _per_page_verdicts(root, pages)
    assert expected == {pages[0]: True, refused: False, pages[2]: True}

    store_requests.clear()
    egress.clear_decision_memo()
    state, admitted = _carried_pages(root)

    assert state == "available"
    assert {page: page in admitted for page in pages} == expected
    assert len(store_requests) == 1


def test_a_batched_answer_is_never_given_to_another_caller(service) -> None:  # noqa: F811
    """A verdict decided for one audience must not answer the next audience's question for the same page."""
    root = service.root
    pages = _enrol_store_collections(root, 1)
    write_scope(root, paths="Records/**", default_deny=True)
    write_rule(root, ceiling=egress.LEVEL_FULL)
    lexstore.ensure_fresh(root)
    egress.clear_decision_memo()
    stranger = RequestPrincipal(audience_id="stranger", surface="mcp")

    with deciding(pages):
        with request_scope(_external()), state_paths.resolution_scope():
            assert egress.release_walk_filter(root)(pages[0])
        with request_scope(stranger), state_paths.resolution_scope():
            assert not egress.release_walk_filter(root)(pages[1])


# --------------------------------------------------------------------------
# The owner under no file policy: nothing names its verdicts, the store still owns pages
# --------------------------------------------------------------------------


def test_a_page_the_store_refuses_stays_withheld_from_the_owner_while_the_others_are_answered(
    service, store_requests: list[dict], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from exomem.cli_ops import OpError

    root = service.root
    pages = _enrol_store_collections(root, 2)
    lexstore.ensure_fresh(root)
    refused = pages[1]
    real = egress.projection_decision

    def refusing(vault_root, rel_path, *args, **kwargs):
        if rel_path == refused:
            raise OpError("COLLECTION_STORE_UNAVAILABLE", "the store cannot place this page")
        return real(vault_root, rel_path, *args, **kwargs)

    monkeypatch.setattr(egress, "projection_decision", refusing)
    expected = _per_page_verdicts(root, pages, OWNER)
    assert expected == {pages[0]: True, refused: False, pages[2]: True}

    store_requests.clear()
    egress.clear_decision_memo()
    state, admitted = _carried_pages(root, OWNER)

    assert state == "available"
    assert {page: page in admitted for page in pages} == expected
    assert len(store_requests) == 1
