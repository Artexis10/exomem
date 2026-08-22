from __future__ import annotations

import base64
import dataclasses
import hmac

import pytest

from exomem.governance import authorization_sessions, scrubber


def _credential(locator: bytes, secret: bytes) -> str:
    return (
        "as1."
        + base64.urlsafe_b64encode(locator).rstrip(b"=").decode("ascii")
        + "."
        + base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
    )


def _binding(
    **changes: object,
) -> authorization_sessions.AuthorizationSessionBinding:
    values: dict[str, object] = {
        "session_id": "session-018f621c",
        "principal_id": "principal:local-owner:1000",
        "issuer_family": "cli-local-owner",
        "cell_id": "cell-7bd27031",
        "logical_vault_id": "vault-a2699d30",
        "keyring_id": "keyring-e7901e43",
        "credential_generation": 3,
        "expires_at": 1_800_003_600,
    }
    values.update(changes)
    return authorization_sessions.AuthorizationSessionBinding(**values)  # type: ignore[arg-type]


def test_shared_authorization_session_parser_returns_exact_bounded_parts() -> None:
    bearer = _credential(bytes(range(16)), bytes(range(32)))

    parsed = authorization_sessions.parse_credential(bearer)

    assert parsed is not None
    assert parsed.encoded == bearer
    assert parsed.locator == bytes(range(16))
    assert parsed.secret == bytes(range(32))
    assert bearer not in repr(parsed)
    assert bytes(range(32)).hex() not in repr(parsed)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"session_id": ""}, "session_id"),
        ({"principal_id": "x" * 513}, "principal_id"),
        ({"credential_generation": 0}, "credential_generation"),
        ({"credential_generation": 2**64}, "credential_generation"),
        ({"expires_at": 0}, "expires_at"),
        ({"expires_at": 2**64}, "expires_at"),
    ],
)
def test_binding_rejects_empty_or_unframeable_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _binding(**changes)


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"as1.not-a-string",
        " as1." + "A" * 22 + "." + "A" * 43,
        "as1." + "A" * 22 + "." + "A" * 43 + "\n",
        "as1." + "A" * 21 + "=" + "." + "A" * 43,
        "as1." + "A" * 21 + "+" + "." + "A" * 43,
        "as1." + "A" * 21 + "/" + "." + "A" * 43,
        "as1." + "A" * 21 + "B" + "." + "A" * 43,
        "as1." + "A" * 22 + "." + "A" * 42 + "B",
    ],
)
def test_shared_authorization_session_parser_rejects_noncanonical_values(
    value: object,
) -> None:
    assert authorization_sessions.parse_credential(value) is None


def test_shared_matcher_finds_adjacent_credentials_and_overlapping_starts() -> None:
    first = _credential(bytes(range(16)), bytes(range(32)))
    second = _credential(bytes(reversed(range(16))), bytes(reversed(range(32))))
    text = f"as1.x{first}{second}y"

    assert list(authorization_sessions.iter_credential_spans(text)) == [
        (5, 75),
        (75, 145),
    ]


def test_issue_binds_credential_without_persisting_bearer_or_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator = bytes(range(16))
    secret = bytes(range(32))

    def deterministic_bytes(size: int) -> bytes:
        return {16: locator, 32: secret}[size]

    monkeypatch.setattr(authorization_sessions.secrets, "token_bytes", deterministic_bytes)

    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=_binding(),
    )

    assert issued.bearer == _credential(locator, secret)
    assert issued.record.binding == _binding()
    assert issued.record.status == "active"
    assert issued.record.locator_digest.hex() == (
        "67154d8db47a1c6438bd7625569028792d89fe273e235af70157fae09cbc6600"
    )
    assert issued.record.verifier.hex() == (
        "723506256cf9c61c3257360099f2865cec9df4f743c1051cb6719d44513cbc48"
    )
    persisted = repr(dataclasses.asdict(issued.record))
    assert issued.bearer not in persisted
    assert secret.hex() not in persisted
    assert issued.bearer not in repr(issued)


@pytest.mark.parametrize("field_name", ["locator_digest", "verifier"])
def test_verifier_record_rejects_text_disguised_as_digest(field_name: str) -> None:
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=_binding(),
    )

    with pytest.raises(ValueError, match=field_name):
        dataclasses.replace(issued.record, **{field_name: "x" * 32})


@pytest.mark.parametrize(
    "binding_change",
    [
        {"principal_id": "principal:local-owner:1001"},
        {"issuer_family": "rest-api-key"},
        {"cell_id": "cell-other"},
        {"logical_vault_id": "vault-other"},
        {"keyring_id": "keyring-other"},
        {"credential_generation": 4},
    ],
)
def test_verify_rejects_every_cross_binding(
    binding_change: dict[str, object],
) -> None:
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=_binding(),
    )

    assert not authorization_sessions.verify_credential(
        issued.bearer,
        record=issued.record,
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        expected_binding=_binding(**binding_change),
        now=1_800_000_000,
    )


def test_verify_accepts_only_active_unexpired_record_and_matching_key() -> None:
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=_binding(),
    )
    common = {
        "record": issued.record,
        "verifier_key": b"k" * 32,
        "verifier_key_id": "auth-key-2026-08",
        "expected_binding": _binding(),
    }

    assert authorization_sessions.verify_credential(
        issued.bearer, **common, now=1_800_000_000
    )
    assert not authorization_sessions.verify_credential(
        issued.bearer, **common, now=1_800_003_600
    )
    assert not authorization_sessions.verify_credential(
        issued.bearer,
        **{**common, "record": dataclasses.replace(issued.record, status="closed")},
        now=1_800_000_000,
    )
    assert not authorization_sessions.verify_credential(
        issued.bearer,
        **{**common, "verifier_key": b"z" * 32},
        now=1_800_000_000,
    )
    assert not authorization_sessions.verify_credential(
        issued.bearer,
        **{**common, "verifier_key_id": "unknown-key"},
        now=1_800_000_000,
    )
    assert not authorization_sessions.verify_credential(
        issued.bearer, **common, now="not-a-clock"  # type: ignore[arg-type]
    )


def test_verify_accepts_a_matching_utf8_verifier_key_id() -> None:
    key_id = "auth-key-å-2026"
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id=key_id,
        binding=_binding(),
    )

    assert authorization_sessions.verify_credential(
        issued.bearer,
        record=issued.record,
        verifier_key=b"k" * 32,
        verifier_key_id=key_id,
        expected_binding=_binding(),
        now=1_800_000_000,
    )


def test_verify_compares_digest_before_rejecting_validly_parsed_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = authorization_sessions.issue_credential(
        verifier_key=b"k" * 32,
        verifier_key_id="auth-key-2026-08",
        binding=_binding(),
    )
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def recording_compare(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return real(left, right)

    monkeypatch.setattr(authorization_sessions.hmac, "compare_digest", recording_compare)

    assert not authorization_sessions.verify_credential(
        issued.bearer,
        record=issued.record,
        verifier_key=b"z" * 32,
        verifier_key_id="auth-key-2026-08",
        expected_binding=_binding(principal_id="principal:other"),
        now=1_800_000_000,
    )
    assert len(compared) >= 2


def test_terminal_scrubber_uses_the_shared_authorization_session_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bearer = _credential(bytes(range(16)), bytes(range(32)))
    calls: list[object] = []
    real = authorization_sessions.parse_credential

    def recording_parser(
        value: object,
    ) -> authorization_sessions.AuthorizationSessionCredential | None:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(authorization_sessions, "parse_credential", recording_parser)

    cleaned, blocked = scrubber.scrub_text(f"prefix {bearer} suffix")

    assert blocked is True
    assert bearer not in cleaned
    assert bearer in calls
