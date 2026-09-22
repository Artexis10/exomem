"""Tasks 1.1 and 1.2 — the continuity token and the `anchor` override.

Two additions to the merged compiler, both of which only matter once turns
arrive in sequence.

**Continuity** is a client-carried token, not server state. It is minted from
the packet AS SERVED — after the egress guard — so it can never carry a ref the
guard removed, and it is validated against the serving index's own identity and
role-registry hash rather than trusted. Its evidence kind is a QUALIFIER: it
strengthens an anchor the current turn already reached and can never reach one
on its own, because a token that could conjure an anchor would be exactly the
"you looked at this a lot" failure `usage_prior` is fenced off from.

**The override** is the other half of the same idea: the agent is the only
decider of an ambiguous turn, so it names the sense it means and the server
runs the lanes for that anchor alone. An unknown ref and a ref the caller's
audience may not see receive the SAME error, because two distinguishable
refusals would turn the argument into an existence oracle.
"""

from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path

import pytest
from test_governance_egress import (
    _external,
    _reset_caches,
    write_rule,
    write_scope,
)
from test_working_set_egress import _packet, _release

from exomem import commands, working_set, working_set_index
from exomem import working_set_resolve as resolve_module
from exomem import working_set_runtime as runtime_module
from exomem.governance import egress
from exomem.governance.principal import request_scope

TURN = "I'm planning to tow the Cargo Sled north — how much depot stock is left?"
NONSENSE_TURN = "zqxwvu plonktastic frobnitz quibblewhomp"

IDENTITY = "0:12345"
OTHER_IDENTITY = "0:99999"
ROLES_HASH = "abc"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _candidate(
    anchor_id: str,
    *,
    evidence: tuple[str, ...],
    kind: str = "resource",
    ref: str | None = None,
) -> resolve_module.CandidateFacts:
    return resolve_module.CandidateFacts(
        anchor_id=anchor_id,
        path=anchor_id,
        ref=ref,
        title=anchor_id,
        kind=kind,
        lifecycle="active",
        categories=(),
        neighbourhood=frozenset(),
        anchor_neighbourhood=frozenset(),
        evidence=frozenset(evidence),
    )


def _row(
    path: str, *, title: str = "", kind: str = "hub", ref: str | None = None
) -> resolve_module.AnchorFacts:
    return resolve_module.AnchorFacts(
        anchor_id=path,
        path=path,
        ref=ref,
        title=title or path,
        kind=kind,
        lifecycle="active",
        aliases=(),
        terms=(),
        categories=(),
        neighbourhood=frozenset(),
        anchor_neighbourhood=frozenset(),
    )


def _token(
    *,
    identity: str = IDENTITY,
    roles_hash: str = ROLES_HASH,
    generation: int = 3,
    refs: tuple[str, ...] = ("a.md",),
    roles: tuple[str, ...] = ("resources",),
) -> str:
    return runtime_module.encode_continuity(
        identity=identity,
        roles_hash=roles_hash,
        generation=generation,
        refs=refs,
        roles=roles,
    )


@pytest.fixture
def activation_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from test_working_set_index import _seed_planning, _seed_structure

    _seed_structure(vault)
    _seed_planning(vault)
    runtime_module.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    _reset_caches()
    return vault


def _markdown_bytes(vault: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(vault)): path.read_bytes()
        for path in sorted(vault.rglob("*.md"))
    }


def _refs(packet: dict) -> list[str]:
    return [str(anchor.get("ref")) for anchor in packet.get("anchors") or ()]


def _resolved_refs(packet: dict) -> list[str]:
    """The refs of `resolved` anchors only -- what the token now actually
    carries, since a `partial` anchor is listed for the agent but never
    minted (see `test_the_mint_encodes_only_resolved_anchors_never_partial_ones`)."""
    return [
        str(anchor.get("ref"))
        for anchor in packet.get("anchors") or ()
        if anchor.get("status") == "resolved"
    ]


def _decoded(token: str) -> dict:
    payload = runtime_module.decode_continuity(token)
    assert payload is not None, "the token the packet returned must decode"
    return payload


# --------------------------------------------------------------------------- #
# The codec and its validation
# --------------------------------------------------------------------------- #


def test_token_round_trips_identity_roles_hash_generation_refs_and_roles() -> None:
    token = _token(refs=("b.md", "a.md"), roles=("resources", "people"), generation=7)

    payload = _decoded(token)

    assert payload["identity"] == IDENTITY
    assert payload["roles_hash"] == ROLES_HASH
    assert payload["generation"] == 7
    assert set(payload["refs"]) == {"a.md", "b.md"}
    assert set(payload["roles"]) == {"resources", "people"}


def test_the_token_is_opaque_base64_with_no_padding_or_whitespace() -> None:
    token = _token()

    assert "=" not in token
    assert token == token.strip()
    # Opaque to a reader, but not encrypted: the design says the server trusts
    # nothing in it, so there is no secret to protect.
    assert b"identity" in base64.urlsafe_b64decode(token + "==")


def test_an_absent_token_is_reported_absent() -> None:
    for absent in (None, ""):
        refs, state = runtime_module.read_continuity(
            absent, identity=IDENTITY, roles_hash=ROLES_HASH
        )
        assert refs == frozenset()
        assert state == runtime_module.CONTINUITY_ABSENT


@pytest.mark.parametrize(
    "token",
    [
        "not base64 at all !!",
        base64.urlsafe_b64encode(b"{not json").decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'["a list"]').decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 99}').decode("ascii").rstrip("="),
    ],
)
def test_an_undecodable_token_is_stale(token: str) -> None:
    refs, state = runtime_module.read_continuity(
        token, identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert refs == frozenset()
    assert state == runtime_module.CONTINUITY_STALE


def _raw_token(body: str) -> str:
    """A token whose JSON text is exactly `body` — including text `json.dumps`
    will not produce, which is the whole point: a forged token is a string a
    stranger chose, not one this server round-tripped."""
    return base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii").rstrip("=")


#: Forged tokens that escape `(ValueError, binascii.Error, UnicodeDecodeError)`.
#: `1e400` parses as `inf`, and `int(inf)` raises OverflowError — an
#: ArithmeticError, not a ValueError. Nesting deep enough raises RecursionError, a
#: RuntimeError. Both reached the REST door as a 500 before this was fixed, so a
#: stranger's `continuity` argument could make the operation fail rather than
#: ignore it, which is the one thing the delta says it must not do.
#: The identity and roles hash a forged token would carry to get past the two
#: equality checks, so each shape below is refused by the DECODER rather than by
#: happening not to match this vault.
_FORGED_HEAD = f'"identity":"{IDENTITY}","roles_hash":"{ROLES_HASH}",'


def _forged(fields: str) -> str:
    """A token whose JSON text is exactly `{"v":1,<fields>}`."""
    return _raw_token('{"v":1,' + fields + "}")


def _many(template: str, count: int) -> str:
    return ",".join(template.format(index=index) for index in range(count))


FORGED_TOKENS = {
    "generation is positive infinity": _forged(
        _FORGED_HEAD + '"generation":1e400,"refs":["a.md"],"roles":[]'
    ),
    "generation is negative infinity": _forged(
        _FORGED_HEAD + '"generation":-1e400,"refs":["a.md"],"roles":[]'
    ),
    "generation is a vast integer": _forged(
        _FORGED_HEAD + '"generation":' + "9" * 5000 + ',"refs":["a.md"],"roles":[]'
    ),
    # Deep enough to exhaust the interpreter's stack, short enough to get past
    # the length bound — so this exercises the exception guard and not the bound.
    "refs are nested past the recursion limit": _forged(
        '"refs":' + "[" * 2000 + "]" * 2000
    ),
    # Far past the bound: refused without ever being base64-decoded.
    "token is megabytes long": "A" * (17 * 1024 * 1024),
    "refs are an unbounded list": _forged(
        _FORGED_HEAD
        + '"generation":1,"refs":['
        + _many('"r{index}.md"', 500)
        + '],"roles":[]'
    ),
    "roles are an unbounded list": _forged(
        _FORGED_HEAD
        + '"generation":1,"refs":["a.md"],"roles":['
        + _many('"role{index}"', 500)
        + "]"
    ),
}


@pytest.mark.parametrize("token", FORGED_TOKENS.values(), ids=FORGED_TOKENS.keys())
def test_a_forged_token_is_stale_and_never_raises(token: str) -> None:
    refs, state = runtime_module.read_continuity(
        token, identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert refs == frozenset()
    assert state == runtime_module.CONTINUITY_STALE


@pytest.mark.parametrize("token", FORGED_TOKENS.values(), ids=FORGED_TOKENS.keys())
def test_a_forged_token_abstains_the_packet_rather_than_failing_it(
    activation_vault: Path, token: str
) -> None:
    packet = commands.op_activate_context(
        activation_vault, turn=TURN, continuity=token
    )

    assert packet["generation"]["continuity"] == runtime_module.CONTINUITY_STALE
    assert packet["abstained"] is False, "the turn still resolves on its own evidence"


@pytest.mark.parametrize("token", FORGED_TOKENS.values(), ids=FORGED_TOKENS.keys())
def test_a_forged_token_reaches_the_rest_door_as_a_served_packet(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """A 500 here would be a stranger's string deciding whether the operation
    works. The delta says an undecodable token is ignored and reported."""
    from starlette.testclient import TestClient

    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    client = TestClient(server.build_server(require_auth=False).http_app())

    response = client.post(
        "/api/activate_context",
        json={"turn": TURN, "continuity": token},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 200, response.text[:400]
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["generation"]["continuity"] == runtime_module.CONTINUITY_STALE


def test_an_oversized_token_is_refused_without_being_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is the cheap half of the defence: a megabytes-long argument must
    not buy a megabytes-long base64 decode on the request thread."""
    called: list[int] = []
    real = base64.urlsafe_b64decode

    def _counting(value):
        called.append(len(value))
        return real(value)

    monkeypatch.setattr(runtime_module.base64, "urlsafe_b64decode", _counting)

    assert runtime_module.decode_continuity("A" * 9000) is None
    assert called == []
    assert runtime_module.decode_continuity(_token()) is not None
    assert called, "a token inside the bound is still decoded"


def test_the_refs_and_roles_ceilings_are_declared() -> None:
    assert runtime_module.CONTINUITY_MAX_CHARS == 8192
    assert runtime_module.CONTINUITY_MAX_REFS == 32
    assert runtime_module.CONTINUITY_MAX_ROLES == 32


def test_a_token_at_the_refs_ceiling_still_applies() -> None:
    """The bound refuses the absurd, not the legitimate: a packet reports at most
    `MAX_ANCHORS` anchors, so a real token is far inside it."""
    refs = tuple(f"a{index}.md" for index in range(runtime_module.CONTINUITY_MAX_REFS))

    _kept, state = runtime_module.read_continuity(
        _token(refs=refs), identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert state == runtime_module.CONTINUITY_APPLIED
    assert resolve_module.MAX_ANCHORS <= runtime_module.CONTINUITY_MAX_REFS


def test_a_token_from_another_index_is_stale() -> None:
    refs, state = runtime_module.read_continuity(
        _token(identity=OTHER_IDENTITY), identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert refs == frozenset()
    assert state == runtime_module.CONTINUITY_STALE


def test_a_token_under_another_roles_hash_is_stale() -> None:
    refs, state = runtime_module.read_continuity(
        _token(roles_hash="different"), identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert refs == frozenset()
    assert state == runtime_module.CONTINUITY_STALE


@pytest.mark.parametrize("generation", [1, 3, 9999])
def test_any_generation_of_the_same_index_stays_valid(generation: int) -> None:
    """A generation moves on every vault write, so gating on it would discard
    continuity on every capture — the token would never once be used."""
    refs, state = runtime_module.read_continuity(
        _token(generation=generation, refs=("a.md", "b.md")),
        identity=IDENTITY,
        roles_hash=ROLES_HASH,
    )

    assert refs == frozenset({"a.md", "b.md"})
    assert state == runtime_module.CONTINUITY_APPLIED


def test_an_unstamped_index_has_no_identity_and_never_applies_a_token() -> None:
    """A legacy sidecar with no `instance` row reads as `0`, which every such
    vault would share. An identity that cannot tell two vaults apart must not
    be used to accept a token from either."""
    assert runtime_module.index_identity_from_token((0, 4, 0)) == ""

    refs, state = runtime_module.read_continuity(
        _token(identity=""), identity="", roles_hash=ROLES_HASH
    )

    assert refs == frozenset()
    assert state == runtime_module.CONTINUITY_STALE


def test_the_identity_excludes_the_generation_so_a_write_does_not_change_it() -> None:
    assert runtime_module.index_identity_from_token(
        (0, 4, 12345)
    ) == runtime_module.index_identity_from_token((0, 5, 12345))


def test_a_rebuilt_sidecar_has_a_different_identity(vault: Path) -> None:
    """The identity is the sidecar's own, so a vault whose sidecar was deleted
    and rebuilt does not accept a token minted against the old one."""
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    first = runtime_module.index_identity(index)
    assert first, "a built index must carry an identity"

    index.close()
    working_set_index.sidecar_path(vault).unlink()
    working_set_index.WorkingSetIndex(vault).rebuild()
    second = runtime_module.index_identity(working_set_index.WorkingSetIndex(vault))

    assert second
    assert second != first


def test_the_identity_survives_a_fresh_index_object(vault: Path) -> None:
    """The token is read out of sqlite, so it is stable across process restarts
    — a token that died with the server process would never survive a turn."""
    working_set_index.WorkingSetIndex(vault).rebuild()

    first = runtime_module.index_identity(working_set_index.WorkingSetIndex(vault))
    second = runtime_module.index_identity(working_set_index.WorkingSetIndex(vault))

    assert first and first == second


# --------------------------------------------------------------------------- #
# Continuity is a qualifier
# --------------------------------------------------------------------------- #


def test_continuity_and_agent_choice_are_in_the_closed_vocabulary() -> None:
    assert "continuity" in resolve_module.EVIDENCE_KINDS
    assert "agent_choice" in resolve_module.EVIDENCE_KINDS


def test_continuity_is_not_a_contact_kind() -> None:
    assert "continuity" not in resolve_module.CONTACT_KINDS


def test_one_contact_kind_plus_continuity_resolves_where_the_kind_alone_is_partial() -> None:
    candidate = _candidate("a.md", evidence=("lexical_overlap",))

    without = resolve_module.resolve((candidate,))
    with_token = resolve_module.resolve(
        resolve_module.apply_continuity((candidate,), frozenset({"a.md"}))
    )

    assert without.anchors[0].status == "partial"
    assert without.status == "unresolved"
    assert with_token.anchors[0].status == "resolved"
    assert with_token.status == "resolved"
    assert with_token.anchors[0].evidence == ("continuity", "lexical_overlap")


def test_continuity_never_creates_a_candidate() -> None:
    """The token names an anchor this turn did not reach; nothing appears."""
    candidates = resolve_module.apply_continuity((), frozenset({"a.md", "b.md"}))

    assert candidates == ()
    assert resolve_module.resolve(candidates).status == "unresolved"


def test_continuity_never_promotes_an_anchor_the_turn_never_reached() -> None:
    reached = _candidate("a.md", evidence=("lexical_overlap",))

    candidates = resolve_module.apply_continuity((reached,), frozenset({"b.md"}))

    assert [item.anchor_id for item in candidates] == ["a.md"]
    assert candidates[0].evidence == frozenset({"lexical_overlap"})


def test_a_continuity_ref_naming_a_removed_anchor_is_dropped_silently() -> None:
    """A vault write that retires one of the token's anchors must not cost the
    others their continuity, and must not report anything: the token is a hint,
    and a hint that half-missed is still a hint."""
    kept = _candidate("a.md", evidence=("lexical_overlap",))

    candidates = resolve_module.apply_continuity(
        (kept,), frozenset({"a.md", "gone.md"})
    )

    assert len(candidates) == 1
    assert "continuity" in candidates[0].evidence


def test_continuity_alone_cannot_satisfy_the_two_kinds_rule() -> None:
    """Defence in depth: even a candidate hand-built with continuity as its
    only kind must not resolve, so a future caller that reaches the resolver by
    another path cannot turn the token into a contact kind."""
    candidate = _candidate("a.md", evidence=("continuity",))

    assert resolve_module._status_for(candidate) != "resolved"


# --------------------------------------------------------------------------- #
# The `anchor` override
# --------------------------------------------------------------------------- #


def test_the_override_resolves_the_named_anchor_on_agent_choice_alone() -> None:
    rows = (_row("a.md", title="A"), _row("b.md", title="B"))

    candidate = resolve_module.override_candidate(rows, "b.md")
    assert candidate is not None
    resolution = resolve_module.resolve((candidate,))

    assert resolution.status == "resolved"
    assert [anchor.as_dict()["ref"] for anchor in resolution.anchors] == ["b.md"]
    assert resolution.anchors[0].evidence == ("agent_choice",)
    assert resolution.ambiguity == ()


def test_the_override_matches_a_declared_ref_as_well_as_a_path() -> None:
    rows = (_row("b.md", title="B", ref="entity:b"),)

    assert resolve_module.override_candidate(rows, "entity:b") is not None
    assert resolve_module.override_candidate(rows, "b.md") is not None


def test_the_override_of_an_unknown_ref_yields_no_candidate() -> None:
    rows = (_row("a.md"),)

    assert resolve_module.override_candidate(rows, "nowhere.md") is None
    assert resolve_module.override_candidate(rows, "") is None


# --------------------------------------------------------------------------- #
# Cache keying (design D8)
# --------------------------------------------------------------------------- #


def test_a_token_and_an_override_enter_the_packet_cache_key() -> None:
    base = dict(
        freshness_key="k", index_generation=3, roles_hash="abc", turn="t", max_chars=4000
    )

    plain = runtime_module.cache_key(**base)
    with_token = runtime_module.cache_key(**base, continuity=_token())
    with_other = runtime_module.cache_key(**base, continuity=_token(refs=("z.md",)))
    with_anchor = runtime_module.cache_key(**base, anchor="a.md")
    with_other_anchor = runtime_module.cache_key(**base, anchor="b.md")

    assert len({plain, with_token, with_other, with_anchor, with_other_anchor}) == 5
    assert runtime_module.cache_key(**base, continuity=_token()) == with_token

    signature = inspect.signature(runtime_module.cache_key)
    assert "purpose" not in signature.parameters


# --------------------------------------------------------------------------- #
# Minting from the packet as served
# --------------------------------------------------------------------------- #


def test_the_token_never_carries_a_ref_the_guard_removed(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _packet(), _release())

    assert guarded is not None
    served = _refs(guarded)
    assert served, "the guard must leave the open anchor in place"

    payload = _decoded(runtime_module.mint_continuity(guarded, identity=IDENTITY))

    assert set(payload["refs"]) == set(served)
    assert "kill-switch-for-risky-releases.md" not in " ".join(payload["refs"])


def test_minting_reads_the_generation_and_roles_the_packet_reports() -> None:
    packet = _packet()

    payload = _decoded(runtime_module.mint_continuity(packet, identity=IDENTITY))

    assert payload["generation"] == packet["generation"]["index_generation"]
    assert payload["roles_hash"] == packet["generation"]["roles_hash"]
    assert payload["roles"] == ["resources"]


def test_an_abstained_packet_mints_no_token() -> None:
    abstained = working_set.abstained_packet(
        reason="unresolved",
        max_chars=4000,
        generation={"freshness_key": "", "index_generation": 1, "roles_hash": "abc"},
    )

    assert runtime_module.mint_continuity(abstained, identity=IDENTITY) == ""


def test_no_token_is_minted_without_an_index_identity() -> None:
    assert runtime_module.mint_continuity(_packet(), identity="") == ""


def test_the_mint_encodes_only_resolved_anchors_never_partial_ones() -> None:
    """The change's own scenario ("A listed candidate is not carried forward"):
    `anchors[]` lists a `partial` candidate beside a `resolved` one, but only an
    anchor the packet actually RESOLVED may reach the next turn as continuity --
    otherwise a candidate merely LISTED on one turn is silently promoted on the
    next, exactly the widening the resolver's soundness rule exists to stop.
    """
    packet = {
        "abstained": False,
        "generation": {"index_generation": 3, "roles_hash": ROLES_HASH},
        "roles": [{"id": "resources"}],
        "anchors": [
            {"ref": "a.md", "status": "resolved", "evidence": ["exact_alias"]},
            {"ref": "b.md", "status": "partial", "evidence": ["rare_term"]},
        ],
    }

    payload = _decoded(runtime_module.mint_continuity(packet, identity=IDENTITY))

    assert set(payload["refs"]) == {"a.md"}


#: A ref that cannot be encoded as strict UTF-8. Vault paths reach Python through
#: filesystem decoding, so a name with invalid UTF-8 arrives as a lone surrogate;
#: `json.dumps(..., ensure_ascii=False).encode("utf-8")` then raises on it.
SURROGATE_REF = "Knowledge Base/Products/Cargo\ud800 Sled.md"


def test_the_mint_cannot_raise_on_a_ref_it_cannot_encode() -> None:
    """The mint runs on a read that has already succeeded and been guarded. A
    filename the encoder dislikes must cost the turn its token, never its
    packet — the same posture the hooks' own digests already take with
    `surrogatepass`."""
    packet = _packet()
    packet["anchors"] = [
        {
            "ref": SURROGATE_REF,
            "path": SURROGATE_REF,
            "title": "Cargo Sled",
            "kind": "resource",
            "status": "resolved",
            "evidence": ["exact_alias"],
        }
    ]

    token = runtime_module.mint_continuity(packet, identity=IDENTITY)

    assert isinstance(token, str)
    if token:
        assert SURROGATE_REF in _decoded(token)["refs"]


def test_a_surrogate_ref_round_trips_rather_than_being_dropped() -> None:
    token = runtime_module.encode_continuity(
        identity=IDENTITY,
        roles_hash=ROLES_HASH,
        generation=1,
        refs=(SURROGATE_REF,),
        roles=("resources",),
    )

    refs, state = runtime_module.read_continuity(
        token, identity=IDENTITY, roles_hash=ROLES_HASH
    )

    assert state == runtime_module.CONTINUITY_APPLIED
    assert refs == frozenset({SURROGATE_REF})


def test_the_codec_is_symmetric_about_surrogates() -> None:
    """Encoding with `surrogatepass` and decoding without it would mint tokens
    this server then calls stale — continuity lost with no diagnosis."""
    token = runtime_module.encode_continuity(
        identity=IDENTITY,
        roles_hash=ROLES_HASH,
        generation=1,
        refs=(SURROGATE_REF, "plain.md"),
        roles=(),
    )

    assert runtime_module.decode_continuity(token) is not None


# --------------------------------------------------------------------------- #
# The operation
# --------------------------------------------------------------------------- #


def test_a_first_turn_reports_continuity_absent_and_returns_a_token(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is False
    assert packet["generation"]["continuity"] == runtime_module.CONTINUITY_ABSENT
    assert packet["continuity"]
    assert set(_decoded(packet["continuity"])["refs"]) == set(_resolved_refs(packet))


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"turn": "   "}, runtime_module.CONTINUITY_ABSENT),
        ({"turn": "   ", "continuity": "anything"}, runtime_module.CONTINUITY_STALE),
    ],
)
def test_every_packet_reports_a_continuity_state(
    activation_vault: Path, kwargs: dict, expected: str
) -> None:
    """Including the abstentions taken before an index was ever consulted: a
    caller cannot tell an ignored token from an applied one by its absence."""
    packet = commands.op_activate_context(activation_vault, **kwargs)

    assert packet["abstained"] is True
    assert packet["generation"]["continuity"] == expected


def test_the_kill_switch_still_reports_a_continuity_state(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")

    packet = commands.op_activate_context(activation_vault, turn=TURN, continuity=_token())

    assert packet["abstention"]["reason"] == "disabled"
    assert packet["generation"]["continuity"] == runtime_module.CONTINUITY_STALE
    # An abstained packet has no anchors to carry, so it mints no token.
    assert "continuity" not in packet


def test_the_returned_token_is_accepted_and_reported_applied(
    activation_vault: Path,
) -> None:
    first = commands.op_activate_context(activation_vault, turn=TURN)

    second = commands.op_activate_context(
        activation_vault, turn=TURN, continuity=first["continuity"]
    )

    assert second["generation"]["continuity"] == runtime_module.CONTINUITY_APPLIED
    evidence = {
        kind for anchor in second["anchors"] for kind in anchor.get("evidence") or ()
    }
    assert "continuity" in evidence


def test_a_listed_partial_candidate_is_not_promoted_on_the_next_turn(
    activation_vault: Path,
) -> None:
    """The MAJOR the merge exposed: main's rule lists `partial` candidates in
    `anchors[]` beside resolved ones, and continuity used to be minted from
    every one of them. On this fixture `turn1` resolves two anchors and lists
    Depot Ledger as `partial` (weak evidence, never a contact kind strong
    enough alone). A second turn that reaches Depot Ledger by that same weak
    evidence, carrying the first turn's token, must still leave it `partial`
    -- and an anchor the first turn actually RESOLVED (Cargo Sled, via
    `exact_alias`), reached by the second turn through only a single weak
    contact kind that alone stays `partial`, must still gain `continuity` and
    resolve, so the fix does not also break the feature.

    `turn1` is `TURN` plus one extra, free-standing "depot" (fix/activation-
    competing-senses, R2): "depot stock" is itself the Records collection
    "Depot stock"'s own spelled-out name, so R2 correctly stops that single
    occurrence of "depot" from separately leaking `rare_term` to Depot
    Ledger, an unrelated page that only shares the word -- the exact shape
    R2 exists to fix (see `test_r2_a_word_inside_a_spelled_multiword_name_
    earns_no_rare_term_for_a_different_anchor` in test_working_set_resolve.py).
    This fixture's own precondition is that Depot Ledger is STILL weakly
    reachable, so it needs a second, free-standing "depot" mention outside
    "depot stock"'s span (R2's own documented escape hatch) to keep meaning
    what it always meant, without changing anything else this test exercises
    (Cargo Sled's resolution, the collection's own resolution, the turn's
    cues).
    """
    ledger_ref = "Knowledge Base/Systems/Depot Ledger.md"
    sled_ref = "Knowledge Base/Products/Cargo Sled.md"
    turn1 = (
        "I'm planning to tow the Cargo Sled north — how much depot stock "
        "is left at the depot?"
    )

    first = commands.op_activate_context(activation_vault, turn=turn1)
    by_ref = {a["ref"]: a for a in first["anchors"]}
    assert by_ref[ledger_ref]["status"] == "partial", by_ref[ledger_ref]
    assert by_ref[sled_ref]["status"] == "resolved", by_ref[sled_ref]
    token = first["continuity"]
    token_refs = _decoded(token)["refs"]
    assert ledger_ref not in token_refs, "a merely-listed anchor must not reach the token"
    assert sled_ref in token_refs

    turn2 = "How much sled capacity does the depot have right now?"
    without = commands.op_activate_context(activation_vault, turn=turn2)
    by_ref_without = {a["ref"]: a for a in without["anchors"]}
    # Both are only weakly reached by turn 2 alone -- the fixture's precondition.
    assert by_ref_without[ledger_ref]["status"] == "partial", by_ref_without[ledger_ref]
    assert by_ref_without[sled_ref]["status"] == "partial", by_ref_without[sled_ref]

    with_token = commands.op_activate_context(
        activation_vault, turn=turn2, continuity=token
    )
    by_ref_with = {a["ref"]: a for a in with_token["anchors"]}

    # The regression: never listed in the token, so continuity must not
    # promote it even though turn 2 reaches it by the very same weak evidence.
    assert by_ref_with[ledger_ref]["status"] == "partial", by_ref_with[ledger_ref]
    assert "continuity" not in by_ref_with[ledger_ref]["evidence"]

    # The feature: turn 1 actually RESOLVED Cargo Sled, so a single weak
    # contact kind on turn 2 plus continuity still resolves it.
    assert by_ref_with[sled_ref]["status"] == "resolved", by_ref_with[sled_ref]
    assert "continuity" in by_ref_with[sled_ref]["evidence"]


def test_a_token_from_another_index_is_reported_stale_and_ignored(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        activation_vault, turn=TURN, continuity=_token(identity=OTHER_IDENTITY)
    )

    assert packet["generation"]["continuity"] == runtime_module.CONTINUITY_STALE
    evidence = {
        kind for anchor in packet["anchors"] for kind in anchor.get("evidence") or ()
    }
    assert "continuity" not in evidence


def test_an_undecodable_token_is_reported_stale_and_never_raises(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        activation_vault, turn=TURN, continuity="!!! not a token !!!"
    )

    assert packet["generation"]["continuity"] == runtime_module.CONTINUITY_STALE
    assert packet["abstained"] is False


def test_an_unresolved_turn_stays_unresolved_with_a_valid_token(
    activation_vault: Path,
) -> None:
    resolved = commands.op_activate_context(activation_vault, turn=TURN)

    packet = commands.op_activate_context(
        activation_vault, turn=NONSENSE_TURN, continuity=resolved["continuity"]
    )

    assert packet["abstained"] is True
    assert packet["abstention"]["reason"] == "unresolved"
    assert packet["anchors"] == []
    assert packet["units"] == []


def test_the_token_is_minted_after_the_guard_not_before(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the whole safety property: a token minted from the
    unguarded packet would hand the next turn a ref this audience may not see,
    and the server would then honour it as its own evidence."""
    real_guard = egress.guard_working_set

    def _drop_first(vault_root, packet, release, **kwargs):
        guarded = real_guard(vault_root, packet, release, **kwargs)
        if guarded and guarded.get("anchors"):
            guarded["anchors"] = guarded["anchors"][1:]
        return guarded

    served = commands.op_activate_context(activation_vault, turn=TURN)

    monkeypatch.setattr(commands.egress_module, "guard_working_set", _drop_first)
    guarded = commands.op_activate_context(activation_vault, turn=TURN)

    dropped = set(_refs(served)) - set(_refs(guarded))
    assert dropped, "the fixture must actually drop an anchor"
    assert set(_decoded(guarded["continuity"])["refs"]) == set(_resolved_refs(guarded))
    assert not dropped & set(_decoded(guarded["continuity"])["refs"])


def test_a_token_is_never_served_another_requests_cached_packet(
    activation_vault: Path,
) -> None:
    plain = commands.op_activate_context(activation_vault, turn=TURN)
    with_token = commands.op_activate_context(
        activation_vault, turn=TURN, continuity=plain["continuity"]
    )

    assert plain["generation"]["continuity"] == runtime_module.CONTINUITY_ABSENT
    assert with_token["generation"]["continuity"] == runtime_module.CONTINUITY_APPLIED


def test_an_override_is_never_served_another_requests_cached_packet(
    activation_vault: Path,
) -> None:
    plain = commands.op_activate_context(activation_vault, turn=TURN)
    chosen = _refs(plain)[0]
    # The discrimination: the plain packet reaches MORE than the chosen anchor,
    # so a one-anchor answer cannot be the cached plain packet handed back.
    assert len(_refs(plain)) >= 2

    override = commands.op_activate_context(activation_vault, turn=TURN, anchor=chosen)

    assert _refs(override) == [chosen]


def test_an_override_resolves_on_the_chosen_anchor_alone(
    activation_vault: Path,
) -> None:
    plain = commands.op_activate_context(activation_vault, turn=TURN)
    refs = _refs(plain)
    assert len(refs) >= 2, "the fixture turn must reach more than one anchor"

    packet = commands.op_activate_context(activation_vault, turn=TURN, anchor=refs[1])

    assert packet["abstained"] is False
    assert _refs(packet) == [refs[1]]
    assert packet["anchors"][0]["status"] == "resolved"
    assert packet["anchors"][0]["evidence"] == ["agent_choice"]
    assert packet["ambiguity"] == []
    assert refs[0] not in json.dumps(packet["anchors"])


def test_an_override_runs_the_role_lanes(activation_vault: Path) -> None:
    plain = commands.op_activate_context(activation_vault, turn=TURN)

    packet = commands.op_activate_context(
        activation_vault, turn=TURN, anchor=_refs(plain)[0]
    )

    assert packet["roles"], "the lanes must run for the anchor the agent chose"


def test_the_turn_still_chooses_the_lenses_on_the_override_path(
    activation_vault: Path,
) -> None:
    """The override replaces which ANCHOR the turn is about, not which lenses the
    turn asks for. `context_roles.select_roles` reads the turn's own text for its
    `turn_cue` sources, so the same anchor reached by two differently-phrased
    turns can legitimately fill different roles — and a review that reads the
    override branch as ignoring the turn would have deleted that."""
    plain = commands.op_activate_context(activation_vault, turn=TURN)
    chosen = _refs(plain)[0]

    cued = commands.op_activate_context(activation_vault, turn=TURN, anchor=chosen)
    bare = commands.op_activate_context(
        activation_vault, turn="Cargo Sled", anchor=chosen
    )

    assert {role["source"] for role in cued["roles"]} != set()
    assert [role["id"] for role in cued["roles"]] != [
        role["id"] for role in bare["roles"]
    ], "the turn's cues must still reach role selection"


def test_an_unknown_and_a_withheld_anchor_receive_the_same_error(
    activation_vault: Path,
) -> None:
    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            activation_vault, turn=TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    write_scope(activation_vault, paths="Knowledge Base/Products/*", name="Products")
    write_rule(activation_vault, ceiling=0)
    _reset_caches()
    runtime_module.reset_caches_for_tests()

    withheld_ref = "Knowledge Base/Products/Cargo Sled.md"
    with request_scope(_external()):
        with pytest.raises(ValueError) as withheld:
            commands.op_activate_context(
                activation_vault, turn=TURN, anchor=withheld_ref
            )

    assert str(unknown.value) == str(withheld.value)
    assert "Cargo Sled" not in str(withheld.value)
    assert "absent.md" not in str(unknown.value)
    assert str(unknown.value).split(":", 1)[0].isupper()


def test_the_override_records_nothing(activation_vault: Path) -> None:
    plain = commands.op_activate_context(activation_vault, turn=TURN)
    chosen = _refs(plain)[0]
    before = _markdown_bytes(activation_vault)
    generation = working_set_index.WorkingSetIndex(activation_vault).generation()

    first = commands.op_activate_context(activation_vault, turn=TURN, anchor=chosen)
    runtime_module.reset_caches_for_tests()
    second = commands.op_activate_context(activation_vault, turn=TURN, anchor=chosen)

    assert _markdown_bytes(activation_vault) == before
    assert working_set_index.WorkingSetIndex(activation_vault).generation() == generation
    assert _refs(first) == _refs(second)
    assert first["anchors"] == second["anchors"]


# --------------------------------------------------------------------------- #
# Surface parity
# --------------------------------------------------------------------------- #


def test_the_refusal_reaches_a_door_as_a_structured_client_error(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaf raises; the doors are what a caller sees. A refusal that arrived
    as a 500 would read as "the server broke" rather than "that is not an
    anchor", and an agent would retry the same ref forever."""
    from starlette.testclient import TestClient

    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    client = TestClient(server.build_server(require_auth=False).http_app())

    response = client.post(
        "/api/activate_context",
        json={"turn": TURN, "anchor": "Knowledge Base/Nowhere/absent.md"},
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "INVALID_ANCHOR"
    assert "absent.md" not in response.text


def test_both_arguments_are_optional_on_the_one_leaf() -> None:
    signature = inspect.signature(commands.op_activate_context)

    for name in ("continuity", "anchor"):
        assert name in signature.parameters
        assert signature.parameters[name].default is None

    command = next(
        item for item in commands.PRODUCT_COMMANDS if item.name == "activate_context"
    )
    params = {param.name: param for param in command.params}
    for name in ("continuity", "anchor"):
        assert name in params, f"{name} must reach MCP, REST and the CLI"
        assert params[name].required is False
        assert params[name].help, f"{name} must carry its documented contract"


def test_the_short_cli_alias_passes_both_arguments_through(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`exomem activate` is a hand-written alias rather than a derived
    subcommand, so a new leaf argument does not reach it for free."""
    from exomem.__main__ import main as cli_main

    with pytest.raises(SystemExit):
        cli_main(["activate", "--help"])
    help_text = capsys.readouterr().out

    assert "--continuity" in help_text
    assert "--anchor" in help_text


# --------------------------------------------------------------------------- #
# A referential turn through the door (close-memory-loop D2). The catalogue is
# made fresh first, as the live service keeps it, so the lexical stage of the
# request is the real one and not its degraded fallback.
# --------------------------------------------------------------------------- #


def _live_cell(vault: Path) -> None:
    """Seed the freshness registry the way the running service does."""
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _age_everything(vault: Path, *, newest: Path) -> None:
    import os
    import time

    now = time.time()
    for index, page in enumerate(sorted((vault / "Knowledge Base").rglob("*.md"))):
        os.utime(page, (now - 10_000 - index, now - 10_000 - index))
    os.utime(newest, (now, now))
    _live_cell(vault)


def test_a_referential_turn_with_a_token_resolves_the_previous_anchor(
    activation_vault: Path,
) -> None:
    """"continue" names nothing; the token says what this conversation was
    last answered with, and that is its referent — served through the lanes
    like any resolved anchor, and saying why."""
    from exomem import lexstore

    lexstore.ensure_fresh(activation_vault)
    served = commands.op_activate_context(activation_vault, turn=TURN)
    previous = _resolved_refs(served)
    assert "Knowledge Base/Products/Cargo Sled.md" in previous

    packet = commands.op_activate_context(
        activation_vault, turn="continue", continuity=served["continuity"]
    )

    assert packet["abstained"] is False, packet.get("abstention")
    resolved = [item for item in packet["anchors"] if item["status"] == "resolved"]
    assert "Knowledge Base/Products/Cargo Sled.md" in {item["ref"] for item in resolved}
    assert {item["ref"] for item in resolved} <= set(previous)
    assert all({"recency", "continuity"} <= set(item["evidence"]) for item in resolved)
    assert packet["units"], "a recency-resolved anchor runs its lanes like any other"
    # And the answer carries forward: this packet mints its own token.
    assert packet["continuity"]


def test_a_referential_turn_without_a_token_resolves_the_freshest_edit(
    activation_vault: Path,
) -> None:
    """A fresh session has no token. The freshest edit is then the account of
    what was being worked on."""
    from exomem import lexstore

    _age_everything(
        activation_vault,
        newest=activation_vault / "Knowledge Base" / "Products" / "Cargo Sled.md",
    )
    lexstore.ensure_fresh(activation_vault)

    packet = commands.op_activate_context(activation_vault, turn="continue")

    assert packet["abstained"] is False, packet.get("abstention")
    resolved = [item for item in packet["anchors"] if item["status"] == "resolved"]
    assert [item["ref"] for item in resolved] == ["Knowledge Base/Products/Cargo Sled.md"]
    assert resolved[0]["evidence"] == ["recency"]
    assert packet["units"]
    assert packet["recent_context"][0]["path"] == "Knowledge Base/Products/Cargo Sled.md"


# R-G through the door: the reviewer's p3/p15 misfires and p11 keep-phrases.
from test_working_set_resolve import (  # noqa: E402
    CUE_WORD_BUT_NOT_POINTING_BACK_TURNS,
    POINTING_BACK_TURNS,
)


@pytest.fixture
def hot_sled_vault(activation_vault: Path) -> Path:
    from exomem import lexstore

    _age_everything(
        activation_vault,
        newest=activation_vault / "Knowledge Base" / "Products" / "Cargo Sled.md",
    )
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()
    return activation_vault


@pytest.mark.parametrize("turn", POINTING_BACK_TURNS)
def test_a_turn_that_only_points_back_resolves_the_hottest_anchor(
    hot_sled_vault: Path, turn: str
) -> None:
    packet = commands.op_activate_context(hot_sled_vault, turn=turn)

    assert packet["abstained"] is False, (turn, packet.get("abstention"), packet["anchors"])
    resolved = {
        item["ref"]: item["evidence"] for item in packet["anchors"] if item["status"] == "resolved"
    }
    assert "recency" in resolved.get("Knowledge Base/Products/Cargo Sled.md", ()), resolved


@pytest.mark.parametrize(
    "turn",
    [
        *CUE_WORD_BUT_NOT_POINTING_BACK_TURNS[:9],
        *CUE_WORD_BUT_NOT_POINTING_BACK_TURNS[11:20],
    ],
)
def test_a_cue_word_in_its_ordinary_sense_is_not_answered_by_recency(
    hot_sled_vault: Path, turn: str
) -> None:
    """The reviewer's misfires: each resolved the hottest anchor and served
    its units. None names anything in this vault, so each abstains, still
    carrying what was recently worked on."""
    packet = commands.op_activate_context(hot_sled_vault, turn=turn)

    assert packet["abstained"] is True, (turn, packet["anchors"])
    assert packet["abstention"] == {"reason": "unresolved"}
    assert all("recency" not in item["evidence"] for item in packet["anchors"])
    assert packet["units"] == []
    assert packet["recent_context"]


def test_a_referential_turn_keeps_its_referent_past_recall_partials(
    activation_vault: Path,
) -> None:
    """The reviewer's p2: "let's continue the work, what's pending?" reached
    seven pages whose text says "pending work" by recall, each a partial that
    sorted ahead of the hot anchor, and the referent was cut before
    resolution. Bare "continue" resolved; this did not."""
    from exomem import lexstore

    for name in ("Anvil Crate", "Bolt Tray", "Brace Kit", "Buoy Rack", "Awl Case", "Axle Bin", "Bale Hook"):
        (activation_vault / "Knowledge Base" / "Products" / f"{name}.md").write_text(
            f"---\ntype: note\nstatus: active\n---\n\n# {name}\n\n## Summary\n\n"
            "Pending work: the pending work on this item is still pending.\n",
            encoding="utf-8",
        )
    working_set_index.WorkingSetIndex(activation_vault).rebuild()
    _age_everything(
        activation_vault,
        newest=activation_vault / "Knowledge Base" / "Products" / "Cargo Sled.md",
    )
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()

    packet = commands.op_activate_context(
        activation_vault, turn="let's continue the work, what's pending?"
    )

    assert packet["abstained"] is False, (packet.get("abstention"), packet["anchors"])
    resolved = [item for item in packet["anchors"] if item["status"] == "resolved"]
    assert [item["ref"] for item in resolved] == ["Knowledge Base/Products/Cargo Sled.md"]
    assert "recency" in resolved[0]["evidence"]
    assert packet["units"]


# R-J: a write burst is a batch, not the user's work.


SLED = "Knowledge Base/Products/Cargo Sled.md"
MARIT = "Knowledge Base/Entities/People/Marit Solheim.md"
_SLED_ID = "0b7c9e2a-4f1d-4c3a-9e8b-5a6d7c8e9f01"
_MARIT_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


def _give_id(vault: Path, rel: str, exomem_id: str) -> None:
    page = vault / rel
    page.write_text(
        page.read_text(encoding="utf-8").replace("---\n", f"---\nexomem_id: {exomem_id}\n", 1),
        encoding="utf-8",
    )


def _batch_after(
    vault: Path, *, edits: dict[str, float], monkeypatch: pytest.MonkeyPatch, read: str | None
) -> tuple[dict, dict]:
    """Every page old, `edits` = {page: seconds ago}, then `backfill-ids` runs.
    Returns the "continue" packets before and after the batch; `read` is the
    one page the usage snapshot says was read, if any."""
    import os
    import time

    from exomem import file_watcher, lexstore

    working_set_index.WorkingSetIndex(vault).rebuild()
    now = time.time()
    for index, page in enumerate(sorted((vault / "Knowledge Base").rglob("*.md"))):
        os.utime(page, (now - 10 * 86400 - index, now - 10 * 86400 - index))
    for rel, ago in edits.items():
        os.utime(vault / rel, (now - ago, now - ago))

    def settle() -> None:
        file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
        lexstore.ensure_fresh(vault)
        working_set_index.WorkingSetIndex(vault).rebuild()
        runtime_module.reset_caches_for_tests()

    snapshot = {read: 2.5} if read else {}
    monkeypatch.setattr(working_set, "_activation_snapshot", lambda: snapshot)
    settle()
    before = commands.op_activate_context(vault, turn="continue")
    commands.op_maintain_memory(vault, mode="backfill-ids", dry_run=False)
    anchors = {row.path for row in working_set_index.WorkingSetIndex(vault).anchors()}
    rewritten = {
        str(page.relative_to(vault))
        for page in (vault / "Knowledge Base").rglob("*.md")
        if page.stat().st_mtime > now - 1
    }
    assert len(rewritten & anchors) >= 3, "the batch must rewrite anchors, or this proves nothing"
    settle()
    return before, commands.op_activate_context(vault, turn="continue")


def _resolved_paths(packet: dict) -> list[str]:
    return [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]


@pytest.mark.parametrize("read", [None, SLED], ids=["nothing-read", "sled-read"])
def test_a_maintenance_batch_does_not_pick_the_referent(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, read: str | None
) -> None:
    """The reviewer's p4: the user last edited Cargo Sled; a maintenance pass
    then rewrote thirty pages. "continue" used to resolve whichever of those
    the batch wrote last. After a batch, no edit that came before it carries
    a signal: what was read decides, and with nothing read the turn
    abstains rather than guess."""
    _give_id(activation_vault, SLED, _SLED_ID)

    before, after = _batch_after(
        activation_vault, edits={SLED: 5}, monkeypatch=monkeypatch, read=read
    )

    assert _resolved_paths(before) == [SLED]
    assert (activation_vault / SLED).stat().st_mtime < time_now() - 1, "the batch left it alone"
    if read:
        assert _resolved_paths(after) == [SLED], after["anchors"]
    else:
        assert after["abstention"] == {"reason": "unresolved"}, after["anchors"]
        assert after["recent_context"]


@pytest.mark.parametrize("read", [None, SLED], ids=["nothing-read", "sled-read"])
def test_a_batch_that_rewrote_the_users_page_never_promotes_an_old_edit(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, read: str | None
) -> None:
    """The reviewer's r2 shape [e]: the user's last work, Cargo Sled an hour
    ago, had no identifier, so the batch rewrote it and its edit signal went
    with the batch. Marit Solheim, edited once two days ago, then became the
    referent because it was the freshest page outside the batch — however
    old. Only an edit NEWER than the latest batch counts."""
    _give_id(activation_vault, MARIT, _MARIT_ID)

    before, after = _batch_after(
        activation_vault,
        edits={MARIT: 2 * 86400, SLED: 3600},
        monkeypatch=monkeypatch,
        read=read,
    )

    assert _resolved_paths(before) == [SLED]
    assert (activation_vault / SLED).stat().st_mtime > time_now() - 60, "the batch rewrote it"
    assert MARIT not in _resolved_paths(after), after["anchors"]
    if read:
        assert _resolved_paths(after) == [SLED], after["anchors"]
    else:
        assert after["abstention"] == {"reason": "unresolved"}, after["anchors"]


def time_now() -> float:
    import time

    return time.time()


def _one_batch(vault: Path, *, count: int) -> list[Path]:
    """The whole vault written in one batch, `count` new person pages with it."""
    import os
    import time

    from exomem import file_watcher

    people = vault / "Knowledge Base" / "Entities" / "People"
    pages = []
    for index in range(count):
        page = people / f"Batch Person {index:02d}.md"
        page.write_text(
            f"---\ntype: entity\nentity_type: person\nstatus: active\n---\n\n"
            f"# Batch Person {index:02d}\n\n## Summary\n\nImported in bulk.\n",
            encoding="utf-8",
        )
        pages.append(page)
    working_set_index.WorkingSetIndex(vault).rebuild()
    stamp = time.time_ns()
    for page in (vault / "Knowledge Base").rglob("*.md"):
        os.utime(page, ns=(stamp, stamp))
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    runtime_module.reset_caches_for_tests()
    return pages


def test_a_vault_written_in_one_batch_is_never_a_menu_of_its_pages(
    activation_vault: Path,
) -> None:
    """The reviewer's p14: synthetic people written in one kernel tick tied on
    every component and "continue" was answered with a five-way menu of
    them. A batch carries no edit signal, so with no reads either the profile
    falls through to nothing and the turn abstains."""
    _one_batch(activation_vault, count=8)
    rows = working_set_resolve_rows(activation_vault)

    assert working_set.hot_profile(activation_vault, rows=rows) == frozenset()
    packet = commands.op_activate_context(activation_vault, turn="continue")

    assert packet["abstention"] == {"reason": "unresolved"}, packet["anchors"]
    assert packet["ambiguity"] == []


def test_a_batch_falls_through_to_what_was_read(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _one_batch(activation_vault, count=8)
    read = str(pages[3].relative_to(activation_vault))
    monkeypatch.setattr(working_set, "_activation_snapshot", lambda: {read: 2.5})
    rows = working_set_resolve_rows(activation_vault)

    assert working_set.hot_profile(activation_vault, rows=rows) == frozenset({read})


def working_set_resolve_rows(vault: Path):
    return resolve_module.facts_from_rows(working_set_index.WorkingSetIndex(vault).anchors())


# R-M: the continuity tier leads only while it is the latest thing that happened.


def test_a_token_says_when_it_was_minted(activation_vault: Path) -> None:
    import time

    before = time.time_ns()
    served = commands.op_activate_context(activation_vault, turn=TURN)
    after = time.time_ns()

    minted = runtime_module.continuity_minted_ns(served["continuity"])

    assert minted is not None and before <= minted <= after
    assert runtime_module.decode_continuity(served["continuity"])["minted_ns"] == minted


def test_a_token_minted_before_the_field_existed_still_reads() -> None:
    token = _token()

    assert runtime_module.decode_continuity(token)["minted_ns"] is None
    assert runtime_module.continuity_minted_ns(token) is None
    assert runtime_module.continuity_minted_ns("!!! not a token !!!") is None


def _heat_after(vault: Path, *, minted_offset_s: float | None) -> frozenset[str]:
    """Marit carried by a token; Cargo Sled edited once, alone, at `now`; the
    token minted `minted_offset_s` seconds relative to that edit."""
    import os
    import time

    rows = working_set_resolve_rows(vault)
    now = time.time()
    for index, page in enumerate(sorted((vault / "Knowledge Base").rglob("*.md"))):
        os.utime(page, (now - 10_000 - index, now - 10_000 - index))
    os.utime(vault / "Knowledge Base" / "Products" / "Cargo Sled.md", (now, now))
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    marit = next(row for row in rows if row.path.endswith("Marit Solheim.md"))
    minted = None if minted_offset_s is None else int((now + minted_offset_s) * 1e9)
    return working_set.hot_profile(
        vault,
        rows=rows,
        continuity_refs=frozenset({resolve_module.anchor_ref(marit)}),
        continuity_minted_ns=minted,
    )


def test_an_edit_after_the_token_unseats_the_continuity_tier(activation_vault: Path) -> None:
    """The user moved on after that packet: its refs no longer lead."""
    assert _heat_after(activation_vault, minted_offset_s=-60) == frozenset(
        {"Knowledge Base/Products/Cargo Sled.md"}
    )


def test_an_edit_before_the_token_leaves_the_continuity_tier_leading(
    activation_vault: Path,
) -> None:
    assert _heat_after(activation_vault, minted_offset_s=60) == frozenset(
        {"Knowledge Base/Entities/People/Marit Solheim.md"}
    )


def test_a_token_that_does_not_say_when_it_was_minted_leads(activation_vault: Path) -> None:
    assert _heat_after(activation_vault, minted_offset_s=None) == frozenset(
        {"Knowledge Base/Entities/People/Marit Solheim.md"}
    )


def test_continue_after_moving_on_follows_the_new_work_not_the_old_token(
    activation_vault: Path,
) -> None:
    """The reviewer's sticky-token question, through the door: the hook keeps
    the last token for the session, so "continue" after the user went and
    edited something else must follow the edit."""
    import os
    import time

    from exomem import file_watcher, lexstore

    now = time.time()
    for index, page in enumerate(sorted((activation_vault / "Knowledge Base").rglob("*.md"))):
        os.utime(page, (now - 10_000 - index, now - 10_000 - index))
    file_watcher.FileWatcher(activation_vault)._reconcile_once(seed=True)
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()
    served = commands.op_activate_context(activation_vault, turn=TURN)
    assert "Knowledge Base/Systems/Depot Ledger.md" not in _resolved_refs(served)
    later = time.time() + 5
    ledger = activation_vault / "Knowledge Base" / "Systems" / "Depot Ledger.md"
    os.utime(ledger, (later, later))
    file_watcher.FileWatcher(activation_vault)._reconcile_once(seed=True)
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()

    packet = commands.op_activate_context(
        activation_vault, turn="continue", continuity=served["continuity"]
    )

    resolved = [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]
    assert resolved == ["Knowledge Base/Systems/Depot Ledger.md"], packet["anchors"]


# R-N1 through the door: the reviewer's r2 acronym probe.


@pytest.mark.parametrize(
    "turn",
    [
        "I got a C on my chemistry exam",
        "I got a c on my chemistry exam",
        "should I GO with the cheaper build machines?",
        "should I go with the cheaper build machines?",
    ],
)
def test_capitals_in_ordinary_prose_never_serve_an_unrelated_anchor(
    activation_vault: Path, turn: str
) -> None:
    from exomem import file_watcher, lexstore

    pages = {
        "Building C": "The chemistry building, where every exam hall is.",
        "Go Toolchain": "Notes on the cheaper build machines for the toolchain.",
    }
    for title, body in pages.items():
        (activation_vault / "Knowledge Base" / "Products" / f"{title}.md").write_text(
            f"---\ntype: note\nstatus: active\n---\n\n# {title}\n\n## Summary\n\n{body}\n",
            encoding="utf-8",
        )
    working_set_index.WorkingSetIndex(activation_vault).rebuild()
    file_watcher.FileWatcher(activation_vault)._reconcile_once(seed=True)
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()

    packet = commands.op_activate_context(activation_vault, turn=turn)

    assert not [item for item in packet["anchors"] if item["status"] == "resolved"], (
        packet["anchors"]
    )
    assert packet["units"] == []


# R-N5: a token minted before an anchor gained its identifier still names it.


def test_a_token_from_before_a_backfill_still_leads_by_path(activation_vault: Path) -> None:
    """The reviewer's r2 refcheck: the packet was served while Cargo Sled
    had no identifier, so its token names the page by path. `backfill-ids`
    then gives the page an identifier, its ref changes, and the token's tier
    matched nothing; "continue" resolved an old collection instead."""
    import time

    from exomem import file_watcher, lexstore

    file_watcher.FileWatcher(activation_vault)._reconcile_once(seed=True)
    lexstore.ensure_fresh(activation_vault)
    runtime_module.reset_caches_for_tests()
    served = commands.op_activate_context(activation_vault, turn=TURN)
    sled = "Knowledge Base/Products/Cargo Sled.md"
    assert sled in runtime_module.decode_continuity(served["continuity"])["refs"]
    time.sleep(1.2)
    commands.op_maintain_memory(activation_vault, mode="backfill-ids", dry_run=False)
    file_watcher.FileWatcher(activation_vault)._reconcile_once(seed=True)
    lexstore.ensure_fresh(activation_vault)
    working_set_index.WorkingSetIndex(activation_vault).rebuild()
    runtime_module.reset_caches_for_tests()
    rows = working_set_resolve_rows(activation_vault)
    assert [resolve_module.anchor_ref(row) for row in rows if row.path == sled] != [sled], (
        "the backfill must have changed the page's ref, or this proves nothing"
    )

    packet = commands.op_activate_context(
        activation_vault, turn="continue", continuity=served["continuity"]
    )

    resolved = {
        item["path"]: item["evidence"] for item in packet["anchors"] if item["status"] == "resolved"
    }
    # The previous answer, whole — Cargo Sled included, now named by path.
    served_paths = {item["path"] for item in served["anchors"] if item["status"] == "resolved"}
    assert set(resolved) == served_paths, packet["anchors"]
    assert {"continuity", "recency"} <= set(resolved[sled])


def test_continuity_qualifies_an_anchor_named_by_its_path() -> None:
    candidate = resolve_module.CandidateFacts(
        anchor_id="a",
        path="Products/Page.md",
        ref="exomem://memory/0b7c9e2a-4f1d-4c3a-9e8b-5a6d7c8e9f01",
        title="Page",
        kind="resource",
        lifecycle="active",
        categories=(),
        neighbourhood=frozenset(),
        evidence=frozenset({"retrieval"}),
    )

    (qualified,) = resolve_module.apply_continuity((candidate,), frozenset({"Products/Page.md"}))

    assert "continuity" in qualified.evidence
