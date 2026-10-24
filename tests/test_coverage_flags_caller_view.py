"""A coverage flag follows what the caller may see, not the whole vault.

A flag that reports a derived structure as stale, lagging or incomplete flips on
every write. For a caller who may not see the written page, that flip is a
one-bit signal that a page it cannot see changed. These tests pin the rule: a
restricted caller's flag moves only when a path that changed since the
structure's published generation is one that caller may see. The owner keeps
the vault-wide flag.
"""

from __future__ import annotations

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
def activation(vault: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A managed runtime whose activation catalogue was built at the current stamp.

    A scheduled build never runs, so the catalogue stays behind until a test
    publishes it: the request reports what it can prove about that lag.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    hidden = vault / HIDDEN_HUB
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(HUB_BODY.format("First."), encoding="utf-8")
    write_scope(vault)
    write_rule(vault, ceiling=0)
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


def test_a_withheld_write_leaves_the_restricted_lexical_flag_as_it_was(
    activation: SimpleNamespace,
) -> None:
    """The lexical catalogue lags the registry here; only the flag is caller-relative."""
    before = _lexical_flag(activation.vault)
    assert before == "available"

    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _lexical_flag(activation.vault) == before


def test_a_visible_write_marks_the_restricted_lexical_flag_stale(
    activation: SimpleNamespace,
) -> None:
    _lexical_flag(activation.vault)
    page = activation.vault / "Knowledge Base/Products/Cargo Sled.md"
    _write_as_the_service_does(
        activation.vault, "Knowledge Base/Products/Cargo Sled.md",
        page.read_text(encoding="utf-8") + "\nAn added line.\n",
    )

    assert _lexical_flag(activation.vault) == "stale"


def test_a_withheld_write_marks_the_owner_lexical_flag_stale(
    activation: SimpleNamespace,
) -> None:
    _lexical_flag(activation.vault, restricted=False)
    _write_as_the_service_does(activation.vault, HIDDEN_HUB, HUB_BODY.format("Second."))

    assert _lexical_flag(activation.vault, restricted=False) == "stale"


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
