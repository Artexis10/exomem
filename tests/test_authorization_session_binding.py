from __future__ import annotations

import base64

import pytest

from exomem.governance import authorization_sessions, scrubber


def _credential(locator: bytes, secret: bytes) -> str:
    return (
        "as1."
        + base64.urlsafe_b64encode(locator).rstrip(b"=").decode("ascii")
        + "."
        + base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
    )


def test_shared_authorization_session_parser_returns_exact_bounded_parts() -> None:
    bearer = _credential(bytes(range(16)), bytes(range(32)))

    parsed = authorization_sessions.parse_credential(bearer)

    assert parsed is not None
    assert parsed.encoded == bearer
    assert parsed.locator == bytes(range(16))
    assert parsed.secret == bytes(range(32))


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
