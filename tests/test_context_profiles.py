"""Profiles read through the real command, with currency, sync and caller isolation."""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

import pytest
from conftest import _drain_background_threads
from test_context_catalogue import INSIGHTS, KB, _built, _page, _write
from test_context_catalogue_sync import _command, _settle
from test_context_views import (
    MOORING,
    SECRET,
    THREAD_W,
    TIDE,
    _ask,
    _cold,
    _frozen,
    _leave_pending,
    _outcome,
    _pages,
    _second_vault,
    _warm_catalogue,
    _world,
)
from test_governance_egress import _external, write_rule
from test_latency_gate import _seed_freshness_live
from test_working_set_withheld_twins import _withhold

from exomem import commands, freshness, lexstore, pending_recall, working_set
from exomem import context_catalogue as cc
from exomem import context_views as cv
from exomem.activation_result import INDEX_WARMING
from exomem.governance import egress, principal
from exomem.writer_lease import invoke_command

HUB = f"{INSIGHTS}/harbour-hub.md"


def _seed_profile(vault: Path, *, with_w: bool = True) -> None:
    _world(vault, with_w=with_w)
    _page(
        vault, "harbour-hub", "tags: [hub]\ntopics: [harbour]\nupdated: 2026-10-12\n",
        body="Harbour operations.\n\n- [decision] Reserve the inner quay (2026-10-10) ^reserve\n",
    )
    _page(
        vault, "mooring-lines", "topics: [harbour]\nupdated: 2026-08-01\n",
        body="- [decision] Use doubled lines (2026-07-31) ^lines\n",
    )
    _page(
        vault, "tide-work", "project: harbour-ops\nupdated: 2026-09-01\n",
        body="- [decision] Work at slack tide ^slack\n",
    )
    _built(vault)
    lexstore.ensure_fresh(vault)


def _items(pages: list[dict]) -> list[dict]:
    return [item for page in pages for item in page["items"]]


@pytest.mark.parametrize("mention", ["plain", "unit", "state"] )
def test_profiles_page_to_exhaustion_without_leaking_a_withheld_member_or_mention(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mention: str
) -> None:
    """The P3 twins stay identical through profiles, including their generations and signed continuations."""
    _frozen(monkeypatch, page_size=1)
    twin = _second_vault(tmp_path)
    refs = (cv.anchor_profile_ref(HUB), cv.topic_profile_ref("harbour"))
    for root, present in ((vault, True), (twin, False)):
        _seed_profile(root, with_w=present)
        if present:
            _write(root, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# secret-cluster\n\nHidden.\n")
        if mention == "unit":
            page = root / HUB
            page.write_text(page.read_text().replace("Reserve the inner quay", "Reserve the inner quay beside [[secret-cluster]]"))
            note = root / MOORING
            note.write_text(note.read_text().replace("Use doubled lines", "Use doubled lines beside [[secret-cluster]]"))
        if mention == "state":
            crane = _page(root, "quay-crane", "topics: [harbour]\nupdated: 2026-10-12\nstate: 'Parked beside [[secret-cluster]]'\n",
                          folder=f"{KB}/Products")
            _write(root, f"{KB}/_Schema/context-roles.yaml", "roles:\n  current_state:\n    anchor_defaults: [resource, collection, hub]\n")
            refs = (cv.anchor_profile_ref(crane), cv.topic_profile_ref("harbour"))
        _withhold(root, THREAD_W, SECRET, ceiling=0)
        _built(root)
        lexstore.ensure_fresh(root)
        _cold()
        owner = {ref: _pages(root, ref) for ref in refs}
        assert all(len(pages) > 1 for pages in owner.values())
    guests = []
    for root in (vault, twin):
        _cold()
        guests.append({ref: _pages(root, ref, _external()) for ref in refs})
    assert json.dumps(guests[0], sort_keys=True) == json.dumps(guests[1], sort_keys=True)
    for ref, pages in guests[0].items():
        encoded = json.dumps(pages, sort_keys=True)
        assert THREAD_W not in encoded and SECRET not in encoded
        assert "[[" not in encoded
        if mention == "unit":
            linked_text = "Reserve the inner quay beside" if ref == cv.anchor_profile_ref(HUB) else "Use doubled lines beside"
            assert linked_text in encoded
        if mention == "state":
            assert any(item["kind"] == "current_state" and item["statement"] == "state: Parked beside" for item in _items(pages))
            assert any(item["kind"] == "pointers" and item["title"] == "state: Parked beside" for item in _items(pages))
        assert all(len(page["items"]) <= 1 for page in pages)
        assert len(pages) > 1 and pages[0]["continuation"]
        assert len({(item["kind"], item.get("ref") or item.get("anchor")) for item in _items(pages)}) == len(_items(pages))
        print(json.dumps({"workflow": "restricted profile read", "ref": ref,
                          "pages": len(pages), "items": len(_items(pages)),
                          "withheld_path_present": THREAD_W in encoded or SECRET in encoded, "twins_equal": True, "mention": mention}))


def test_profile_units_keep_their_own_dates_supersession_and_whole_claims(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page's update date cannot date an undated unit or make an authored predecessor current."""
    _world(vault, with_w=False)
    _page(
        vault, "harbour-hub", "tags: [hub]\ntopics: [harbour]\nupdated: 2026-10-12\n",
        body="Harbour operations.\n\n"
        "- [decision] Reserve the inner quay ^old\n"
        "- [decision] Keep the tide budget fixed ^budget\n\n"
        "## Decision\n- id: outer\n- relations: supersedes: [[#old]]\n\n"
        "Reserve the outer quay instead.\n\n## Observations\n\n"
        f"- [decision] {'A whole qualification. ' * 50} ^long\n",
    )
    _built(vault)
    lexstore.ensure_fresh(vault)
    monkeypatch.setattr(cv, "VIEW_PAGE_SIZE", 1)
    items = _items(_pages(vault, cv.anchor_profile_ref(HUB)))
    units = {item["ref"].rsplit("#", 1)[-1]: item for item in items if item["kind"] == "units"}
    assert "outer" in units
    assert "old" not in units or units["old"]["lifecycle"] == "superseded"
    assert units["budget"]["updated"] == ""
    assert all(unit["updated"] != "2026-10-12" for unit in units.values())
    assert "long" not in units
    assert any(item["ref"].endswith("#long") and item["kind"] == "pointers" for item in items)


def test_topic_profile_reads_declared_notes_and_does_not_import_lexical_mentions(vault: Path) -> None:
    """Only the D3 membership join supplies the topic neighbourhood, even when a mention is newer."""
    _seed_profile(vault, with_w=False)
    _page(
        vault, "passing-mention", "updated: 2026-10-20\n",
        body="See [[harbour-hub]].\n\n- [decision] The unrelated ferry leaves at noon ^ferry\n",
    )
    _built(vault)
    lexstore.ensure_fresh(vault)
    items = _items(_pages(vault, cv.topic_profile_ref("harbour")))
    units = [item for item in items if item["kind"] == "units"]
    assert any(item["provenance"]["path"] == MOORING and item["updated"] == "2026-07-31" for item in units)
    assert any(item["provenance"]["path"] == TIDE and item["updated"] == "" for item in units)
    assert "passing-mention" not in json.dumps(items)
    # The shared ref contract also resolves deprecated topics and refuses page-only options.
    _write(
        vault, f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\ndomains:\n  harbour: {label: Harbour, path_label: Harbour}\n"
        "  old-harbour: {label: Old harbour, path_label: Old Harbour, status: deprecated, replaced_by: harbour}\n",
    )
    assert _ask(vault, cv.topic_profile_ref("old-harbour"))["ref"] == cv.topic_profile_ref("harbour")
    for option in ({"unit_ref": "u"}, {"frontmatter_only": True}, {"links": True},
                   {"include_raw": True}, {"include_history": True}):
        with pytest.raises(ValueError, match="INVALID_VIEW_READ_OPTIONS"):
            _ask(vault, cv.anchor_profile_ref(HUB), **option)
    assert _outcome(vault, cv.topic_profile_ref("missing")) == "NOT_FOUND: memory identifier is unavailable"

    # A topic with declared notes still has a profile when it has no eligible anchor.
    (vault / HUB).unlink()
    _write(vault, f"{KB}/_Schema/project-keys.yaml", "projects: {}\n")
    _page(vault, "tide-work", "topics: [harbour]\nupdated: 2026-09-01\n",
          body="- [decision] Work at slack tide ^slack\n")
    catalogue = _built(vault)
    joined = catalogue.memberships().topics
    assert not any(entry.kind in (cc.KIND_ANCHOR, cc.KIND_COLLECTION) and "harbour" in joined[entry.entry_id]
                   for entry in catalogue.entries())
    lexstore.ensure_fresh(vault)
    notes_only = _items(_pages(vault, cv.topic_profile_ref("harbour")))
    assert any(item["kind"] == "units" and item["provenance"]["path"] == MOORING for item in notes_only)
    assert any(item["kind"] == "units" and item["provenance"]["path"] == TIDE for item in notes_only)
    assert "passing-mention" not in json.dumps(notes_only)


def test_profile_marks_the_published_index_build_stale_until_the_member_is_folded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending member cannot silently replace the published profile with partially refreshed lanes."""
    _seed_profile(vault)
    ref = cv.topic_profile_ref("harbour")
    baseline = _ask(vault, ref)
    _warm_catalogue(monkeypatch)
    _leave_pending(vault, MOORING, monkeypatch)
    lexstore.ensure_fresh(vault)
    stale = _ask(vault, ref)
    assert stale == {**baseline, "stale": True}
    lexstore.ensure_fresh(vault)
    visible_pending = _ask(vault, ref, _external())
    assert visible_pending["state"] == "ready" and visible_pending["items"] and visible_pending["stale"] is True
    cc.ContextCatalogue(vault).update()
    lexstore.ensure_fresh(vault)
    refreshed = _ask(vault, ref)
    assert refreshed["state"] == "ready" and refreshed["items"] and refreshed["stale"] is False
    # A hidden pending write does not change the restricted caller's freshness byte.
    _withhold(vault, THREAD_W, ceiling=0)
    _cold()
    guest = _ask(vault, ref, _external())
    _leave_pending(vault, THREAD_W, monkeypatch)
    # Publish recall while the context catalogue stays pending; failed recall lanes warm separately.
    lexstore.ensure_fresh(vault)
    assert _ask(vault, ref, _external()) == guest
    _warm_catalogue(monkeypatch, unlisted=True)
    waiting = _ask(vault, ref, _external())
    assert (waiting["state"], waiting["items"], waiting["stale"]) == ("index_warming", [], False)


def test_a_build_started_before_a_write_never_replaces_the_newer_profile(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue token arbitrates publication, including a newer build published during old lane work."""
    _seed_profile(vault, with_w=False)
    ref = cv.topic_profile_ref("harbour")
    newer = []
    run = working_set.run_lanes

    def lanes_then_write(*args, **kwargs):
        old = run(*args, **kwargs)
        _page(vault, "mooring-lines", "topics: [harbour]\nupdated: 2026-10-21\n",
              body="- [decision] Use braided lines (2026-10-21) ^braided\n")
        cc.ContextCatalogue(vault).update([MOORING])
        lexstore.ensure_fresh(vault)
        with monkeypatch.context() as patch:
            patch.setattr(working_set, "run_lanes", run)
            newer.append(_ask(vault, ref))
        return old

    monkeypatch.setattr(working_set, "run_lanes", lanes_then_write)
    response = _ask(vault, ref)
    assert newer and response["state"] == INDEX_WARMING and not response["items"]
    with monkeypatch.context() as patch:
        patch.setattr(working_set, "run_lanes", run)
        current = _ask(vault, ref)
    assert current == newer[0]
    assert "Use braided lines" in json.dumps(current)
    assert "Use doubled lines" not in json.dumps(current)


@pytest.mark.parametrize("live", [True, False], ids=["event-maintained", "non-live-warming"])
def test_a_captured_decision_reaches_the_anchor_profile_after_derived_sync(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A completed catalogue receipt must invalidate a warmed anchor profile and retain the decision's own date."""
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    _seed_profile(vault, with_w=False)
    if live:
        _seed_freshness_live(vault)
        lexstore.ensure_fresh(vault)
    assert freshness.recall_is_live(vault, "kb") is live
    ref = cv.anchor_profile_ref(HUB)
    before = _ask(vault, ref)
    creation = {
        "title": "Harbour watch decision", "status": "active", "topics": ["harbour"],
        "content": "## Observations\n\n- [decision] Keep the night watch (2026-10-22) ^watch\n\n"
        f"## Relations\n\n- member_of [[{HUB.removesuffix('.md')}]]\n",
    }
    draft = commands.op_remember(vault, **creation, validate_only=True)
    batches = _command(
        tmp_path, vault, "remember", lambda root: commands.op_remember(
            root, **creation, draft_id=draft["draft_id"], draft_hash=draft["draft_hash"],
            draft_token=draft["draft_token"], relation_disposition="reviewed_none",
            relation_review_hash=draft["draft_hash"], relation_review_reason="The membership edge organizes this fixture; no epistemic relation applies.",
        ), 1,
    )
    assert batches
    _settle(vault, batches)
    if not live:
        caplog.set_level(logging.DEBUG, logger=working_set.__name__)
        # Hold the existing background repair until both callers observe the same pending state.
        store = lexstore.get_store(vault)
        repair = store.rebuild_atomic
        release = threading.Event()

        def held_repair():
            assert release.wait(20), "the warming reads did not release background repair"
            return repair()

        with monkeypatch.context() as patch:
            patch.setattr(store, "rebuild_atomic", held_repair)
            try:
                owner_waiting = _ask(vault, ref)
                guest_waiting = _ask(vault, ref, _external())
                assert owner_waiting == guest_waiting == {
                    "ref": ref, "state": INDEX_WARMING, "generation": None,
                    "stale": False, "items": [], "continuation": None,
                }
                failures = [record for record in caplog.records if record.name == cv.__name__ and "profile lane failed" in record.message]
                assert failures and any(record.args[-1] == "RetrievalIndexWarming" for record in failures)
                assert len(failures) == len({(record.args[0], record.args[1]) for record in failures})
                assert not any(record.name == working_set.__name__ and "activation lane" in record.message
                               for record in caplog.records)
            finally:
                release.set()
                _drain_background_threads()
    after = _ask(vault, ref)
    decisions = [item for item in after["items"] if item["kind"] == "units" and "night watch" in item["text"]]
    assert decisions and decisions[0]["updated"] == "2026-10-22", after
    assert after["generation"] != before["generation"] and after["stale"] is False


@pytest.mark.parametrize("kind", ["units", "pointers", "current_state"])
def test_the_profile_guard_drops_an_unpinned_item_naming_a_withheld_page(vault: Path, kind: str) -> None:
    """An invented line cannot gain authority from a real response's generation."""
    _seed_profile(vault)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# secret-cluster\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    _built(vault)
    lexstore.ensure_fresh(vault)
    _cold()
    ref = cv.topic_profile_ref("harbour")
    forged = {"kind": kind, "ref": "invented", "anchor": HUB, "text": "Forged prose",
              "provenance": {"path": SECRET, "level": "unit", "anchor": HUB}}
    with principal.request_scope(_external()):
        response = _ask(vault, ref, _external())
        guarded = egress.postfilter("read_memory", {**response, "items": [*response["items"], forged]}, vault,
                                   request_kwargs={"path": ref})
    assert guarded == response


def test_profile_pins_replace_forged_literals(vault: Path) -> None:
    """Literal secrets cannot replace pinned unit text, pointer titles or current-state fields."""
    _seed_profile(vault)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# secret-cluster\n\nHidden.\n")
    _page(vault, "quay-crane", "topics: [harbour]\nstate: parked\nupdated: 2026-10-12\n", folder=f"{KB}/Products")
    _built(vault)
    _withhold(vault, SECRET, ceiling=0)
    lexstore.ensure_fresh(vault)
    _cold()
    ref = cv.topic_profile_ref("harbour")
    with principal.request_scope(_external()):
        response = _ask(vault, ref, _external())
        assert set(cv.PROFILE_SECTIONS) <= {item["kind"] for item in response["items"]}
        forged = copy.deepcopy(response)
        for item in forged["items"]:
            item["text"] = SECRET
            item["title"] = "Secret literal"
            item["statement"] = SECRET
            item["source"] = SECRET
        assert egress.postfilter("read_memory", forged, vault, request_kwargs={"path": ref}) == response


def test_the_guard_strips_a_corrects_ref_to_a_withheld_unit(vault: Path) -> None:
    """A visible claim cannot disclose a withheld seed through correction provenance."""
    _seed_profile(vault)
    _write(vault, SECRET, "---\ntype: insight\nstatus: active\n---\n\n# secret-cluster\n\nHidden.\n")
    _withhold(vault, SECRET, ceiling=0)
    lexstore.ensure_fresh(vault)
    _cold()
    response = _ask(vault, cv.topic_profile_ref("harbour"), _external())
    with principal.request_scope(_external()):
        unit = next(item for item in response["items"] if item["kind"] == "units")
        corrected = {**unit, "provenance": {**unit["provenance"], "corrects": SECRET + "#private"}}
        guarded = egress.guard_working_set(vault, {"units": [corrected]}, egress.AnnotatedHits(hits=[]))
    assert guarded and guarded["units"]
    assert "corrects" not in guarded["units"][0]["provenance"]
    assert SECRET not in json.dumps(guarded)


def test_profile_compile_and_guard_keep_the_declared_purpose(vault: Path) -> None:
    """A unit withheld only for marketing cannot reach that purpose through either dispatch pass."""
    _seed_profile(vault, with_w=False)
    _withhold(vault, MOORING, ceiling=0)
    write_rule(vault, ceiling=0, extra="purpose: marketing\npurpose_condition: matches\n")
    lexstore.ensure_fresh(vault)
    _cold()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")
    ref = cv.topic_profile_ref("harbour")
    with principal.request_scope(_external()):
        unrestricted = invoke_command(command, vault, path=ref)
        marketing = invoke_command(command, vault, path=ref, purpose="marketing")
    assert MOORING in json.dumps(unrestricted)
    assert MOORING not in json.dumps(marketing)
    assert marketing["state"] == "ready" and marketing["items"]


def test_a_withheld_registry_never_resolves_a_profile_alias_or_moves_an_anchor_continuation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private taxonomy content gives no topic-ref or generation oracle, even during catalogue warmup."""
    _seed_profile(vault, with_w=False)
    taxonomy = f"{KB}/_Schema/source-taxonomy.yaml"
    path = vault / taxonomy
    path.write_text(path.read_text() + "  old-harbour: {label: Old harbour, path_label: Old Harbour, status: deprecated, replaced_by: harbour}\n")
    keys = f"{KB}/_Schema/project-keys.yaml"
    _withhold(vault, keys, ceiling=0)
    lexstore.ensure_fresh(vault)
    _cold()
    _frozen(monkeypatch, page_size=1)
    refs = (cv.anchor_profile_ref(HUB), cv.topic_profile_ref("harbour"))
    before = {ref: _ask(vault, ref, _external()) for ref in refs}
    assert all(page["continuation"] for page in before.values())
    key_file = vault / keys
    key_file.write_text(key_file.read_text() + "  extra-ops: {folder: Extra operations, category: domain, domain: fleet}\n")
    cc.ContextCatalogue(vault).update()
    lexstore.ensure_fresh(vault)
    _cold()
    for ref in refs:
        assert _ask(vault, ref, _external()) == before[ref]
        assert _ask(vault, ref, _external(), continuation=before[ref]["continuation"])["items"]
    _withhold(vault, taxonomy, ceiling=0)
    _cold()
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("a refused topic read touched registry definitions or catalogue readiness")

    for warming in (False, True):
        with monkeypatch.context() as patch:
            patch.setattr(cv, "_Registries", forbidden_lookup)
            if warming:
                patch.setattr(cc, "ensure_unlisted", forbidden_lookup)
            for key in ("harbour", "old-harbour", "missing"):
                assert _outcome(vault, cv.topic_profile_ref(key), _external()) == "NOT_FOUND: memory identifier is unavailable"
    ref = cv.anchor_profile_ref(HUB)
    first = _ask(vault, ref, _external())
    assert first["continuation"]
    second = _ask(vault, ref, _external(), continuation=first["continuation"])
    path.write_text(path.read_text().replace("Quays and moorings.", "A private registry edit."))
    _cold()
    lexstore.ensure_fresh(vault)
    assert _ask(vault, ref, _external()) == first
    assert _ask(vault, ref, _external(), continuation=first["continuation"]) == second


def test_profile_reports_lane_truncation_and_live_notice_removal(vault: Path) -> None:
    """A bounded lane and a later notice-level revocation remain visible instead of looking complete."""
    _seed_profile(vault, with_w=False)
    _page(vault, "mooring-lines", "topics: [harbour]\nupdated: 2026-10-12\n",
          body="\n".join(f"- [decision] Reserve berth {n} ^berth-{n}" for n in range(working_set.UNIT_LANE_LIMIT + 1)))
    _built(vault)
    lexstore.ensure_fresh(vault)
    _cold()
    ref = cv.topic_profile_ref("harbour")
    with principal.request_scope(_external()):
        response = _ask(vault, ref, _external())
        assert response["missing"] and all(entry["reason"] == "lane_truncated" for entry in response["missing"])
        # The revocation lands between the compile and the guard passes of one request.
        _withhold(vault, MOORING, ceiling=1)
        _cold()
        revoked = egress.postfilter("read_memory", response, vault, request_kwargs={"path": ref})
        assert egress.postfilter("read_memory", revoked, vault, request_kwargs={"path": ref}) == revoked
    assert {"role": "units", "reason": "withheld"} in revoked["missing"]
    assert all(entry["reason"] in ("lane_truncated", "withheld") for entry in revoked["missing"])
    assert all(item.get("provenance", {}).get("path") != MOORING for item in revoked["items"])
