"""Recurring incidents reach their Records collection by themselves.

Five parts, one file: claim hygiene (function words and inflection),
structured `match` predicates, a bounded grouped backfill, a prominence-driven
disposition on the write response, and a noise budget for
`collection_candidate`. Every fixture name is invented.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    audit,
    collection_candidate,
    collection_claims,
    due_state,
    prominence,
    semantic_writes,
    vocabulary_fold,
)
from exomem import (
    structured_collections as collections,
)
from exomem.collection_claims import RoutingTarget, route

INCIDENTS = "Knowledge Base/Records/Incidents/_collection.md"
LEDGER = "Knowledge Base/Records/Ledger/_collection.md"
NOW = dt.datetime(2026, 9, 27, 12, tzinfo=dt.UTC)

_INCIDENT_CLAIMS = (
    "claims:\n"
    "  tags: [widget-app, dogfood, failures, regressions, incidents]\n"
    "  terms: [context compiler, activation, capture, records, evidence, routing, retrieval]\n"
)
_INCIDENT_MATCH = "  match:\n    type: [failure]\n    project: [widget-app]\n"


def _incident_manifest(*, claims: str = _INCIDENT_CLAIMS + _INCIDENT_MATCH) -> str:
    return f"""---
type: collection
exomem_id: 21111111-1111-4111-8111-111111111111
title: Widget incidents
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Entries
  format_version: 1
item_schema:
  natural_key: [incident]
  fields:
    incident:
      type: string
      required: true
    observed_on:
      type: date
      required: true
    project:
      type: string
    symptom:
      type: string
    status:
      type: enum
      enum: [open, resolved]
    tags:
      type: array
      items:
        type: string
    sources:
      type: array
      items:
        type: link
{claims}---

Product failure incidents.
"""


def _write_incidents(tmp_path: Path, **kwargs: str) -> Path:
    path = tmp_path / INCIDENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_incident_manifest(**kwargs), encoding="utf-8")
    (path.parent / "Entries").mkdir(exist_ok=True)
    return path


def _write_prose_ledger(tmp_path: Path) -> Path:
    """A collection whose declared terms are prose, as authors actually write them."""
    path = tmp_path / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _incident_manifest(
            claims=(
                "claims:\n"
                "  terms: [not because after first use, release windows]\n"
            )
        )
        .replace("21111111-1111-4111-8111-111111111111", "31111111-1111-4111-8111-111111111111")
        .replace("Widget incidents", "Release ledger"),
        encoding="utf-8",
    )
    (path.parent / "Entries").mkdir(exist_ok=True)
    return path


def _write_failure_note(
    tmp_path: Path,
    index: int,
    *,
    created: str = "2025-11-02",
    title: str | None = None,
    note_type: str = "failure",
    project: str = "widget-app",
    tags: str = "[widget-app, failure]",
) -> tuple[str, str]:
    relative = f"Knowledge Base/Notes/Failures/widget-failure-{index:03d}.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    exomem_id = f"61111111-1111-4111-8111-{index:012d}"
    path.write_text(
        "---\n"
        f"type: {note_type}\n"
        f"exomem_id: {exomem_id}\n"
        f"title: {title or f'Widget panel froze on load {index}'}\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        "status: active\n"
        f"project: {project}\n"
        f"tags: {tags}\n"
        "sources: []\n"
        "---\n\n"
        "## Observations\n\n"
        f"- [failure] The panel stopped responding after load {index} ^event\n",
        encoding="utf-8",
    )
    return relative, f"exomem://memory/{exomem_id}"


def _target(
    collection: str,
    claims: set[str],
    *,
    match: dict[str, frozenset[str]] | None = None,
) -> RoutingTarget:
    return RoutingTarget(
        collection=collection,
        title=collection.rsplit("/", 2)[-2],
        claims=collection_claims.normalize_terms(claims),
        natural_key=("incident",),
        natural_key_types=("string",),
        match=match or {},
    )


# --- Part 1: claim hygiene -------------------------------------------------


def test_fold_term_folds_plurals_and_progressives_conservatively() -> None:
    fold = vocabulary_fold.fold_term
    assert fold("failures") == fold("failure") == "failure"
    assert fold("dogfooding") == fold("dogfood") == "dogfood"
    assert fold("regressions") == "regression"
    assert fold("Matches") == "match"
    assert fold("stopped") == "stop"
    assert fold("Context  Compiler") == "context-compiler"
    assert fold("snake_case term") == "snake-case-term"
    # Short words and declared exceptions are never folded.
    for word in ("bus", "gas", "news", "series", "status", "process", "analysis", "ring"):
        assert fold(word) == word
    # Folding is idempotent: a stored folded term re-folds to itself.
    for word in ("dressings", "failures", "processes", "entries", "stopping"):
        assert fold(fold(word)) == fold(word)


def test_function_words_never_become_claim_or_observation_terms() -> None:
    assert collection_claims.normalize_terms(["not because after first use"]) == frozenset()


def test_prose_claims_no_longer_capture_a_page_sharing_only_function_words() -> None:
    ledger = _target(LEDGER, {"not because after first use", "release windows"})

    assert route(["It did not open because the cache was stale after boot"], [ledger]) is None


def test_inflected_tags_meet_their_claims() -> None:
    incidents = _target(INCIDENTS, {"dogfood", "failures", "widget-app"})

    advisory = route(["Panel froze", "failure", "dogfooding"], [incidents])

    assert advisory is not None
    assert advisory["collection"] == INCIDENTS
    assert advisory["matched_terms"] == ["dogfood", "failure"]


# --- Part 2: structured claims ---------------------------------------------


def test_manifest_match_predicates_parse_describe_and_validate(tmp_path: Path) -> None:
    manifest = collections.load_manifest(tmp_path, _write_incidents(tmp_path))

    assert {key: list(values) for key, values in manifest.claim_match.items()} == {
        "type": ["failure"],
        "project": ["widget-app"],
    }
    assert {name: list(values) for name, values in manifest.claims.items()} == {
        "tags": ["widget-app", "dogfood", "failures", "regressions", "incidents"],
        "terms": [
            "context compiler",
            "activation",
            "capture",
            "records",
            "evidence",
            "routing",
            "retrieval",
        ],
    }
    described = collections.manifest_authoring_contract()
    assert "match" in described["json_schema"]["properties"]["claims"]["properties"]
    assert set(described["claims"]["match_keys"]) == {"type", "category", "project", "tags"}


@pytest.mark.parametrize(
    ("match", "fragment"),
    [
        ("  match: [failure]\n", "claims.match must be a mapping"),
        ("  match:\n    colour: [red]\n", "claims.match has unknown key: colour"),
        ("  match:\n    type: failure\n", "claims.match.type must be a list"),
        ("  match:\n    type: []\n", "claims.match.type must be a list"),
        ("  match:\n    type: [failure, 7]\n", "claims.match.type requires strings"),
    ],
)
def test_malformed_match_predicates_refuse_naming_the_key(
    tmp_path: Path, match: str, fragment: str
) -> None:
    text = _incident_manifest(claims=_INCIDENT_CLAIMS + match)

    with pytest.raises(collections.CollectionError) as refused:
        collections.parse_manifest_bytes(tmp_path, tmp_path / INCIDENTS, text.encode("utf-8"))

    assert refused.value.code == "INVALID_COLLECTION_CLAIMS"
    assert fragment in str(refused.value)


def test_predicate_route_is_strong_without_any_word_overlap() -> None:
    incidents = _target(
        INCIDENTS,
        {"dogfood", "failures", "widget-app"},
        match={"type": frozenset({"failure"}), "project": frozenset({"widget-app"})},
    )

    advisory = route(
        ["Panel froze on load"],
        [incidents],
        facets={"type": ["failure"], "project": ["widget-app"]},
    )

    assert advisory is not None
    assert advisory["collection"] == INCIDENTS
    assert advisory["strength"] == "strong"
    assert advisory["matched_predicates"] == ["project:widget-app", "type:failure"]


def test_predicates_require_every_key_and_accept_any_listed_value() -> None:
    incidents = _target(
        INCIDENTS,
        {"dogfood", "failures"},
        match={"type": frozenset({"failure", "incident"}), "project": frozenset({"widget-app"})},
    )

    assert route(["Panel froze"], [incidents], facets={"type": ["incident"], "project": ["widget-app"]})
    assert route(["Panel froze"], [incidents], facets={"type": ["failure"]}) is None
    assert route(["Panel froze"], [incidents], facets={"type": ["decision"], "project": ["widget-app"]}) is None


def test_predicates_only_widen_and_declared_ties_stay_silent() -> None:
    incidents = _target(
        INCIDENTS,
        {"dogfood", "failures", "widget-app"},
        match={"type": frozenset({"failure"})},
    )
    twin = _target(LEDGER, {"ledger", "release"}, match={"type": frozenset({"failure"})})

    # A page that fails the predicate still routes by claims coverage.
    by_terms = route(["dogfood", "failures"], [incidents], facets={"type": ["decision"]})
    assert by_terms is not None and by_terms["collection"] == INCIDENTS
    # Two collections declaring the same membership tie: silence.
    assert route(["Panel froze"], [incidents, twin], facets={"type": ["failure"]}) is None


def test_write_path_routing_sees_type_category_and_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incidents = _target(
        INCIDENTS,
        {"dogfood", "failures", "widget-app"},
        match={"type": frozenset({"failure"}), "project": frozenset({"widget-app"})},
    )
    state = SimpleNamespace(
        title="Panel froze on load",
        frontmatter={"type": "failure", "project": "widget-app", "tags": ["ui"]},
        document=SimpleNamespace(units=(SimpleNamespace(tags=(), category="failure"),)),
    )
    monkeypatch.setattr(due_state, "routing_targets", lambda *_a, **_k: [incidents])

    facets = semantic_writes._records_routing_facets(state)
    advisory = semantic_writes._records_routing(tmp_path, state)

    assert facets == {"type": ["failure"], "category": ["failure"], "project": ["widget-app"]}
    assert advisory is not None and advisory["strength"] == "strong"


def test_routing_targets_carry_the_declared_predicates(tmp_path: Path) -> None:
    _write_incidents(tmp_path)
    due_state.reconcile(tmp_path, now=NOW)

    (target,) = due_state.routing_targets(tmp_path)

    assert target.match == {
        "type": frozenset({"failure"}),
        "project": frozenset({"widget-app"}),
    }


def test_audit_routes_an_existing_failure_note_by_predicate(tmp_path: Path) -> None:
    _write_incidents(tmp_path)
    path, _ref = _write_failure_note(tmp_path, 1, created="2026-09-20", tags="[ui]")

    projection = due_state.reconcile(tmp_path, now=NOW)

    entry = projection["categories"]["unreflected_observations"][path]["open"][0]
    assert entry["component"]["collection"] == INCIDENTS
    assert entry["component"]["matched_predicates"] == ["project:widget-app", "type:failure"]


# --- Part 3: backfill on create or claims change ---------------------------


def _backfill_entry(projection: dict) -> dict:
    (entry,) = projection["categories"]["unreflected_observations"][INCIDENTS]["open"]
    return entry


def test_creating_a_collection_backfills_one_grouped_item(tmp_path: Path) -> None:
    for index in range(1, 13):
        _write_failure_note(tmp_path, index)
    _write_failure_note(tmp_path, 99, note_type="decision", project="other-app", tags="[ui]")
    _write_incidents(tmp_path)

    projection = due_state.reconcile(tmp_path, now=NOW)

    entry = _backfill_entry(projection)
    component = entry["component"]
    assert component["kind"] == "backfill"
    assert component["count"] == 12
    assert len(component["refs"]) == audit.BACKFILL_SAMPLE_REFS
    assert component["refs"][0] == "exomem://memory/61111111-1111-4111-8111-000000000001"
    # No per-page entries for pages outside the discovery lookback.
    assert set(projection["categories"]["unreflected_observations"]) == {INCIDENTS}
    served = due_state.served_entries(tmp_path, now=NOW)
    assert [row["category"] for row in served] == ["unreflected_observations"]


def test_backfill_signal_is_stable_until_the_claims_change(tmp_path: Path) -> None:
    for index in range(1, 4):
        _write_failure_note(tmp_path, index)
    manifest_path = _write_incidents(tmp_path)
    first = _backfill_entry(due_state.reconcile(tmp_path, now=NOW))

    # A new matching page grows the count but keeps the decision's identity.
    _write_failure_note(tmp_path, 4)
    grown = _backfill_entry(due_state.reconcile(tmp_path, now=NOW))
    assert grown["component"]["count"] == 4
    assert grown["fingerprint"] == first["fingerprint"]

    # Changing the claims is a new question.
    manifest_path.write_text(
        _incident_manifest(claims=_INCIDENT_CLAIMS.replace("retrieval", "recall") + _INCIDENT_MATCH),
        encoding="utf-8",
    )
    changed = _backfill_entry(due_state.reconcile(tmp_path, now=NOW))
    assert changed["fingerprint"] != first["fingerprint"]
    assert changed["component"]["signal_version"] != first["component"]["signal_version"]


def test_backfill_is_not_re_raised_after_a_decision(tmp_path: Path) -> None:
    from exomem import review_state

    for index in range(1, 4):
        _write_failure_note(tmp_path, index)
    _write_incidents(tmp_path)
    entry = _backfill_entry(due_state.reconcile(tmp_path, now=NOW))
    review_state.ReviewStateStore(tmp_path).apply(
        entry["item_id"], entry["fingerprint"], action="dismiss", why="not now"
    )
    _write_failure_note(tmp_path, 4)
    due_state.reconcile(tmp_path, now=NOW)

    assert due_state.served_entries(tmp_path, now=NOW) == []


def test_backfill_is_bounded_and_off_the_request_path(tmp_path: Path) -> None:
    assert audit.BACKFILL_MAX_PAGES >= audit.BACKFILL_SAMPLE_REFS
    for index in range(1, audit.BACKFILL_SAMPLE_REFS + 3):
        _write_failure_note(tmp_path, index)
    _write_incidents(tmp_path)
    due_state.reconcile(tmp_path, now=NOW)

    # A compiled write never walks the vault for backfill: it maintains only
    # its own page's entry, and the grouped item is left exactly as it was.
    before = due_state.load(tmp_path)["categories"]["unreflected_observations"][INCIDENTS]
    path, ref = _write_failure_note(tmp_path, 500, created="2026-09-27")
    due_state.apply_observation_write_delta(
        tmp_path,
        path=path,
        observation_ref=ref,
        terms=["Widget panel froze", "widget-app", "failure"],
        routing=None,
        observed_at=NOW,
    )
    after = due_state.load(tmp_path)["categories"]["unreflected_observations"][INCIDENTS]
    assert after == before


def test_backfill_excludes_pages_a_record_already_reflects(tmp_path: Path) -> None:
    for index in range(1, 4):
        _write_failure_note(tmp_path, index)
    manifest_path = _write_incidents(tmp_path)
    (manifest_path.parent / "Entries" / "panel-froze-1.md").write_text(
        "---\n"
        "record_id: rec-1\n"
        "incident: Widget panel froze on load 1\n"
        "observed_on: 2025-11-02\n"
        "sources: ['exomem://memory/61111111-1111-4111-8111-000000000001']\n"
        "---\n",
        encoding="utf-8",
    )

    entry = _backfill_entry(due_state.reconcile(tmp_path, now=NOW))

    assert entry["component"]["count"] == 2


# --- Part 4: act, don't advise ---------------------------------------------


def _routed_state(tmp_path: Path, *, title: str = "Widget panel froze on load 7") -> SimpleNamespace:
    path, _ref = _write_failure_note(tmp_path, 7, created="2026-09-27", title=title)
    return SimpleNamespace(
        path=path,
        identity="61111111-1111-4111-8111-000000000007",
        identity_kind="exomem_id",
        eligible_compiled=True,
        title=title,
        frontmatter={
            "type": "failure",
            "project": "widget-app",
            "tags": ["widget-app", "failure"],
            "created": "2026-09-27",
        },
        document=SimpleNamespace(units=(SimpleNamespace(tags=(), category="failure"),)),
    )


def _deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, state=None) -> dict | None:
    monkeypatch.setattr(prominence, "effective_capture_level", lambda *_a, **_k: level)
    state = state or _routed_state(tmp_path)
    routing = semantic_writes._records_routing(tmp_path, state)
    return semantic_writes._records_routing_for_delivery(tmp_path, routing, state=state)


def test_maximal_files_a_strong_failure_route_with_a_ready_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_incidents(tmp_path)
    due_state.reconcile(tmp_path, now=NOW)

    delivered = _deliver(tmp_path, monkeypatch, "maximal")

    assert delivered is not None
    assert delivered["disposition"] == "file"
    assert "without asking" in delivered["instruction"]
    call = delivered["record_memory"]
    assert call["action"] == "append"
    assert call["collection"] == INCIDENTS
    assert call["item"] == {
        "incident": "Widget panel froze on load 7",
        "observed_on": "2026-09-27",
        "project": "widget-app",
        "tags": ["widget-app", "failure"],
        "sources": ["exomem://memory/61111111-1111-4111-8111-000000000007"],
    }
    assert call["why"]


def test_balanced_asks_one_domain_question_once_per_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_incidents(tmp_path)
    due_state.reconcile(tmp_path, now=NOW)

    first = _deliver(tmp_path, monkeypatch, "balanced")
    again = _deliver(tmp_path, monkeypatch, "balanced")

    assert first is not None and first["disposition"] == "ask"
    question = first["question"]
    assert question.endswith("?") and "Widget incidents" in question
    for jargon in ("Records", "collection", "record_memory", "Exomem", "routing"):
        assert jargon not in question
    assert again is not None and again["disposition"] == "hold" and "question" not in again


@pytest.mark.parametrize("level", ["light", "off"])
def test_light_and_off_hold_for_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    _write_incidents(tmp_path)
    due_state.reconcile(tmp_path, now=NOW)

    delivered = _deliver(tmp_path, monkeypatch, level)

    assert delivered is not None and delivered["disposition"] == "hold"
    assert "record_memory" not in delivered and "question" not in delivered


def test_a_recurrence_appends_an_occurrence_to_the_existing_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_incidents(tmp_path)
    (manifest_path.parent / "Entries" / "panel-froze.md").write_text(
        "---\n"
        "record_id: rec-7\n"
        "incident: Widget panel froze on load\n"
        "observed_on: 2026-08-01\n"
        "sources: []\n"
        "---\n",
        encoding="utf-8",
    )
    due_state.reconcile(tmp_path, now=NOW)
    state = _routed_state(tmp_path, title="Widget panel froze on load")

    delivered = _deliver(tmp_path, monkeypatch, "maximal", state=state)

    assert delivered is not None
    assert delivered["disposition"] == "append_occurrence"
    call = delivered["record_memory"]
    assert call["action"] == "update"
    assert call["item_key"] == "Widget panel froze on load"
    assert call["add_sources"] == ["exomem://memory/61111111-1111-4111-8111-000000000007"]


def test_moderate_routes_keep_the_plain_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_incidents(tmp_path, claims=_INCIDENT_CLAIMS)
    due_state.reconcile(tmp_path, now=NOW)
    state = _routed_state(tmp_path)
    state.frontmatter = {"type": "decision", "tags": ["dogfood", "regressions"]}
    state.document = SimpleNamespace(units=())

    delivered = _deliver(tmp_path, monkeypatch, "maximal", state=state)

    assert delivered is not None and delivered["strength"] == "moderate"
    assert "disposition" not in delivered


# --- Part 5: collection_candidate noise budget -----------------------------

_GENERIC = ("result", "technique", "timing", "evidence", "method", "constraint", "workflow")


def _generic_rows(count: int = 300) -> list[dict[str, object]]:
    start = dt.date(2026, 3, 1)
    return [
        {
            "page": f"Knowledge Base/Notes/Insights/general-{index % 60:02d}.md",
            "unit_ref": f"exomem://memory/71111111-1111-4111-8111-{index:012d}#u",
            "terms": [_GENERIC[index % 7], _GENERIC[(index + 2) % 7], _GENERIC[(index + 4) % 7]],
            "date": start + dt.timedelta(days=index % 90),
            "text": f"The approach changed on 2026-03-{1 + index % 28:02d}",
        }
        for index in range(count)
    ]


def _domain_rows() -> list[dict[str, object]]:
    start = dt.date(2026, 4, 1)
    return [
        {
            "page": f"Knowledge Base/Notes/Insights/lease-{index}.md",
            "unit_ref": f"exomem://memory/81111111-1111-4111-8111-{index:012d}#event",
            "terms": ["boat-mooring", "harbour-north", "berth-twelve"],
            "date": start + dt.timedelta(days=7 * index),
            "text": text,
        }
        for index, text in enumerate(
            [
                "Paid the mooring fee of $140",
                "Renewed the mooring for spring",
                "Cancelled the winter berth",
                "Refunded $30 on 2026-04-22",
            ]
        )
    ]


def test_generic_vocabulary_yields_no_strong_candidates() -> None:
    before_budget = [
        item for item in collection_candidate.detect(_generic_rows()) if item.strength == "strong"
    ]

    assert before_budget == []
    assert collection_candidate.detect(_generic_rows()) == []


def test_a_real_recurring_domain_still_yields_its_one_candidate(tmp_path: Path) -> None:
    rows = _generic_rows() + _domain_rows()

    selected = collection_candidate.select(collection_candidate.detect(rows), rows)

    assert [item.term for item in selected] == ["boat-mooring"]


def test_candidate_constants_are_provisional_and_bounded() -> None:
    source = Path(collection_candidate.__file__).read_text(encoding="utf-8")
    for name in ("MAX_TERM_UNIT_SHARE", "UNIT_SHARE_MIN_POPULATION", "MAX_SERVED_CANDIDATES"):
        assert f"{name} = " in source
        line = next(row for row in source.splitlines() if row.startswith(f"{name} = "))
        assert "PROVISIONAL" in line
    assert 0 < collection_candidate.MAX_TERM_UNIT_SHARE < 1
    assert collection_candidate.MAX_SERVED_CANDIDATES >= 1


def test_candidate_population_survives_audience_recomposition() -> None:
    rows = _generic_rows() + _domain_rows()
    domain_only = [row for row in rows if "boat-mooring" in row["terms"]]

    full = collection_candidate.detect(rows, terms=("boat-mooring",))
    recomposed = collection_candidate.detect(
        domain_only, terms=("boat-mooring",), population=len(rows)
    )

    assert [item.term for item in full] == [item.term for item in recomposed] == ["boat-mooring"]


def test_served_candidates_are_capped_per_response(tmp_path: Path) -> None:
    notes = tmp_path / "Knowledge Base/Notes/Insights"
    notes.mkdir(parents=True)
    domains = [f"domain-{letter}" for letter in "abcdefgh"]
    for d_index, domain in enumerate(domains):
        for index, (day, text) in enumerate(
            [
                ("2026-08-01", "Purchased a licence for $120"),
                ("2026-08-08", "Renewed the licence"),
                ("2026-08-15", "Cancelled the licence"),
                ("2026-08-22", "Refunded $40"),
            ],
            start=1,
        ):
            (notes / f"{domain}-{index}.md").write_text(
                "---\n"
                "type: insight\n"
                f"exomem_id: 9{d_index}111111-1111-4111-8111-00000000000{index}\n"
                f"title: {domain} event {index}\n"
                f"created: {day}\n"
                f"updated: {day}\n"
                "status: active\n"
                f"tags: [{domain}]\n"
                "sources: []\n"
                "---\n\n"
                "## Observations\n\n"
                f"- [purchase] {text} #{domain} #vendor-{domain} #site-{domain} ^event\n",
                encoding="utf-8",
            )
    due_state.reconcile(tmp_path, now=NOW)

    served = [
        row
        for row in due_state.served_entries(tmp_path, now=NOW)
        if row["category"] == "collection_candidate"
    ]

    assert 0 < len(served) <= collection_candidate.MAX_SERVED_CANDIDATES
