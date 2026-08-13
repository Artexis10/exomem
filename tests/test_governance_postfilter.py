"""Terminal secret scrubber + withheld cross-check at the shared dispatcher.

Design decisions D1 (the postfilter lives in `writer_lease.invoke_command`,
the ONE dispatcher shared by MCP/REST/hosted/CLI, with a second pass in the
MCP-only `bind_vault` wrapper) and D7 (the credential scrubber is always on —
it runs even on the empty-policy fast path, which is this change's single
intentional behavior change for an ungoverned vault).

The scrubber is content-pattern-based and policy-independent, so it must NOT
consult `policy.load()` to decide whether to run. The withheld cross-check IS
policy-dependent and therefore carries the same three-state contract as every
other consumer: empty -> open, blocked -> fail closed, else decide.
"""

from __future__ import annotations

import json
import pickle
import re
from pathlib import Path

import pytest

from exomem.governance import egress, scrubber


class _FakeAlias:
    """A product-alias stand-in for the planted-defect proofs."""

    def __init__(self, *, routes: tuple[str, ...], leaf=None) -> None:
        self.routes = routes
        self.leaf = leaf

# --------------------------------------------------------------------------
# Credential corpus
# --------------------------------------------------------------------------

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAx7Vv8mQ2kFq3nZpLdT4wYs9RbHcJ1eXvNgKu5aPzWmDrTyBl\n"
    "QhFdEoUnCiKvMxSaGtJwYrZbNfHpLcXeVdQmAoIBAQC9kTnWpXsYbGvHqLmRdFzE\n"
    "-----END RSA PRIVATE KEY-----"
)
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_16C7e42F292c6912E7710c838347Ae178B4a"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
BEARER = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
HIGH_ENTROPY = "s3cr3tK3yZ9qWmPvXtLnBdRfGhJkYuIoAeCxSvTgNbMh"
AS1_BEARER = "as1." + "A" * 22 + "." + "A" * 43
AS1_VECTOR_BEARER = (
    "as1."
    + "AAECAwQFBgcICQoLDA0ODw"
    + "."
    + "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
)
ISSUANCE_EXPIRY = "2026-08-13T12:34:56Z"

# Structural identifiers that MUST survive untouched.
CONTENT_HASH = "9f2c4e1a7b8d3f0e6c5a9b2d4e7f1a3c8b6d0e2f4a7c9b1d3e5f7a9c0b2d4e6f"
MEMORY_REF = "exomem://note/kb-rrf-fusion-beats-score-normalization-a1b2c3"


@pytest.fixture(autouse=True)
def _clear_caches():
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


# --------------------------------------------------------------------------
# D7 — credential patterns are blocked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("private_key", PRIVATE_KEY),
        ("aws_access_key", AWS_KEY),
        ("github_token", GITHUB_TOKEN),
        ("jwt", JWT),
        ("bearer", BEARER),
        ("high_entropy", HIGH_ENTROPY),
    ],
)
def test_credential_shapes_are_blocked(label: str, secret: str) -> None:
    text = f"here is the value: {secret} — end"
    cleaned, blocked = scrubber.scrub_text(text)
    assert blocked, f"{label} was not detected"
    assert secret not in cleaned
    assert scrubber.NOTICE in cleaned


def test_every_credential_pattern_has_an_anchor() -> None:
    """The literal prescan is a performance shortcut that MUST NOT be a leak.

    For every credential in the corpus, the anchor sweep has to say "maybe" —
    if it says "no", the alternation never runs and the credential ships. This
    test is the contract between `_ANCHORS` and `_CREDENTIAL_PATTERN`.
    """
    for secret in (PRIVATE_KEY, AWS_KEY, GITHUB_TOKEN, JWT, BEARER):
        assert scrubber._may_contain_credential(secret), (
            f"anchor sweep would skip the alternation for {secret[:24]!r}"
        )
        # And the pattern itself must actually fire on it.
        assert scrubber._CREDENTIAL_PATTERN.search(secret) is not None


# --------------------------------------------------------------------------
# M13 — the anchor-superset invariant, enumerated instead of sampled
# --------------------------------------------------------------------------


def test_credential_patterns_are_a_named_enumerable_collection() -> None:
    """Five hardcoded samples cannot prove a property of an open set.

    The invariant "every alternative in `_CREDENTIAL_PATTERN` is reachable
    through at least one `_ANCHORS` literal" has to be checked by walking the
    alternatives themselves — otherwise adding a sixth pattern without its
    anchor silently turns the prescan from an optimization into a leak, and
    no test notices.
    """
    specs = scrubber.CREDENTIAL_PATTERNS
    assert len(specs) >= 8
    names = [spec.name for spec in specs]
    assert len(names) == len(set(names)), "credential pattern names must be unique"


def test_every_credential_alternative_declares_covering_anchors() -> None:
    """Enumerated, not sampled: each alternative must declare anchors, and
    each declared anchor must actually be in the prescan sweep."""
    for spec in scrubber.CREDENTIAL_PATTERNS:
        assert spec.anchors, f"{spec.name} declares no prescan anchor"
        for anchor in spec.anchors:
            assert anchor == anchor.lower(), f"{spec.name}: anchor {anchor!r} is not lowered"
            assert anchor in scrubber._ANCHORS, (
                f"{spec.name}: anchor {anchor!r} is not in the prescan sweep"
            )


def test_the_prescan_sweep_is_derived_from_the_pattern_collection() -> None:
    """`_ANCHORS` must not be a second hand-maintained list — drift between
    the two IS the defect. Deriving it makes the superset relation structural
    rather than remembered."""
    declared = {anchor for spec in scrubber.CREDENTIAL_PATTERNS for anchor in spec.anchors}
    assert set(scrubber._ANCHORS) == declared


def test_every_alternative_is_reachable_through_its_own_anchor() -> None:
    """Per-alternative proof, driven off the collection: every sample a
    pattern claims to match must (a) match that pattern, (b) survive the
    prescan via one of THAT pattern's anchors, and (c) match the compiled
    union the scrubber actually runs."""
    for spec in scrubber.CREDENTIAL_PATTERNS:
        assert spec.samples, f"{spec.name} carries no sample to verify against"
        compiled = re.compile(spec.pattern, re.DOTALL)
        for sample in spec.samples:
            assert compiled.search(sample), f"{spec.name} does not match its own sample"
            lowered = sample.lower()
            assert any(anchor in lowered for anchor in spec.anchors), (
                f"{spec.name}: sample is not reachable through its declared anchors"
            )
            assert scrubber._may_contain_credential(sample), (
                f"{spec.name}: the prescan would skip the alternation"
            )
            assert scrubber._CREDENTIAL_PATTERN.search(sample), (
                f"{spec.name}: the compiled union does not fire"
            )
            cleaned, blocked = scrubber.scrub_text(f"value: {sample} end")
            assert blocked and sample not in cleaned, f"{spec.name} leaked through scrub_text"


def test_a_pattern_added_without_an_anchor_is_refused() -> None:
    """Planted defect: the collection itself must refuse an anchorless
    alternative, so the mistake cannot reach the prescan at all."""
    with pytest.raises(ValueError, match="anchor"):
        scrubber.CredentialPattern(
            name="anchorless",
            pattern=r"\bzzsecretshape[0-9]{10}\b",
            anchors=(),
            samples=("zzsecretshape0123456789",),
        )


def test_a_pattern_added_without_a_sample_is_refused() -> None:
    """Same planted defect, other half: an alternative with no sample cannot
    have its anchor claim verified, so it is refused too."""
    with pytest.raises(ValueError, match="sample"):
        scrubber.CredentialPattern(
            name="sampleless",
            pattern=r"\bzz_[0-9]{10}\b",
            anchors=("zz_",),
            samples=(),
        )


def test_declaring_an_anchor_the_pattern_does_not_guarantee_is_refused() -> None:
    """The invariant is about REACHABILITY, not about listing something. A
    spec whose own sample does not contain its declared anchor is a broken
    superset claim, and the collection refuses it at construction."""
    with pytest.raises(ValueError, match="not reachable through its own anchors"):
        scrubber.CredentialPattern(
            name="mislabelled",
            pattern=r"\bzzq[0-9]{10}\b",
            anchors=("nowhere-near-it",),
            samples=("zzq0123456789",),
        )


def test_a_second_gate_the_sample_cannot_satisfy_is_refused() -> None:
    """`also_requires` is a superset condition too, so it gets the same
    construction-time proof: a gate the pattern's own sample fails would skip
    a real credential."""
    with pytest.raises(ValueError, match="also_requires"):
        scrubber.CredentialPattern(
            name="over-gated",
            pattern=r"\bzzr[0-9]{10}\b",
            anchors=("zzr",),
            samples=("zzr0123456789",),
            also_requires=("@@@",),
        )


def test_every_alternative_gate_is_satisfied_by_its_own_samples() -> None:
    """Enumerated over the shipped collection: for every alternative, every
    sample must pass BOTH gates the scan applies — the anchor sweep and any
    `also_requires` — or the scan would skip a credential it can match."""
    for spec in scrubber.CREDENTIAL_PATTERNS:
        for sample in spec.samples:
            lowered = sample.lower()
            assert any(anchor in lowered for anchor in spec.anchors), spec.name
            if spec.also_requires:
                assert any(x in lowered for x in spec.also_requires), spec.name
            assert spec.name in {
                scrubber.CREDENTIAL_PATTERNS[i].name
                for i in scrubber._active_alternatives(lowered)
            }, f"{spec.name}: its own sample does not activate it"


def test_anchor_prescan_skips_clean_prose() -> None:
    """The whole point of the prescan: ordinary vault prose never reaches the
    alternation."""
    assert not scrubber._may_contain_credential(
        "Retry with full jitter backoff avoids thundering herds."
    )


def test_clean_text_is_returned_unchanged() -> None:
    text = "A perfectly ordinary sentence about retry backoff and jitter."
    cleaned, blocked = scrubber.scrub_text(text)
    assert cleaned == text
    assert not blocked


def test_structural_identifiers_are_not_false_positives() -> None:
    """A 64-hex content hash and an `exomem://` ref are high-entropy by
    construction — the allowlist must run BEFORE the pattern scan."""
    payload = {
        "path": "Knowledge Base/Notes/Insights/x.md",
        "content_hash": CONTENT_HASH,
        "ref": MEMORY_REF,
        "fingerprint": CONTENT_HASH,
        "expected_hash": CONTENT_HASH,
    }
    cleaned, blocked = scrubber.scrub_value(payload)
    assert not blocked
    assert cleaned == payload


def test_identifier_shaped_fields_are_structural() -> None:
    """The suffix rule, not an enumeration: the product mints round-tripping
    capability identifiers (`transition_token`, `draft_token`, `unit_ref`,
    `idempotency_key`) and the client echoes them back on the next call, so
    mangling one breaks a guarded write. A suffix rule covers a new identifier
    field the day it is added."""
    for name in (
        "transition_token",
        "draft_token",
        "relation_review_hash",
        "unit_ref",
        "idempotency_key",
        "receipt_id",
        "active_capability_sha256",
    ):
        assert scrubber._is_structural_field(name), name


def test_credential_named_fields_are_never_structural() -> None:
    """The suffix rule must widen coverage of identifiers, never open a hole
    for a field whose own name says it holds a secret."""
    for name in (
        "api_key",
        "apikey",
        "secret_token",
        "access_key",
        "private_key",
        "password",
        "auth_token",
        "bearer_token",
    ):
        assert not scrubber._is_structural_field(name), name
    # And end-to-end: a high-entropy value in such a field is still blocked.
    cleaned, blocked = scrubber.scrub_value({"secret_token": HIGH_ENTROPY})
    assert blocked
    assert HIGH_ENTROPY not in json.dumps(cleaned)


def test_allowlisted_field_still_scrubs_an_actual_private_key() -> None:
    """The allowlist is a field-name allowlist for *structural identifiers*,
    not a bypass: a private-key block parked in `content_hash` is still a
    credential leaving the boundary."""
    cleaned, blocked = scrubber.scrub_value({"content_hash": PRIVATE_KEY})
    assert blocked
    assert "BEGIN RSA PRIVATE KEY" not in json.dumps(cleaned)


def test_vault_paths_are_never_scrubbed() -> None:
    """Regression: including `/` in the entropy token class made whole vault
    path segments read as base64, and the scrubber rewrote real paths — which
    then broke adoption apply/copy. A path is separator-delimited; a
    credential is one contiguous run."""
    paths = [
        "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md",
        "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
        "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md",
        "Reference/Some-Very-Long-Curated-Document-Name-2026.md",
    ]
    for path in paths:
        cleaned, blocked = scrubber.scrub_text(path)
        assert not blocked, f"scrubber falsely flagged the vault path {path!r}"
        assert cleaned == path
    # And nested under an arbitrary (non-allowlisted) key, not just `path`.
    payload = {"destination": paths[0], "sources": paths}
    cleaned, blocked = scrubber.scrub_value(payload)
    assert not blocked
    assert cleaned == payload


def test_kebab_and_snake_slugs_are_not_credentials() -> None:
    for slug in (
        "progressive-disclosure-without-mode-fragmentation",
        "vendor_package_mismatch_with_shipping_platform",
        "autovacuum-thresholds-prevent-table-bloat-2026",
    ):
        _, blocked = scrubber.scrub_text(slug)
        assert not blocked, slug


def test_nested_structures_are_walked() -> None:
    payload = {"hits": [{"excerpt": f"leaked {AWS_KEY} here"}, {"excerpt": "fine"}]}
    cleaned, blocked = scrubber.scrub_value(payload)
    assert blocked
    assert AWS_KEY not in json.dumps(cleaned)
    assert cleaned["hits"][1]["excerpt"] == "fine"


def test_canonical_as1_bearers_and_mapping_keys_are_scrubbed() -> None:
    payload = {
        AS1_BEARER: "value",
        "nested": {AWS_KEY: AS1_BEARER},
    }

    cleaned, blocked = scrubber.scrub_value(payload)

    encoded = json.dumps(cleaned)
    assert blocked
    assert AS1_BEARER not in encoded
    assert AWS_KEY not in encoded
    assert scrubber.NOTICE in encoded


def test_as1_parser_accepts_fixed_canonical_vectors_and_rejects_unused_bits() -> None:
    assert scrubber._parse_authorization_bearer(AS1_BEARER) == AS1_BEARER
    assert scrubber._parse_authorization_bearer(AS1_VECTOR_BEARER) == AS1_VECTOR_BEARER

    locator, secret = AS1_BEARER.removeprefix("as1.").split(".")
    for malformed in (
        AS1_BEARER[:-1],
        AS1_BEARER + "x",
        AS1_BEARER.replace("as1.", "AS1."),
        AS1_BEARER.replace(".", "_", 1),
        f"as1.{locator[:-1]}B.{secret}",
        f"as1.{locator}.{secret[:-1]}B",
    ):
        assert scrubber._parse_authorization_bearer(malformed) is None


def test_arbitrary_content_scanning_calls_the_canonical_as1_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    real = scrubber._parse_authorization_bearer

    def recording_parser(value: object) -> str | None:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(scrubber, "_parse_authorization_bearer", recording_parser)

    cleaned, blocked = scrubber.scrub_text(f"prefix {AS1_VECTOR_BEARER} suffix")

    assert blocked
    assert AS1_VECTOR_BEARER not in cleaned
    assert AS1_VECTOR_BEARER in calls


def test_canonical_as1_scanning_ignores_adjacent_base64url_characters() -> None:
    # The first `as1.` is a malformed start whose fixed 70-character window
    # overlaps the canonical bearer that follows it. The scanner must advance
    # to the later start, then continue through an adjacent second bearer.
    payload = f"as1.x{AS1_VECTOR_BEARER}{AS1_BEARER}y"

    cleaned, blocked = scrubber.scrub_text(payload)

    assert blocked
    assert AS1_VECTOR_BEARER not in cleaned
    assert AS1_BEARER not in cleaned
    assert cleaned == f"as1.x{scrubber.NOTICE}{scrubber.NOTICE}y"


def test_canonical_as1_in_mapping_key_is_scrubbed_without_token_boundaries() -> None:
    payload = {f"x{AS1_VECTOR_BEARER}y": "safe"}

    cleaned, blocked = scrubber.scrub_value(payload)

    assert blocked
    assert cleaned == {f"x{scrubber.NOTICE}y": "safe"}


def _issuance_response(bearer: str = AS1_BEARER) -> dict[str, object]:
    return {
        "status": "ok",
        "issued_credential": {
            "kind": "authorization-session-bearer",
            "bearer": bearer,
            "expires_at": ISSUANCE_EXPIRY,
        },
    }


def test_exact_issuance_context_allows_only_one_matching_typed_response() -> None:
    context = scrubber._new_issuance_context("open", AS1_BEARER)
    response = _issuance_response()

    cleaned, blocked = scrubber._scrub_issuance_response("open", response, context)

    assert not blocked
    assert cleaned == response


def test_issuance_context_cannot_be_serialized() -> None:
    with pytest.raises(TypeError, match="process-local"):
        pickle.dumps(scrubber._new_issuance_context("rotate", AS1_BEARER))


def test_issuance_context_validates_action_and_private_seal() -> None:
    with pytest.raises(ValueError, match="action"):
        scrubber._new_issuance_context("get", AS1_BEARER)

    forged = scrubber._IssuanceContext(
        action="open", bearer=AS1_BEARER, _seal=object()
    )
    cleaned, blocked = scrubber._scrub_issuance_response(
        "open", _issuance_response(), forged
    )

    assert blocked
    assert AS1_BEARER not in json.dumps(cleaned)


@pytest.mark.parametrize(
    ("command", "response"),
    [
        ("get", _issuance_response()),
        ("rotate", _issuance_response()),
        ("open", {**_issuance_response(), "copy": AS1_BEARER}),
        ("open", {"status": "error", "issued_credential": _issuance_response()["issued_credential"]}),
        ("open", {"issued_credential": _issuance_response()["issued_credential"]}),
        ("open", {"status": "ok", "issued_credential": {**_issuance_response()["issued_credential"], "extra": 1}}),
        ("open", {"status": "ok", "issued_credential": {**_issuance_response()["issued_credential"], "kind": "other"}}),
        ("open", {"status": "ok", "issued_credential": {**_issuance_response()["issued_credential"], "expires_at": "tomorrow"}}),
        ("open", _issuance_response(AS1_BEARER + "x")),
    ],
)
def test_issuance_context_scrubs_wrong_or_duplicate_bearers(
    command: str, response: dict[str, object]
) -> None:
    context = scrubber._new_issuance_context("open", AS1_BEARER)

    cleaned, blocked = scrubber._scrub_issuance_response(command, response, context)

    assert blocked
    assert AS1_BEARER not in json.dumps(cleaned)


def test_rejected_issuance_scrubs_embedded_duplicate_canonical_bearers() -> None:
    context = scrubber._new_issuance_context("open", AS1_BEARER)
    response = {
        **_issuance_response(),
        f"x{AS1_VECTOR_BEARER}y": f"x{AS1_BEARER}{AS1_VECTOR_BEARER}y",
    }

    cleaned, blocked = scrubber._scrub_issuance_response("open", response, context)

    encoded = json.dumps(cleaned)
    assert blocked
    assert AS1_BEARER not in encoded
    assert AS1_VECTOR_BEARER not in encoded
    assert encoded.count(scrubber.NOTICE) == 4


def test_issuance_without_process_local_context_is_refused() -> None:
    cleaned, blocked = scrubber._scrub_issuance_response(
        "open", _issuance_response(), None
    )

    assert blocked
    assert AS1_BEARER not in json.dumps(cleaned)


def test_live_success_and_control_envelopes_never_receive_issuance_exception(
    vault: Path,
) -> None:
    response = _issuance_response()

    cleaned = egress.postfilter("open", response, vault)

    assert AS1_BEARER not in json.dumps(cleaned)
    assert scrubber.NOTICE in json.dumps(cleaned)


@pytest.mark.parametrize("rejected_issuance", [False, True])
def test_mapping_key_scrubbing_is_collision_safe_and_order_independent(
    rejected_issuance: bool,
) -> None:
    entries = [
        (scrubber.NOTICE, "literal notice"),
        (f"{scrubber.NOTICE}#1", "literal suffix one"),
        (f"{scrubber.NOTICE}#3", "literal suffix three"),
        (AS1_BEARER, "authorization bearer key"),
        (AWS_KEY, "aws key"),
    ]

    outputs = []
    for ordered in (entries, list(reversed(entries))):
        payload = dict(ordered)
        if rejected_issuance:
            cleaned, blocked = scrubber._scrub_issuance_response("get", payload, None)
        else:
            cleaned, blocked = scrubber.scrub_value(payload)
        assert blocked
        assert len(cleaned) == len(payload)
        assert sorted(cleaned.values()) == sorted(value for _, value in entries)
        assert AS1_BEARER not in cleaned
        assert AWS_KEY not in cleaned
        outputs.append(cleaned)

    assert outputs[0] == outputs[1]


def test_error_details_scrub_bearers_and_credential_mapping_keys(vault: Path) -> None:
    error = ValueError("upstream bearer " + AS1_BEARER)
    error.details = {AWS_KEY: {AS1_BEARER: "value"}}

    egress.postfilter_error("get", error, vault)

    encoded = json.dumps({"args": error.args, "details": error.details})
    assert AS1_BEARER not in encoded
    assert AWS_KEY not in encoded


def test_scrubber_cannot_be_disabled_by_a_standing_rule(vault: Path) -> None:
    assert scrubber.enabled(vault) is True
    disable = vault / "Knowledge Base" / "_Governance" / "rules" / "no-scrubber.yaml"
    disable.parent.mkdir(parents=True, exist_ok=True)
    disable.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: owner\nceiling: 6\n"
        "options:\n  credential_scrubber: off\n",
        encoding="utf-8",
    )
    scope = vault / "Knowledge Base" / "_Governance" / "scopes" / "all.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: [\"**\"]\n",
        encoding="utf-8",
    )
    assert scrubber.enabled(vault) is True


def test_scrubber_stays_on_for_a_blocked_policy(vault: Path) -> None:
    """The three-state contract INVERTS here, and that is deliberate.

    Everywhere else `blocked` means "withhold". For the scrubber, `blocked`
    means the policy cannot be trusted — so the rule that would have disabled
    it cannot be trusted either, and the safe reading is ON. `empty` is ON for
    the same reason: no rule exists to have turned it off.
    """
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "s.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: [\"**\"]\n",
        encoding="utf-8",
    )
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "r.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\naudience: owner\nceiling: 9\n"
        "options:\n  credential_scrubber: off\n",
        encoding="utf-8",
    )
    from exomem.governance import policy as policy_module

    assert policy_module.load(vault).blocked is True
    assert scrubber.enabled(vault) is True


# --------------------------------------------------------------------------
# D1 — postfilter walks results, ToolResult text blocks only
# --------------------------------------------------------------------------


def test_postfilter_blocks_a_credential_on_an_ungoverned_vault(vault: Path) -> None:
    """The one intentional behavior change for a vault with no `_Governance/`:
    the credential never crosses the boundary, and a notice reports the block."""
    result = {"hits": [{"path": "a.md", "excerpt": f"key={AWS_KEY}"}]}
    out = egress.postfilter("find", result, vault)
    assert AWS_KEY not in json.dumps(out)
    assert scrubber.NOTICE in json.dumps(out)


def test_postfilter_leaves_a_clean_ungoverned_result_identical(vault: Path) -> None:
    result = {"hits": [{"path": "a.md", "excerpt": "retry with jitter"}], "ref": MEMORY_REF}
    assert egress.postfilter("find", result, vault) == result


def test_postfilter_is_idempotent(vault: Path) -> None:
    """`invoke_command` runs the filter and `bind_vault` runs it again on the
    MCP path. The second pass must be a no-op — an already-replaced credential
    is `NOTICE` text and matches nothing, so defense in depth costs a walk,
    not a double rewrite."""
    result = {"hits": [{"path": "a.md", "excerpt": f"key={AWS_KEY}"}]}
    once = egress.postfilter("find", result, vault)
    twice = egress.postfilter("find", json.loads(json.dumps(once)), vault)
    assert once == twice
    assert json.dumps(once).count(scrubber.NOTICE) == 1


def test_postfilter_scans_tool_result_text_blocks_only(vault: Path) -> None:
    """`op_get_video_frames` returns image bytes beside its text block. The
    walker must scan the text and never touch — or entropy-scan — the JPEG."""
    from fastmcp.tools import ToolResult
    from mcp.types import ImageContent, TextContent

    jpeg_b64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBk"
    result = ToolResult(
        content=[
            TextContent(type="text", text=f"meta with {AWS_KEY}"),
            ImageContent(type="image", data=jpeg_b64, mimeType="image/jpeg"),
        ],
        structured_content={"frames": 1},
    )
    out = egress.postfilter("get_video_frames", result, vault)
    text_blocks = [b for b in out.content if getattr(b, "type", None) == "text"]
    image_blocks = [b for b in out.content if getattr(b, "type", None) == "image"]
    assert AWS_KEY not in text_blocks[0].text
    assert scrubber.NOTICE in text_blocks[0].text
    # The image payload is byte-identical: never scanned, never rewritten.
    assert image_blocks[0].data == jpeg_b64


def test_postfilter_blocked_policy_returns_no_content(vault: Path) -> None:
    """The `.blocked` fail-closed contract at the postfilter consumer: a
    refused cold-start compile must not fall through to the open fast path."""
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "s.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: [\"**\"]\n",
        encoding="utf-8",
    )
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "r.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\naudience: external\nceiling: 9\n",
        encoding="utf-8",
    )
    from exomem.governance import policy as policy_module
    from exomem.governance.principal import most_restrictive_principal, request_scope

    assert policy_module.load(vault).blocked is True
    result = {"hits": [{"path": "Knowledge Base/Notes/Insights/x.md", "excerpt": "secret"}]}
    with request_scope(most_restrictive_principal(surface="rest")):
        out = egress.postfilter("find", result, vault)
    assert out["hits"] == []


def test_postfilter_is_registered_at_the_shared_dispatcher() -> None:
    """D1: the terminal filter must sit in `writer_lease.invoke_command`, not
    in `bind_vault` — `bind_vault` is MCP-only and the `EXOMEM_RETRIEVE_INJECT`
    hook deliberately travels the REST-then-CLI paths that skip it."""
    import inspect

    from exomem import writer_lease

    source = inspect.getsource(writer_lease)
    assert "postfilter" in source, (
        "the terminal postfilter is missing from the one dispatcher shared by "
        "MCP, REST, hosted, and CLI"
    )


def test_postfilter_second_pass_registered_at_bind_vault() -> None:
    import inspect

    from exomem import command_surface

    assert "postfilter" in inspect.getsource(command_surface)


def test_every_product_command_resolves_to_a_registered_projector() -> None:
    """Coverage is registry-derived and DEFAULT-DENY: every command needs a
    declared projector kind unless it is on the explicit metadata-only
    opt-out."""
    from exomem import commands

    registry = {command.name: command for command in commands.COMMANDS}
    assert egress.unprojected_commands(registry) == ()
    egress.assert_projectors_registered(registry)


def test_a_new_command_without_a_projector_fails_the_check() -> None:
    """The planted-defect proof. A backstop whose test cannot fail when the
    backstop is removed is not a backstop — the previous version of this test
    handed a TUPLE of Command objects to a signature that declares a mapping,
    so the string intersection was always empty and it passed vacuously."""
    from exomem import commands

    registry = {command.name: command for command in commands.COMMANDS}
    registry["brand_new_content_surface"] = object()
    assert egress.unprojected_commands(registry) == ("brand_new_content_surface",)
    with pytest.raises(RuntimeError, match="PROJECTOR_MISSING"):
        egress.assert_projectors_registered(registry)


def test_removing_a_projector_makes_boot_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove an existing projector and the assertion must refuse."""
    from exomem import commands

    registry = {command.name: command for command in commands.COMMANDS}
    patched = dict(egress._PROJECTORS)
    patched.pop("page")
    monkeypatch.setattr(egress, "_PROJECTORS", patched)
    missing = egress.unprojected_commands(registry)
    assert "get" in missing and "fetch" in missing
    with pytest.raises(RuntimeError, match="PROJECTOR_MISSING"):
        egress.assert_projectors_registered(registry)


def test_coverage_check_rejects_a_non_mapping_loudly() -> None:
    """The exact defect that made the old test vacuous: a sequence silently
    intersected to nothing instead of being rejected."""
    from exomem import commands

    with pytest.raises(TypeError, match="mapping"):
        egress.unprojected_commands(commands.COMMANDS)


def test_metadata_only_opt_out_names_no_content_surface() -> None:
    """The opt-out is the only place a command escapes the requirement, so it
    must stay small and genuinely metadata-only."""
    for name in ("find", "search", "fetch", "get", "graph_context", "overview"):
        assert name not in egress._METADATA_ONLY_COMMANDS, name


# --------------------------------------------------------------------------
# P1 — the ALIAS layer is a second registry the leaf check cannot see
# --------------------------------------------------------------------------


def test_the_product_alias_layer_is_invisible_to_the_leaf_coverage_check() -> None:
    """The blind spot this check exists to close.

    `browse_memory`, `review_memory` and `maintain_memory` are the names a
    client actually calls, and NONE of them is in `commands.COMMANDS` — so
    `assert_projectors_registered({COMMANDS})` cannot say anything about
    them. Any alias layer that dispatches to leaves is a second registry
    with no coverage guarantee unless it gets its own default-deny check.
    """
    from exomem import commands

    leaves = {command.name for command in commands.COMMANDS}
    aliases = {command.name for command in commands.PRODUCT_COMMANDS}
    assert {"browse_memory", "review_memory", "maintain_memory"} <= aliases - leaves


def test_every_product_alias_resolves_to_a_registered_projector() -> None:
    """Default-deny over the ALIAS registry: an alias is covered only when
    every route it dispatches to reaches a covered leaf, or when the alias
    itself is on the explicit metadata-only opt-out."""
    from exomem import commands

    aliases = {command.name: command for command in commands.PRODUCT_COMMANDS}
    leaves = {command.name: command for command in commands.COMMANDS}
    assert egress.unprojected_aliases(aliases, leaves) == ()
    egress.assert_alias_projectors_registered(aliases, leaves)


def test_a_new_alias_without_a_projector_fails_the_check() -> None:
    """Planted defect, ALIAS layer: a product-facing name that routes nowhere
    and is not opted out must refuse the boot."""
    from exomem import commands

    aliases = {command.name: command for command in commands.PRODUCT_COMMANDS}
    leaves = {command.name: command for command in commands.COMMANDS}
    aliases["brand_new_product_alias"] = _FakeAlias(routes=())
    assert egress.unprojected_aliases(aliases, leaves) == ("brand_new_product_alias",)
    with pytest.raises(RuntimeError, match="ALIAS_PROJECTOR_MISSING"):
        egress.assert_alias_projectors_registered(aliases, leaves)


def test_an_alias_routing_to_an_unprojected_leaf_fails_the_check() -> None:
    """The subtler defect: the alias HAS routes, but one of them reaches a
    leaf with no projector. Partial coverage is not coverage."""
    from exomem import commands

    aliases = {command.name: command for command in commands.PRODUCT_COMMANDS}
    leaves = {command.name: command for command in commands.COMMANDS}
    leaves["ungated_leaf"] = object()
    aliases["half_gated_alias"] = _FakeAlias(routes=("find", "ungated_leaf"))
    assert "half_gated_alias" in egress.unprojected_aliases(aliases, leaves)
    with pytest.raises(RuntimeError, match="ALIAS_PROJECTOR_MISSING"):
        egress.assert_alias_projectors_registered(aliases, leaves)


def test_alias_check_rejects_a_non_mapping_loudly() -> None:
    """Same vacuity trap the leaf check already closed."""
    from exomem import commands

    leaves = {command.name: command for command in commands.COMMANDS}
    with pytest.raises(TypeError, match="mapping"):
        egress.unprojected_aliases(commands.PRODUCT_COMMANDS, leaves)


def test_removing_a_leaf_projector_breaks_the_aliases_that_route_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias check is not decorative: it inherits leaf coverage, so
    dropping the `hit` projector must surface at the alias that uses it."""
    from exomem import commands

    aliases = {command.name: command for command in commands.PRODUCT_COMMANDS}
    leaves = {command.name: command for command in commands.COMMANDS}
    patched = dict(egress._PROJECTORS)
    patched.pop("hit")
    monkeypatch.setattr(egress, "_PROJECTORS", patched)
    assert "ask_memory" in egress.unprojected_aliases(aliases, leaves)


def test_alias_boot_assertion_runs_at_commands_import() -> None:
    """Structural, not advisory: the module that builds the alias registry
    must run the check at import so a bad alias fails the build."""
    import inspect

    from exomem import commands

    assert "assert_alias_projectors_registered" in inspect.getsource(commands)


# --------------------------------------------------------------------------
# P2 / H9 — hand-registered MCP resources + prompt (D1's named residual)
# --------------------------------------------------------------------------

ADOPTION_RUN_ID = "20260101-governed-vault-aaaaaaaa"
ADOPTION_WITHHELD = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"


def _govern_patterns_shut(vault: Path) -> None:
    """Withhold `Notes/Patterns/**` from `external` entirely (ceiling L0)."""
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "patterns.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Patterns\npaths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "patterns-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


def _seed_adoption_run(vault: Path, *, run_ref: str) -> None:
    """Persist a run document that names a governed vault path.

    Realistic shape: `inventory` rows and `selection.paths` are exactly how a
    run records which vault items it touched, and `status()` returns both
    verbatim.
    """
    from exomem.adoption_run import AdoptionRunStore

    AdoptionRunStore(vault).save(
        {
            "schema_version": 1,
            "run_id": ADOPTION_RUN_ID,
            "run_ref": run_ref,
            "created": "2026-01-01T00:00:00Z",
            "phase": "selecting",
            "source_root": "",
            "scan_summary": {},
            "inventory": [
                {
                    "path": ADOPTION_WITHHELD,
                    "bytes": 10,
                    "mtime": 0.0,
                    "eligible": True,
                    "junk": False,
                    "reason": None,
                }
            ],
            "inventory_truncated": 0,
            "selection": {"paths": [ADOPTION_WITHHELD]},
            "plan": None,
            "outcomes": {},
            "finish": None,
            "cancel": None,
            "errors": [],
        }
    )


def _read_adoption_resource(vault: Path, uri: str) -> str:
    """Drive the REAL registered handler through FastMCP's resource reader."""
    import asyncio

    from fastmcp import FastMCP

    from exomem import server as server_module

    mcp = FastMCP("test-adoption")
    server_module.register_adoption_mcp(mcp, vault_root=vault)
    result = asyncio.run(mcp.read_resource(uri))
    blocks = [
        getattr(block, "content", None) or getattr(block, "text", "")
        for block in result.contents
    ]
    assert blocks and any(blocks), f"resource {uri} returned nothing to assert on"
    return json.dumps(blocks, default=str)


def _as_external_connector_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `resolve_mcp_principal()` — the resolver the handler itself calls —
    answer with a remote `external` audience instead of local stdio owner."""
    from exomem.governance import principal as principal_module

    monkeypatch.setattr(
        principal_module,
        "resolve_mcp_principal",
        lambda: principal_module.RequestPrincipal(audience_id="external", surface="mcp"),
    )


def test_adoption_run_document_names_a_withheld_path_before_the_gate(
    vault: Path,
) -> None:
    """The hole, stated as a fact: `adoption_run.status()` — which the
    `exomem://adoption/run/{id}` resource returns verbatim — carries the
    governed path with no filtering of its own."""
    from exomem import adoption_run

    _govern_patterns_shut(vault)
    _seed_adoption_run(vault, run_ref="exomem://adoption/run/x")
    doc = adoption_run.status(vault, run_id=ADOPTION_RUN_ID)
    assert ADOPTION_WITHHELD in json.dumps(doc, default=str)


def test_adoption_run_resource_withholds_a_governed_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H9: `@mcp.resource` never reaches `bind_vault` or `invoke_command`, so
    without an explicit gate the resource is a clean bypass of the whole
    release plane."""
    _as_external_connector_client(monkeypatch)
    _govern_patterns_shut(vault)
    _seed_adoption_run(vault, run_ref="exomem://adoption/run/x")
    payload = _read_adoption_resource(vault, f"exomem://adoption/run/{ADOPTION_RUN_ID}")
    assert ADOPTION_WITHHELD not in payload


def test_adoption_run_resource_still_serves_the_owner(vault: Path) -> None:
    """The gate is a release decision, not a blanket redaction: local stdio
    resolves to the owner and still sees its own run."""
    _govern_patterns_shut(vault)
    _seed_adoption_run(vault, run_ref="exomem://adoption/run/x")
    payload = _read_adoption_resource(vault, f"exomem://adoption/run/{ADOPTION_RUN_ID}")
    assert ADOPTION_WITHHELD in payload


def test_adoption_runs_collection_resource_is_gated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collection resource is scrubbed too — same handler class."""
    _as_external_connector_client(monkeypatch)
    _seed_adoption_run(vault, run_ref=f"ref {AWS_KEY}")
    payload = _read_adoption_resource(vault, "exomem://adoption/runs")
    assert AWS_KEY not in payload
    assert scrubber.NOTICE in payload


def test_continue_adoption_prompt_is_gated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`continue_adoption` returns `handoff.prompt_text`, built from stored run
    fields — it must cross the same egress boundary as every tool result."""
    import asyncio

    from fastmcp import FastMCP

    from exomem import server as server_module

    _as_external_connector_client(monkeypatch)
    _seed_adoption_run(vault, run_ref=f"ref {AWS_KEY}")
    mcp = FastMCP("test-adoption-prompt")
    server_module.register_adoption_mcp(mcp, vault_root=vault)
    rendered = asyncio.run(mcp.render_prompt("continue_adoption"))
    text = json.dumps(
        [getattr(m.content, "text", "") for m in rendered.messages], default=str
    )
    assert AWS_KEY not in text
    assert scrubber.NOTICE in text


#: Modules that hand-register HTTP routes serving NO vault content: liveness,
#: readiness, favicons, the Studio SPA bundle, OAuth discovery documents, and
#: the hosted control-plane lifecycle operations (quiesce/seal/resume/export),
#: which are the owner's own portability and operator surface rather than a
#: disclosure surface. Every entry is a deliberate, reviewed exemption — the
#: list is the argument, so adding to it is a decision someone has to defend.
_NO_VAULT_CONTENT_ROUTE_MODULES = frozenset({"server_assets.py"})


def _gated(text: str) -> bool:
    """Does this module reach the release plane at all?

    Either directly (`postfilter`, the binary-egress consults) or by handing
    the request to `writer_lease.invoke_command`, which runs the postfilter
    itself — that is how `server_rest`'s `/api/{cmd}` routes are covered, and
    routing through the shared dispatcher is the *preferred* shape. A handler
    that does neither is streaming or serializing on its own authority.
    """
    return any(
        marker in text
        for marker in (
            "postfilter",
            "release_allows_download",
            "release_allows_frames",
            "invoke_command(",
        )
    )


def test_no_hand_registered_mcp_handler_skips_the_gate() -> None:
    """Sweep guard: every `@mcp.resource` / `@mcp.prompt` handler in the tree
    must sit in a module that consults the release plane. A new
    hand-registered handler in an ungated module fails this immediately."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "@mcp.resource" not in text and "@mcp.prompt" not in text:
            continue
        if not _gated(text):
            offenders.append(path.name)
    assert offenders == []


def test_no_hand_registered_http_route_skips_the_gate() -> None:
    """The other half of the sweep, and the one that was missing.

    `custom_route` handlers bypass `bind_vault` and `invoke_command` exactly as
    `@mcp.resource` does — that is how the hosted transfer-v2 download route
    streamed a withheld file's complete bytes on nothing but a signed grant.
    Any module registering HTTP routes must either consult the release plane or
    be on the reviewed no-vault-content exemption list.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "custom_route(" not in text:
            continue
        if path.name in _NO_VAULT_CONTENT_ROUTE_MODULES:
            continue
        if not _gated(text):
            offenders.append(path.name)
    assert offenders == [], (
        f"hand-registered HTTP routes with no release consult: {offenders}"
    )


def test_the_route_exemption_list_stays_honest() -> None:
    """An exemption that no longer names a real module is a stale waiver, and
    a module that gains a release consult should leave the list."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    for name in _NO_VAULT_CONTENT_ROUTE_MODULES:
        module = src / name
        assert module.exists(), f"exemption names a module that no longer exists: {name}"
        assert "custom_route(" in module.read_text(encoding="utf-8"), (
            f"{name} no longer registers routes; drop the exemption"
        )


def test_scrub_value_rebuilds_namedtuples_correctly() -> None:
    """M16: the postfilter walks EVERY command result, so a leaf returning a
    NamedTuple crashed the boundary — `type(value)(items)` passes one iterable
    to a constructor expecting N positional fields."""
    from typing import NamedTuple

    class Row(NamedTuple):
        path: str
        note: str

    cleaned, blocked = scrubber.scrub_value(Row(path="a.md", note=f"key={AWS_KEY}"))
    assert isinstance(cleaned, Row)
    assert cleaned.path == "a.md"
    assert blocked and AWS_KEY not in cleaned.note

    plain, _ = scrubber.scrub_value(("a", "b"))
    assert plain == ("a", "b")


# --------------------------------------------------------------------------
# N3 — a bare `str` payload has no entries to filter
# --------------------------------------------------------------------------


def test_continue_adoption_prompt_redacts_a_withheld_reference(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: `continue_adoption` returns a bare string, and the dispatcher's
    walker returns any non-Mapping/non-list node by identity — so
    `handoff.prompt_text` carried the withheld path, and its wikilink, all the
    way to the wire. Binding a principal and scrubbing credentials cannot
    touch this shape; the string itself needs reference-aware redaction.
    """
    import asyncio

    from fastmcp import FastMCP

    from exomem import server as server_module

    _as_external_connector_client(monkeypatch)
    _govern_patterns_shut(vault)
    _seed_adoption_run(
        vault,
        run_ref=f"{ADOPTION_WITHHELD} and [[kill-switch-for-risky-releases]]",
    )
    mcp = FastMCP("test-adoption-n3")
    server_module.register_adoption_mcp(mcp, vault_root=vault)
    rendered = asyncio.run(mcp.render_prompt("continue_adoption"))
    text = json.dumps(
        [getattr(m.content, "text", "") for m in rendered.messages], default=str
    )
    assert ADOPTION_WITHHELD not in text, "withheld path survived in the prompt"
    assert "kill-switch-for-risky-releases" not in text, (
        "withheld wikilink survived in the prompt"
    )


def test_continue_adoption_prompt_keeps_permitted_references(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redaction must be a release decision, not a blanket strip."""
    import asyncio

    from fastmcp import FastMCP

    from exomem import server as server_module

    permitted = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
    _as_external_connector_client(monkeypatch)
    _govern_patterns_shut(vault)
    _seed_adoption_run(vault, run_ref=permitted)
    mcp = FastMCP("test-adoption-n3-keep")
    server_module.register_adoption_mcp(mcp, vault_root=vault)
    rendered = asyncio.run(mcp.render_prompt("continue_adoption"))
    text = json.dumps(
        [getattr(m.content, "text", "") for m in rendered.messages], default=str
    )
    assert "rrf-fusion-beats-score-normalization" in text


def test_nested_adoption_payload_gates_every_existing_artifact_kind_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Residual prompt/resource payloads use the same artifact resolver.

    Coverage is based on actual vault artifacts, not a suffix regex: paths
    with spaces, office/media files, bare names, wikilinks, and full refs are
    recursively gated even below `handoff.prompt_text`.
    """
    from urllib.parse import quote

    from exomem import server as server_module

    _as_external_connector_client(monkeypatch)
    _govern_patterns_shut(vault)
    root = vault / "Knowledge Base" / "Notes" / "Patterns" / "Private Artifacts"
    root.mkdir(parents=True, exist_ok=True)
    names = (
        "private note.md",
        "private rows.csv",
        "private rows.tsv",
        "private data.json",
        "private report.pdf",
        "private brief.docx",
        "private image.png",
        "private audio.mp3",
        "private video.mp4",
    )
    for name in names:
        (root / name).write_bytes(b"governed-artifact")
    rel = "Knowledge Base/Notes/Patterns/Private Artifacts/private report.pdf"
    payload = {
        "handoff": {
            "prompt_text": (
                f"Open {rel}; [[{rel}]]; exomem://vault/{quote(rel)}; "
                "and bare private report.pdf twice: private report.pdf"
            )
        },
        "resources": [
            {
                "resource": (
                    "Knowledge Base/Notes/Patterns/Private Artifacts/" + name
                )
            }
            for name in names
        ],
    }
    out = server_module._gated_adoption_egress(vault, "adoption_run", payload)
    serialized = json.dumps(out, default=str)
    assert "Private Artifacts" not in serialized
    assert "Private%20Artifacts" not in serialized
    assert "private report.pdf" not in serialized
    assert egress.WITHHELD_REFERENCE in serialized
    events = vault / "Knowledge Base" / "_Governance" / "events"
    records = [
        json.loads(line)
        for path in sorted(events.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    disclosure = [record for record in records if record["event_type"] == "disclosure"]
    assert disclosure
    report_outcomes = [
        outcome
        for outcome in disclosure[-1]["outcomes"]
        if outcome.get("content_hash")
        == __import__("hashlib").sha256(b"governed-artifact").hexdigest()
    ]
    assert len(report_outcomes) == len(names)


def test_nested_adoption_payload_gates_artifact_references_in_mapping_keys(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import server as server_module

    _as_external_connector_client(monkeypatch)
    _govern_patterns_shut(vault)
    payload = {
        "handoff": {
            "resources": {
                "[[kill-switch-for-risky-releases]]": {"prompt_text": "safe"}
            }
        }
    }
    out = server_module._gated_adoption_egress(vault, "adoption_run", payload)
    assert "kill-switch-for-risky-releases" not in json.dumps(out, default=str)


# --------------------------------------------------------------------------
# N8 — alias coverage must not be granted by a hand-written annotation
# --------------------------------------------------------------------------


def test_routes_are_derived_from_the_leaf_not_trusted() -> None:
    """N8: `adoption_studio` declares `adopt` and never calls `op_adopt`, so
    it claimed coverage through the very leaf that N1 showed leaking — while
    reaching its content by another path entirely. A coverage check that
    believes an annotation is checking the annotation, not the code."""
    from exomem import commands

    alias = {c.name: c for c in commands.PRODUCT_COMMANDS}["adoption_studio"]
    derived = egress.derived_routes(alias.leaf)
    assert "adopt" in (alias.routes or ()), "fixture assumption: it declares adopt"
    assert "adopt" not in derived, "fixture assumption: it never calls op_adopt"


def test_a_fabricated_route_cannot_grant_alias_coverage() -> None:
    """Planted defect: an alias whose leaf reaches nothing gated must stay
    uncovered no matter what its `routes` tuple claims."""
    from exomem import commands

    def _leaf_that_calls_nothing_gated(vault_root, **kwargs):
        return {"path": "Knowledge Base/Notes/Patterns/secret.md"}

    aliases = {c.name: c for c in commands.PRODUCT_COMMANDS}
    leaves = {c.name: c for c in commands.COMMANDS}
    aliases["liar"] = _FakeAlias(routes=("find",), leaf=_leaf_that_calls_nothing_gated)
    assert "liar" in egress.unprojected_aliases(aliases, leaves)


def test_every_op_a_leaf_actually_calls_is_declared() -> None:
    """The dangerous divergence direction: an op that IS called but is not
    declared means the capability map understates what the alias reaches."""
    from exomem import commands

    leaf_names = {c.name for c in commands.COMMANDS}
    undeclared: dict[str, set[str]] = {}
    for alias in commands.PRODUCT_COMMANDS:
        derived = egress.derived_routes(alias.leaf) & leaf_names
        missing = derived - set(alias.routes or ())
        if missing:
            undeclared[alias.name] = missing
    assert undeclared == {}, f"called but undeclared: {undeclared}"


def test_real_alias_registry_still_covered_under_derived_routes() -> None:
    """…and the shipped registry must still pass once coverage stops
    trusting declarations."""
    from exomem import commands

    aliases = {c.name: c for c in commands.PRODUCT_COMMANDS}
    leaves = {c.name: c for c in commands.COMMANDS}
    assert egress.unprojected_aliases(aliases, leaves) == ()


# --------------------------------------------------------------------------
# Post-#327: the dispatcher was restructured under us. Prove the hook still
# sits on the real dispatch path, at RUNTIME rather than by reading source.
# --------------------------------------------------------------------------


def test_postfilter_runs_through_the_restructured_dispatcher(vault: Path) -> None:
    """PR #327 moved semantic validation and model loading outside the
    mutation boundary and turned `invoke_command` into a thin delegator to
    `get_manager().invoke(...)`. The postfilter wraps that call's RESULT, so
    the restructuring should not have moved it — but "should not have" is not
    evidence, and an inspect-based assertion only proves the source contains
    the string.

    This drives a real command through the real dispatcher on a governed vault
    and asserts the credential never crosses the boundary.
    """
    from exomem import commands
    from exomem.writer_lease import invoke_command

    leaky = f"key={AWS_KEY}"
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "leaky.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ntype: insight\n---\n{leaky}\n", encoding="utf-8")

    command = {c.name: c for c in commands.COMMANDS}["get"]
    result = invoke_command(command, vault, path="Knowledge Base/Notes/Insights/leaky.md")
    blob = json.dumps(result, default=str)
    assert AWS_KEY not in blob, "the postfilter is no longer on the dispatch path"
    assert scrubber.NOTICE in blob


def test_every_surface_adapter_still_routes_through_invoke_command() -> None:
    """The premise D1 rests on: `invoke_command` is the ONE dispatcher, and
    `get_manager().invoke` has exactly one caller. If a surface ever reaches
    the manager directly, the terminal filter is bypassed for that surface."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    direct_manager_callers = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "get_manager().invoke(" in text and path.name != "writer_lease.py":
            direct_manager_callers.append(path.name)
    assert direct_manager_callers == [], (
        f"these bypass invoke_command and therefore the postfilter: "
        f"{direct_manager_callers}"
    )


# --------------------------------------------------------------------------
# An error is a payload — the terminal filter applies to it too
#
# `postfilter` guards the value a command RETURNS. `_invoke()` is evaluated as
# an ARGUMENT to it, so a command that raises never reaches the filter and its
# payload crosses `invoke_command` — the one dispatcher shared by MCP, REST,
# hosted and CLI — untouched. `AMBIGUOUS_REFERENCE` proved the class reachable
# by embedding the colliding vault paths in its message.
# --------------------------------------------------------------------------

ERROR_WITHHELD = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
ERROR_WITHHELD_STEM = "kill-switch-for-risky-releases"

# A withheld item the CALLER NEVER NAMED. This is the reference class the error
# filter exists for: the vault volunteered it (a collision list, a resolver's
# second candidate), so redacting it removes something the caller did not have.
#
# Do NOT write these tests against a path the caller also passes as an argument.
# A reference the caller supplied is exempt from redaction ON PURPOSE — see
# `egress.postfilter_error`. Substituting a marker for the caller's own input
# turns `NOT_FOUND` into a binary existence oracle ("[withheld]" = exists,
# echoed path = absent), which is strictly worse than echoing it back. An
# earlier revision of these tests used the caller's own path throughout and so
# asserted the oracle rather than the guarantee.
# Every spelling a caller may legitimately use for the SAME item. Each must be
# echoed back identically whether the item exists-and-is-withheld or is absent;
# a difference in any one of them is an existence oracle for that form.
_CALLER_INPUT_FORMS = (
    "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md",
    "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases",
    "Notes/Patterns/kill-switch-for-risky-releases.md",
    "Notes/Patterns/kill-switch-for-risky-releases",
    "kill-switch-for-risky-releases",
)

ERROR_VOLUNTEERED = "Knowledge Base/Notes/Patterns/never-named-by-caller.md"
ERROR_VOLUNTEERED_STEM = "never-named-by-caller"
# A caller-supplied path that is NOT the one under test, so the exemption
# covers only what the request actually named.
ERROR_CALLER_PATH = "Knowledge Base/Notes/Patterns/caller-asked-for-this.md"


def _seed_volunteered(vault: Path) -> None:
    """The volunteered item must exist, or there is nothing to withhold."""
    target = vault / ERROR_VOLUNTEERED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntype: pattern\n---\nx\n", encoding="utf-8")


def _raising_command(name: str, message: str, *, mutating: bool = False):
    """A real registered command with a leaf that raises.

    A synthetic leaf is the point: no call site has ever gated it, so anything
    that filters its payload is filtering at the dispatcher.
    """
    from dataclasses import replace

    from exomem import commands

    def _leaf(*_args, **_kwargs):
        raise ValueError(message)

    command = {c.name: c for c in commands.COMMANDS}[name]
    return replace(command, leaf=_leaf, cli_writes=mutating)


def _external_scope():
    from exomem.governance.principal import RequestPrincipal, request_scope

    return request_scope(RequestPrincipal(audience_id="external", surface="mcp"))


# Every way the VOLUNTEERED reference can be written. Each must redact, and
# each is a form the caller never sent — so the exemption cannot mask a miss.
ERROR_REFERENCE_FORMS = (
    ERROR_VOLUNTEERED,
    "Notes/Patterns/never-named-by-caller.md",
    "never-named-by-caller.md",
    f"[[{ERROR_VOLUNTEERED.removesuffix('.md')}]]",
    f"[[{ERROR_VOLUNTEERED_STEM}]]",
    "exomem://vault/Knowledge%20Base/Notes/Patterns/"
    "never-named-by-caller.md",
)


@pytest.mark.parametrize("form", ERROR_REFERENCE_FORMS)
def test_a_raised_payload_naming_a_withheld_item_is_filtered_at_the_dispatcher(
    vault: Path, form: str
) -> None:
    """Every form the reference can be written in, on the read-only path."""
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)
    _seed_volunteered(vault)
    command = _raising_command("get", f"could not read {form}")

    with _external_scope():
        with pytest.raises(ValueError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)

    text = str(excinfo.value)
    assert ERROR_VOLUNTEERED_STEM not in text, (
        f"{form!r} crossed the boundary: {text!r}"
    )
    assert "Knowledge Base/Notes/Patterns/never" not in text
    assert egress.WITHHELD_REFERENCE in text


def test_a_raised_payload_is_filtered_on_the_mutation_path_too(vault: Path) -> None:
    """The read-only path takes the disclosure boundary and the mutation path
    does not, so the two are separate returns in the dispatcher — and the
    mutation path is the one a client reaches through every write command."""
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)
    command = _raising_command("note", f"could not write {ERROR_WITHHELD}", mutating=True)

    with _external_scope():
        with pytest.raises(Exception) as excinfo:
            invoke_command(command, vault, title="x", body="y")

    text = str(excinfo.value)
    assert ERROR_WITHHELD_STEM not in text, f"mutation path leaked: {text!r}"
    assert egress.WITHHELD_REFERENCE in text


def test_a_raised_payload_is_filtered_in_its_structured_details(vault: Path) -> None:
    """`OpError.details` is merged into the public error dict, so a reference
    parked there reaches a client without ever appearing in the message."""
    from dataclasses import replace

    from exomem import commands
    from exomem.cli_ops import OpError
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)

    def _leaf(*_args, **_kwargs):
        raise OpError(
            "REFERENCE_NOT_FOUND",
            "no such page",
            details={"candidates": [ERROR_VOLUNTEERED]},
        )

    _seed_volunteered(vault)
    command = replace({c.name: c for c in commands.COMMANDS}["get"], leaf=_leaf)
    with _external_scope():
        with pytest.raises(OpError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)

    error = excinfo.value
    assert error.code == "REFERENCE_NOT_FOUND", "the stable code must survive filtering"
    assert ERROR_VOLUNTEERED_STEM not in json.dumps(
        error.as_public_dict(), default=str
    )


def test_two_different_volunteered_items_raise_byte_identical_errors(
    vault: Path,
) -> None:
    """Indistinguishability across volunteered items: if the filtered text
    differed per item it would still be an oracle for which one the vault hit.

    Both references are ones the caller never named, so both must redact to the
    same marker. The caller's own path is held constant and is a THIRD path, so
    the exemption cannot mask the comparison."""
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)
    _seed_volunteered(vault)
    second = vault / "Knowledge Base" / "Notes" / "Patterns" / "another-private.md"
    second.write_text("---\ntype: pattern\n---\nx\n", encoding="utf-8")

    def _text(reference: str) -> str:
        command = _raising_command("get", f"could not read {reference}")
        with _external_scope():
            with pytest.raises(ValueError) as excinfo:
                invoke_command(command, vault, path=ERROR_CALLER_PATH)
        return str(excinfo.value)

    assert _text(ERROR_VOLUNTEERED) == _text(
        "Knowledge Base/Notes/Patterns/another-private.md"
    )
    assert ERROR_VOLUNTEERED_STEM not in _text(ERROR_VOLUNTEERED)


def test_a_caller_supplied_path_is_echoed_whether_or_not_it_exists(
    vault: Path,
) -> None:
    """THE indistinguishability guarantee, stated correctly.

    One path, asked for twice: once while it exists and is withheld, once after
    deleting it. The caller controls the input, so the ONLY bit that can differ
    is whether the item exists — and that bit must not be observable.

    Comparing two DIFFERENT paths (as an earlier revision did) cannot fail: the
    error legitimately echoes whichever path the caller asked for, so differing
    inputs are expected to differ. Vary the condition, hold the input fixed."""
    from exomem import commands
    from exomem import find as find_module
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)
    target = vault / ERROR_WITHHELD

    def _text(form: str) -> str:
        """The REAL `get` leaf, not a synthetic raiser.

        A synthetic command builds its message once from a fixed constant, so
        the two conditions are identical by construction and the test cannot
        observe the branch asymmetry that only the real leaf produces — which
        is exactly where the residual oracle lived."""
        command = {c.name: c for c in commands.COMMANDS}["get"]
        with _external_scope():
            with pytest.raises(ValueError) as excinfo:
                invoke_command(command, vault, path=form)
        return str(excinfo.value)

    assert target.is_file()
    present = {form: _text(form) for form in _CALLER_INPUT_FORMS}
    target.unlink()
    find_module.clear_cache()
    absent = {form: _text(form) for form in _CALLER_INPUT_FORMS}

    divergent = {f: (present[f], absent[f]) for f in _CALLER_INPUT_FORMS
                 if present[f] != absent[f]}
    assert not divergent, (
        "EXISTENCE ORACLE: a caller-supplied path yields different text "
        f"depending on whether it exists, for form(s): {divergent}"
    )
    for form in _CALLER_INPUT_FORMS:
        assert egress.WITHHELD_REFERENCE not in present[form], form


def test_an_ungoverned_vault_keeps_its_error_text(vault: Path) -> None:
    """The empty-policy fast path is untouched: no governance tree means no
    reference gating, and only the always-on scrubber may alter the text."""
    from exomem.writer_lease import invoke_command

    message = f"could not read {ERROR_WITHHELD}"
    command = _raising_command("get", message)
    with pytest.raises(ValueError) as excinfo:
        invoke_command(command, vault, path=ERROR_WITHHELD)
    assert str(excinfo.value) == message


def test_an_ungoverned_vault_still_scrubs_a_credential_out_of_an_error(
    vault: Path,
) -> None:
    """D7's always-on scrubber is policy-independent, and an error payload is
    exactly as capable of carrying a leaked key as a result is."""
    from exomem.writer_lease import invoke_command

    command = _raising_command("get", f"upstream rejected {AWS_KEY}")
    with pytest.raises(ValueError) as excinfo:
        invoke_command(command, vault, path=ERROR_WITHHELD)
    text = str(excinfo.value)
    assert AWS_KEY not in text
    assert scrubber.NOTICE in text


def test_removing_the_dispatcher_error_filter_reopens_the_leak(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planted-defect proof. A backstop whose test cannot fail when the
    backstop is removed is not a backstop: neutralise the dispatcher's error
    filter and the same probe must leak again."""
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)
    _seed_volunteered(vault)
    monkeypatch.setattr(
        egress, "postfilter_error", lambda name, error, root, **_kwargs: error
    )
    command = _raising_command("get", f"could not read {ERROR_VOLUNTEERED}")

    with _external_scope():
        with pytest.raises(ValueError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)

    assert ERROR_VOLUNTEERED in str(excinfo.value)


def test_the_error_guarantee_does_not_depend_on_the_raise_site(vault: Path) -> None:
    """The structural claim, stated as a test.

    `memory_refs` is the raise site known to be reachable, but a guarantee that
    lives there covers exactly one leaf. This drives a leaf that has never been
    reviewed by anyone and shares no code with `memory_refs`, and it must still
    be covered — which is only true if the filter sits at the dispatcher.
    """
    import inspect

    from exomem import writer_lease
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)

    class _NeverReviewedError(RuntimeError):
        """A brand-new exception type, from a brand-new leaf."""

    def _leaf(*_args, **_kwargs):
        raise _NeverReviewedError(f"collision at {ERROR_VOLUNTEERED}")

    from dataclasses import replace

    from exomem import commands

    _seed_volunteered(vault)
    command = replace({c.name: c for c in commands.COMMANDS}["get"], leaf=_leaf)
    with _external_scope():
        with pytest.raises(_NeverReviewedError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)
    assert ERROR_VOLUNTEERED_STEM not in str(excinfo.value)
    assert isinstance(excinfo.value, _NeverReviewedError), "the type must survive"
    assert "postfilter_error" in inspect.getsource(writer_lease.invoke_command), (
        "the terminal error filter is no longer on the dispatch path"
    )


def test_a_raised_payload_naming_a_withheld_memory_ref_is_filtered(
    vault: Path,
) -> None:
    """The immutable-reference form. `exomem://memory/<id>` resolves through
    the reference index, so a payload naming one must be gated exactly as a
    path is — the same alias set the result path already covers."""
    from exomem import memory_refs
    from exomem.writer_lease import invoke_command

    target = vault / ERROR_WITHHELD
    updated, identity = memory_refs.add_id_to_markdown(
        target.read_text(encoding="utf-8"), memory_refs.new_id()
    )
    target.write_text(updated, encoding="utf-8")
    _govern_patterns_shut(vault)
    ref = memory_refs.memory_ref(identity)

    command = _raising_command("get", f"could not read {ref}")
    with _external_scope():
        with pytest.raises(ValueError) as excinfo:
            invoke_command(command, vault, path=ERROR_WITHHELD)

    text = str(excinfo.value)
    assert identity not in text, f"the immutable ref crossed the boundary: {text!r}"
    assert egress.WITHHELD_REFERENCE in text


def test_an_identity_collision_does_not_name_the_colliding_pages(vault: Path) -> None:
    """The reachable case, end to end. Duplicating an identity is an ordinary
    consequence of a merge or a sync copy, so a caller can manufacture the
    collision and then read the colliding vault paths out of the error."""
    from exomem import commands, memory_refs
    from exomem.writer_lease import invoke_command

    patterns = vault / "Knowledge Base" / "Notes" / "Patterns"
    identity = memory_refs.new_id()
    for name in ("kill-switch-for-risky-releases.md", "kill-switch-copy.md"):
        page = patterns / name
        updated, _ = memory_refs.add_id_to_markdown(
            page.read_text(encoding="utf-8")
            if page.exists()
            else "---\ntype: pattern\n---\nx\n",
            identity,
        )
        page.write_text(updated, encoding="utf-8")
    _govern_patterns_shut(vault)

    command = {c.name: c for c in commands.COMMANDS}["get"]
    with _external_scope():
        with pytest.raises(Exception) as excinfo:
            invoke_command(command, vault, path=memory_refs.memory_ref(identity))

    text = str(excinfo.value)
    assert "AMBIGUOUS_REFERENCE" in text, f"the stable code must survive: {text!r}"
    assert "2" in text, "the match count is what an owner needs"
    for leaked in ("kill-switch-for-risky-releases", "kill-switch-copy", "Patterns"):
        assert leaked not in text, f"collision named {leaked!r}: {text!r}"


def test_a_blocked_policy_fails_the_error_path_closed(vault: Path) -> None:
    """The same three-state contract the result path carries: `empty` leaves
    the text alone, `blocked` (a cold-start compile refusal with no prior good
    state) cannot trust any decision, so every resolvable reference goes."""
    from exomem.writer_lease import invoke_command

    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "broken.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 9\n',
        encoding="utf-8",
    )
    _seed_volunteered(vault)
    command = _raising_command("get", f"could not read {ERROR_VOLUNTEERED}")

    with _external_scope():
        with pytest.raises(ValueError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)

    text = str(excinfo.value)
    assert ERROR_VOLUNTEERED_STEM not in text, f"a blocked policy leaked: {text!r}"
    assert egress.WITHHELD_REFERENCE in text


def test_postfilter_error_redacts_the_exception_in_place(vault: Path) -> None:
    """The load-bearing contract behind the dispatcher's bare `raise`.

    `invoke_command` calls `postfilter_error(...)` and then re-raises the
    ORIGINAL exception object, discarding the return value — so the redaction
    only reaches a caller if it happened on that object. An implementation
    that built a clean COPY and returned it would leave the raised object
    untouched and silently restore the leak, while every direct unit test of
    the filter's return value still passed. This pins the object identity and
    the in-place rewrite together.
    """
    _govern_patterns_shut(vault)
    _seed_volunteered(vault)
    error = ValueError(f"could not read {ERROR_VOLUNTEERED}")

    returned = egress.postfilter_error("get", error, vault)

    assert returned is error, "the filter must redact in place, not return a copy"
    # The raised object itself — not a copy — no longer carries the reference.
    assert ERROR_VOLUNTEERED_STEM not in str(error)
    assert ERROR_VOLUNTEERED_STEM not in "".join(str(a) for a in error.args)
    assert egress.WITHHELD_REFERENCE in str(error)


def test_a_copying_error_filter_reopens_the_leak_through_the_bare_reraise(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted defect for the same contract, driven through the dispatcher.

    Swap in a filter that redacts correctly but returns a NEW exception rather
    than mutating the one that is travelling. The dispatcher's `raise` still
    re-raises the original, so the withheld path crosses the boundary — which
    is exactly the failure mode `test_postfilter_error_redacts_the_exception_
    in_place` exists to make impossible.
    """
    from exomem.writer_lease import invoke_command

    _govern_patterns_shut(vault)

    def _copying_filter(command_name, error, vault_root, **_kwargs):
        clean = type(error)(
            egress.redact_withheld_references(vault_root, str(error))
        )
        return clean  # never touches `error`

    _seed_volunteered(vault)
    monkeypatch.setattr(egress, "postfilter_error", _copying_filter)
    command = _raising_command("get", f"could not read {ERROR_VOLUNTEERED}")

    with _external_scope():
        with pytest.raises(ValueError) as excinfo:
            invoke_command(command, vault, path=ERROR_CALLER_PATH)

    assert ERROR_VOLUNTEERED in str(excinfo.value), (
        "a copying filter must be observably broken, or the in-place contract "
        "is not actually load-bearing and this test is decoration"
    )
