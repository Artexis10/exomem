"""Always-on deterministic credential scrubber at the egress boundary.

Design decision D7 — and the one intentional behavior change this change ships
for a vault with **no** `_Governance/` directory (owner-confirmed). Unlike
every other consumer in this package, the scrubber is *policy-independent*: it
is content-pattern-based, so it runs on the empty-policy fast path too. It has
no policy-controlled disable switch.

Two properties make it safe to run on every result:

- **Structural-field allowlist first.** `content_hash`, `ref`, `fingerprint`,
  and friends are high-entropy by construction; checking the field name before
  the pattern scan is what stops the entropy heuristic from eating the
  product's own identifiers. It is an allowlist for *identifier shapes*, not a
  bypass — a real private-key block parked in `content_hash` is still blocked.
- **Compiled alternation, single pass, gated per alternative.** The text is
  swept once for cheap literal anchors, and only the alternatives those
  anchors admit are compiled into a union and run — still one pass over the
  text, but over a union sized to what could actually match. The
  all-or-nothing version of this cost ~8 ms per 100 KB on ordinary prose
  (because `token`, `secret`, `password` and `bearer` are English words), well
  over the < 2 ms budget pinned by `test_governance_overhead.py`.
"""

from __future__ import annotations

import datetime as dt
import functools
import hmac
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import authorization_sessions

#: What replaces a blocked value. Deliberately fixed text: a per-credential
#: description would itself carry information about what was found.
NOTICE = "[credential blocked by exomem egress policy]"

# The future authorization-session wire token is parsed here, rather than by
# each future transport, so the terminal scanner and issuance validation share
# one canonical spelling. This broad regex remains the fail-closed sweep for
# malformed candidates in a rejected issuance. Canonical scanning cannot use
# token boundaries: an adversary may place valid base64url characters directly
# beside a bearer, so `_scrub_canonical_authorization_bearers` locates every
# literal start and makes the parser the sole accept/reject authority.
_AUTHORIZATION_BEARER_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])as1\.[A-Za-z0-9_-]{1,256}\.[A-Za-z0-9_-]{1,256}(?![A-Za-z0-9_-])"
)
_ISSUANCE_ACTIONS = frozenset({"open", "rotate"})
_ISSUANCE_CONTEXT_SEAL = object()
_RFC3339_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z",
    re.ASCII,
)


def _parse_authorization_bearer(value: object) -> str | None:
    """Compatibility wrapper over the shared exact capability parser."""

    parsed = authorization_sessions.parse_credential(value)
    return None if parsed is None else parsed.encoded


@dataclass(frozen=True, slots=True)
class _IssuanceContext:
    """Private, non-serializable authority for one just-issued bearer."""

    action: str
    bearer: str
    _seal: object

    def __reduce__(self) -> object:
        raise TypeError("issuance context is process-local")


class _IssuanceProjection(dict[Any, Any]):
    """JSON-shaped terminal retaining process-local issuance authority."""

    def __init__(self, value: Mapping[Any, Any], context: _IssuanceContext) -> None:
        super().__init__(value)
        self._issuance_context = context

    def __reduce__(self) -> object:
        raise TypeError("issuance projection is process-local")


def _new_issuance_context(action: object, bearer: object) -> _IssuanceContext:
    if action not in _ISSUANCE_ACTIONS:
        raise ValueError("issuance action must be open or rotate")
    canonical = _parse_authorization_bearer(bearer)
    if canonical is None:
        raise ValueError("issued bearer is not canonical")
    return _IssuanceContext(str(action), canonical, _ISSUANCE_CONTEXT_SEAL)


def _issuance_projection_context(value: object) -> _IssuanceContext | None:
    if type(value) is not _IssuanceProjection:
        return None
    context = value._issuance_context
    if (
        type(context) is not _IssuanceContext
        or context._seal is not _ISSUANCE_CONTEXT_SEAL
    ):
        return None
    return context


def _trusted_issuance_projection(
    command_name: object,
    arguments: Mapping[str, Any],
    canonical_result: object,
    projected_result: object,
) -> object:
    """Mark only a dispatcher-produced open/rotate terminal for wire issuance."""

    if (
        command_name != "govern_memory"
        or arguments.get("operation") != "session"
        or arguments.get("session_action") not in _ISSUANCE_ACTIONS
        or not isinstance(canonical_result, Mapping)
        or not isinstance(projected_result, Mapping)
    ):
        return projected_result
    leaf = canonical_result.get("leaf_result")
    if type(leaf) is not dict:
        return projected_result
    credential = leaf.get("issued_credential")
    bearer = credential.get("bearer") if type(credential) is dict else None
    try:
        context = _new_issuance_context(arguments["session_action"], bearer)
    except (KeyError, TypeError, ValueError):
        return projected_result
    checked, blocked = _scrub_issuance_response(context.action, leaf, context)
    if blocked or checked != leaf:
        return projected_result
    wire_projection = dict(projected_result)
    if "diagnostics" not in wire_projection and set(wire_projection) != {
        "status",
        "issued_credential",
    }:
        wire_projection["issued_credential"] = dict(credential)
    return _IssuanceProjection(wire_projection, context)


def _valid_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or not (20 <= len(value) <= 64):
        return False
    if _RFC3339_RE.fullmatch(value) is None:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None

#: Field names whose value is a structural identifier, not prose. Checked
#: BEFORE the entropy heuristic; the explicit credential patterns still apply.
_STRUCTURAL_FIELDS = frozenset(
    {
        "content_hash",
        "ref",
        "fingerprint",
        "expected_hash",
        "expected_fingerprint",
        "source_hash",
        "unit_ref",
        "parent_ref",
        "node_key",
        "edge_key",
        "src_key",
        "dst_key",
        "sha256",
        "digest",
        "etag",
        "path",
        "mtime",
        "id",
    }
)

#: Identifier-shaped field-name suffixes. The product's capability and
#: correlation identifiers (`transition_token`, `draft_token`, `content_hash`,
#: `unit_ref`, `idempotency_key`, …) are high-entropy by construction AND they
#: ROUND-TRIP: the client echoes the value back on the next call, so mangling
#: one breaks a two-step guarded write rather than merely altering a response.
#: A suffix rule matches the codebase's naming convention, so a new identifier
#: field is covered the day it is added instead of being discovered as a
#: production breakage.
_STRUCTURAL_SUFFIXES = (
    "_token",
    "_hash",
    "_ref",
    "_id",
    "_fingerprint",
    "_sha256",
    "_key",
)

#: …unless the field name itself says it holds a credential. These get the
#: full treatment, entropy heuristic included — the suffix rule must widen
#: coverage of *identifiers*, never open a hole for a named secret.
_CREDENTIAL_FIELD_WORDS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "api_key",
    "access_key",
    "private_key",
    "auth",
    "bearer",
)


def _is_structural_field(name: str) -> bool:
    lowered = name.lower()
    if any(word in lowered for word in _CREDENTIAL_FIELD_WORDS):
        return False
    return lowered in _STRUCTURAL_FIELDS or lowered.endswith(_STRUCTURAL_SUFFIXES)

@dataclass(frozen=True, slots=True)
class CredentialPattern:
    """One alternative of the credential union, with its prescan contract.

    `anchors` is the load-bearing field, and the reason this is a named
    collection rather than a bare `"|".join(...)` of regex strings. The
    scrubber skips the whole alternation when a cheap literal sweep finds
    none of the anchors, so an alternative whose anchors are not a SUPERSET
    of what it can match is not a slow pattern — it is a silent leak. When
    the alternatives were anonymous strings and the anchor list was
    hand-maintained beside them, the only thing standing between the two was
    five hardcoded test samples; adding a sixth pattern broke the invariant
    with nothing to notice.

    `samples` exist so the claim is verifiable per alternative:
    `test_every_alternative_is_reachable_through_its_own_anchor` walks this
    collection and proves, for each entry, that its sample matches the
    pattern AND contains one of that entry's own anchors.
    """

    name: str
    pattern: str
    anchors: tuple[str, ...]
    samples: tuple[str, ...]
    #: A SECOND independent superset condition, ANDed with `anchors`: at least
    #: one of these literals must also be present for the pattern to be able
    #: to match. Only set it where the regex makes the literal mandatory.
    #: `secret`, `password` and `token` are ordinary English words, so their
    #: anchors fire on prose constantly — but the pattern they guard also
    #: requires an assignment operator, which prose usually lacks. That second
    #: condition is what turns the common case from "run the alternation" into
    #: "skip it".
    also_requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.anchors:
            raise ValueError(
                f"credential pattern {self.name!r} declares no prescan anchor; "
                "the literal sweep would skip it and the credential would ship"
            )
        for anchor in self.anchors:
            if anchor != anchor.lower():
                raise ValueError(
                    f"credential pattern {self.name!r}: anchor {anchor!r} must be "
                    "lower-cased — the sweep runs against lower-cased text"
                )
        if not self.samples:
            raise ValueError(
                f"credential pattern {self.name!r} carries no sample; its anchor "
                "claim cannot be verified"
            )
        for sample in self.samples:
            lowered = sample.lower()
            if not any(anchor in lowered for anchor in self.anchors):
                raise ValueError(
                    f"credential pattern {self.name!r}: sample is not reachable "
                    "through its own anchors — the prescan would skip it"
                )
            if self.also_requires and not any(
                literal in lowered for literal in self.also_requires
            ):
                raise ValueError(
                    f"credential pattern {self.name!r}: sample does not satisfy "
                    f"also_requires {self.also_requires!r}"
                )


#: The credential shapes, one named alternative each. Adding a shape here is
#: the ONLY way to add one to the scrubber, and the dataclass refuses an entry
#: that cannot be checked.
#:
#: Every sample is ASSEMBLED from parts rather than written out as one literal.
#: A realistic-looking credential literal in this file is flagged by secret
#: scanners — GitHub push protection blocked a branch over the Slack sample and
#: Trivy failed CI over the GitHub one — and the samples only need to match
#: their own pattern and anchors, not look plausible. Keep new ones synthesised.
CREDENTIAL_PATTERNS: tuple[CredentialPattern, ...] = (
    CredentialPattern(
        name="authorization_bearer",
        pattern=_AUTHORIZATION_BEARER_CANDIDATE_RE.pattern,
        anchors=("as1.",),
        samples=("as1." + "A" * 22 + "." + "A" * 43,),
    ),
    CredentialPattern(
        name="pem_private_key",
        pattern=r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
        anchors=("-----begin",),
        samples=("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",),
    ),
    CredentialPattern(
        name="pgp_private_key",
        pattern=(
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?"
            r"-----END PGP PRIVATE KEY BLOCK-----"
        ),
        anchors=("-----begin",),
        samples=(
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n"
            "-----END PGP PRIVATE KEY BLOCK-----",
        ),
    ),
    CredentialPattern(
        name="aws_access_key_id",
        pattern=r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b",
        anchors=("akia", "asia", "abia", "acca"),
        samples=("AKIA" + "A" * 16, "ASIA" + "B" * 16),
    ),
    CredentialPattern(
        name="aws_secret_access_key",
        pattern=r"\baws_secret_access_key\s*[=:]\s*\S+",
        anchors=("aws_secret_access_key",),
        samples=("aws_secret_access_key = " + "A" * 40,),
        # `[=:]` is mandatory in the pattern.
        also_requires=("=", ":"),
    ),
    CredentialPattern(
        name="github_token",
        pattern=r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
        anchors=("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
        samples=("ghp_" + "A" * 36,),
    ),
    CredentialPattern(
        name="github_fine_grained_pat",
        pattern=r"\bgithub_pat_[A-Za-z0-9_]{60,}\b",
        anchors=("github_pat_",),
        samples=("github_pat_" + "1A" * 31,),
    ),
    CredentialPattern(
        name="compact_jwt",
        # Three base64url segments, alg header first.
        pattern=r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        anchors=("eyj",),
        samples=("eyJ" + "A" * 12 + "." + "B" * 12 + "." + "C" * 12,),
    ),
    CredentialPattern(
        name="bearer_credential",
        # Scoped `(?i:(?:...))` groups, never a global `(?i)`: an inline flag
        # anywhere but position 0 is a compile error in an alternation. The
        # inner non-capturing group is not cosmetic — a scoped flag followed
        # immediately by a backslash escape reads as a Windows drive prefix to
        # `public_artifact_privacy`'s absolute-local-path rule, which then
        # reports a leaked local path. Keeping a bare `(` after the colon
        # avoids that shape.
        pattern=r"(?i:(?:\bbearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}))",
        anchors=("bearer",),
        samples=("Authorization: Bearer " + "A" * 40,),
    ),
    CredentialPattern(
        name="labelled_secret_assignment",
        pattern=(
            r"(?i:(?:\b(?:api[_-]?key|secret|password|passwd|token)\s*[=:]\s*"
            r"[\"']?[A-Za-z0-9._~+/-]{16,}[\"']?))"
        ),
        anchors=("apikey", "api_key", "api-key", "secret", "password", "passwd", "token"),
        samples=(
            "api_key=Zm9vYmFyYmF6cXV4MTIzNDU2",
            "password: hunter2hunter2hunter2",
            "token = abcdefghijklmnopqrstuvwx",
        ),
        # THE throughput fix. Every anchor above is an ordinary English word
        # that appears in runbooks, incident notes and API prose, so this
        # alternative used to run on nearly every real result — and it is the
        # most expensive one in the union. The pattern cannot match without
        # the mandatory `[=:]`, so requiring an assignment operator to be
        # present anywhere in the text is a free, sound second gate.
        also_requires=("=", ":"),
    ),
    CredentialPattern(
        name="sk_provider_token",
        pattern=r"\bsk-[A-Za-z0-9-]{20,}\b",
        anchors=("sk-",),
        samples=("sk-proj-" + "A" * 40,),
    ),
    CredentialPattern(
        name="slack_token",
        pattern=r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",
        anchors=("xox",),
        # Assembled rather than written out, like the google_api_key sample
        # below: a literal of this shape trips GitHub push protection and
        # blocks the branch. Keep every realistic-looking sample synthesised.
        samples=("xoxb-" + "1" * 12 + "-" + "1" * 13 + "-" + "A" * 20,),
    ),
    CredentialPattern(
        name="google_api_key",
        pattern=r"\bAIza[0-9A-Za-z_-]{35}\b",
        anchors=("aiza",),
        samples=("AIza" + "B" * 35,),
    ),
)

# The FULL union over every credential shape, derived from the collection
# above. The scan path does not use this one — it compiles a union over just
# the alternatives an anchor sweep admits (`_union_for`) — but this stays as
# the canonical "every shape the scrubber knows" pattern that the tests check
# each alternative against. Order inside an alternation is irrelevant: the
# union matches the leftmost-longest of whichever fires.
_CREDENTIAL_PATTERN = re.compile(
    "|".join(spec.pattern for spec in CREDENTIAL_PATTERNS),
    re.DOTALL,
)

#: Entropy heuristic for a bare, UNLABELLED credential — the residual case the
#: explicit patterns above cannot name. Deliberately narrow on three axes at
#: once, because every axis alone has a large false-positive class:
#:
#: - **Contiguous.** `-` and `_` are word separators, and `/` is a path
#:   separator. A real API key is one unbroken run; a vault path
#:   (`Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases`) and a
#:   kebab-case slug are separator-delimited. Including `/` in the token class
#:   made whole path segments read as base64 and rewrote real vault paths.
#: - **Mixed case + digits.** A single-case 32+ run is a hex hash or an
#:   identifier, not a credential.
#: - **Shannon entropy.** The final discriminator against long English words.
#:
#: Labelled/prefixed credentials (JWT, `ghp_`, `sk-`, PEM, `secret=`) are
#: matched by `_CREDENTIAL_PATTERN` regardless of separators, so tightening
#: this residual heuristic does not narrow real credential coverage.
_ENTROPY_MIN_LEN = 32
_ENTROPY_MIN_BITS = 3.6
_TOKEN_PATTERN = re.compile(rf"\b[A-Za-z0-9+]{{{_ENTROPY_MIN_LEN},}}={{0,2}}\b")
_MIXED_CASE = re.compile(r"(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])")


def _shannon_bits(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _is_high_entropy_token(value: str) -> bool:
    if len(value) < _ENTROPY_MIN_LEN:
        return False
    if not _MIXED_CASE.match(value):
        return False
    return _shannon_bits(value) >= _ENTROPY_MIN_BITS


#: Lower-cased literal prefixes, DERIVED from `CREDENTIAL_PATTERNS` rather
#: than hand-maintained beside it. Python's `re` has no multi-literal
#: prefilter, so running the full alternation over every result costs ~6 ms
#: per 100 KB; a `str.__contains__` sweep (memchr-backed) costs ~0.3 ms and
#: lets clean prose skip the alternation entirely.
#:
#: Correctness condition: if a credential pattern can match, at least one of
#: these MUST be present in the lower-cased text. Deriving the list is what
#: makes that structural — a second hand-written list is a second thing to
#: forget, and forgetting it turns the prescan from an optimization into a
#: leak. `test_the_prescan_sweep_is_derived_from_the_pattern_collection`
#: pins the derivation; `test_every_alternative_is_reachable_through_its_own
#: _anchor` pins the superset claim per alternative.
_ANCHORS: tuple[str, ...] = tuple(
    dict.fromkeys(anchor for spec in CREDENTIAL_PATTERNS for anchor in spec.anchors)
)


def _active_alternatives(lowered: str) -> tuple[int, ...]:
    """Indices of the alternatives that COULD match this text.

    The union used to be all-or-nothing: one anchor hit anywhere and all
    twelve alternatives ran over the whole text. But the anchors that fire on
    real vault prose are `token`, `secret`, `password` and `bearer` — ordinary
    English — so the common case paid for the PEM, AWS, GitHub, JWT, Slack and
    Google alternatives too, none of which could possibly match text that
    contains none of THEIR anchors. Gating per alternative is the same
    superset argument the whole-union prescan already rests on, applied at the
    granularity where it actually pays.
    """
    seen: dict[str, bool] = {}

    def present(literal: str) -> bool:
        # Each miss costs a full memchr sweep of the text, so two specs
        # sharing an anchor (`-----begin`) must not pay for it twice.
        hit = seen.get(literal)
        if hit is None:
            hit = literal in lowered
            seen[literal] = hit
        return hit

    active = []
    for index, spec in enumerate(CREDENTIAL_PATTERNS):
        # `also_requires` is checked FIRST because it is the narrower, shorter
        # condition. The spec with the most anchors is exactly the one whose
        # anchors are ordinary English (`secret`, `password`, `token`), and it
        # is the one an assignment operator rejects outright — so testing two
        # single characters before seven words is what keeps ordinary prose
        # from paying for seven sweeps it was always going to fail.
        if spec.also_requires and not any(present(x) for x in spec.also_requires):
            continue
        if not any(present(anchor) for anchor in spec.anchors):
            continue
        active.append(index)
    return tuple(active)


@functools.lru_cache(maxsize=512)
def _union_for(indices: tuple[int, ...]) -> re.Pattern[str]:
    """One compiled union over exactly the alternatives that can fire.

    Still a SINGLE pass over the text — the subset is chosen before the scan,
    never by re-walking per pattern. Cached because real workloads produce a
    handful of distinct subsets, not 2^12.
    """
    return re.compile(
        "|".join(CREDENTIAL_PATTERNS[index].pattern for index in indices), re.DOTALL
    )


def _may_contain_credential(text: str) -> bool:
    return bool(_active_alternatives(text.lower()))


def _scrub_canonical_authorization_bearers(text: str) -> tuple[str, int]:
    """Replace canonical bearers at every literal start, without boundaries.

    Each parser call receives one fixed 70-character window. Advancing past an
    accepted window makes adjacent occurrences deterministic; rejected starts
    advance by one character so a later start is never hidden by malformed
    surrounding text. The fixed window keeps the work linear and bounded.
    """
    parts: list[str] = []
    copied_through = 0
    replacements = 0
    for start, end in authorization_sessions.iter_credential_spans(text):
        parts.extend((text[copied_through:start], NOTICE))
        copied_through = end
        replacements += 1
    if not replacements:
        return text, 0
    parts.append(text[copied_through:])
    return "".join(parts), replacements


def _scrub_explicit(text: str) -> tuple[str, int]:
    """Run only the alternatives this text can possibly match."""
    indices = _active_alternatives(text.lower())
    if not indices:
        return text, 0
    replacements = 0
    authorization_indices = tuple(
        index
        for index in indices
        if CREDENTIAL_PATTERNS[index].name == "authorization_bearer"
    )
    if authorization_indices:
        text, authorization_replacements = _scrub_canonical_authorization_bearers(
            text
        )
        replacements += authorization_replacements
    ordinary_indices = tuple(
        index
        for index in indices
        if CREDENTIAL_PATTERNS[index].name != "authorization_bearer"
    )
    if ordinary_indices:
        text, ordinary_replacements = _union_for(ordinary_indices).subn(NOTICE, text)
        replacements += ordinary_replacements
    return text, replacements


def scrub_text(text: str) -> tuple[str, bool]:
    """Replace credential-shaped substrings in `text`; report whether any fired."""
    if not text:
        return text, False
    cleaned, replacements = _scrub_explicit(text)
    blocked = replacements > 0
    # Second pass: bare, unlabelled high-entropy tokens. Applied after the
    # explicit patterns so an already-replaced credential is not rescanned.
    def _maybe_entropy(match: re.Match[str]) -> str:
        nonlocal blocked
        token = match.group(0)
        if _is_high_entropy_token(token):
            blocked = True
            return NOTICE
        return token

    cleaned = _TOKEN_PATTERN.sub(_maybe_entropy, cleaned)
    return cleaned, blocked


def _scrub_structural(value: str) -> tuple[str, bool]:
    """A structural identifier: explicit credential patterns only, no entropy."""
    cleaned, replacements = _scrub_explicit(value)
    return cleaned, replacements > 0


def _stable_mapping_key(value: Any) -> tuple[str, str, str]:
    """Order JSON-shaped mapping keys without comparing unlike Python types."""
    value_type = type(value)
    return value_type.__module__, value_type.__qualname__, repr(value)


def _allocate_mapping_keys(
    entries: list[tuple[Any, Any, bool]],
) -> list[Any]:
    """Allocate collision-free transformed keys independent of insertion order.

    Unchanged authored keys reserve their spelling first. Transformed keys are
    then assigned by their stable original identity, and a suffix counter loops
    until it finds a genuinely unused key. This preserves every entry even when
    an adversary pre-populates NOTICE and arbitrary NOTICE#N spellings.
    """
    assigned: list[Any] = [None] * len(entries)
    used: set[Any] = set()
    for index in sorted(
        range(len(entries)),
        key=lambda item: (
            entries[item][2],
            _stable_mapping_key(entries[item][0]),
        ),
    ):
        _original, desired, _changed = entries[index]
        candidate = desired
        suffix = 1
        while candidate in used:
            candidate = f"{desired}#{suffix}"
            suffix += 1
        assigned[index] = candidate
        used.add(candidate)
    return assigned


def scrub_value(value: Any, *, field_name: str | None = None) -> tuple[Any, bool]:
    """Walk any JSON-shaped result, scrubbing every string it contains.

    `field_name` carries the key a string arrived under so the structural
    allowlist can suppress the entropy heuristic for identifier fields.
    """
    if isinstance(value, str):
        if field_name is not None and _is_structural_field(field_name):
            return _scrub_structural(value)
        return scrub_text(value)
    if isinstance(value, Mapping):
        blocked = False
        entries: list[tuple[Any, Any, bool, Any, bool]] = []
        for key, item in value.items():
            cleaned_key = key
            key_hit = False
            if isinstance(key, str):
                cleaned_key, key_hit = scrub_text(key)
                blocked = blocked or key_hit
            cleaned, hit = scrub_value(item, field_name=str(key))
            entries.append((key, cleaned_key, key_hit, cleaned, hit))
            blocked = blocked or hit
        allocated = _allocate_mapping_keys(
            [
                (original_key, cleaned_key, key_hit)
                for original_key, cleaned_key, key_hit, _, _ in entries
            ]
        )
        out = {
            allocated[index]: cleaned
            for index, (_, _, _, cleaned, _) in enumerate(entries)
        }
        return out, blocked
    if isinstance(value, (list, tuple)):
        blocked = False
        items = []
        for item in value:
            cleaned, hit = scrub_value(item, field_name=field_name)
            items.append(cleaned)
            blocked = blocked or hit
        if isinstance(value, tuple):
            # A NamedTuple's constructor takes its fields positionally, not a
            # single iterable, so `type(value)(items)` raises for one — and the
            # postfilter walks EVERY command result, so any leaf returning a
            # NamedTuple crashed the boundary rather than passing through it.
            # `_make` is the documented rebuild path; plain tuples keep the
            # ordinary constructor.
            rebuild = getattr(type(value), "_make", None)
            return (rebuild(items) if rebuild is not None else type(value)(items)), blocked
        return items, blocked
    return value, False


def _scrub_issuance_candidates(value: Any) -> tuple[Any, bool]:
    """Fail closed over malformed `as1` candidates in a rejected issuance."""
    if isinstance(value, str):
        cleaned, exact_replacements = _scrub_canonical_authorization_bearers(value)
        cleaned, broad_replacements = _AUTHORIZATION_BEARER_CANDIDATE_RE.subn(
            NOTICE, cleaned
        )
        return cleaned, exact_replacements + broad_replacements > 0
    if isinstance(value, Mapping):
        blocked = False
        entries: list[tuple[Any, Any, bool, Any, bool]] = []
        for key, item in value.items():
            cleaned_key, key_hit = _scrub_issuance_candidates(key)
            cleaned, hit = _scrub_issuance_candidates(item)
            entries.append((key, cleaned_key, key_hit, cleaned, hit))
            blocked = blocked or key_hit or hit
        allocated = _allocate_mapping_keys(
            [
                (original_key, cleaned_key, key_hit)
                for original_key, cleaned_key, key_hit, _, _ in entries
            ]
        )
        out = {
            allocated[index]: cleaned
            for index, (_, _, _, cleaned, _) in enumerate(entries)
        }
        return out, blocked
    if isinstance(value, (list, tuple)):
        items = []
        blocked = False
        for item in value:
            cleaned, hit = _scrub_issuance_candidates(item)
            items.append(cleaned)
            blocked = blocked or hit
        return (tuple(items) if isinstance(value, tuple) else items), blocked
    return value, False


def _scrub_issuance_response(
    command_name: str, response: Any, context: _IssuanceContext | None
) -> tuple[Any, bool]:
    """Allow exactly one typed open/rotate bearer; scrub every other shape.

    The dispatcher supplies the sealed context only for a just-committed
    open/rotate terminal. Every other caller supplies no context, so the
    production default remains unconditional scrubbing.
    """
    context_valid = (
        type(context) is _IssuanceContext
        and context._seal is _ISSUANCE_CONTEXT_SEAL
        and context.action == command_name
        and context.action in _ISSUANCE_ACTIONS
    )
    response_valid = (
        type(response) is dict
        and set(response) == {"status", "issued_credential"}
        and response.get("status") == "ok"
    )
    credential = response.get("issued_credential") if response_valid else None
    credential_valid = (
        type(credential) is dict
        and set(credential) == {"kind", "bearer", "expires_at"}
        and credential.get("kind") == "authorization-session-bearer"
        and _parse_authorization_bearer(credential.get("bearer")) is not None
        and _valid_rfc3339(credential.get("expires_at"))
    )
    valid = context_valid and credential_valid and hmac.compare_digest(
        credential["bearer"], context.bearer
    )
    if valid:
        return response, False
    cleaned, blocked = _scrub_issuance_candidates(response)
    fallback, fallback_blocked = scrub_value(cleaned)
    return fallback, blocked or fallback_blocked


def _scrub_issuance_projection(
    value: Any,
    context: _IssuanceContext,
) -> tuple[Any, bool]:
    """Release one exact issued bearer and reject every additional credential."""

    public = dict(value) if isinstance(value, Mapping) else value
    if type(public) is not dict:
        return scrub_value(public)

    if type(public.get("diagnostics")) is dict:
        location = "diagnostics"
        response = public[location]
    elif set(public) == {"status", "issued_credential"}:
        location = "root"
        response = public
    elif type(public.get("issued_credential")) is dict:
        location = "compact"
        response = {
            "status": "ok",
            "issued_credential": public["issued_credential"],
        }
    else:
        return scrub_value(public)

    checked, issuance_blocked = _scrub_issuance_response(
        context.action,
        response,
        context,
    )
    if issuance_blocked or checked != response:
        return scrub_value(public)

    protected_response = dict(response)
    protected_credential = dict(protected_response["issued_credential"])
    protected_credential["bearer"] = "[authorized session credential]"
    protected_response["issued_credential"] = protected_credential
    protected = dict(public)
    if location == "root":
        protected = protected_response
    elif location == "diagnostics":
        protected[location] = protected_response
    else:
        protected["issued_credential"] = protected_credential

    cleaned, blocked = scrub_value(protected)
    if blocked or not isinstance(cleaned, dict):
        return scrub_value(public)
    if location == "compact":
        cleaned_response = None
        cleaned_credential = cleaned.get("issued_credential")
    else:
        cleaned_response = cleaned if location == "root" else cleaned.get(location)
        if not isinstance(cleaned_response, dict):
            return scrub_value(public)
        cleaned_credential = cleaned_response.get("issued_credential")
    if not isinstance(cleaned_credential, dict):
        return scrub_value(public)
    restored_credential = dict(cleaned_credential)
    restored_credential["bearer"] = context.bearer
    restored = dict(cleaned)
    if location == "compact":
        restored["issued_credential"] = restored_credential
    else:
        assert isinstance(cleaned_response, dict)
        restored_response = dict(cleaned_response)
        restored_response["issued_credential"] = restored_credential
    if location == "root":
        restored = restored_response
    elif location == "diagnostics":
        restored[location] = restored_response
    return _IssuanceProjection(restored, context), False


def enabled(vault_root: Path) -> bool:
    """The terminal credential scrubber is not a policy-controlled option."""
    del vault_root
    return True


def scrub_sequence(values: Sequence[Any]) -> tuple[list[Any], bool]:
    cleaned, blocked = scrub_value(list(values))
    return cleaned, blocked
