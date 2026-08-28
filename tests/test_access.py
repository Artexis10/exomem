"""access: per-path tiers from _access.yaml layered over built-in defaults."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from exomem import access


def _write_cfg(vault: Path, text: str) -> Path:
    p = vault / "Knowledge Base" / "_access.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_default_tiers_without_config(vault: Path) -> None:
    assert access.access_tier(vault, "Knowledge Base/Notes/Insights/foo.md") == access.TIER_READ_WRITE
    assert access.access_tier(vault, "Knowledge Base/Sources/Articles/x.md") == access.TIER_APPEND_ONLY
    assert access.access_tier(vault, "Knowledge Base/Evidence/Legal/x.pdf") == access.TIER_APPEND_ONLY
    # No config → nothing is excluded, everything (bar excluded) is indexable.
    assert access.is_indexable(vault, "Knowledge Base/Notes/x.md") is True
    assert access.writable_reason(vault, "Knowledge Base/Notes/x.md") is None


def test_append_only_tier_is_case_insensitive() -> None:
    """On a case-insensitive filesystem (Windows/macOS) an uppercase `SOURCES/`
    aliases the real `Sources/` on disk, so the append-only guard must match
    regardless of case — else raw Sources/Evidence are editable via the alias.
    Regression for the audit's confirmed HIGH."""
    from exomem import vault as vault_module

    for variant in ("SOURCES", "sources", "Sources", "SoUrCeS"):
        assert vault_module.in_append_only_tree(f"Knowledge Base/{variant}/Articles/x.md") is not None, variant
    for variant in ("EVIDENCE", "evidence", "Evidence"):
        assert vault_module.in_append_only_tree(f"Knowledge Base/{variant}/Legal/x.pdf") is not None, variant
    # access_tier (the batch-write backstop's source of truth) agrees.
    assert access.access_tier(Path("/vault"), "Knowledge Base/SOURCES/Articles/x.md") == access.TIER_APPEND_ONLY
    assert access.access_tier(Path("/vault"), "Knowledge Base/EVIDENCE/x.pdf") == access.TIER_APPEND_ONLY
    # A folder that merely starts with the reserved name is NOT append-only.
    assert vault_module.in_append_only_tree("Knowledge Base/Sources-of-truth/x.md") is None


def test_readonly_from_config(vault: Path) -> None:
    _write_cfg(vault, "readonly:\n  - Reference\n  - Library\n")
    assert access.access_tier(vault, "Knowledge Base/Reference/Strategy.md") == access.TIER_READONLY
    # nested + KB-stripped form both resolve to the same tier
    assert access.access_tier(vault, "Reference/sub/deep.md") == access.TIER_READONLY
    assert access.writable_reason(vault, "Knowledge Base/Reference/Strategy.md") is not None
    # readonly is still findable
    assert access.is_indexable(vault, "Knowledge Base/Reference/Strategy.md") is True
    # a non-listed folder stays read-write
    assert access.access_tier(vault, "Knowledge Base/Notes/x.md") == access.TIER_READ_WRITE


def test_excluded_hides_and_blocks(vault: Path) -> None:
    _write_cfg(vault, "excluded:\n  - Private\n")
    assert access.access_tier(vault, "Knowledge Base/Private/secret.md") == access.TIER_EXCLUDED
    assert access.is_indexable(vault, "Knowledge Base/Private/secret.md") is False
    assert access.is_indexable(vault, "Knowledge Base/Notes/x.md") is True
    assert access.writable_reason(vault, "Knowledge Base/Private/secret.md") is not None


def test_excluded_outranks_readonly(vault: Path) -> None:
    _write_cfg(vault, "readonly:\n  - Shared\nexcluded:\n  - Shared/Private\n")
    assert access.access_tier(vault, "Shared/notes.md") == access.TIER_READONLY
    assert access.access_tier(vault, "Shared/Private/secret.md") == access.TIER_EXCLUDED


def test_config_live_reloads_on_mtime_change(vault: Path) -> None:
    p = _write_cfg(vault, "readonly:\n  - Reference\n")
    assert access.access_tier(vault, "Reference/AI.md") == access.TIER_READONLY
    future = time.time() + 2
    p.write_text("readonly: []\n", encoding="utf-8")
    os.utime(p, (future, future))
    assert access.access_tier(vault, "Reference/AI.md") == access.TIER_READ_WRITE


def test_batch_write_refuses_readonly_tree(vault: Path) -> None:
    # The central enforcement: a content write into a readonly tree is refused.
    from exomem.vault import PlannedWrite, batch_atomic_write
    _write_cfg(vault, "readonly:\n  - Reference\n")
    blocked = vault / "Knowledge Base" / "Reference" / "x.md"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="WRITE_REFUSED"):
        batch_atomic_write([PlannedWrite(path=blocked, content="hi")], vault_root=vault)
    # a normal path still writes fine (no-op guard for read-write tiers)
    ok = vault / "Knowledge Base" / "Notes" / "ok.md"
    ok.parent.mkdir(parents=True, exist_ok=True)
    batch_atomic_write([PlannedWrite(path=ok, content="hi")], vault_root=vault)
    assert ok.read_text(encoding="utf-8") == "hi"


def test_find_hides_excluded_tree(vault: Path) -> None:
    from exomem import find as find_module
    secret = vault / "Knowledge Base" / "Private" / "secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("---\ntype: source\n---\nzzqqxx unique marker\n", encoding="utf-8")
    find_module.clear_cache()
    # control: surfaced when nothing excludes it
    assert any("secret" in h.path.lower() for h in find_module.find(vault, query="zzqqxx"))
    # exclude Private/ → the page disappears from results
    _write_cfg(vault, "excluded:\n  - Private\n")
    find_module.clear_cache()
    assert not any("secret" in h.path.lower() for h in find_module.find(vault, query="zzqqxx"))


def test_find_hot_cache_invalidates_when_access_policy_changes(vault: Path) -> None:
    from exomem import find as find_module

    secret = vault / "Knowledge Base" / "Private" / "cached-secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(
        "---\ntype: source\n---\naccess-cache-secret-marker\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    first = find_module.find(vault, query="access-cache-secret-marker")
    assert any(hit.path.endswith("cached-secret.md") for hit in first)

    _write_cfg(vault, "excluded:\n  - Private\n")

    second = find_module.find(vault, query="access-cache-secret-marker")
    assert not any(hit.path.endswith("cached-secret.md") for hit in second)


def test_publication_policy_snapshot_is_bounded_and_fails_closed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = access.publication_policy_snapshot(vault)
    assert snapshot is not None
    assert snapshot.fingerprint == "missing"

    config = _write_cfg(vault, "readonly: []\n")
    snapshot = access.publication_policy_snapshot(vault)
    assert snapshot is not None
    assert snapshot.fingerprint == access.policy_fingerprint(vault)

    config.write_bytes(b"x" * (access.PUBLICATION_POLICY_MAX_BYTES + 1))
    assert access.publication_policy_snapshot(vault) is None

    from exomem import vault as vault_module

    monkeypatch.setattr(
        vault_module,
        "read_bounded_guarded_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )
    assert access.publication_policy_snapshot(vault) is None


# --- Access-policy loading fails closed on transient errors (task 1.6) -------
#
# A transient stat/read failure on `_access.yaml` must never install a policy
# more permissive than the last good one, and must never move the access
# fingerprint that recall identity is built from.


class _Blip:
    """Toggle for a path-scoped transient filesystem error."""

    def __init__(self) -> None:
        self.active = False


def _install_blip(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    target: Path,
    error: OSError,
) -> _Blip:
    """Make `target` (and only `target`) raise `error` from `Path.<method>`."""
    blip = _Blip()
    real = getattr(Path, method)

    def fake(self: Path, *args: object, **kwargs: object) -> object:
        if blip.active and os.path.normcase(str(self)) == os.path.normcase(str(target)):
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, fake)
    return blip


def _sharing_violation() -> OSError:
    return PermissionError(13, "The process cannot access the file")


def _bump_signature(config: Path) -> None:
    """Move the stat signature without touching content.

    The loader short-circuits on an unchanged signature, so a read-error pin
    that skips this never reaches `read_bytes` and silently tests nothing.

    A MOVED signature is also the contract under test: transient errors reuse
    the last successfully loaded policy and fingerprint regardless of whether
    the signature moved. Reuse can only hold visibility narrower or equal to
    the real policy, never wider, and convergence to the changed content
    happens at the next successful read.
    """
    stamp = config.stat().st_mtime + 10
    os.utime(config, (stamp, stamp))


def test_transient_policy_read_error_cannot_widen_visibility(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read blip must never install a policy wider than the last good one.

    Regression: `_refresh_config` stored `{"readonly": [], "excluded": []}`
    with an `unavailable:` fingerprint on OSError, so every later
    `_load_config` under the unchanged stat signature served a policy in
    which nothing was excluded — a cached fail-open on a privacy boundary.

    The signature is deliberately moved first: the contract is that a
    transient error reuses the last successfully loaded policy and fingerprint
    REGARDLESS of signature movement, converging at the next successful read.
    """
    config = _write_cfg(vault, "excluded:\n  - Private\n")
    secret = "Knowledge Base/Private/secret.md"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    good = access.policy_fingerprint(vault)

    blip = _install_blip(monkeypatch, "read_bytes", config, _sharing_violation())
    _bump_signature(config)
    blip.active = True
    during = access.policy_fingerprint(vault)
    assert during == good, "a read blip moved the access fingerprint"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED

    blip.active = False
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    assert access.policy_fingerprint(vault) == good


def test_transient_policy_stat_error_cannot_widen_visibility(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat blip must reuse the last good policy, not fall back to `missing`."""
    config = _write_cfg(vault, "excluded:\n  - Private\n")
    secret = "Knowledge Base/Private/secret.md"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    good = access.policy_fingerprint(vault)

    blip = _install_blip(monkeypatch, "stat", config, _sharing_violation())
    blip.active = True
    assert access.policy_fingerprint(vault) == good, "a stat blip moved the fingerprint"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED


def test_unreadable_policy_without_prior_load_fails_closed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no successful load yet, an unreadable policy excludes everything."""
    config = _write_cfg(vault, "excluded:\n  - Private\n")
    ordinary = "Knowledge Base/Notes/ordinary.md"
    blip = _install_blip(monkeypatch, "read_bytes", config, _sharing_violation())
    blip.active = True

    assert access.access_tier(vault, ordinary) == access.TIER_EXCLUDED
    assert access.is_indexable(vault, ordinary) is False
    assert access.refuse_if_excluded(vault, ordinary) is True
    assert access.writable_reason(vault, ordinary) is not None

    blip.active = False
    assert access.access_tier(vault, ordinary) == access.TIER_READ_WRITE


def test_transient_policy_blip_does_not_flip_recall_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recall identity changes only when policy content provably changed.

    A blip that flips the fingerprint makes `live_recall_checkpoint` fail
    closed and drives `recall_checkpoint` into its reprojection branch, which
    advances the recall generation with zero writes — the measured
    serve/refuse admission flap.
    """
    from exomem import recall_policy

    config = _write_cfg(vault, "excluded:\n  - Private\n")
    before = recall_policy.recall_policy_identity(vault)

    read_blip = _install_blip(monkeypatch, "read_bytes", config, _sharing_violation())
    _bump_signature(config)
    read_blip.active = True
    assert recall_policy.recall_policy_identity(vault) == before
    read_blip.active = False

    stat_blip = _install_blip(monkeypatch, "stat", config, _sharing_violation())
    stat_blip.active = True
    assert recall_policy.recall_policy_identity(vault) == before
    stat_blip.active = False

    assert recall_policy.recall_policy_identity(vault) == before


def test_policy_vanishing_between_stat_and_read_is_transient(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file present at stat and gone at read is a race, not a deletion.

    Delete-then-write saves (editors, sync clients) briefly unlink the file.
    Treating that read-position FileNotFoundError as a settled absence both
    widened visibility AND popped the last-known-good entry, destroying the
    fallback every later transient error depends on. Only an absence observed
    at stat time is the genuine missing-policy identity.
    """
    config = _write_cfg(vault, "excluded:\n  - Private\n")
    secret = "Knowledge Base/Private/secret.md"
    ordinary = "Knowledge Base/Notes/ordinary.md"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    good = access.policy_fingerprint(vault)

    blip = _install_blip(
        monkeypatch, "read_bytes", config, FileNotFoundError(2, "No such file or directory")
    )
    _bump_signature(config)
    blip.active = True

    assert access.policy_fingerprint(vault) == good, "a read-position FNF moved the fingerprint"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    # Still reusing the last known-good entry rather than failing closed proves
    # the cache survived the race: a popped entry would leave `_degraded_state`
    # with nothing and exclude every ordinary path too.
    assert access.access_tier(vault, ordinary) == access.TIER_READ_WRITE

    blip.active = False
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    assert access.policy_fingerprint(vault) == good


def test_policy_deleted_before_stat_becomes_the_missing_identity(vault: Path) -> None:
    """A genuine deletion, observed at stat time, is a real policy change."""
    config = _write_cfg(vault, "excluded:\n  - Private\n")
    secret = "Knowledge Base/Private/secret.md"
    assert access.access_tier(vault, secret) == access.TIER_EXCLUDED
    assert access.policy_fingerprint(vault) != access.MISSING_POLICY_FINGERPRINT

    config.unlink()

    assert access.policy_fingerprint(vault) == access.MISSING_POLICY_FINGERPRINT
    assert access.access_tier(vault, secret) == access.TIER_READ_WRITE
