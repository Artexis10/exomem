"""Every RETRIEVAL_INDEX_WARMING refusal names its site, and is bounded.

Measured on the personal cell at 0.64.1, fully warm and converged, inside one
minute: MCP ask_memory served real hits, REST keyword served in 2.1s, four REST
hybrid calls refused at exactly the 60s client timeout with the server never
answering, rerank=true served in 44s, rerank=false refused at 60s.

Two defects that evidence exposes:

* the envelope is IDENTICAL from all ten raise sites, so which gate refused is
  unknowable from the response and diagnosis becomes archaeology; and
* a refusal took 31-90s server-side, because a follower thread waits up to
  ``_RECALL_RESOLVER_BUILD_WAIT_SECONDS`` (120s) for a leader doing a full
  vault walk. A wait longer than any client timeout is not an optimisation,
  it is a hang.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from exomem import find as find_module

SOURCE = Path(find_module.__file__)


def test_the_site_vocabulary_is_stable_and_distinct() -> None:
    """The discriminator set is a closed, content-free vocabulary."""
    sites = find_module.RETRIEVAL_WARMING_SITES

    assert len(sites) == len(set(sites)), "duplicate refusal site discriminators"
    for site in sites:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", site), site


def test_every_raise_site_names_a_discriminator() -> None:
    """No raise site may ship without a site, including ones added later.

    A structural guard on purpose: the whole failure was that every raise site
    produced the same envelope, and the next one added without a discriminator
    would silently reopen it.
    """
    source = SOURCE.read_text(encoding="utf-8")
    raises = re.findall(r"raise RetrievalIndexWarming\((.*?)\)\s*(?:from \w+)?\n", source, re.S)

    assert raises, "no raise sites found; the guard is looking at the wrong shape"
    for raw in raises:
        assert "site=" in raw, f"a RetrievalIndexWarming raise carries no site: {raw!r}"


def test_the_envelope_projects_the_site_and_wait() -> None:
    """`site` and `waited_ms` ride OpError.details, so REST and MCP see them."""
    error = find_module.RetrievalIndexWarming(
        site="projection_unavailable",
        waited_ms=1234,
    )

    assert error.details["site"] == "projection_unavailable"
    assert error.details["waited_ms"] == 1234
    assert error.site == "projection_unavailable"
    # The pre-existing contract is unchanged.
    assert error.details["complete"] is False
    assert error.details["retry_after_ms"] > 0


def test_a_refusal_logs_one_content_free_line(caplog: pytest.LogCaptureFixture) -> None:
    """One INFO line per refusal, naming the site and nothing about content."""
    with caplog.at_level("INFO", logger="exomem.find"):
        find_module.RetrievalIndexWarming(site="catalog_proof_incomplete")

    lines = [r for r in caplog.records if "catalog_proof_incomplete" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one refusal log line, got {len(lines)}"
    assert lines[0].levelname == "INFO"


def test_the_catalog_outcome_site_is_named() -> None:
    """`_raise_catalog_outcome` is a distinct gate and must say so."""
    readiness = type("R", (), {"status": "stale"})()

    with pytest.raises(find_module.RetrievalIndexWarming) as caught:
        find_module._raise_catalog_outcome(readiness)

    assert caught.value.details["site"] == "catalog_outcome"


# --- The 60s server-side hang ----------------------------------------------


def test_the_follower_wait_is_bounded_to_single_digit_seconds() -> None:
    """A wait longer than any client timeout is a hang, not an optimisation."""
    assert find_module._RECALL_RESOLVER_FOLLOWER_WAIT_SECONDS <= 9.0


def test_a_managed_follower_refuses_quickly_instead_of_hanging(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing request returns within the bound, carrying retry_after_ms.

    The measured production shape: a leader is mid-build (holding the
    single-flight event) while a managed request arrives on a warm vault. The
    request must decline rather than wait out the leader's whole-vault walk.
    """
    from exomem import file_watcher, freshness

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    checkpoint = freshness.live_recall_checkpoint(vault, "vault")
    assert checkpoint is not None, "the registry did not warm; wrong precondition"

    monkeypatch.setattr(find_module, "_RECALL_RESOLVER_FOLLOWER_WAIT_SECONDS", 0.2)

    # A leader that never finishes, exactly like a long vault walk in flight.
    root = Path(vault)
    stuck = threading.Event()
    with find_module._RECALL_REBUILD_LOCK:
        find_module._RECALL_RESOLVER_BUILDS[root] = stuck
    try:
        started = time.monotonic()
        with pytest.raises(find_module.RetrievalIndexWarming) as caught:
            find_module.recall_resolver_snapshot(
                root,
                allow_fallback=False,
                expected_checkpoint=checkpoint,
            )
        elapsed = time.monotonic() - started
    finally:
        with find_module._RECALL_REBUILD_LOCK:
            find_module._RECALL_RESOLVER_BUILDS.pop(root, None)
        stuck.set()

    assert elapsed < 5.0, f"a refusing request waited {elapsed:.1f}s on a leader"
    assert caught.value.details["site"] == "resolver_build_wait"
    assert caught.value.details["retry_after_ms"] > 0
    assert caught.value.details["waited_ms"] >= 0


def test_a_fallback_caller_still_waits_out_the_leader(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-scenario: a CLI/cold caller may still wait and then build.

    Bounding the managed request must not silently cut cold callers from 120s
    to 3s — they have no client deadline, and duplicating the leader's
    whole-vault walk is the more expensive option.

    Asserting only "a resolver came back" does NOT discriminate: a caller
    wrongly given the short bound also returns one, by timing out early and
    building its own. So pin the SELECTION at the call site, with a
    non-vacuity guard — a spy that was never called proves nothing.
    """
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)

    observed: list[bool] = []
    real = find_module._follower_wait_seconds

    def spy(*, allow_fallback: bool) -> float:
        observed.append(allow_fallback)
        return real(allow_fallback=allow_fallback)

    monkeypatch.setattr(find_module, "_follower_wait_seconds", spy)
    monkeypatch.setattr(find_module, "_RECALL_RESOLVER_BUILD_WAIT_SECONDS", 0.2)

    root = Path(vault)
    stuck = threading.Event()
    with find_module._RECALL_REBUILD_LOCK:
        find_module._RECALL_RESOLVER_BUILDS[root] = stuck
    try:
        resolver = find_module.recall_resolver_snapshot(root, allow_fallback=True)
    finally:
        with find_module._RECALL_REBUILD_LOCK:
            find_module._RECALL_RESOLVER_BUILDS.pop(root, None)
        stuck.set()

    assert resolver is not None
    assert observed == [True], (
        "the call site did not select its wait through _follower_wait_seconds "
        f"exactly once with allow_fallback=True (observed: {observed})"
    )


def test_a_managed_caller_selects_its_wait_through_the_same_seam(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The managed side of the same selection, so neither branch drifts."""
    from exomem import file_watcher, freshness

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    checkpoint = freshness.live_recall_checkpoint(vault, "vault")
    assert checkpoint is not None

    observed: list[bool] = []

    def spy(*, allow_fallback: bool) -> float:
        observed.append(allow_fallback)
        return 0.05

    monkeypatch.setattr(find_module, "_follower_wait_seconds", spy)

    root = Path(vault)
    stuck = threading.Event()
    with find_module._RECALL_REBUILD_LOCK:
        find_module._RECALL_RESOLVER_BUILDS[root] = stuck
    try:
        with pytest.raises(find_module.RetrievalIndexWarming):
            find_module.recall_resolver_snapshot(
                root,
                allow_fallback=False,
                expected_checkpoint=checkpoint,
            )
    finally:
        with find_module._RECALL_REBUILD_LOCK:
            find_module._RECALL_RESOLVER_BUILDS.pop(root, None)
        stuck.set()

    assert observed == [False], f"managed selection bypassed the seam: {observed}"


def test_the_public_envelope_carries_the_site() -> None:
    """REST and MCP both project `OpError.as_public_dict()`, so pin it there."""
    error = find_module.RetrievalIndexWarming(
        site="resolver_build_wait",
        status="temporarily_unavailable",
        waited_ms=3000,
    )

    payload = error.as_public_dict()

    assert payload["error_code"] == "RETRIEVAL_INDEX_WARMING"
    assert payload["site"] == "resolver_build_wait"
    assert payload["waited_ms"] == 3000
    assert payload["retry_after_ms"] > 0


def test_every_declared_site_is_actually_used() -> None:
    """The vocabulary is a registry, not a wish list.

    A name nobody raises is dead documentation that will drift; a raise that
    uses a name outside the vocabulary escapes the closed set.
    """
    source = SOURCE.read_text(encoding="utf-8")
    used = set(re.findall(r'site="([a-z0-9_]+)"', source))
    declared = set(find_module.RETRIEVAL_WARMING_SITES)

    assert used - declared == set(), f"raise sites outside the vocabulary: {used - declared}"
    assert declared - used == set(), f"declared but never raised: {declared - used}"


def test_a_managed_caller_never_selects_the_long_wait() -> None:
    """Pin the wait SELECTION directly, without sitting through it.

    The production defect was a managed request inheriting the 120s
    fall-back wait. Asserting that by observing elapsed time means hanging for
    two minutes to prove it; asserting the choice is instant and exact.
    """
    managed = find_module._follower_wait_seconds(allow_fallback=False)
    cold = find_module._follower_wait_seconds(allow_fallback=True)

    assert managed == find_module._RECALL_RESOLVER_FOLLOWER_WAIT_SECONDS
    assert managed <= 9.0, "a managed request can outlive a client deadline"
    assert cold == find_module._RECALL_RESOLVER_BUILD_WAIT_SECONDS
    assert cold > managed


# --- The vocabulary is enforced where it cannot be evaded ------------------
#
# The source-scan and registry tests below are a real second layer, but they
# read SHAPES: a double-quoted literal, a `raise Cls(` call. Any construction
# that does not match those shapes slips past them and reaches the public
# envelope. The constructor is the only place the contract cannot be evaded.


def test_an_unknown_site_is_refused_at_construction() -> None:
    """An out-of-vocabulary site must not be constructible at all."""
    with pytest.raises(ValueError, match="unknown retrieval refusal site"):
        find_module.RetrievalIndexWarming(site="not_a_real_site")


def test_a_site_carrying_content_is_refused_at_construction() -> None:
    """The leak this closes: an f-string site interpolating a vault path.

    A refusal envelope reaches REST and MCP verbatim, so a site built from a
    path would publish that path to every client.
    """
    leaked = "Knowledge Base/Private/secret.md"

    with pytest.raises(ValueError, match="unknown retrieval refusal site"):
        find_module.RetrievalIndexWarming(site=f"projection_unavailable:{leaked}")


def test_quote_style_cannot_evade_the_vocabulary() -> None:
    """A single-quoted literal is invisible to a double-quote-only regex."""
    with pytest.raises(ValueError, match="unknown retrieval refusal site"):
        find_module.RetrievalIndexWarming(site='resolver_build_wait_typo')


def test_an_indirect_raise_is_still_validated() -> None:
    """Constructing then raising separately evades a `raise Cls(` scan."""
    with pytest.raises(ValueError, match="unknown retrieval refusal site"):
        _exc = find_module.RetrievalIndexWarming(site="smuggled_site")
        raise _exc


def test_every_declared_site_remains_constructible() -> None:
    """The check must not be so strict it rejects the real vocabulary."""
    for site in find_module.RETRIEVAL_WARMING_SITES:
        error = find_module.RetrievalIndexWarming(site=site)
        assert error.details["site"] == site
