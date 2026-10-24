"""A coverage flag follows what the caller may see, not the whole vault.

A flag that reports a derived structure as stale, lagging or incomplete flips on
every write. For a caller who may not see the written page, that flip is a
one-bit signal that a page it cannot see changed. These tests pin the rule: a
restricted caller's flag moves only when a path that changed since the
structure's published generation is one that caller may see. The owner keeps
the vault-wide flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_activate_context_request_path import TURN, _seed_live_recall
from test_governance_egress import _external, write_rule, write_scope
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    context_catalogue,
    context_pack,
    context_views,
    freshness,
    lexstore,
    readiness,
    view_lag,
    working_set,
    working_set_index,
    working_set_runtime,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope
from exomem.published_admission import admission_of

ANCHOR = "Knowledge Base/Notes/Insights/coverage-anchor.md"
OUTLINKED = "Knowledge Base/Notes/Insights/coverage-outlinked.md"
BACKLINKER = "Knowledge Base/Notes/Insights/coverage-backlinker.md"
NEWCOMER = "Knowledge Base/Notes/Insights/coverage-newcomer.md"
HIDDEN = "Knowledge Base/Notes/Patterns/coverage-hidden.md"
ANCHOR_BODY = "# Anchor\n\nAnchor lede. See [[coverage-outlinked]].\n"


def _write_as_the_service_does(vault: Path, rel: str, body: str) -> None:
    """A committed service write: the file lands, then freshness announces it."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    freshness.on_files_changed(vault, changed=[path])


def _pages(vault: Path) -> None:
    for rel, body in (
        (ANCHOR, ANCHOR_BODY),
        (OUTLINKED, "# Outlinked\n\nOutlinked lede.\n"),
        (BACKLINKER, "# Backlinker\n\nBacklinker lede. See [[coverage-anchor]].\n"),
        (HIDDEN, "# Hidden\n\nHidden lede. See [[coverage-anchor]].\n"),
    ):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


@pytest.fixture
def served(vault: Path) -> Path:
    """A governed vault in a server process: the inbound index and the resolver are warm."""
    _pages(vault)
    write_scope(vault)
    write_rule(vault, ceiling=0)
    find_module.clear_cache()
    vault_module.clear_inbound_index()
    _seed_live_recall(vault)
    lexstore.ensure_fresh(vault)
    vault_module.register_inbound_index(vault)
    assert vault_module.prime_inbound_index(vault, eager=True)
    find_module._get_query_resolver(vault)
    return vault


def _caller(restricted: bool):
    """The request scope of the restricted external reader, or of the owner."""
    return request_scope(_external() if restricted else owner_principal())


def _links(vault: Path, *, restricted: bool):
    """The anchor's `links` field, as `get(links=True)` returns it."""
    with _caller(restricted):
        return commands.op_get(vault, path=ANCHOR, links=True)["links"]


def _pack_truncation(vault: Path, *, restricted: bool) -> tuple[list[str], list[str]]:
    """The anchor's neighbours, and each side the pack reports as unknown."""
    page = find_module._CACHE.get(vault / ANCHOR, vault)
    assert page is not None
    with _caller(restricted):
        neighbours, _dropped, incomplete = context_pack._neighborhood(vault, [page], 10)
    return sorted(item["path"] for item in neighbours), sorted(incomplete)


BOTH_SIDES_UNKNOWN = sorted(
    [context_pack.INBOUND_INCOMPLETE_TRUNCATION, context_pack.OUTBOUND_INCOMPLETE_TRUNCATION]
)


# ---- direct read and context pack ----------------------------------------


def test_a_withheld_write_leaves_the_restricted_inbound_field_as_it_was(served: Path) -> None:
    before = _links(served, restricted=True)
    assert [item["path"] for item in before["inbound"]] == [BACKLINKER]

    _write_as_the_service_does(served, HIDDEN, "# Hidden\n\nRewritten. See [[coverage-anchor]].\n")

    assert _links(served, restricted=True) == before


def test_a_withheld_write_leaves_the_restricted_pack_complete(served: Path) -> None:
    before = _pack_truncation(served, restricted=True)
    assert before == ([BACKLINKER, OUTLINKED], [])

    _write_as_the_service_does(served, HIDDEN, "# Hidden\n\nRewritten. See [[coverage-anchor]].\n")

    assert _pack_truncation(served, restricted=True) == before


def test_a_visible_write_is_reported_to_the_restricted_reader(served: Path) -> None:
    _write_as_the_service_does(served, NEWCOMER, "# Newcomer\n\nSee [[coverage-anchor]].\n")

    assert _links(served, restricted=True)["inbound"] is None
    assert _pack_truncation(served, restricted=True) == ([], BOTH_SIDES_UNKNOWN)


def test_the_owner_still_sees_every_write_as_a_lag(served: Path) -> None:
    _write_as_the_service_does(served, HIDDEN, "# Hidden\n\nRewritten. See [[coverage-anchor]].\n")

    assert _links(served, restricted=False)["inbound"] is None
    assert _pack_truncation(served, restricted=False) == ([], BOTH_SIDES_UNKNOWN)


def test_a_lag_history_cannot_bridge_is_reported_to_the_restricted_reader(served: Path) -> None:
    """A reconcile that replaced the registry leaves nothing to say what changed."""
    _write_as_the_service_does(served, HIDDEN, "# Hidden\n\nRewritten.\n")
    freshness.seed(
        served, "vault", [(str(p), freshness.stat_signature(p)) for p in served.rglob("*.md")]
    )

    assert _links(served, restricted=True)["inbound"] is None
    assert context_pack.INBOUND_INCOMPLETE_TRUNCATION in _pack_truncation(served, restricted=True)[1]


def test_a_lag_longer_than_the_bound_is_reported_without_a_decision(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the bound the request reports the lag and decides no path of it."""
    decided: list[str] = []
    real = egress.visible_page_filter

    def counting(root: Path, **kwargs):
        decide = real(root, **kwargs)
        if decide is None:
            return None

        def visible(rel: str) -> bool:
            decided.append(rel)
            return decide(rel)

        return visible

    monkeypatch.setattr(egress, "visible_page_filter", counting)
    for number in range(view_lag.MAX_DECIDED_PATHS + 1):
        _write_as_the_service_does(
            served, f"Knowledge Base/Notes/Patterns/coverage-burst-{number}.md", "# Burst\n"
        )

    assert _links(served, restricted=True)["inbound"] is None
    assert not [rel for rel in decided if "coverage-burst" in rel]


# ---- the bound -----------------------------------------------------------


def _delta(*changed: str, deleted: tuple[str, ...] = (), complete: bool = True):
    return SimpleNamespace(
        complete=complete, changed=changed, deleted=deleted, requires_source_proof=False
    )


def test_decisions_follow_the_lag_and_stop_at_the_first_visible_path(tmp_path: Path) -> None:
    decided: list[str] = []

    def visible(rel: str) -> bool:
        decided.append(rel)
        return rel.endswith("shown.md")

    withheld = [str(tmp_path / f"hidden-{n}.md") for n in range(3)]

    assert view_lag.only_withheld(tmp_path, _delta(*withheld), visible)
    assert sorted(decided) == [f"hidden-{n}.md" for n in range(3)]

    decided.clear()
    assert not view_lag.only_withheld(tmp_path, _delta(*withheld, str(tmp_path / "shown.md")), visible)
    assert len(decided) <= 4

    decided.clear()
    too_many = [str(tmp_path / f"hidden-{n}.md") for n in range(view_lag.MAX_DECIDED_PATHS + 1)]
    assert not view_lag.only_withheld(tmp_path, _delta(*too_many), visible)
    assert decided == []


def test_a_delta_that_names_a_path_outside_the_vault_is_a_visible_lag(tmp_path: Path) -> None:
    assert not view_lag.only_withheld(tmp_path, _delta("/elsewhere/page.md"), lambda _rel: False)


# ---- activation ----------------------------------------------------------

HIDDEN_HUB = "Knowledge Base/Notes/Patterns/coverage-hidden-hub.md"
HUB_BODY = "---\ntype: note\ntags: [hub]\nstatus: active\n---\n\n# COVERAGEHUB\n\n{}\n"


def _stamp(vault: Path) -> str:
    return working_set_runtime._key_text(
        find_module.FreshnessSnapshot(vault).projection_key("kb")
    )


@pytest.fixture
def withheld_path(request: pytest.FixtureRequest) -> str | None:
    """A page, besides the hidden hub, that the external reader may not see (`indirect` parameter)."""
    return getattr(request, "param", None)


RESTRICTED_TAG = "coverage-restricted"


@pytest.fixture
def withheld_tag(request: pytest.FixtureRequest) -> str | None:
    """A tag that withholds the pages carrying it from the external reader (`indirect` parameter).

    The scope exists from the start, so tagging a page later moves no policy.
    """
    return getattr(request, "param", None)


def _withhold_from_external(vault: Path, selector: str) -> None:
    scope, rule = "01ARZ3NDEKTSV4RRFFQ69G5FB2", "01ARZ3NDEKTSV4RRFFQ69G5FB3"
    governance = vault / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes/extra.yaml").write_text(
        f"governance_version: 1\nid: {scope}\nname: Extra\n{selector}\n", encoding="utf-8"
    )
    (governance / "rules/extra-external.yaml").write_text(
        f'governance_version: 1\nid: {rule}\nscope_ids: ["{scope}"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


@pytest.fixture
def activation(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    withheld_path: str | None,
    withheld_tag: str | None,
) -> SimpleNamespace:
    """A managed runtime whose activation catalogue was built at the current stamp.

    A scheduled build or lexical repair never runs, so each catalogue stays behind
    until a test publishes it: the request reports what it can prove about that lag.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    hidden = vault / HIDDEN_HUB
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(HUB_BODY.format("First."), encoding="utf-8")
    write_scope(vault)
    write_rule(vault, ceiling=0)
    if withheld_path is not None:
        _withhold_from_external(vault, f'paths: ["{withheld_path}"]')
    if withheld_tag is not None:
        _withhold_from_external(vault, f'tags: ["{withheld_tag}"]')
    _seed_live_recall(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    working_set_index.WorkingSetIndex(vault).update(freshness_stamp=_stamp(vault))
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    scheduled: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "_schedule_build",
        lambda root, freshness_stamp="": scheduled.append(freshness_stamp),
    )
    # A real repair worker would converge the catalogue mid-test, so a lag would not stay where the test put it.
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: False)
    return SimpleNamespace(vault=vault, scheduled=scheduled)


def _activate(vault: Path, *, restricted: bool = True) -> dict:
    with _caller(restricted):
        packet = commands.op_activate_context(vault, turn=TURN)
    packet.pop("continuity", None)  # a fresh random thread, not coverage
    return packet


def _write_hidden_hub(vault: Path, text: str) -> None:
    _write_as_the_service_does(vault, HIDDEN_HUB, HUB_BODY.format(text))
    lexstore.ensure_fresh(vault)


def test_a_withheld_write_leaves_the_restricted_activation_packet_as_it_was(
    activation: SimpleNamespace,
) -> None:
    before = _activate(activation.vault)
    assert before["abstained"] is False, before.get("abstention")
    assert "index_stale" not in before["generation"]

    _write_hidden_hub(activation.vault, "Second.")

    assert _activate(activation.vault) == before
    assert activation.scheduled, "the catalogue was behind, so a build was scheduled"


def _units_by_role(packet: dict) -> set[str]:
    return {unit["role"] for unit in packet["units"]}


def test_a_withheld_write_leaves_the_restricted_packet_whole_while_the_lexical_catalogue_lags(
    activation: SimpleNamespace,
) -> None:
    """The lexical catalogue is not refreshed after the write, as it is until the watcher folds it in."""
    before = _activate(activation.vault)
    assert before["generation"]["correction_context"] == "available"
    assert "constraints" in _units_by_role(before)

    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _activate(activation.vault) == before


def test_a_visible_write_is_reported_by_the_restricted_packet_while_the_lexical_catalogue_lags(
    activation: SimpleNamespace,
) -> None:
    _activate(activation.vault)
    _append_to_sled(activation.vault)

    after = _activate(activation.vault)

    assert after["generation"]["lexical_evidence"] == "stale"
    assert {"role": "constraints", "reason": "lane_failed"} in after["missing"]


def test_a_withheld_write_is_reported_by_the_owner_packet_while_the_lexical_catalogue_lags(
    activation: SimpleNamespace,
) -> None:
    _activate(activation.vault, restricted=False)
    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    after = _activate(activation.vault, restricted=False)

    assert after["generation"]["lexical_evidence"] == "stale"
    assert {"role": "constraints", "reason": "lane_failed"} in after["missing"]


def test_a_visible_write_marks_the_restricted_activation_packet_stale(
    activation: SimpleNamespace,
) -> None:
    _activate(activation.vault)
    page = activation.vault / "Knowledge Base/Products/Cargo Sled.md"
    _write_as_the_service_does(
        activation.vault, "Knowledge Base/Products/Cargo Sled.md",
        page.read_text(encoding="utf-8") + "\nAn added line.\n",
    )
    lexstore.ensure_fresh(activation.vault)

    assert _activate(activation.vault)["generation"]["index_stale"] is True


def test_a_withheld_write_marks_the_owner_activation_packet_stale(
    activation: SimpleNamespace,
) -> None:
    _activate(activation.vault, restricted=False)
    _write_hidden_hub(activation.vault, "Second.")

    assert _activate(activation.vault, restricted=False)["generation"]["index_stale"] is True


def _lexical_flag(vault: Path, *, restricted: bool = True) -> str:
    return _activate(vault, restricted=restricted)["generation"]["lexical_evidence"]


SLED = "Knowledge Base/Products/Cargo Sled.md"


def _request_snapshot(vault: Path):
    """The snapshot of a restricted request, with its lexical checkpoint decided as the request decides it."""
    snapshot = find_module.FreshnessSnapshot(vault)
    working_set_runtime.pin_lexical_checkpoint(vault, snapshot, egress.visible_page_filter(vault))
    return snapshot


def _lexical(vault: Path, *, restricted: bool = True) -> tuple[list[str], str]:
    """The activation lexical lane as the request runs it: `(hit paths, state)`."""
    with _caller(restricted):
        checkpoint = _request_snapshot(vault).lexical_checkpoint("kb")
        hits, state = working_set_runtime.lexical_evidence(
            vault, TURN, working_set_index.WorkingSetIndex(vault).anchors(),
            limit=10, freshness=checkpoint.triple, recall_checkpoint=checkpoint,
        )
    return [hit.path for hit in hits], state


def _append_to_sled(vault: Path, line: str = "An added line.") -> None:
    page = vault / SLED
    _write_as_the_service_does(vault, SLED, page.read_text(encoding="utf-8") + f"\n{line}\n")


def test_a_withheld_write_leaves_the_restricted_lexical_lane_as_it_was(
    activation: SimpleNamespace,
) -> None:
    """The lexical catalogue lags the registry here; the lane is served from it all the same."""
    assert _lexical_flag(activation.vault) == "available"
    before = _lexical(activation.vault)
    assert before == ([SLED], "available")

    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _lexical(activation.vault) == before
    assert _lexical_flag(activation.vault) == "available"


def test_a_visible_write_serves_no_lexical_hits_to_the_restricted_reader(
    activation: SimpleNamespace,
) -> None:
    _append_to_sled(activation.vault)

    assert _lexical(activation.vault) == ([], "stale")
    assert _lexical_flag(activation.vault) == "stale"


def test_a_withheld_write_serves_no_lexical_hits_to_the_owner(
    activation: SimpleNamespace,
) -> None:
    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _lexical(activation.vault, restricted=False) == ([], "stale")
    assert _lexical_flag(activation.vault, restricted=False) == "stale"


def test_the_dependence_check_reads_the_catalogue_the_lexical_lane_was_served(
    activation: SimpleNamespace,
) -> None:
    """Which pages the turn still reaches without a word; the same catalogue, the same reader."""
    def reached() -> frozenset[str]:
        with _caller(True):
            return working_set._lexical_hits_without(
                activation.vault, TURN, frozenset({SLED}), frozenset({"north"}),
                freshness_snapshot=_request_snapshot(activation.vault),
            )

    before = reached()
    assert before == {SLED}

    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert reached() == before


@pytest.mark.parametrize("withheld_tag", [RESTRICTED_TAG], indirect=True)
def test_a_page_retagged_as_withheld_is_not_released_from_the_lagging_catalogue(
    activation: SimpleNamespace,
) -> None:
    """The lagging catalogue still ranks the page by its old text; the release decision, made
    on the live file for each request, drops it."""
    assert SLED in json.dumps(_activate(activation.vault))
    page = activation.vault / SLED
    _write_as_the_service_does(
        activation.vault, SLED,
        page.read_text(encoding="utf-8").replace("type: note\n", f"type: note\ntags: [{RESTRICTED_TAG}]\n"),
    )

    with _caller(True):
        assert not egress.visible_page_filter(activation.vault)(SLED)
    assert SLED not in json.dumps(_activate(activation.vault))


VISIBLE_HUB = "Knowledge Base/Notes/Insights/northern-corridor-hub.md"
TAGGED = "Knowledge Base/Notes/Insights/coverage-tagged.md"
TAGGED_BODY = "---\ntype: note\ntags: [{tags}]\nstatus: active\n---\n\n# Tagged\n\nTagged lede.\n"


def _delete_as_the_service_does(vault: Path, rel: str) -> None:
    path = vault / rel
    path.unlink()
    freshness.on_files_changed(vault, deleted=[path])


def _hold_verdicts(vault: Path) -> frozenset[str]:
    """What a search of the restricted reader leaves in the catalogue: a verdict per page."""
    with _caller(True):
        admission = admission_of(egress.visible_page_filter(vault))
        assert admission is not None
        store = lexstore.get_store(vault)
        held = store.published_admitted_paths(
            "kb", store.published_recall_checkpoint("kb"), admission,
            find_module.FreshnessSnapshot(vault).recall_paths("kb"),
        )
    assert held is not None
    return held


def test_deleting_a_page_held_as_withheld_leaves_the_restricted_packet_as_it_was(
    activation: SimpleNamespace,
) -> None:
    before = _activate(activation.vault)

    _delete_as_the_service_does(activation.vault, HIDDEN_HUB)

    assert _activate(activation.vault) == before


def test_deleting_a_withheld_page_with_no_held_verdict_is_reported_to_the_restricted_reader(
    activation: SimpleNamespace,
) -> None:
    _delete_as_the_service_does(activation.vault, HIDDEN_HUB)

    assert _activate(activation.vault)["generation"]["lexical_evidence"] == "stale"


def test_deleting_a_visible_page_is_reported_to_the_restricted_reader(
    activation: SimpleNamespace,
) -> None:
    _activate(activation.vault)

    _delete_as_the_service_does(activation.vault, VISIBLE_HUB)

    assert _activate(activation.vault)["generation"]["lexical_evidence"] == "stale"


@pytest.mark.parametrize("withheld_tag", [RESTRICTED_TAG], indirect=True)
def test_a_verdict_held_before_the_page_became_visible_does_not_hide_its_deletion(
    activation: SimpleNamespace,
) -> None:
    """The catalogue holds the page as visible by now; the earlier verdict describes other text."""
    _write_as_the_service_does(
        activation.vault, TAGGED, TAGGED_BODY.format(tags=RESTRICTED_TAG)
    )
    lexstore.ensure_fresh(activation.vault)
    assert TAGGED not in _hold_verdicts(activation.vault)
    _write_as_the_service_does(activation.vault, TAGGED, TAGGED_BODY.format(tags="open"))
    lexstore.ensure_fresh(activation.vault)

    _delete_as_the_service_does(activation.vault, TAGGED)

    assert _activate(activation.vault)["generation"]["lexical_evidence"] == "stale"


def test_a_lag_with_no_remembered_stamp_is_reported_to_the_restricted_reader(
    activation: SimpleNamespace,
) -> None:
    """After a restart no stamp has a checkpoint, so what changed since is unknown."""
    _activate(activation.vault)
    _write_hidden_hub(activation.vault, "Second.")
    working_set_runtime._STAMP_CHECKPOINTS.clear()

    assert _activate(activation.vault)["generation"]["index_stale"] is True


def test_a_withheld_publication_during_the_request_does_not_withdraw_the_packet(
    activation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another publisher moves the catalogue by a withheld page after readiness."""
    before = _activate(activation.vault)
    published: list[bool] = []
    real_compile = working_set.compile_packet

    def compile_then_publish(*args, **kwargs):
        packet = real_compile(*args, **kwargs)
        if not published:
            _write_hidden_hub(activation.vault, "Second.")
            working_set_index.WorkingSetIndex(activation.vault).update(
                freshness_stamp=_stamp(activation.vault)
            )
            published.append(True)
        return packet

    monkeypatch.setattr(working_set, "compile_packet", compile_then_publish)
    token = working_set_index.WorkingSetIndex(activation.vault).token()

    served = _activate(activation.vault)

    assert published == [True]
    assert working_set_index.WorkingSetIndex(activation.vault).token() != token
    assert served == before


def test_a_visible_publication_during_the_request_still_withdraws_the_packet(
    activation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate(activation.vault)
    published: list[bool] = []
    real_compile = working_set.compile_packet
    page = activation.vault / "Knowledge Base/Products/Cargo Sled.md"

    def compile_then_publish(*args, **kwargs):
        packet = real_compile(*args, **kwargs)
        if not published:
            _write_as_the_service_does(
                activation.vault, "Knowledge Base/Products/Cargo Sled.md",
                page.read_text(encoding="utf-8").replace("400 kg", "275 kg"),
            )
            lexstore.ensure_fresh(activation.vault)
            working_set_index.WorkingSetIndex(activation.vault).update(
                freshness_stamp=_stamp(activation.vault)
            )
            published.append(True)
        return packet

    monkeypatch.setattr(working_set, "compile_packet", compile_then_publish)

    served = _activate(activation.vault)

    assert published == [True]
    assert served.get("abstention") == {"reason": "unavailable"}


# ---- activation map ------------------------------------------------------


@pytest.fixture
def mapped(activation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The activation fixture, plus a context catalogue folded at the current stamp.

    The catalogue's own fold is warm, so the only lag a hidden write leaves is
    the activation index's: `unlisted` follows that lag alone.
    """
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    stamp = _stamp(activation.vault)
    catalogue = context_catalogue.ContextCatalogue(activation.vault)
    catalogue.rebuild()
    catalogue.update(freshness_stamp=stamp)
    catalogue.close()
    context_catalogue._CHECKPOINTS[activation.vault.absolute()] = freshness.consumer_checkpoint(
        activation.vault, "kb"
    )
    yield activation
    context_catalogue._CHECKPOINTS.clear()


def _map_activation(vault: Path, *, restricted: bool = True) -> dict:
    with _caller(restricted):
        return commands.op_activate_context(vault, turn="")


def test_a_withheld_write_leaves_the_restricted_map_served(mapped: SimpleNamespace) -> None:
    before = _map_activation(mapped.vault)
    assert before["abstained"] is False, before.get("abstention")

    _write_as_the_service_does(mapped.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    after = _map_activation(mapped.vault)
    assert after["abstained"] is False
    assert after["map"]["items"] == before["map"]["items"]


def test_a_visible_write_holds_the_restricted_map_back_as_warming(mapped: SimpleNamespace) -> None:
    _map_activation(mapped.vault)
    page = mapped.vault / "Knowledge Base/Products/Cargo Sled.md"
    _write_as_the_service_does(
        mapped.vault, "Knowledge Base/Products/Cargo Sled.md",
        page.read_text(encoding="utf-8") + "\nAn added line.\n",
    )

    assert _map_activation(mapped.vault)["abstention"] == {"reason": "index_warming"}


def test_a_withheld_write_marks_the_owner_map_stale(mapped: SimpleNamespace) -> None:
    _map_activation(mapped.vault, restricted=False)
    _write_as_the_service_does(mapped.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _map_activation(mapped.vault, restricted=False)["map"]["stale"] is True


def test_deleting_a_visible_page_holds_the_restricted_map_back_as_warming(
    mapped: SimpleNamespace,
) -> None:
    _map_activation(mapped.vault)

    _delete_as_the_service_does(mapped.vault, VISIBLE_HUB)

    assert _map_activation(mapped.vault)["abstention"] == {"reason": "index_warming"}


def test_deleting_a_page_held_as_withheld_leaves_the_restricted_map_served(
    mapped: SimpleNamespace,
) -> None:
    before = _map_activation(mapped.vault)
    assert HIDDEN_HUB not in _hold_verdicts(mapped.vault)

    _delete_as_the_service_does(mapped.vault, HIDDEN_HUB)

    after = _map_activation(mapped.vault)
    assert after["abstained"] is False
    assert after["map"]["items"] == before["map"]["items"]


REGISTRY = "Knowledge Base/_Schema/project-keys.yaml"
EDIT = "  maritime-route:\n    folder: Maritime Route\n    category: logistics\n"


def _edit_registry(vault: Path) -> None:
    registry = vault / REGISTRY
    _write_as_the_service_does(vault, REGISTRY, registry.read_text(encoding="utf-8") + EDIT)


@pytest.mark.parametrize("withheld_path", [REGISTRY], indirect=True)
def test_a_withheld_registry_edit_leaves_the_restricted_map_served(mapped: SimpleNamespace) -> None:
    with _caller(True):
        assert not context_views.CallerView.current(mapped.vault).projects_admitted
    before = _map_activation(mapped.vault)
    assert before["abstained"] is False, before.get("abstention")

    _edit_registry(mapped.vault)

    after = _map_activation(mapped.vault)
    assert after["abstained"] is False
    assert after["map"]["items"] == before["map"]["items"]


def test_a_released_registry_edit_holds_the_restricted_map_back_as_warming(
    mapped: SimpleNamespace,
) -> None:
    with _caller(True):
        assert context_views.CallerView.current(mapped.vault).projects_admitted
    _map_activation(mapped.vault)

    _edit_registry(mapped.vault)

    assert _map_activation(mapped.vault)["abstention"] == {"reason": "index_warming"}
