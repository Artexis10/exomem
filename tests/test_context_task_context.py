"""Task discovery reaches evidence and revisions outside an anchor's neighborhood."""

from pathlib import Path

import pytest
from test_activate_context_maps import _activate, _without_response_id
from test_activation_log import _rows
from test_activation_log import activation_log as activation_log  # noqa: F401  (the pytest fixture)
from test_collection_store_service import service as service  # noqa: F401  (the pytest fixture)
from test_collection_store_writer import manifest_path
from test_context_catalogue import _built, _page, _source_schema, _write
from test_context_views import _second_vault
from test_governance_egress import _external, write_rule
from test_working_set_index import _seed_structure
from test_working_set_withheld_twins import _withhold

from exomem import commands, context_catalogue, freshness, lexstore, working_set_runtime
from exomem.governance import principal
from exomem.writer_lease import invoke_command


def test_public_reads_already_carry_methods_in_distinct_domains(vault: Path) -> None:
    """The public reader needs no new method document or domain-specific operation."""
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    for name, content in (
        ("optical-inspection", "- [technique] Inspect optical alignment against the reference target ^alignment\n"),
        ("seed-comparison", "- [technique] Compare seed germination in equal soil volumes ^germination\n"),
    ):
        path = _page(vault, name, body=content)
        with principal.request_scope(principal.owner_principal(surface="library")):
            result = invoke_command(command, vault, path=path)
        assert content.strip() in str(result)


def test_task_discovery_does_not_require_an_anchor(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh client finds an ordinary note and a reusable method without an anchor."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setattr(context_catalogue, "_managed", lambda: True)
    note = _page(vault, "alignment-visit", body="Optical alignment workshop: the calibration appointment remains undecided.\n")
    method = _page(vault, "reference-comparison", body="- [technique] Inspect optical alignment against the reference target ^alignment\n\nExclude handheld field checks.\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, turn="Inspect the optical alignment before the workshop", max_chars=8000)
    block = packet["task_context"]
    assert block["state"] in ("ready", "partial")
    assert {note, method} <= {item["source_ref"] for item in block["candidates"]}
    projected = next(item for item in block["methods"]["items"] if item["source_ref"] == method)
    assert projected["applicability"] == "read_required"
    assert projected["read_ref"] == method
    assert projected["source_version"]
    assert packet["anchors"] == []
    token = working_set_runtime.decode_continuity(packet["continuity"])
    assert token["refs"] == []
    assert {item["source_ref"] for item in token["method_versions"]} == {method}

    # Another client changes an exclusion outside the selected unit. The held map stays current.
    page = vault / method
    page.write_text(page.read_text().replace("Exclude handheld field checks.", "Exclude all optical alignment checks."))
    freshness.on_files_changed(vault, [page])
    refreshed = _activate(vault, turn="Compare seed germination", continuity=packet["continuity"], max_chars=8000)
    assert refreshed["map"].get("unchanged") is True
    assert refreshed["task_context"]["methods"]["reset"] is True
    assert refreshed["task_context"]["methods"]["items"] == []


def test_a_public_method_edit_invalidates_an_unchanged_unit_across_clients(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An independently authored exclusion changes while the selected procedure stays identical."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    procedure = "Compare total coating time and cleanup effort at the same exposure requirement. Record the evidence and prefer the lower combined time."
    old = "Do not use this method for food-contact surfaces."
    new = "Do not use this method for food-contact surfaces or any outdoor panel exposed to rain."
    method = _page(vault, "display-finishing", "exomem_id: 8902409d-2ee7-408f-8c39-4283125dc578\n", body=f"## Procedure\n\n{procedure}\n\n## Applicability\n\nThis method applies to temporary display panels.\n\n## Exclusions\n\n{old}\n")
    from test_lexstore import _seed_live_scopes

    _seed_live_scopes(vault)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    turn = "What method should I use to compare finishes for a temporary display that will stand in rain?"
    first = _activate(vault, turn=turn, max_chars=8000, client="fixture-a")
    before = next(item for item in first["task_context"]["methods"]["items"] if item["source_ref"] == method)
    with principal.request_scope(principal.owner_principal(surface="library")):
        from exomem.vault import content_hash

        operation = {"kind": "replace_string", "old_string": old, "new_string": new,
                     "expected_hash": content_hash((vault / method).read_text())}
        preview = commands.op_edit_memory(vault, path=method, why="Revise the method exclusion", operation={**operation, "validate_only": True})["semantic"]
        editor = next(item for item in commands.PRODUCT_COMMANDS if item.name == "edit_memory")
        invoke_command(editor, vault, path=method, why="Revise the method exclusion", operation={
            **operation, "transition_token": preview["transition_token"],
            "relation_disposition": "reviewed_none", "relation_review_hash": preview["relation_review_hash"],
            "relation_review_reason": "The changed exclusion adds no authored relation.",
        })
    assert procedure in (vault / method).read_text()
    refreshed = _activate(vault, turn=turn, max_chars=8000, continuity=first["continuity"], client="fixture-a")
    fresh = _activate(vault, turn=turn, max_chars=8000, client="fixture-b")
    assert refreshed["task_context"]["methods"]["reset"] is True
    after = next(item for item in fresh["task_context"]["methods"]["items"] if item["source_ref"] == method)
    assert after["source_version"] != before["source_version"]
    assert after["ref"] == before["ref"]
    assert after["applicability"] == "read_required" and after["read_ref"] == method
    reader = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    with principal.request_scope(principal.owner_principal(surface="library")):
        source = invoke_command(reader, vault, path=after["read_ref"])
    assert new in str(source)


def test_legacy_and_malformed_method_hints_preserve_map_continuity() -> None:
    """An optional hint cannot invalidate an independently valid older map token."""
    options = dict(identity="map", roles_hash="roles", conventions_hash="conventions", generation=0, refs=[], roles=[], map_generation="map")
    legacy = working_set_runtime.decode_continuity(working_set_runtime.encode_continuity(**options))
    malformed = working_set_runtime.decode_continuity(working_set_runtime.encode_continuity(**options, method_versions=[{"ref": "x"}]))
    assert legacy["map_generation"] == malformed["map_generation"] == "map"
    assert malformed.get("method_versions", []) == []


def test_method_continuation_overflow_resets_before_evicting_held_dependencies() -> None:
    """A token that cannot retain every dependency never silently certifies held methods."""
    from exomem import context_task

    held = [{"ref": f"method-{index}", "source_ref": "a" * 300, "source_version": "v"} for index in range(32)]
    packet = {
        "task_context": context_task.empty(), "map": {"description": "Map.", "items": []},
        "budget": {"limit_chars": 500, "used_chars": 0},
    }
    token = context_task.continuity_token(packet, held=held, identity="map", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[], map_generation="map")
    assert len(token) <= working_set_runtime.CONTINUITY_MAX_CHARS
    assert packet["task_context"]["methods"]["reset"] is True
    assert working_set_runtime.decode_continuity(token).get("method_versions", []) == []


def test_a_withheld_method_and_a_missing_method_reset_identically(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revalidation must not reveal a method after its caller loses access."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    twin = _second_vault(tmp_path)
    hidden = _page(vault, "hidden-method", body="- [technique] Inspect optical alignment ^alignment\n")
    for root in (vault, twin):
        _page(root, "visible-reference", body="Seed germination reference.\n")
        _withhold(root, hidden, ceiling=0)
        _built(root).close()
        lexstore.ensure_fresh(root)
    token = working_set_runtime.encode_continuity(
        identity="old", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[], map_generation="old",
        method_versions=[{"ref": hidden + "#alignment", "source_ref": hidden, "source_version": "old"}],
    )
    with_hidden = _activate(vault, _external(), turn="Seed germination", continuity=token, max_chars=8000)
    absent = _activate(twin, _external(), turn="Seed germination", continuity=token, max_chars=8000)
    assert _without_response_id(with_hidden) == _without_response_id(absent)
    assert with_hidden["task_context"]["methods"]["reset"] is True
    assert hidden not in str(with_hidden)


def test_a_forged_task_block_cannot_borrow_a_larger_packet_allowance(vault: Path) -> None:
    """Final egress restores actual compiler fields and binds them to the packet cap."""
    from exomem import context_task, context_views

    with principal.request_scope(principal.owner_principal(surface="library")), principal.invocation_scope():
        caller = context_views.CallerView.current(vault)
        original = context_task.pin(vault, caller, context_task.empty(), limit=8000)
        forged = {**original, "candidates": [{"source_ref": "invented", "reason": "Invented fact"}]}
        assert context_task.guarded(vault, caller, forged, limit=8000) == original
        smaller = context_task.guarded(vault, caller, forged, limit=500)
    assert smaller["state"] == "unavailable" and smaller["candidates"] == []


def test_method_reset_advice_fits_the_smallest_crowded_packet(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalidating a held method must not push a full map past the minimum public cap."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _seed_structure(vault)
    _built(vault).close()
    token = working_set_runtime.encode_continuity(
        identity="old", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[], map_generation="old",
        method_versions=[{"ref": "missing", "source_ref": "Knowledge Base/Notes/missing.md", "source_version": "old"}],
    )
    packet = _activate(vault, turn="", continuity=token, max_chars=500)
    assert packet["task_context"]["methods"]["reset"] is True
    assert packet["budget"]["used_chars"] <= 500


def test_method_projection_survives_older_role_overrides(vault: Path) -> None:
    """An overlay that predates projection retains the shipped method shape."""
    from exomem import context_roles

    inherited = context_roles.load_roles(vault, proposal={"schema_version": 1, "roles": {"methods": {"add_categories": ["finding"]}}})
    assert inherited.roles["methods"].projection == "method"
    explicit = context_roles.load_roles(vault, proposal={"schema_version": 1, "roles": {"methods": {"projection": ""}}})
    assert explicit.roles["methods"].projection == ""


def test_a_competing_anchor_does_not_hide_a_note_or_episode(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An independent note and episode remain readable beside an unrelated resolved subject."""
    from exomem import episode_memory

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "on")
    note = _page(vault, "gauge-service", body="Bring the handheld gauge and its old case to the depot service desk for a calibration quote. No repair is approved.\n")
    _page(vault, "Storage bins", folder="Knowledge Base/Products", body="Compare storage bins by internal dimensions.\n")
    with principal.request_scope(principal.owner_principal(surface="library")):
        episode = episode_memory.record(
            vault, _source_schema(vault), episode="ep-" + "ac" * 16, subject="Workshop follow-up",
            summary="The gauge quote remains unresolved. Bring the gauge and its old case on the next depot visit; do not treat a quote as a repair order.",
            open=["The gauge quote remains unresolved."],
        )
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, turn="I will stop at the depot tomorrow to look at storage bins. Is there anything else I should prepare?", max_chars=8000)
    refs = {item["source_ref"] for item in packet["task_context"]["candidates"]}
    assert note in refs
    assert episode["source"]["path"] in str(packet)
    assert packet["turn_slice"]["items"]


def test_collection_discovery_returns_the_manifest_without_querying_rows(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Activation routes through claims and metadata, leaving exact facts to bounded queries."""
    from exomem import record_governance

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    _seed_structure(vault)
    manifest = "Knowledge Base/Records/Depot Stock/_collection.md"
    _write(vault, "Knowledge Base/Records/Depot Stock/Items/raw-row.md", "Raw row content must not enter activation.\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)

    def forbid_rows(*args, **kwargs):
        raise AssertionError("activation must not enumerate a relevant collection")

    monkeypatch.setattr(record_governance, "query_collection", forbid_rows)
    packet = _activate(vault, turn="Where can I query depot stock observations?", max_chars=8000)
    assert manifest in str(packet)
    assert "raw-row" not in str(packet)



def test_current_methods_follow_admitted_unit_currency_before_reuse(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live page must not offer replaced or expired methods, even with matching carried source bytes."""
    from exomem import semantic_index

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    path = _page(vault, "alignment-methods", body=(
        "## Procedure\n- id: before\n\nInspect optical alignment with the earlier target.\n"
        "## Procedure\n- id: current\n- relations: supersedes: [[#before]]\n\nInspect optical alignment with the current target.\n"
        "## Procedure\n- id: expired\n- until: 2020-01-01\n\nInspect optical alignment with the expired target.\n"
        "## Procedure\n- id: review\n- check_by: 2020-01-01\n\nInspect optical alignment and check the source first.\n"
    ))
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    first = _activate(vault, turn="Inspect optical alignment", max_chars=8000)
    methods = [item for item in first["task_context"]["methods"]["items"] if item["source_ref"] == path]
    assert {item["ref"].rpartition("#")[2] for item in methods} == {"current", "review"}
    assert all(item["read_ref"] == path and item["applicability"] == "read_required" for item in methods)
    with principal.request_scope(principal.owner_principal(surface="library")):
        document = semantic_index.selected_parent_index_state(vault, path).document
    expired = next(unit.unit_ref for unit in document.units if unit.anchor == "expired")
    token = working_set_runtime.encode_continuity(
        identity="old", roles_hash="r", conventions_hash="c", generation=0, refs=[], roles=[], map_generation="old",
        method_versions=[{"ref": expired, "source_ref": path, "source_version": methods[0]["source_version"]}],
    )
    unrelated = _activate(vault, turn="", continuity=token, max_chars=8000)
    assert unrelated["task_context"]["methods"]["reset"] is True
    assert unrelated["task_context"]["methods"]["items"] == []


def test_withheld_unit_cannot_retire_an_admitted_method(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller must receive method currency computed after its unit release decisions."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "private-reference", body="Restricted inspection reference.\n")
    path = _page(vault, "alignment-methods", body=(
        "## Procedure\n- id: before\n\nInspect optical alignment with the earlier target.\n"
        "## Procedure\n- id: replacement\n- relations: supersedes: [[#before]]\n\n"
        f"Inspect optical alignment with [[{hidden}]].\n"
    ))
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    owner = _activate(vault, turn="Inspect optical alignment", max_chars=8000)
    guest = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    def identities(packet):
        return {item["ref"].rpartition("#")[2] for item in packet["task_context"]["methods"]["items"] if item["source_ref"] == path}
    assert identities(owner) == {"replacement"}
    assert identities(guest) == {"before"}
    assert hidden not in str(guest["task_context"])


def test_unavailable_method_currency_preserves_admitted_read_routes(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed method stage must leave discovered source routes usable with an honest partial state."""
    from exomem import working_set_currency

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    path = _page(vault, "alignment-methods", body="- [technique] Inspect optical alignment with the reference target ^target\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    def unavailable(*args, **kwargs):
        raise working_set_currency.CurrencyUnavailable("fixture current-unit evidence unavailable")
    monkeypatch.setattr(working_set_currency, "annotate", unavailable)
    packet = _activate(vault, turn="Inspect optical alignment", max_chars=8000)
    task = packet["task_context"]
    assert packet["abstained"] is False and "map" in packet
    assert path in {item["read_ref"] for item in task["candidates"]}
    assert task["state"] == "partial" and task["methods"]["items"] == []
    assert {"role": "task_context", "reason": "lane_failed"} in task["missing"]


@pytest.mark.parametrize("caller", [_external(), principal.owner_principal(surface="library")], ids=["external", "owner"])
def test_released_corpus_discovery_is_unavailable_by_default(vault: Path, monkeypatch: pytest.MonkeyPatch, caller) -> None:
    """Without the raw fast path, a request gets an unavailable task context and an unchanged map by default."""
    monkeypatch.delenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", raising=False)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    path = _page(vault, "alignment-reference", body="- [technique] Inspect optical alignment against a reference target ^target\n")
    # A file release policy removes the raw fast path for every caller, the owner included.
    _withhold(vault, _page(vault, "restricted-target", body="Confidential reference material.\n"), ceiling=0)
    write_rule(vault, ceiling=0, audience=caller.audience_id)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, caller, turn="Inspect optical alignment", max_chars=8000)
    task = packet["task_context"]
    assert packet["abstained"] is False and packet["map"]["items"]
    assert task["state"] == "unavailable"
    assert task["candidates"] == [] and task["methods"]["items"] == []
    assert path not in str(task)

    # The service restarts to apply the setting; drop the packet cache that a restart would drop.
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    working_set_runtime.reset_caches_for_tests(packets_only=True)
    served = _activate(vault, caller, turn="Inspect optical alignment", max_chars=8000)
    assert path in {item["source_ref"] for item in served["task_context"]["candidates"]}


@pytest.mark.parametrize("caller", [_external(), principal.owner_principal(surface="library")], ids=["external", "owner"])
def test_withheld_units_cannot_create_restricted_task_routes(vault: Path, monkeypatch: pytest.MonkeyPatch, caller) -> None:
    """A mixed page stays discoverable for its visible unit without matching its withheld unit."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    visible = "- [technique] Inspect optical alignment against a reference target ^visible\n"
    withheld = f"- [technique] Evaluate germination using [[{hidden}]] ^hidden\n"
    path = _page(vault, "measurement-reference", body=visible + "\n" + withheld)
    _withhold(vault, hidden, ceiling=0)
    write_rule(vault, ceiling=0, audience=caller.audience_id)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    before = _activate(vault, caller, turn="Evaluate germination", max_chars=8000)
    allowed = _activate(vault, caller, turn="Inspect optical alignment", max_chars=8000)
    assert path in str(allowed["task_context"])
    (vault / path).write_text((vault / path).read_text().replace("\n" + withheld, ""))
    freshness.on_files_changed(vault, [vault / path])
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    after = _activate(vault, caller, turn="Evaluate germination", max_chars=8000)
    assert before["task_context"] == after["task_context"]


def test_restricted_task_admission_precedes_candidate_limits(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Withheld matches must not consume a raw top-k window ahead of a permitted source."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    for index in range(13):
        _page(vault, f"measurement-reference-{index}", body=(
            "- [technique] Inspect optical alignment ^visible\n\n"
            f"- [technique] Evaluate germination germination germination using [[{hidden}]] ^hidden\n"
        ))
    permitted = _page(vault, "public-observation", body="Evaluate germination in the current observation record.\n")
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, _external(), turn="Evaluate germination", max_chars=8000)
    refs = {item["source_ref"] for item in packet["task_context"]["candidates"]}
    assert permitted in refs
    assert not any("measurement-reference-" in ref for ref in refs)


@pytest.mark.parametrize("caller", [_external(), principal.owner_principal(surface="library")], ids=["external", "owner"])
def test_excerpt_only_sources_do_not_expose_task_hashes_or_metadata(vault: Path, monkeypatch: pytest.MonkeyPatch, caller) -> None:
    """An excerpt permission keeps canonical pull routes without exposing whole-source versions or metadata."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    import hashlib
    import json

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    path = _page(vault, "measurement-reference", "title: Restricted metadata label\n",
                 body="- [technique] Inspect optical alignment ^visible\n\n" + "Neutral observation. " * 60)
    _withhold(vault, path, ceiling=5)
    write_rule(vault, ceiling=5, audience=caller.audience_id)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    raw_hash = hashlib.sha256((vault / path).read_bytes()).hexdigest()
    reader = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")
    with principal.request_scope(caller):
        canonical = invoke_command(reader, vault, path=path)
    assert canonical["release_level"] == 5 and canonical["body"]
    assert "content_hash" not in canonical and raw_hash not in json.dumps(canonical)
    packet = _activate(vault, caller, turn="Inspect optical alignment", max_chars=8000)
    assert packet["abstained"] is False and "map" in packet
    assert packet["task_context"]["state"] in ("partial", "unavailable")
    assert path not in {item["source_ref"] for item in packet["task_context"]["candidates"]}
    assert packet["task_context"]["methods"]["items"] == []
    assert raw_hash not in json.dumps(packet)
    assert "Restricted metadata label" not in json.dumps(packet["task_context"])


def test_restricted_method_version_covers_visible_exclusions_outside_the_unit(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A released-source version must retain operative prose outside the selected method unit."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    path = _page(vault, "alignment-method", body="- [technique] Inspect optical alignment ^visible\n\nExclude handheld field checks.\n")
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    first = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    before = next(item for item in first["task_context"]["methods"]["items"] if item["source_ref"] == path)
    (vault / path).write_text((vault / path).read_text().replace("Exclude handheld field checks.", "Exclude workshop and handheld checks."))
    freshness.on_files_changed(vault, [vault / path])
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    unrelated = _activate(vault, _external(), turn="Evaluate seed germination", continuity=first["continuity"], max_chars=8000)
    fresh = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    after = next(item for item in fresh["task_context"]["methods"]["items"] if item["source_ref"] == path)
    assert unrelated["task_context"]["methods"]["reset"] is True
    assert before["ref"] == after["ref"] and before["source_version"] != after["source_version"]


def test_restricted_graph_omission_keeps_visible_recall_without_origin_leaks(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A withheld unit's graph edge cannot admit a source that the released text did not match."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    neighbour = _page(vault, "secondary-reference", body="A separate measurement note.\n")
    path = _page(vault, "alignment-method", body=(
        "- [technique] Inspect optical alignment ^visible\n\n"
        f"- [technique] Evaluate germination using [[{hidden}]] and [[{neighbour}]] ^hidden\n"
    ))
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    task = packet["task_context"]
    assert path in str(task)
    assert neighbour not in {item["source_ref"] for item in task["candidates"]}
    # The links lane is the omitted one: a restricted lead names no link target, withheld or not.
    assert all("links" not in item for item in task["candidates"])
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "lane_failed"} in task["missing"]


def test_incomplete_restricted_corpus_never_serves_a_prepared_prefix(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A preparation deadline must preserve the map without ranking a biased source prefix."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    import contextlib
    import time

    from exomem import request_budget, runtime_resources

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    _page(vault, "a-matching-reference", body="Inspect optical alignment with the target.\n")
    _page(vault, "z-matching-reference", body="Inspect optical alignment with the alternate target.\n")
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    budget = request_budget.RequestBudget(seconds=60)
    original = runtime_resources.semantic_preparation
    prepared = []
    @contextlib.contextmanager
    def expires(root, path, **kwargs):
        with original(root, path, **kwargs) as source:
            prepared.append(path)
            if path.name == "z-matching-reference.md":
                # The activation runs on its own budget beneath this one; expire the one it reads.
                request_budget.current().deadline = time.monotonic()
            yield source
    monkeypatch.setattr(runtime_resources, "semantic_preparation", expires)
    token = request_budget.set_current(budget)
    try:
        packet = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    finally:
        request_budget.reset_current(token)
    assert {path.name for path in prepared} >= {"a-matching-reference.md", "z-matching-reference.md"}
    assert packet["abstained"] is False and "map" in packet
    assert packet["task_context"]["state"] in ("partial", "unavailable")
    assert packet["task_context"]["candidates"] == []
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_restricted_corpus_capacity_cannot_rank_a_prepared_prefix(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aggregate capacity exhaustion must not turn the first prepared matching source into an answer."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    from exomem import context_task, working_set

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    hidden = _page(vault, "restricted-target", body="Confidential reference material.\n")
    first = _page(vault, "a-budget-reference", body="Inspect optical alignment with the target.\n")
    last = _page(vault, "z-budget-reference", body="Calibration observation. " * 5000 + "\nInspect optical alignment.\n")
    _withhold(vault, hidden, ceiling=0)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    monkeypatch.setattr(working_set, "TASK_CORPUS_MAX_BYTES", 1024 * 1024)
    original = context_task.project_source
    prepared = []
    def record(root, path, caller, **kwargs):
        source = original(root, path, caller, **kwargs)
        if source is not None:
            prepared.append(path)
        return source
    monkeypatch.setattr(context_task, "project_source", record)
    packet = _activate(vault, _external(), turn="Inspect optical alignment", max_chars=8000)
    assert first in prepared and last in prepared
    assert packet["abstained"] is False and "map" in packet
    assert packet["task_context"]["state"] in ("partial", "unavailable")
    assert packet["task_context"]["candidates"] == []


def test_owner_keeps_task_discovery_on_an_enrolled_store_while_a_store_page_waits_for_its_own_decision(
    service, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The owner keeps task discovery on an enrolled store, and a store outage withholds the store's own page."""
    from exomem import context_task, context_views
    from exomem.collection_store import runtime
    from exomem.governance import egress

    root = service.root
    monkeypatch.delenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", raising=False)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The turn slice drops a candidate it already carries; with it off, the task context shows discovery itself.
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    ordinary = _page(root, "gauge-calibration", body="Bring the handheld gauge to the depot service desk for a calibration quote.\n")
    _built(root).close()
    lexstore.ensure_fresh(root)
    turn = "Where is the calibration quote for the handheld gauge from the depot service desk?"
    owner = principal.owner_principal(surface="library")

    def discovered() -> tuple[str, set[str]]:
        task = _activate(root, owner, turn=turn, max_chars=8000)["task_context"]
        return task["state"], {item["source_ref"] for item in task["candidates"]}

    def store_page_projected() -> bool:
        # A store page reaches the raw path through `project_source`, which held methods and the terminal guard call.
        with principal.request_scope(owner):
            return context_task.project_source(root, manifest_path(), context_views.CallerView.current(root)) is not None

    state, served = discovered()
    assert state == "ready" and ordinary in served
    assert store_page_projected()

    with monkeypatch.context() as outage:
        outage.setattr(runtime, "_server", lambda _root: None)
        egress.clear_decision_memo()
        working_set_runtime.reset_caches_for_tests(packets_only=True)
        state, served = discovered()
        assert state == "ready" and ordinary in served
        assert not store_page_projected()


def test_a_slow_method_search_leaves_time_to_serve_the_leads_already_found(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold method search once ran to the request deadline, so no found lead was projected and task context was dropped."""
    import time

    from exomem import request_budget

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The turn slice drops a lead it already carries; with it off, the task context shows discovery itself.
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    lead = _page(vault, "alignment-visit", body="Inspect the optical alignment before the workshop.\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    searched = []

    def runs_to_its_deadline(self, *args, **kwargs):
        searched.append(True)
        # Stands in for a cold catalogue query that only the request budget stops.
        while request_budget.current().remaining() > 0:
            time.sleep(0.05)
        return lexstore.CatalogQueryResult(None, lexstore.CatalogReadiness(lexstore.DEADLINE_STATUS, False, "sqlite"))

    monkeypatch.setattr(type(lexstore.get_store(vault)), "search_semantic_units_result", runs_to_its_deadline)
    budget = request_budget.RequestBudget(seconds=8.0)
    token = request_budget.set_current(budget)
    try:
        packet = _activate(vault, turn="Inspect the optical alignment before the workshop", max_chars=8000)
    finally:
        request_budget.reset_current(token)
    task = packet["task_context"]
    assert searched
    assert lead in {item["source_ref"] for item in task["candidates"]}
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "budget"} in task["missing"]


def _clock_at(monkeypatch: pytest.MonkeyPatch, start: float = 1000.0) -> list[float]:
    """Replace the request budget's clock with one the test advances, so a starved host takes no real time."""
    from types import SimpleNamespace

    from exomem import request_budget

    clock = [start]
    monkeypatch.setattr(request_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


def _activate_within(vault: Path, seconds: float, **kwargs):
    """One activation under a request budget of `seconds`, with the budget it left for the caller to read."""
    from exomem import request_budget

    budget = request_budget.RequestBudget(seconds=seconds)
    token = request_budget.set_current(budget)
    try:
        return _activate(vault, **kwargs), budget
    finally:
        request_budget.reset_current(token)


def test_a_deadline_during_projection_serves_the_leads_already_projected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starved host once ran past the deadline here or dropped every lead; the leads read before it must still be served."""
    from exomem import context_task

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    pages = {
        _page(
            vault,
            f"alignment-visit-{number}",
            body="Inspect the optical alignment before the workshop.\n",
        )
        for number in range(3)
    }
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    turn = "Inspect the optical alignment before the workshop"
    unhurried, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    assert pages <= {item["source_ref"] for item in unhurried["task_context"]["candidates"]}

    clock = _clock_at(monkeypatch)
    project = context_task.project_source
    projected: list[str] = []

    def starves_after_the_first_lead(root, source_ref, *args, **kwargs):
        source = project(root, source_ref, *args, **kwargs)
        if not projected:
            projected.append(source_ref)
            clock[0] += 60.0  # the host was starved for the CPU while this lead was read
        return source

    monkeypatch.setattr(context_task, "project_source", starves_after_the_first_lead)
    packet, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    task = packet["task_context"]
    assert len(projected) == 1
    assert [item["source_ref"] for item in task["candidates"]] == projected
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert not packet.get("abstained") and packet["map"]


def test_a_deadline_during_the_catalogue_scan_stops_the_scan_and_keeps_the_map(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starved host once ran this scan to its end however late; it stops, and a half-read catalogue gives no lead."""
    from exomem import request_budget

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    # One check per entry, so the scan has a middle to stop in.
    monkeypatch.setattr(request_budget, "CHECKPOINT_EVERY", 1)
    for number in range(3):
        _page(
            vault,
            f"alignment-visit-{number}",
            body="Inspect the optical alignment before the workshop.\n",
        )
    catalogue = _built(vault)
    total = len(catalogue.task_entries())
    catalogue.close()
    assert total > 3
    lexstore.ensure_fresh(vault)
    turn = "Inspect the optical alignment before the workshop"
    unhurried, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    assert unhurried["task_context"]["candidates"]

    clock = _clock_at(monkeypatch)
    entries = context_catalogue.ContextCatalogue.task_entries
    pulled: list[int] = []

    def starves_in_the_third_entry(self):
        for position, entry in enumerate(entries(self)):
            pulled.append(position)
            if position == 2:
                clock[0] += 60.0
            yield entry

    monkeypatch.setattr(
        context_catalogue.ContextCatalogue, "task_entries", starves_in_the_third_entry
    )
    packet, budget = _activate_within(vault, 50, turn=turn, max_chars=8000)
    task = packet["task_context"]
    assert len(pulled) == 3
    assert task["state"] == "partial" and task["candidates"] == []
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert "working_set.task_context_entries" in budget.truncated
    assert not packet.get("abstained") and packet["map"]


def test_a_deadline_during_the_text_query_stops_it_short_of_the_deadline_and_serves_the_route_lead(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starved host once let this query run on past the deadline; it stops early enough to project the lead already routed."""
    import sqlite3

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    _seed_structure(vault)
    manifest = "Knowledge Base/Records/Depot Stock/_collection.md"
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    turn = "Where can I query depot stock observations?"
    unhurried, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    assert manifest in {item["source_ref"] for item in unhurried["task_context"]["candidates"]}

    clock = _clock_at(monkeypatch)
    store_type = type(lexstore.get_store(vault))
    query = store_type._bm25_query
    outcomes: list[str] = []

    def stalls_past_the_query_share(self, conn, *args, **kwargs):
        # Only the task text query keeps raw material and leaves navigation out; no other lane asks for that pair.
        if kwargs.get("exclude_navigation") and not kwargs.get("exclude_raw_material"):
            # 12 s of the 15 s the activation gets: past what the query may spend, with the projection reserve still unspent.
            clock[0] += 12.0
            try:
                conn.execute(
                    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 500000) "
                    "SELECT count(*) FROM n"
                ).fetchone()
            except sqlite3.OperationalError:
                outcomes.append("interrupted")
                raise
            outcomes.append("finished")
        return query(self, conn, *args, **kwargs)

    monkeypatch.setattr(store_type, "_bm25_query", stalls_past_the_query_share)
    packet, budget = _activate_within(vault, 50, turn=turn, max_chars=8000)
    task = packet["task_context"]
    assert outcomes == ["interrupted"]
    assert manifest in {item["source_ref"] for item in task["candidates"]}
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert "working_set.task_context_text" in budget.truncated
    assert not packet.get("abstained") and packet["map"]


def test_a_deadline_spent_by_the_turn_slice_marks_task_context_partial_and_keeps_the_map(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the turn slice on, a starved slice compile once left task context "unavailable" with no reason given."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    for number in range(3):
        _page(
            vault,
            f"alignment-visit-{number}",
            body="Inspect the optical alignment before the workshop.\n",
        )
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    turn = "Inspect the optical alignment before the workshop"
    unhurried, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    assert unhurried["task_context"]["candidates"]

    clock = _clock_at(monkeypatch)
    compile_slice = commands._compile_guarded_activation

    def starved_slice(*args, **kwargs):
        compiled = compile_slice(*args, **kwargs)
        clock[0] += 60.0  # the host was starved for the CPU while the turn slice compiled
        return compiled

    monkeypatch.setattr(commands, "_compile_guarded_activation", starved_slice)
    packet, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    task = packet["task_context"]
    assert not packet.get("abstained") and packet["map"]
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert task["candidates"] == []


def test_an_activation_stops_at_its_own_deadline_inside_a_longer_request_budget(
    vault: Path, monkeypatch: pytest.MonkeyPatch, activation_log: Path
) -> None:
    """A request that allows 50 s gave its activation all of them; it now gets the configured seconds and the row says so."""
    import sqlite3

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    monkeypatch.setenv("EXOMEM_ACTIVATION_BUDGET_SECONDS", "2")
    _seed_structure(vault)
    manifest = "Knowledge Base/Records/Depot Stock/_collection.md"
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    turn = "Where can I query depot stock observations?"
    clock = _clock_at(monkeypatch)
    unhurried, _ = _activate_within(vault, 50, turn=turn, max_chars=8000)
    assert manifest in {item["source_ref"] for item in unhurried["task_context"]["candidates"]}

    store_type = type(lexstore.get_store(vault))
    query = store_type._bm25_query
    outcomes: list[str] = []

    def stalls_past_the_activation_deadline(self, conn, *args, **kwargs):
        # Only the task text query keeps raw material and leaves navigation out; no other lane asks for that pair.
        if kwargs.get("exclude_navigation") and not kwargs.get("exclude_raw_material"):
            # 3 s: past the activation's 2 s, 47 s short of what the request allows.
            clock[0] += 3.0
            try:
                conn.execute(
                    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 500000) "
                    "SELECT count(*) FROM n"
                ).fetchone()
            except sqlite3.OperationalError:
                outcomes.append("interrupted")
                raise
            outcomes.append("finished")
        return query(self, conn, *args, **kwargs)

    monkeypatch.setattr(store_type, "_bm25_query", stalls_past_the_activation_deadline)
    packet, budget = _activate_within(vault, 50, turn=turn, max_chars=8000)
    task = packet["task_context"]
    assert outcomes == ["interrupted"]
    assert packet.get("abstained") is False and packet["map"]
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert packet["request_budget"]["seconds"] == 2.0
    assert budget.remaining() > 40.0
    assert "working_set.task_context_text" in budget.truncated
    served, limited = _rows(activation_log)
    assert served["activation_result"]["budget_limited"] is False
    assert limited["outcome"] == "served"
    assert limited["activation_result"] == {"abstained": False, "reason": None, "budget_limited": True}
    assert limited["budget"]["seconds"] == 2.0
    assert "working_set.task_context_text" in limited["budget"]["truncated"]


def test_the_short_activation_door_still_runs_the_method_search(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook's doors bind a 6 s budget; a reserve sized for the 50 s MCP budget would skip methods on every turn."""
    from exomem import request_budget

    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "off")
    _page(vault, "alignment-visit", body="Inspect the optical alignment before the workshop.\n")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    store_type = type(lexstore.get_store(vault))
    real = store_type.search_semantic_units_result
    searched = []

    def recorded(self, *args, **kwargs):
        searched.append(True)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(store_type, "search_semantic_units_result", recorded)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=request_budget.ACTIVATION_DOOR_BUDGET_SECONDS))
    try:
        packet = _activate(vault, turn="Inspect the optical alignment before the workshop", max_chars=8000)
    finally:
        request_budget.reset_current(token)
    assert searched
    assert {"role": "task_context", "reason": "budget"} not in packet["task_context"]["missing"]
