"""Frozen schema-v3 governance authority used by migration compatibility tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from exomem.governance import store

DIRECT_SOURCE_POLICY_PATH = "scopes/fixture.yaml"
DIRECT_SOURCE_POLICY_BYTES = (
    b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n"
)

# This pins the complete logical SQLite schema plus all representative rows below.
# An intentional schema-v3 change must regenerate and explicitly review this value.
FROZEN_V3_DUMP_SHA256 = "6dd3b810a19ea810188a80f4b1e7387101616f81c2d838a9eb7634cacd688da8"


def frozen_v3_dump(vault_root: Path) -> tuple[str, ...]:
    """Return a deterministic logical dump without opening any product writer."""

    connection = sqlite3.connect(store.sidecar_path(vault_root))
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def install_frozen_v3_fixture(vault_root: Path) -> None:
    """Install one exact rich v3 authority and its direct-source policy workspace."""

    root = Path(vault_root)
    governance = root / "Knowledge Base" / "_Governance"
    policy_path = governance / DIRECT_SOURCE_POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    policy_path.write_bytes(DIRECT_SOURCE_POLICY_BYTES)

    connection = store.open_connection(root)
    try:
        connection.execute("UPDATE meta SET value=314159 WHERE key='instance'")
        connection.execute(
            "INSERT INTO compiled_policy (fingerprint, snapshot, compiled_at) VALUES (?, ?, ?)",
            ("legacy-policy-fingerprint", '{"blocked":false,"rules":[]}', 10.0),
        )
        connection.execute(
            "INSERT INTO receipt_instance (singleton, instance_id) VALUES (1, ?)",
            ("legacy-receipt-instance",),
        )
        connection.execute(
            "INSERT INTO receipts_head "
            "(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, "
            "path, byte_offset) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-receipt-instance",
                7,
                "d" * 64,
                7,
                "o" * 64,
                "Knowledge Base/_Governance/receipts.jsonl",
                777,
            ),
        )
        connection.execute(
            "INSERT INTO receipt_secrets (name, value) VALUES (?, ?)",
            ("hmac-v1", b"fixture-secret-not-a-real-credential"),
        )
        connection.execute(
            "INSERT INTO withhold_tokens "
            "(jti, audience, max_level, fingerprints, paths, expires_at, minted_at, "
            "consumed_at, authorization_session, purpose, org_ceiling, status, "
            "prepared_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-token",
                "external",
                6,
                '["content-fingerprint"]',
                '["Notes/fixture.md"]',
                4_000_000_000,
                20.0,
                None,
                "legacy-session",
                "fixture-review",
                6,
                "active",
                "legacy-recovery-event",
            ),
        )
        connection.execute(
            "INSERT INTO governance_proposals "
            "(proposal_id, created_at, expires_at, proposal_json, "
            "fingerprint_at_propose, membership_manifest, status, reserved_event_id, "
            "attempt_no, attempt_nonce, spent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-proposal",
                21.0,
                4_000_000_000.0,
                '{"operation":"suspend"}',
                "legacy-policy-fingerprint",
                '[{"path":"Notes/fixture.md","scope_ids":["fixture"]}]',
                "pending",
                "legacy-recovery-event",
                1,
                "legacy-attempt",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO governance_operation_journals "
            "(event_id, operation, causation_id, authorization_session, principal_id, "
            "phase, direction, prior_digest, prepared_digest, final_digest, affected_ids, "
            "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
            "marker_required, created_at, updated_at, blocked_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-recovery-event",
                "suspend",
                "legacy-causation",
                "legacy-session",
                "external",
                "pending",
                "widening",
                "prior-digest",
                "prepared-digest",
                "final-digest",
                '["01ARZ3NDEKTSV4RRFFQ69G5FAV"]',
                '["policy-intent"]',
                '["policy-terminal"]',
                "legacy-proposal",
                1,
                1,
                22.0,
                23.0,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO governance_operation_components "
            "(event_id, phase, ordinal, component_kind, component_key, value_json, "
            "value_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-recovery-event",
                "prepared",
                0,
                "policy",
                "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                '{"status":"prepared"}',
                "component-digest",
                "prepared",
            ),
        )
        connection.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session, audience, purpose, ceiling, paths, "
            "fingerprints, token_jti, status, prepared_event_id, created_at, expires_at, "
            "revoked_at, membership_manifest, policy_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-grant",
                "legacy-session",
                "external",
                "fixture-review",
                6,
                '["Notes/fixture.md"]',
                '["content-fingerprint"]',
                "legacy-token",
                "active",
                "legacy-recovery-event",
                20.0,
                4_000_000_000.0,
                None,
                '[{"path":"Notes/fixture.md","scope_ids":["fixture"]}]',
                "legacy-policy-fingerprint",
            ),
        )
        connection.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-session",
                "external",
                "fixture-review",
                "active",
                "legacy-recovery-event",
                20.0,
                4_000_000_000.0,
            ),
        )
        connection.execute(
            "INSERT INTO governance_session_purpose_staging "
            "(event_id, authorization_session, principal_id, purpose, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-purpose-stage",
                "legacy-session",
                "external",
                "fixture-review",
                20.0,
                4_000_000_000.0,
            ),
        )
        connection.execute(
            "INSERT INTO governance_policy_archives "
            "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-policy-archive",
                "legacy-recovery-event",
                DIRECT_SOURCE_POLICY_PATH,
                DIRECT_SOURCE_POLICY_BYTES,
                "archive-prior-hash",
                20.0,
            ),
        )
        connection.commit()
    finally:
        connection.close()
