from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    audit,
    due_state,
    mutation_terminal,
    record_formats,
    record_governance,
    semantic_writes,
)
from exomem import (
    structured_collections as collections,
)
from exomem.collection_claims import RoutingTarget, route

MANIFEST_PATH = "Knowledge Base/Records/Accounts/_collection.md"


def _manifest(*, claims: str = "", lifecycle: str = "active") -> str:
    return f"""---
type: collection
exomem_id: 11111111-1111-4111-8111-111111111111
title: Account status ledger
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: {lifecycle}
storage:
  strategy: markdown-items
  source: Entries
  format_version: 1
item_schema:
  natural_key: [account, effective_on]
  fields:
    account:
      type: string
      required: true
    effective_on:
      type: date
      required: true
    status:
      type: enum
      enum: [active, cancelled, refunded]
    provider:
      type: string
    note:
      type: string
    tags:
      type: array
      items:
        type: string
    sources:
      type: array
      items:
        type: link
{claims}---

Observed account state.
"""


def _write_manifest(tmp_path: Path, *, claims: str = "", lifecycle: str = "active") -> Path:
    path = tmp_path / MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_manifest(claims=claims, lifecycle=lifecycle), encoding="utf-8")
    (path.parent / "Entries").mkdir()
    return path


def test_declared_claims_round_trip_through_manifest_inspection_and_describe(
    tmp_path: Path,
) -> None:
    path = _write_manifest(
        tmp_path,
        claims=(
            "claims:\n"
            "  tags: [Accounts, recurring-state]\n"
            "  terms: [Provider Alpha]\n"
            "  entity_types: [service-account]\n"
            "  evidence_kinds: [receipt]\n"
        ),
    )

    manifest = collections.load_manifest(tmp_path, path)
    inspected = record_governance.inspect_collection(tmp_path, manifest)
    described = collections.manifest_authoring_contract()

    expected = {
        "tags": ["Accounts", "recurring-state"],
        "terms": ["Provider Alpha"],
        "entity_types": ["service-account"],
        "evidence_kinds": ["receipt"],
    }
    assert {name: list(values) for name, values in manifest.claims.items()} == expected
    assert inspected["contract"]["claims"] == expected
    assert described["json_schema"]["properties"]["claims"] == {
        "type": "object",
        "properties": {
            name: {
                "type": "array",
                "maxItems": 24,
                "items": {"type": "string"},
            }
            for name in ("tags", "terms", "entity_types", "evidence_kinds")
        },
        "additionalProperties": False,
    }
    assert described["claims"]["maximum_items_per_list"] == 24


@pytest.mark.parametrize(
    ("claims", "list_name", "offending"),
    [
        (
            "claims:\n  terms: ["
            + ", ".join(f"term-{index}" for index in range(25))
            + "]\n",
            "terms",
            "term-24",
        ),
        ("claims:\n  evidence_kinds: [receipt, 7]\n", "evidence_kinds", "7"),
    ],
)
def test_malformed_claims_name_the_list_and_offending_entry(
    tmp_path: Path, claims: str, list_name: str, offending: str
) -> None:
    path = _write_manifest(tmp_path, claims=claims)

    with pytest.raises(collections.CollectionError) as raised:
        collections.load_manifest(tmp_path, path)

    assert raised.value.code == "INVALID_COLLECTION_CLAIMS"
    assert list_name in raised.value.reason
    assert offending in raised.value.reason


def test_manifest_without_claims_keeps_the_existing_shape(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path)

    manifest = collections.load_manifest(tmp_path, path)
    inspected = record_governance.inspect_collection(tmp_path, manifest)

    assert manifest.claims is None
    assert "claims" not in inspected["contract"]


def test_effective_claims_unions_declared_and_recurring_derived_values(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        claims="claims:\n  terms: [Account State]\n  evidence_kinds: [billing receipt]\n",
    )
    manifest = collections.load_manifest(tmp_path, path)
    observed = {
        "status": {"active": 2, "cancelled": 2},
        "provider": {"Provider Alpha": 3, "Provider Beta": 1},
        "note": {f"unique note {index}": 1 for index in range(13)},
        "tags": {"subscriptions": 2, "other": 4, "snapshot": 3},
    }

    assert record_governance.effective_claims(manifest, observed) == frozenset(
        {
            "account",
            "state",
            "billing",
            "receipt",
            "active",
            "cancelled",
            "provider",
            "alpha",
            "subscriptions",
        }
    )


def test_effective_claims_never_use_clipped_or_incomplete_display_summaries(
    tmp_path: Path,
) -> None:
    path = _write_manifest(tmp_path)
    manifest = collections.load_manifest(tmp_path, path)
    display_summary = {
        "provider": {
            "values": [
                {"value": "A" * 120, "count": 2, "value_truncated": True},
                {"value": "Provider Alpha", "count": 2, "value_truncated": False},
            ],
            "truncated": True,
        }
    }

    assert record_governance.effective_claims(manifest, display_summary) == frozenset()


def test_routing_target_requires_two_terms_and_active_lifecycle(tmp_path: Path) -> None:
    active = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [subscriptions, accounts]\n"),
    )
    inactive_path = tmp_path / "Knowledge Base/Records/Inactive/_collection.md"
    inactive_path.parent.mkdir(parents=True)
    inactive_path.write_text(
        _manifest(
            claims="claims:\n  terms: [subscriptions, accounts]\n", lifecycle="archived"
        ).replace("source: Entries", "source: Knowledge Base/Records/Inactive/Entries"),
        encoding="utf-8",
    )
    inactive = collections.load_manifest(tmp_path, inactive_path)

    assert record_governance.is_routing_target(active, frozenset({"subscriptions", "accounts"}))
    assert not record_governance.is_routing_target(active, frozenset({"subscriptions"}))
    assert not record_governance.is_routing_target(
        inactive, frozenset({"subscriptions", "accounts"})
    )


def _target(
    collection: str,
    claims: set[str],
    *,
    natural_key_types: tuple[str, ...] = ("string", "date"),
    natural_key_values: frozenset[str] = frozenset(),
) -> RoutingTarget:
    return RoutingTarget(
        collection=collection,
        title=collection.rsplit("/", 2)[-2],
        claims=frozenset(claims),
        natural_key=("account", "effective_on"),
        natural_key_types=natural_key_types,
        natural_key_values=natural_key_values,
    )


def test_route_returns_only_a_strict_winner_with_bounded_sorted_terms() -> None:
    winner = _target(
        "Knowledge Base/Records/Accounts/_collection.md",
        {"accounts", "subscriptions", "billing", "provider", "active", "monthly", "renewal"},
    )
    runner_up = _target(
        "Knowledge Base/Records/Payments/_collection.md", {"billing", "provider"}
    )

    advisory = route(
        ["renewal", "provider", "billing", "monthly", "accounts", "active", "subscriptions"],
        [runner_up, winner],
    )

    assert advisory == {
        "collection": winner.collection,
        "title": "Accounts",
        "matched_terms": ["accounts", "active", "billing", "monthly", "provider", "renewal"],
        "natural_key": ["account", "effective_on"],
        "strength": "moderate",
    }


def test_route_stays_silent_on_a_tie_or_miss() -> None:
    left = _target("Knowledge Base/Records/Left/_collection.md", {"billing", "accounts"})
    right = _target("Knowledge Base/Records/Right/_collection.md", {"billing", "accounts"})

    assert route(["billing", "accounts"], [left, right]) is None
    assert route(["billing"], [left]) is None


def test_claim_routing_normalizes_nfkc_for_authored_and_declared_terms(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(
            tmp_path,
            claims="claims:\n  terms: [ｓｔｕｄｉｏ, ｂｕｌｌｅｔｉｎ]\n",
        ),
    )
    claims = record_governance.effective_claims(manifest, None)
    derived = record_governance.effective_claims(
        manifest, {"provider": {"Ｐｒｏｖｉｄｅｒ Alpha": 2}}
    )
    target = _target(
        "Knowledge Base/Records/Studio/_collection.md",
        {"studio", "bulletin"},
    )

    assert claims == frozenset({"studio", "bulletin"})
    assert {"provider", "alpha"} <= derived
    assert route(["ＳＴＵＤＩＯ", "ＢＵＬＬＥＴＩＮ"], [target]) == {
        "collection": target.collection,
        "title": "Studio",
        "matched_terms": ["bulletin", "studio"],
        "natural_key": ["account", "effective_on"],
        "strength": "moderate",
    }


@pytest.mark.parametrize(
    ("terms", "target", "strength"),
    [
        (
            ["billing", "accounts", "2026-09-10"],
            _target("Knowledge Base/Records/Accounts/_collection.md", {"billing", "accounts"}),
            "strong",
        ),
        (
            ["billing", "accounts", "2026-09-10T12:00:00Z"],
            _target(
                "Knowledge Base/Records/Accounts/_collection.md",
                {"billing", "accounts"},
                natural_key_types=("datetime",),
            ),
            "strong",
        ),
        (
            ["billing", "accounts", "account-alpha"],
            _target(
                "Knowledge Base/Records/Accounts/_collection.md",
                {"billing", "accounts"},
                natural_key_values=frozenset({"account-alpha"}),
            ),
            "strong",
        ),
        (
            ["billing", "accounts", "new account"],
            _target("Knowledge Base/Records/Accounts/_collection.md", {"billing", "accounts"}),
            "moderate",
        ),
    ],
)
def test_route_strength_requires_a_natural_key_shaped_value(
    terms: list[str], target: RoutingTarget, strength: str
) -> None:
    assert route(terms, [target])["strength"] == strength


def _write_item(
    tmp_path: Path,
    *,
    record_id: str,
    account: str,
    effective_on: str,
    status: str,
    provider: str,
    tags: list[str],
    sources: list[str] | None = None,
) -> str:
    relative = f"Knowledge Base/Records/Accounts/Entries/{record_id}.md"
    path = tmp_path / relative
    path.write_text(
        "---\n"
        "type: record\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        f"record_id: {record_id}\n"
        "schema_version: 1\n"
        f"account: {account}\n"
        f"effective_on: {effective_on}\n"
        f"status: {status}\n"
        f"provider: {provider}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"sources: [{', '.join(sources or [])}]\n"
        "---\n\nObserved state.\n",
        encoding="utf-8",
    )
    return relative


def test_claims_projection_reconcile_equals_bounded_record_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account state]\n"),
    )
    _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222221",
        account="account-alpha",
        effective_on="2026-09-01",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
    )
    _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222222",
        account="account-beta",
        effective_on="2026-09-02",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
    )
    due_state.reconcile(tmp_path)
    baseline = due_state.routing_targets(tmp_path)[0]
    assert {"active", "provider", "alpha", "subscriptions"} <= baseline.claims

    first_path = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222223",
        account="account-gamma",
        effective_on="2026-09-03",
        status="cancelled",
        provider="Provider Beta",
        tags=["licences"],
    )
    second_path = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222224",
        account="account-delta",
        effective_on="2026-09-04",
        status="cancelled",
        provider="Provider Beta",
        tags=["licences"],
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("record delta rescanned or rediscovered a collection")

    monkeypatch.setattr("exomem.record_formats.load_adapter", forbidden)
    monkeypatch.setattr("exomem.structured_collections.discover_collections", forbidden)
    monkeypatch.setattr("exomem.structured_collections.discover_collections_with_errors", forbidden)
    due_state.apply_record_write_delta(
        tmp_path,
        manifest,
        path=first_path,
        key="22222222-2222-4222-8222-222222222223",
        values={
            "account": "account-gamma",
            "effective_on": "2026-09-03",
            "status": "cancelled",
            "provider": "Provider Beta",
            "tags": ["licences"],
        },
    )
    assert "cancelled" not in due_state.routing_targets(tmp_path)[0].claims
    due_state.apply_record_write_delta(
        tmp_path,
        manifest,
        path=second_path,
        key="22222222-2222-4222-8222-222222222224",
        values={
            "account": "account-delta",
            "effective_on": "2026-09-04",
            "status": "cancelled",
            "provider": "Provider Beta",
            "tags": ["licences"],
        },
    )
    folded = due_state.routing_targets(tmp_path)[0]
    assert {"cancelled", "provider", "beta", "licences"} <= folded.claims

    monkeypatch.undo()
    reconciled = due_state.reconcile(tmp_path)
    rebuilt = due_state.routing_targets(tmp_path, payload=reconciled)[0]
    assert folded == rebuilt


def test_routing_targets_filter_the_manifest_before_disclosing_claims(tmp_path: Path) -> None:
    _write_manifest(tmp_path, claims="claims:\n  terms: [private account]\n")
    payload = due_state.reconcile(tmp_path)

    assert due_state.routing_targets(tmp_path, payload=payload)
    assert due_state.routing_targets(
        tmp_path, payload=payload, authorize_path=lambda path: path != MANIFEST_PATH
    ) == []


def test_incomplete_claims_census_keeps_declared_terms_but_suppresses_derived(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(
            tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"
        ),
    )
    payload = {
        "claims": {
            manifest.path: {
                "collection_id": str(manifest.collection_id),
                "title": manifest.title,
                "manifest_hash": manifest.manifest_version.hash,
                "complete": False,
                "items": [
                    {
                        "path": f"Knowledge Base/Records/Accounts/Entries/{index}.md",
                        "key": str(index),
                        "values": {"provider": "Provider Alpha"},
                    }
                    for index in range(2)
                ],
            }
        }
    }

    target = due_state.routing_targets(tmp_path, payload=payload)[0]

    assert {"account", "subscriptions"} <= target.claims
    assert "provider" not in target.claims
    assert "alpha" not in target.claims


def test_one_item_cannot_establish_recurrence_with_duplicate_tag_aliases(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, state]\n"),
    )
    payload = {
        "claims": {
            manifest.path: {
                "collection_id": str(manifest.collection_id),
                "title": manifest.title,
                "manifest_hash": manifest.manifest_version.hash,
                "complete": True,
                "items": [
                    {
                        "path": "Knowledge Base/Records/Accounts/Entries/one.md",
                        "key": "one",
                        "values": {
                            "tags": [
                                "studio-licence",
                                "studio-licence",
                                "Studio Licence",
                            ]
                        },
                    }
                ],
            }
        }
    }

    target = due_state.routing_targets(tmp_path, payload=payload)[0]

    assert "studio" not in target.claims
    assert "licence" not in target.claims


def test_compiled_page_routing_uses_title_page_tags_and_unit_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(
        "Knowledge Base/Records/Accounts/_collection.md", {"subscriptions", "account", "alpha"}
    )
    state = SimpleNamespace(
        title="Account update",
        frontmatter={"tags": ["subscriptions"]},
        document=SimpleNamespace(
            units=(SimpleNamespace(tags=("account-alpha", "billing")),)
        ),
    )
    monkeypatch.setattr(due_state, "routing_targets", lambda *_args, **_kwargs: [target])

    advisory = semantic_writes._records_routing(tmp_path, state)

    assert advisory is not None
    assert advisory["collection"] == target.collection
    assert advisory["matched_terms"] == ["account", "alpha", "subscriptions"]


def test_routing_analysis_failure_is_absent_and_never_becomes_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = SimpleNamespace(title="Account update", frontmatter={}, document=None)

    def raising(*args: object, **kwargs: object) -> None:
        raise RuntimeError("routing unavailable")

    monkeypatch.setattr(due_state, "routing_targets", raising)

    assert semantic_writes._records_routing(tmp_path, state) is None


def test_mutation_terminal_projects_one_valid_records_routing_without_changing_terminal() -> None:
    raw = {
        "path": "Knowledge Base/Notes/Insights/account-update.md",
        "warnings": [],
        "records_routing": {
            "collection": MANIFEST_PATH,
            "title": "Account status ledger",
            "matched_terms": ["account", "subscriptions"],
            "natural_key": ["account", "effective_on"],
            "strength": "strong",
        },
    }
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="33333333-3333-4333-8333-333333333333",
        receipt_id="receipt-claims",
        idempotency_key="claims-key",
    )

    compact = mutation_terminal.project_terminal(terminal)

    assert compact["records_routing"] == raw["records_routing"]
    assert compact["state"] == "committed"
    assert compact["mutated"] is True
    assert compact["path"] == raw["path"]
    assert compact["warnings_count"] == 0


@pytest.mark.parametrize(
    "invalid",
    [
        {"collection": MANIFEST_PATH, "title": "Ledger"},
        {
            "collection": MANIFEST_PATH,
            "title": "Ledger",
            "matched_terms": [f"term-{index}" for index in range(7)],
            "natural_key": ["account"],
            "strength": "strong",
        },
        {
            "collection": "Knowledge Base/Records/Hidden/item.md",
            "title": "Ledger",
            "matched_terms": ["account", "subscriptions"],
            "natural_key": ["account"],
            "strength": "numeric",
        },
        {
            "collection": MANIFEST_PATH,
            "title": "Ledger",
            "matched_terms": ["account", "subscriptions"],
            "natural_key": ["account"],
            "strength": ["strong"],
        },
    ],
)
def test_mutation_terminal_drops_malformed_records_routing(invalid: dict[str, object]) -> None:
    terminal = mutation_terminal.committed_terminal(
        {"path": "Knowledge Base/Notes/Insights/account-update.md", "records_routing": invalid},
        request_id="33333333-3333-4333-8333-333333333333",
        receipt_id=None,
        idempotency_key=None,
    )

    assert "records_routing" not in mutation_terminal.project_terminal(terminal)


def test_evidence_preserve_routes_sidecar_title_tags_and_description_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands

    seen: list[str] = []

    def route(_root: Path, terms: list[str]) -> dict[str, object]:
        seen.extend(terms)
        return {
            "collection": MANIFEST_PATH,
            "title": "Warranty ledger",
            "matched_terms": ["receipt", "warranty"],
            "natural_key": ["item", "purchased_on"],
            "strength": "moderate",
        }

    monkeypatch.setattr(semantic_writes, "_records_routing_from_terms", route)

    result = commands.op_preserve(
        tmp_path,
        scope="Home Warranty",
        category="Receipts",
        filename="vacuum.txt",
        content="raw receipt",
        description="Receipt for the vacuum warranty",
    )

    assert result["records_routing"]["collection"] == MANIFEST_PATH
    assert seen == [
        "Evidence: vacuum.txt",
        "evidence",
        "home-warranty",
        "receipts",
        "Receipt for the vacuum warranty",
    ]
    assert (tmp_path / result["sidecar_path"]).exists()


@pytest.mark.parametrize("disposition", ["quiet", "off"])
def test_observation_disposition_suppresses_compiled_and_evidence_routing_delivery(
    tmp_path: Path, disposition: str
) -> None:
    from exomem import commands, review_state

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    due_state.reconcile(tmp_path)
    review_state.ReviewStateStore(tmp_path).set_disposition(
        "unreflected_observations",
        disposition,
        why="intentional: routing is understood",
    )

    compiled = commands.op_note(
        tmp_path,
        content=(
            "## Observations\n\n"
            "- [account] Subscription state changed #subscriptions ^quiet-route\n"
        ),
        note_type="insight",
        title="Account subscription update",
        sources=[],
        tags=["account", "subscriptions"],
        status="active",
    )
    evidence = commands.op_preserve_evidence(
        tmp_path,
        scope="Account subscriptions",
        category="Receipts",
        filename="quiet-account.txt",
        content="Account subscription state changed.",
        description="Account subscription receipt",
    )

    assert "records_routing" not in compiled
    assert "records_routing" not in evidence
    tracked = due_state.load(tmp_path)["categories"]["unreflected_observations"]
    assert compiled["path"] in tracked
    assert evidence["sidecar_path"] in tracked


def _write_observation(tmp_path: Path) -> tuple[str, str]:
    relative = "Knowledge Base/Notes/Insights/account-observation.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    page_ref = "exomem://memory/44444444-4444-4444-8444-444444444444"
    path.write_text(
        "---\n"
        "type: insight\n"
        "exomem_id: 44444444-4444-4444-8444-444444444444\n"
        "title: Account subscription changed\n"
        "created: 2026-09-01\n"
        "updated: 2026-09-01\n"
        "status: active\n"
        "tags: [account, subscriptions]\n"
        "sources: []\n"
        "---\n\n"
        "## Observations\n\n"
        "- [account] Subscription cancelled #subscriptions ^change\n",
        encoding="utf-8",
    )
    return relative, page_ref


def test_observation_delta_opens_pending_then_serves_after_exact_grace(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    due_state.reconcile(tmp_path, today=dt.date(2026, 9, 1))
    observed_at = dt.datetime(2026, 9, 1, 15, 30, tzinfo=dt.UTC)
    routing = {
        "collection": manifest.path,
        "title": manifest.title,
        "matched_terms": ["account", "subscriptions"],
        "natural_key": list(manifest.schema.natural_key),
        "strength": "moderate",
    }

    due_state.apply_observation_write_delta(
        tmp_path,
        path=path,
        observation_ref=page_ref,
        terms=["account", "subscriptions", "cancelled"],
        routing=routing,
        observed_at=observed_at,
    )

    stored = due_state.load(tmp_path)["categories"]["unreflected_observations"][path]
    assert stored["open"] == []
    assert stored["pending"][0]["due_at"] == "2026-09-02T15:30:00+00:00"
    assert due_state.served_entries(
        tmp_path, now=observed_at + dt.timedelta(hours=23, minutes=59)
    ) == []
    served = due_state.served_entries(
        tmp_path, now=observed_at + dt.timedelta(hours=24)
    )
    assert [row["category"] for row in served] == ["unreflected_observations"]
    component = stored["pending"][0]["component"]
    assert component["collection_id"] == str(manifest.collection_id)
    assert component["observation_ref"] == page_ref


def test_observation_fingerprint_changes_when_matched_terms_grow() -> None:
    base = {
        "collection": MANIFEST_PATH,
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "collection_title": "Account status ledger",
        "page_path": "Knowledge Base/Notes/Insights/account.md",
        "observation_ref": "exomem://memory/44444444-4444-4444-8444-444444444444",
        "terms": ["account", "subscriptions", "cancelled"],
        "observed_at": "2026-09-01T15:30:00+00:00",
    }
    first = audit.unreflected_observation_component(
        base, ["account", "subscriptions"]
    )
    second = audit.unreflected_observation_component(
        base, ["account", "cancelled", "subscriptions"]
    )

    assert first.meta["signal_version"] != second.meta["signal_version"]
    assert first.meta["review_partition"] == base["collection_id"]
    assert second.component["matched_terms"] == ["account", "cancelled", "subscriptions"]


@pytest.mark.parametrize("settlement", ["link", "natural_key", "nfkc_natural_key"])
def test_record_delta_settles_only_its_claimed_observation_family(
    tmp_path: Path, settlement: str
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, today=now.date(), now=now)
    assert due_state.load(tmp_path)["categories"]["unreflected_observations"][path][
        "open"
    ]
    account = {
        "link": "account-alpha",
        "natural_key": "account",
        "nfkc_natural_key": "ａｃｃｏｕｎｔ",
    }[settlement]
    values = {
        "account": account,
        "effective_on": "2026-09-03",
        "status": "active",
        "provider": "Provider Alpha",
        "tags": ["subscriptions"],
        "sources": [page_ref] if settlement == "link" else [],
    }
    record_path = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222229",
        account=account,
        effective_on="2026-09-03",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
        sources=[page_ref] if settlement == "link" else [],
    )

    due_state.apply_record_write_delta(
        tmp_path,
        manifest,
        path=record_path,
        key="22222222-2222-4222-8222-222222222229",
        values=values,
        today=now.date(),
    )

    internal = due_state.load(tmp_path)["categories"]["unreflected_observations"][
        path
    ]["open"][0]
    expected_support: dict[str, object] = (
        {"support": "link"}
        if settlement == "link"
        else {"support": "natural_key", "terms": ["account"]}
    )
    assert internal["component"]["reflecting_records"] == [
        {
            "path": record_path,
            "key": "22222222-2222-4222-8222-222222222229",
            **expected_support,
        }
    ]
    assert not [
        row
        for row in due_state.served_entries(tmp_path, now=now)
        if row["category"] == "unreflected_observations"
    ]


def test_record_delta_does_not_settle_on_one_natural_key_token(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    path, _page_ref = _write_observation(tmp_path)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=now)

    due_state.apply_record_write_delta(
        tmp_path,
        manifest,
        path="Knowledge Base/Records/Accounts/Entries/unrelated.md",
        key="22222222-2222-4222-8222-222222222228",
        values={
            "account": "account-unrelated",
            "effective_on": "2026-09-03",
            "status": "active",
            "provider": "Provider Beta",
            "tags": [],
            "sources": [],
        },
        today=now.date(),
    )

    assert due_state.load(tmp_path)["categories"]["unreflected_observations"][path][
        "open"
    ]


def test_reconcile_uses_latest_observation_timestamp_for_lookback_and_grace(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    path, _page_ref = _write_observation(tmp_path)
    page = tmp_path / path
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace("created: 2026-09-01", "created: 2025-01-01")
        .replace("updated: 2026-09-01", "updated: 2026-09-12T11:00:00Z"),
        encoding="utf-8",
    )
    now = dt.datetime(2026, 9, 12, 12, tzinfo=dt.UTC)

    projection = due_state.reconcile(tmp_path, now=now)

    bucket = projection["categories"]["unreflected_observations"][path]
    assert bucket["open"] == []
    assert bucket["pending"][0]["due_at"] == "2026-09-13T11:00:00+00:00"


def test_attention_keeps_observations_quiet_until_exact_grace(
    tmp_path: Path,
) -> None:
    from exomem import attention

    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    path, _page_ref = _write_observation(tmp_path)
    page = tmp_path / path
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace("created: 2026-09-01", "created: 2026-09-12T11:00:00Z")
        .replace("updated: 2026-09-01", "updated: 2026-09-12T11:00:00Z"),
        encoding="utf-8",
    )

    pending = attention.attention(
        tmp_path,
        categories=["unreflected_observations"],
        now=dt.datetime(2026, 9, 13, 10, 59, tzinfo=dt.UTC),
        record_surfacing=False,
    )
    due = attention.attention(
        tmp_path,
        categories=["unreflected_observations"],
        now=dt.datetime(2026, 9, 13, 11, tzinfo=dt.UTC),
        record_surfacing=False,
    )

    assert pending.items == []
    assert [category for item in due.items for category in item.categories] == [
        "unreflected_observations"
    ]


def test_reconcile_retains_tracked_observation_beyond_discovery_horizon(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    path, _page_ref = _write_observation(tmp_path)
    first_now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=first_now)

    later = due_state.reconcile(tmp_path, now=first_now + dt.timedelta(days=100))

    assert later["categories"]["unreflected_observations"][path]["open"]


def test_reconcile_heals_observation_after_linking_record_is_written(tmp_path: Path) -> None:
    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    path, page_ref = _write_observation(tmp_path)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)

    before = due_state.reconcile(tmp_path, now=now)
    assert before["categories"]["unreflected_observations"][path]["open"]
    _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222229",
        account="account-alpha",
        effective_on="2026-09-03",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
        sources=[page_ref],
    )

    after = due_state.reconcile(tmp_path, now=now)

    entries = after["categories"]["unreflected_observations"][path]["open"]
    assert entries[0]["component"]["reflecting_records"] == [
        {
            "path": (
                "Knowledge Base/Records/Accounts/Entries/"
                "22222222-2222-4222-8222-222222222229.md"
            ),
            "key": "22222222-2222-4222-8222-222222222229",
            "support": "link",
        }
    ]
    assert not [
        row
        for row in due_state.served_entries(tmp_path, now=now)
        if row["category"] == "unreflected_observations"
    ]


def test_observation_rewrite_preserves_link_reflection_without_records_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions, billing]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222229",
        account="account-alpha",
        effective_on="2026-09-03",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
        sources=[page_ref],
    )
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=now)

    monkeypatch.setattr(
        record_formats,
        "load_adapter",
        lambda *_args, **_kwargs: pytest.fail("observation delta rescanned Records"),
    )
    due_state.apply_observation_write_delta(
        tmp_path,
        path=path,
        observation_ref=page_ref,
        terms=["billing", "subscriptions"],
        routing={
            "collection": manifest.path,
            "matched_terms": ["billing", "subscriptions"],
        },
        observed_at=now,
    )

    assert not [
        row
        for row in due_state.served_entries(
            tmp_path, now=now + dt.timedelta(hours=25)
        )
        if row["category"] == "unreflected_observations"
    ]


def test_observation_rewrite_revalidates_key_only_reflection_against_new_terms(
    tmp_path: Path,
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions, billing]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222229",
        account="account",
        effective_on="2026-09-03",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
        sources=[],
    )
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=now)

    due_state.apply_observation_write_delta(
        tmp_path,
        path=path,
        observation_ref=page_ref,
        terms=["billing", "subscriptions"],
        routing={
            "collection": manifest.path,
            "matched_terms": ["billing", "subscriptions"],
        },
        observed_at=now,
    )

    served = due_state.served_entries(
        tmp_path, now=now + dt.timedelta(hours=25)
    )
    assert [
        row["category"] for row in served
        if row["category"] == "unreflected_observations"
    ] == ["unreflected_observations"]


def test_private_reflecting_record_does_not_suppress_visible_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress

    _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n")
    observation_path, page_ref = _write_observation(tmp_path)
    record_path = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222229",
        account="account-alpha",
        effective_on="2026-09-03",
        status="active",
        provider="Provider Alpha",
        tags=["subscriptions"],
        sources=[page_ref],
    )
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=now)
    assert not due_state.served_entries(tmp_path, now=now)

    def keep(path: str) -> bool:
        return path != record_path

    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *_args, **_kwargs: keep
    )
    monkeypatch.setattr(audit, "_release_filter", lambda _root: keep)

    served = due_state.served_entries(tmp_path, now=now)
    scoped_audit = audit.audit(
        tmp_path, categories=["unreflected_observations"], now=now
    )
    coverage = due_state.collection_observation_coverage(
        tmp_path, MANIFEST_PATH, authorize_path=keep, now=now
    )
    assert [row["path"] for row in served] == [observation_path]
    assert [finding.path for finding in scoped_audit.findings] == [observation_path]
    assert coverage["unreflected"] == [page_ref]


@pytest.mark.parametrize("support", ["natural_key", "link"])
def test_scoped_serving_revalidates_reflection_against_visible_derived_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, support: str
) -> None:
    from exomem.governance import egress

    _write_manifest(tmp_path, claims="claims:\n  terms: [billing, subscriptions]\n")
    observation_path, page_ref = _write_observation(tmp_path)
    observation = tmp_path / observation_path
    observation.write_text(
        observation.read_text(encoding="utf-8")
        .replace(
            "tags: [account, subscriptions]",
            "tags: [account, subscriptions, billing]",
        )
        .replace("#subscriptions ^change", "#subscriptions #billing ^change"),
        encoding="utf-8",
    )
    visible_record = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222227",
        account="account",
        effective_on="2026-09-02",
        status="active",
        provider="Provider Alpha",
        tags=["billing", "subscriptions"],
        sources=[page_ref] if support == "link" else [],
    )
    hidden_record = _write_item(
        tmp_path,
        record_id="22222222-2222-4222-8222-222222222228",
        account="account",
        effective_on="2026-09-03",
        status="active",
        provider="Provider Beta",
        tags=["billing", "subscriptions"],
        sources=[],
    )
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    due_state.reconcile(tmp_path, now=now)

    def keep(path: str) -> bool:
        return path != hidden_record

    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *_args, **_kwargs: keep
    )
    monkeypatch.setattr(audit, "_release_filter", lambda _root: keep)

    served = [
        row
        for row in due_state.served_entries(tmp_path, now=now)
        if row["category"] == "unreflected_observations"
    ]
    scoped = audit.audit(
        tmp_path, categories=["unreflected_observations"], now=now
    ).findings

    assert (len(served), len(scoped)) == (
        (1, 1) if support == "natural_key" else (0, 0)
    )
    assert visible_record != hidden_record


def test_observation_family_membership_controls_write_time_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    due_state.reconcile(tmp_path, now=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC))
    monkeypatch.setattr(
        due_state,
        "STRUCTURED_DELTA_CATEGORIES",
        tuple(
            category
            for category in due_state.STRUCTURED_DELTA_CATEGORIES
            if category != "unreflected_observations"
        ),
    )

    assert (
        due_state.apply_observation_write_delta(
            tmp_path,
            path=path,
            observation_ref=page_ref,
            terms=["account", "subscriptions"],
            routing={
                "collection": manifest.path,
                "title": manifest.title,
                "matched_terms": ["account", "subscriptions"],
                "natural_key": list(manifest.schema.natural_key),
                "strength": "moderate",
            },
            observed_at=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
        )
        is None
    )


def _candidate_rows() -> list[dict[str, object]]:
    return [
        {
            "page": f"Knowledge Base/Notes/Insights/studio-{index}.md",
            "unit_ref": f"exomem://memory/55555555-5555-4555-8555-55555555555{index}#event",
            "terms": ["studio-licence", "provider-alpha", "workspace-beta"],
            "date": day,
            "text": text,
        }
        for index, (day, text) in enumerate(
            [
                (dt.date(2026, 8, 1), "Purchased a licence for $120"),
                (dt.date(2026, 8, 8), "Renewed the licence"),
                (dt.date(2026, 8, 15), "Cancelled the licence"),
                (dt.date(2026, 8, 22), "Refunded $40"),
            ],
            start=1,
        )
    ]


def test_collection_candidate_detector_is_deterministic_and_bounded() -> None:
    from exomem import collection_candidate

    rows = _candidate_rows()
    first = collection_candidate.detect(rows)
    second = collection_candidate.detect(list(reversed(rows)))

    assert first == second
    studio = next(item for item in first if item.term == "studio-licence")
    assert studio.strength == "moderate"
    assert studio.domain_terms == (
        "studio-licence",
        "provider-alpha",
        "workspace-beta",
    )
    assert len(studio.evidence_units) == 4
    assert len(studio.domain_terms) <= collection_candidate.MAX_DOMAIN_TERMS
    assert len(studio.evidence_units) <= collection_candidate.MAX_EVIDENCE_UNITS


@pytest.mark.parametrize("mutation", ["single_page", "same_day", "no_state"])
def test_collection_candidate_detector_keeps_threshold_twins_quiet(mutation: str) -> None:
    from exomem import collection_candidate

    rows = _candidate_rows()
    if mutation == "single_page":
        rows = [{**row, "page": "Knowledge Base/Notes/Insights/one.md"} for row in rows]
    elif mutation == "same_day":
        rows = [{**row, "date": dt.date(2026, 8, 1)} for row in rows]
    else:
        rows = [{**row, "text": "A durable contextual note"} for row in rows]

    assert collection_candidate.detect(rows) == []


def test_collection_candidate_detector_excludes_already_claimed_term() -> None:
    from exomem import collection_candidate

    findings = collection_candidate.detect(
        _candidate_rows(), covered_terms={"studio-licence"}
    )

    assert all(item.term != "studio-licence" for item in findings)


def _write_candidate_pages(tmp_path: Path) -> None:
    notes = tmp_path / "Knowledge Base/Notes/Insights"
    notes.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(_candidate_rows(), start=1):
        (notes / f"studio-{index}.md").write_text(
            "---\n"
            "type: insight\n"
            f"exomem_id: 55555555-5555-4555-8555-55555555555{index}\n"
            f"title: Studio licence event {index}\n"
            f"created: {row['date'].isoformat()}\n"
            f"updated: {row['date'].isoformat()}\n"
            "status: active\n"
            "tags: [studio-licence]\n"
            "sources: []\n"
            "---\n\n"
            "## Observations\n\n"
            f"- [purchase] {row['text']} #studio-licence #provider-alpha "
            f"#workspace-beta ^event\n",
            encoding="utf-8",
        )


def test_candidate_audit_projects_subject_metadata_and_resolves_by_claims(
    tmp_path: Path,
) -> None:
    _write_candidate_pages(tmp_path)

    report = audit.audit(tmp_path, categories=["collection_candidate"])
    studio = next(
        finding
        for finding in report.findings
        if finding.meta["review_partition"] == "studio-licence"
    )
    assert studio.meta["domain_terms"][:1] == ["studio-licence"]
    assert len(studio.meta["evidence_units"]) == 4
    projection = due_state.reconcile(tmp_path)
    entries = [
        entry
        for bucket in projection["categories"]["collection_candidate"].values()
        for entry in bucket["open"]
    ]
    projected = next(
        entry for entry in entries if entry["component"]["term"] == "studio-licence"
    )
    assert projected["meta"]["domain_terms"] == studio.meta["domain_terms"]
    assert projected["component"]["evidence_units"] == studio.meta["evidence_units"]

    _write_manifest(tmp_path, claims="claims:\n  terms: [studio licence]\n")

    healed = due_state.reconcile(tmp_path)
    assert all(
        entry["component"]["term"] != "studio-licence"
        for bucket in healed["categories"]["collection_candidate"].values()
        for entry in bucket["open"]
    )


def test_candidate_is_opt_in_for_attention_but_registered_for_due_state(tmp_path: Path) -> None:
    from exomem import attention, review_state

    _write_candidate_pages(tmp_path)

    assert "collection_candidate" not in {
        category
        for item in attention.attention(tmp_path, record_surfacing=False).items
        for category in item.categories
    }
    explicit = attention.attention(
        tmp_path, categories=["collection_candidate"], record_surfacing=False
    )
    assert any("collection_candidate" in item.categories for item in explicit.items)
    assert "collection_candidate" in review_state.registered_families()
    assert "collection_candidate" in due_state.PROJECTION_CATEGORIES
    assert "collection_candidate" not in due_state.DELTA_CATEGORIES


def test_candidate_serve_recomposes_when_representative_page_is_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress

    _write_candidate_pages(tmp_path)
    due_state.reconcile(tmp_path)
    hidden = "Knowledge Base/Notes/Insights/studio-1.md"
    monkeypatch.setattr(
        egress,
        "release_walk_filter",
        lambda *_args, **_kwargs: lambda path: path != hidden,
    )

    filtered = [
        row
        for row in due_state.served_entries(tmp_path)
        if row["category"] == "collection_candidate"
    ]

    monkeypatch.undo()
    (tmp_path / hidden).unlink()
    due_state.reconcile(tmp_path)
    absent = [
        row
        for row in due_state.served_entries(tmp_path)
        if row["category"] == "collection_candidate"
    ]
    assert filtered == absent
    assert filtered
    assert all(row["path"] != hidden for row in filtered)


def test_collection_coverage_reports_unreflected_and_inventory_count(tmp_path: Path) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    _write_observation(tmp_path)
    due_state.reconcile(tmp_path, now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC))

    inspected = record_governance.inspect_collection(tmp_path, manifest)
    inventory = record_governance.inventory_collections(tmp_path)

    assert inspected["coverage"] == {
        "committed": 0,
        "held": 0,
        "unreadable": 0,
        "held_refs": [],
        "unreflected": 1,
        "unreflected_refs": [
            "exomem://memory/44444444-4444-4444-8444-444444444444"
        ],
        "pending": 0,
        "pending_refs": [],
        "state": "partial",
    }
    row = inventory["collections"][0]
    assert row["unreflected"] == 1


def test_collection_coverage_lists_recent_observation_as_pending(tmp_path: Path) -> None:
    from exomem import temporal

    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    path, page_ref = _write_observation(tmp_path)
    stamp = temporal.stamp(temporal.now())
    page = tmp_path / path
    page.write_text(
        page.read_text(encoding="utf-8")
        .replace("created: 2026-09-01", f"created: {stamp}")
        .replace("updated: 2026-09-01", f"updated: {stamp}"),
        encoding="utf-8",
    )
    due_state.reconcile(tmp_path)

    coverage = record_governance.inspect_collection(tmp_path, manifest)["coverage"]

    assert coverage["unreflected"] == 0
    assert coverage["pending"] == 1
    assert coverage["pending_refs"] == [page_ref]
    assert coverage["state"] == "complete"


def test_stale_claims_projection_makes_coverage_unknown(tmp_path: Path) -> None:
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"),
    )
    _write_observation(tmp_path)
    due_state.reconcile(tmp_path, now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC))
    manifest_file = tmp_path / manifest.path
    manifest_file.write_text(
        manifest_file.read_text(encoding="utf-8").replace(
            "Account status ledger", "Renamed account status ledger"
        ),
        encoding="utf-8",
    )

    coverage = record_governance.inspect_collection(
        tmp_path, collections.load_manifest(tmp_path, manifest_file)
    )["coverage"]

    assert coverage["state"] == "unknown"


def test_describe_state_ledger_example_is_valid_and_teaches_backfill(tmp_path: Path) -> None:
    described = collections.manifest_authoring_contract()
    example = described["examples"]["state_ledger"]
    path = tmp_path / example["manifest_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example["manifest_text"], encoding="utf-8")

    manifest = collections.load_manifest(tmp_path, path)

    assert manifest.schema.natural_key == ("identity", "effective_on")
    assert manifest.schema.fields["status"].enum
    assert manifest.schema.fields["observed_precision"].enum == (
        "exact",
        "approximate",
        "inferred",
    )
    assert manifest.schema.fields["sources"].items.type == "link"
    assert "never recorded as exact" in example["backfill_rule"]
    assert "cite the unit or artifact" in example["backfill_rule"]


def test_bootstrap_teaches_routing_candidates_and_collection_confirmation(
    tmp_path: Path,
) -> None:
    from exomem import commands, envelope

    post_write = commands.op_bootstrap(tmp_path, profile="compact")["authoring_contract"][
        "post_write"
    ]
    routing = post_write["records_routing"] + " " + post_write["records_routing_handling"]
    candidate = post_write["collection_candidate"]

    assert "served capture disposition" in routing
    assert "held candidate" in routing
    assert "advisory alone" in routing
    assert "describe" in candidate and "validate" in candidate
    assert "one" in candidate and "confirmation" in candidate
    assert "exactly dated" in candidate and "sources" in candidate
    assert "collection creation" in envelope.CONFIRM_REQUIRED
    assert "collections automatically" in envelope.FOUNDER_GATE


def test_mcp_facing_collection_claims_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(
        tmp_path,
        _write_manifest(
            tmp_path, claims="claims:\n  terms: [account, subscriptions]\n"
        ),
    )
    due_state.reconcile(tmp_path)

    preserved = commands.op_preserve_evidence(
        tmp_path,
        scope="Account subscriptions",
        category="Receipts",
        filename="account-alpha.txt",
        content="Account Alpha subscription was cancelled.",
        description="Account subscription cancellation receipt",
    )
    assert preserved["records_routing"]["collection"] == manifest.path
    inspected = commands.op_record_memory(
        tmp_path, action="inspect", collection=manifest.path
    )
    assert inspected["coverage"]["pending"] == 1

    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=25)
    original_served = due_state.served
    monkeypatch.setattr(
        due_state,
        "served",
        lambda root, **kwargs: due_state.block(
            due_state.served_entries(root, now=future, **kwargs)
        ),
    )
    due_block = commands.op_bootstrap(tmp_path)["due_state"]
    assert due_block["categories"]["unreflected_observations"] == 1
    monkeypatch.setattr(due_state, "served", original_served)

    appended = commands.op_record_memory(
        tmp_path,
        action="append",
        collection=manifest.path,
        item={
            "account": "account-alpha",
            "effective_on": dt.date.today().isoformat(),
            "status": "cancelled",
            "provider": "Provider Alpha",
            "tags": ["subscriptions"],
            "sources": [preserved["path"]],
        },
        why="record the observed cancellation",
    )
    assert appended["operation"] == "append"
    reflected = commands.op_record_memory(
        tmp_path, action="inspect", collection=manifest.path
    )["coverage"]
    assert reflected["unreflected"] == 0
    assert reflected["pending"] == 0
    assert reflected["state"] == "complete"
    due_state.reconcile(tmp_path)
    assert commands.op_record_memory(
        tmp_path, action="inspect", collection=manifest.path
    )["coverage"]["unreflected"] == 0

    _write_candidate_pages(tmp_path)
    due_state.reconcile(tmp_path)
    candidate_block = commands.op_bootstrap(tmp_path)["due_state"]
    assert candidate_block["categories"]["collection_candidate"] >= 1

    candidate_path = "Knowledge Base/Records/Studio Licences/_collection.md"
    candidate_manifest = (
        _manifest(claims="claims:\n  terms: [studio licence]\n")
        .replace(
            "11111111-1111-4111-8111-111111111111",
            "66666666-6666-4666-8666-666666666666",
        )
        .replace("Account status ledger", "Studio licence ledger")
    )
    commands.op_record_memory(
        tmp_path,
        action="create",
        manifest_path=candidate_path,
        manifest_text=candidate_manifest,
        why="create the confirmed studio licence ledger",
    )
    due_state.reconcile(tmp_path)
    resolved = commands.op_bootstrap(tmp_path).get("due_state") or {
        "categories": {}
    }
    assert "collection_candidate" not in resolved["categories"]
