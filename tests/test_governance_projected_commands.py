"""Public find acquisition must stay inside the verified projection namespace."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from governance_projection_support import verified_namespace

from exomem import commands
from exomem.find_types import Hit
from exomem.governance import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    egress,
    principal,
    projected_retrieval,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
    store,
)
from exomem.governance.decisions import Decision
from exomem.governance.policy import Policy, Scope


def test_v4_find_never_acquires_from_the_raw_corpus(monkeypatch, tmp_path) -> None:
    decision = Decision(level=6, scope_ids=("visible",))
    projected_hit = Hit(
        path="Knowledge Base/visible.md",
        type="note",
        scope="kb",
        title="Visible",
        updated="",
        excerpt="projection-only term",
        bm25_rank=1,
        snapshot_hash="a" * 64,
        decision=decision,
    )
    runtime = SimpleNamespace(
        snapshot=SimpleNamespace(
            policy=Policy(
                fingerprint="f" * 64,
                scopes={
                    "visible": Scope(id="visible", source="scopes/visible.yaml")
                },
            )
        )
    )
    result = projection_runtime.ProjectedFindResult(
        hits=(projected_hit,),
        withheld_paths=frozenset(),
    )
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "load_active_projection_runtime",
        lambda _root: runtime,
    )
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "find_projected_hits",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        commands.find_module,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v4 projected retrieval reopened the raw corpus")
        ),
    )
    monkeypatch.setattr(
        commands.query_log,
        "log_find_call",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("v4 projected retrieval persisted a path-bearing query log")
        ),
    )
    monkeypatch.setattr(
        commands.memory_refs_module,
        "ReferenceIndex",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("v4 projected retrieval reopened the reference index")
        ),
    )

    with principal.request_scope(principal.owner_principal(surface="library")):
        response = commands.op_find(
            tmp_path,
            query="projection-only term",
            limit=1,
            scope="vault",
            mode="keyword",
            graph=False,
            rerank=False,
        )

    assert response == [
        {
            "path": "Knowledge Base/visible.md",
            "type": "note",
            "scope": "kb",
            "title": "Visible",
            "updated": "",
            "excerpt": "projection-only term",
            "signals": {"bm25_rank": 1},
        }
    ]


def test_low_projection_never_enters_path_bearing_query_log(monkeypatch, tmp_path) -> None:
    decision = Decision(
        level=2,
        scope_ids=("closed",),
        options={"constraint": "Approved readers only"},
    )
    projected_hit = Hit(
        path="Knowledge Base/hidden.md",
        type=None,
        scope="kb",
        title="hidden",
        updated="",
        excerpt="Approved readers only",
        snapshot_hash="a" * 64,
        decision=decision,
    )
    runtime = SimpleNamespace(
        snapshot=SimpleNamespace(
            policy=Policy(
                fingerprint="f" * 64,
                scopes={
                    "closed": Scope(id="closed", source="scopes/closed.yaml")
                },
            )
        )
    )
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "load_active_projection_runtime",
        lambda _root: runtime,
    )
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "find_projected_hits",
        lambda *_args, **_kwargs: projection_runtime.ProjectedFindResult(
            hits=(projected_hit,),
            withheld_paths=frozenset(),
        ),
    )
    monkeypatch.setattr(
        commands.find_module,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v4 projected retrieval reopened the raw corpus")
        ),
    )
    logged: list[list[Hit]] = []
    monkeypatch.setattr(
        commands.query_log,
        "log_find_call",
        lambda **kwargs: logged.append(kwargs["hits"]),
    )
    monkeypatch.setattr(
        commands.memory_refs_module,
        "ReferenceIndex",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("v4 projected retrieval reopened the reference index")
        ),
    )

    with principal.request_scope(principal.owner_principal(surface="library")):
        response = commands.op_find(
            tmp_path,
            query="Approved",
            limit=1,
            scope="vault",
            mode="keyword",
            graph=False,
            rerank=False,
        )

    assert response == [
        {
            "withheld": True,
            "level": 2,
            "scope_label": "closed",
            "constraint": "Approved readers only",
        }
    ]
    assert logged == []


def test_projected_annotation_uses_bound_hash_without_reopening_item(
    monkeypatch, tmp_path
) -> None:
    policy = Policy(
        fingerprint="f" * 64,
        scopes={"closed": Scope(id="closed", source="scopes/closed.yaml")},
    )
    decision = Decision(
        level=2,
        scope_ids=("closed",),
        options={"constraint": "Approved readers only"},
    )
    hit = Hit(
        path="Knowledge Base/missing-on-disk.md",
        type=None,
        scope="kb",
        title="missing-on-disk",
        updated="",
        excerpt="Approved readers only",
        snapshot_hash="a" * 64,
        decision=decision,
    )
    minted: list[str | None] = []
    outcomes: list[str | None] = []
    monkeypatch.setattr(
        egress,
        "_mint_escalation_quietly",
        lambda *_args, **kwargs: minted.append(
            kwargs.get("expected_content_hash")
        ),
    )
    monkeypatch.setattr(
        egress,
        "_outcome_for_decision",
        lambda *_args, **kwargs: outcomes.append(kwargs.get("content_hash")),
    )
    context = authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="session-1",
        principal_id="owner",
        issuer_family="test",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        keyring_id="keyring-1",
        credential_generation=1,
        expires_at=2_000_000_000,
    )
    who = principal.owner_principal(surface="library").with_verified_authorization_session(
        context,
        issuer_family="test",
    )

    release = egress.annotate_projected_hits(
        tmp_path,
        [hit],
        policy=policy,
        principal=who,
        purpose=None,
        withheld_paths=frozenset(),
    )

    assert release.hits == []
    assert release.notices == [
        {
            "withheld": True,
            "level": 2,
            "scope_label": "closed",
            "constraint": "Approved readers only",
        }
    ]
    assert minted == ["a" * 64]
    assert outcomes == ["a" * 64]


def test_projected_annotation_without_v4_session_never_mints_legacy_token(
    monkeypatch,
    tmp_path,
) -> None:
    policy = Policy(
        fingerprint="f" * 64,
        scopes={"closed": Scope(id="closed", source="scopes/closed.yaml")},
    )
    decision = Decision(
        level=2,
        scope_ids=("closed",),
        options={"constraint": "Approved readers only"},
    )
    hit = Hit(
        path="Knowledge Base/not-present.md",
        type=None,
        scope="kb",
        title="not-present",
        updated="",
        excerpt="Approved readers only",
        snapshot_hash="a" * 64,
        decision=decision,
    )
    monkeypatch.setattr(
        egress,
        "_mint_escalation_quietly",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projected annotation entered legacy token minting")
        ),
    )
    monkeypatch.setattr(
        egress,
        "_declared_purpose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projected annotation re-resolved live purpose state")
        ),
    )
    release = egress.annotate_projected_hits(
        tmp_path,
        [hit],
        policy=policy,
        principal=principal.RequestPrincipal(
            audience_id="principal:ordinary",
            surface="test",
        ),
        purpose=None,
        withheld_paths=frozenset(),
    )

    assert release.hits == []
    assert release.notices == [
        {
            "withheld": True,
            "level": 2,
            "scope_label": "closed",
            "constraint": "Approved readers only",
        }
    ]


def test_projected_find_uses_only_bound_variant_text(monkeypatch, tmp_path) -> None:
    closed = Scope(
        id="closed",
        source="scopes/closed.yaml",
        default_deny=True,
    )
    visible = Scope(id="visible", source="scopes/visible.yaml")
    policy = Policy(
        fingerprint="f" * 64,
        scopes={closed.id: closed, visible.id: visible},
    )
    key = projections.ProjectionNamespaceKey(policy.fingerprint, 1, 1)

    def item(path: str, content_hash: str, scope_ids: tuple[str, ...], body: str):
        return projection_store.ProjectionItemVariants(
            item_identity=path,
            content_hash=content_hash,
            scope_ids=scope_ids,
            variants=projections.enumerate_projection_variants(
                item_identity=path,
                content_hash=content_hash,
                scope_ids=scope_ids,
                policy=policy,
                projector_schema_version=1,
                full_search_fields={"title": path.rsplit("/", 1)[-1], "body": body},
            ),
        )

    hidden = item(
        "Knowledge Base/hidden.md",
        "1" * 64,
        (closed.id,),
        "projection term projection term projection term",
    )
    shown = item(
        "Knowledge Base/shown.md",
        "2" * 64,
        (visible.id,),
        "projection term",
    )
    namespace = verified_namespace(key, (hidden, shown))
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="fixture-vault",
            activation_store_id="fixture-store",
            activation_epoch=1,
            activation_state_digest="e" * 64,
            policy_generation_id="fixture-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(
            key, (hidden, shown)
        ),
        projection_namespace_evidence=b"fixture",
    )
    runtime = projection_runtime.ActiveProjectionRuntime(snapshot, namespace)
    monkeypatch.setattr(
        egress,
        "_resolve_l4_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projected authorization sampled a live bridge")
        ),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="projection term",
        limit=1,
        mode="keyword",
        graph=False,
        rerank=False,
        principal=principal.RequestPrincipal(
            audience_id="stranger",
            surface="test",
        ),
        purpose=None,
    )

    assert [hit.path for hit in result.hits] == ["Knowledge Base/shown.md"]
    assert result.withheld_paths == frozenset({"Knowledge Base/hidden.md"})


def test_projected_wire_hit_never_emits_the_full_search_body() -> None:
    decision = Decision(level=6, scope_ids=("visible",))
    hit = projected_retrieval.ProjectedLexicalHit(
        item_identity="Knowledge Base/large.md",
        projection_variant_id="1" * 64,
        decision_level=6,
        score=1.0,
        search_fields={"body": "prefix " + "secret " * 100_000},
        snippet="prefix bounded snippet",
    )
    selection = projected_retrieval.ProjectionSelection(
        item_identity=hit.item_identity,
        content_hash="2" * 64,
        projection_variant_id=hit.projection_variant_id,
        decision=decision,
    )

    wire = projection_runtime._wire_hit(
        hit,
        selection=selection,
        rank=1,
        knowledge_base_name="Knowledge Base",
        keyword=False,
    )

    assert wire.excerpt == "prefix bounded snippet"


@pytest.mark.parametrize(
    ("query", "mode", "graph", "rerank"),
    [
        ("term", "keyword", True, False),
        ("", "keyword", False, False),
    ],
)
def test_projected_runtime_refuses_still_unsupported_public_lanes(
    tmp_path,
    query: str,
    mode: str,
    graph: bool,
    rerank: bool,
) -> None:
    visible = Scope(id="visible", source="scopes/visible.yaml")
    policy = Policy(
        fingerprint="f" * 64,
        scopes={visible.id: visible},
    )
    key = projections.ProjectionNamespaceKey(policy.fingerprint, 1, 1)
    item = projection_store.ProjectionItemVariants(
        item_identity="Knowledge Base/visible.md",
        content_hash="1" * 64,
        scope_ids=(visible.id,),
        variants=projections.enumerate_projection_variants(
            item_identity="Knowledge Base/visible.md",
            content_hash="1" * 64,
            scope_ids=(visible.id,),
            policy=policy,
            projector_schema_version=1,
            full_search_fields={"title": "Visible", "body": "term"},
        ),
    )
    namespace = verified_namespace(key, (item,))
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="fixture-vault",
            activation_store_id="fixture-store",
            activation_epoch=1,
            activation_state_digest="e" * 64,
            policy_generation_id="fixture-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, (item,)),
        projection_namespace_evidence=b"fixture",
    )
    runtime = projection_runtime.ActiveProjectionRuntime(snapshot, namespace)

    with pytest.raises(projection_runtime.ProjectionRuntimeUnavailable):
        projection_runtime.find_projected_hits(
            tmp_path,
            runtime,
            query=query,
            limit=1,
            mode=mode,
            graph=graph,
            rerank=rerank,
            principal=principal.owner_principal(surface="library"),
            purpose=None,
        )


def test_product_recall_does_not_reopen_due_state_on_projected_boundary(
    monkeypatch,
    tmp_path,
) -> None:
    result = [{"path": "Knowledge Base/visible.md"}]
    monkeypatch.setattr(commands, "op_find", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "requires_projected_read_boundary",
        lambda _root: True,
        raising=False,
    )

    assert commands.op_ask_memory(tmp_path, query="term") is result


def test_exact_v4_public_loader_never_builds_namespace_on_request(
    monkeypatch,
    tmp_path,
) -> None:
    connection = sqlite3.connect(":memory:")
    control = SimpleNamespace(
        governance_enrolled=True,
        logical_vault_id="vault-1",
        activation_store_id="store-1",
        activation_epoch=1,
        activation_state_digest="a" * 64,
    )
    monkeypatch.setattr(
        store,
        "open_active_governance_read_connection",
        lambda _root: connection,
    )
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: SimpleNamespace(control=control),
    )
    for loader_name in ("load_active_state", "load_active_policy"):
        monkeypatch.setattr(
            schema_v4,
            loader_name,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("closed v4 request sampled active catalog material")
            ),
        )
    monkeypatch.setattr(
        projection_store,
        "load_projection_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public request rebuilt the projection namespace")
        ),
    )

    with pytest.raises(projection_runtime.ProjectionRuntimeUnavailable):
        projection_runtime.load_active_projection_runtime(tmp_path)


def test_proven_v3_still_uses_the_legacy_reader(monkeypatch, tmp_path) -> None:
    legacy = sqlite3.connect(":memory:")
    monkeypatch.delenv(authorization_custody.KEYRING_FILE_ENV, raising=False)
    monkeypatch.delenv(authorization_custody.CONTROL_FILE_ENV, raising=False)
    monkeypatch.setattr(
        store,
        "open_active_governance_read_connection",
        lambda _root: (_ for _ in ()).throw(store.UnsupportedGovernanceSchema()),
    )
    monkeypatch.setattr(store, "open_readonly_connection", lambda _root: legacy)

    assert projection_runtime.load_active_projection_runtime(tmp_path) is None


def test_unknown_present_schema_never_falls_into_raw_retrieval(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv(authorization_custody.KEYRING_FILE_ENV, raising=False)
    monkeypatch.delenv(authorization_custody.CONTROL_FILE_ENV, raising=False)
    sidecar = store.sidecar_path(tmp_path)
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"future-or-corrupt")
    monkeypatch.setattr(
        store,
        "open_active_governance_read_connection",
        lambda _root: (_ for _ in ()).throw(store.UnsupportedGovernanceSchema()),
    )
    monkeypatch.setattr(store, "open_readonly_connection", lambda _root: None)

    with pytest.raises(projection_runtime.ProjectionRuntimeUnavailable):
        projection_runtime.load_active_projection_runtime(tmp_path)


def test_projection_session_purpose_and_grants_share_one_read_snapshot(
    monkeypatch,
    tmp_path,
) -> None:
    events: list[str] = []

    class Connection:
        in_transaction = False

        def execute(self, statement: str):
            assert statement == "BEGIN"
            assert not self.in_transaction
            self.in_transaction = True
            events.append("begin")

        def commit(self) -> None:
            assert self.in_transaction
            self.in_transaction = False
            events.append("commit")

        def rollback(self) -> None:
            self.in_transaction = False
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    connection = Connection()
    monkeypatch.setattr(
        store,
        "open_authorization_session_connection",
        lambda _root: connection,
    )

    def active_purpose(current, **_kwargs):
        assert current is connection and current.in_transaction
        events.append("purpose")
        return "audit"

    def active_grants(*, connection: Connection, **kwargs):
        assert connection.in_transaction
        assert kwargs["purpose"] == "audit"
        events.append("grants")
        grant = SimpleNamespace(
            grant_id="3" * 64,
            policy_fingerprint="f" * 64,
            scope_ids=("scope-a", "scope-z"),
            audience="principal-1",
            purpose="audit",
            ceiling=6,
        )
        return (("Knowledge Base/a.md", grant), ("Knowledge Base/z.md", grant))

    monkeypatch.setattr(
        authorization_session_authority,
        "active_session_purpose",
        active_purpose,
    )
    monkeypatch.setattr(
        authorization_session_authority,
        "active_session_grants_for_projection_catalog",
        active_grants,
    )
    context = authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="session-1",
        principal_id="principal-1",
        issuer_family="test",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        keyring_id="keyring-1",
        credential_generation=1,
        expires_at=2_000_000_000,
    )
    who = principal.RequestPrincipal(
        audience_id="principal-1",
        surface="test",
        issuer_family="test",
        verified_authorization_session=context,
    )
    runtime = SimpleNamespace(
        namespace=SimpleNamespace(
            items=(
                SimpleNamespace(
                    item_identity="Knowledge Base/a.md",
                    content_hash="1" * 64,
                    scope_ids=("scope-a",),
                ),
                SimpleNamespace(
                    item_identity="Knowledge Base/z.md",
                    content_hash="2" * 64,
                    scope_ids=("scope-z",),
                ),
            )
        ),
        snapshot=SimpleNamespace(policy=SimpleNamespace(fingerprint="f" * 64)),
    )

    declared, grants = projection_runtime._verified_session_grants(
        tmp_path,
        runtime,
        principal=who,
        purpose=None,
    )
    assert declared == "audit"
    assert [(grant.item_identity, grant.scope_ids) for grant in grants] == [
        ("Knowledge Base/a.md", ("scope-a",)),
        ("Knowledge Base/z.md", ("scope-z",)),
    ]
    assert events == ["begin", "purpose", "grants", "commit", "close"]
