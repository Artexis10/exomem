r"""The per-user, per-vault machine-local state root — the single placement seam.

Machine-local derived state (index stores, graph epoch and receipt records,
rebuild scratch, due/review projections) must not live inside the vault: the
vault is user content and users sync user content, and a sync agent hashing,
holding, or replacing live state files has already cost a day-long outage
(see openspec change ``relocate-machine-local-state``).

This module is the ONLY place the external state root is composed. Resolution
order (design.md, settled):

1. ``EXOMEM_STATE_ROOT`` — absolute path, used verbatim (fixtures point it at
   a tmpdir; no test may write the real user state root);
2. ``%LOCALAPPDATA%\exomem\state`` on Windows;
3. ``$XDG_STATE_HOME/exomem/state`` else ``~/.local/state/exomem/state`` on
   POSIX.

Consumers derive their directory from :func:`vault_state_dir` and never
compose the root themselves — the placement suite pins both the resolver's
values and, with a seam spy, every constructor's routing through it
(``tests/test_state_root_placement.py``).

``reserved_paths`` remains the closed authority for state *names*; this module
owns only their *placement*.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

ENV_STATE_ROOT = "EXOMEM_STATE_ROOT"
ENV_HOSTED_STATE_ROOT = "EXOMEM_HOSTED_STATE_ROOT"

#: Length cap for the human-navigability slug in a vault state key.
_SLUG_MAX = 24


# --------------------------------------------------------------------------- #
# The request-scoped resolution memo
# --------------------------------------------------------------------------- #
#
# `Path.resolve()` costs one `lstat` per path component, and placement answers
# are asked for constantly: one measured activation request resolved this
# vault's state location 72 times. That is not an I/O problem — it is a convoy
# problem. Every one of those calls releases the interpreter lock and then waits
# up to the switch interval to get it back from whatever else the process is
# doing, so on a busy worker a few hundred microseconds of syscalls becomes
# seconds of waiting. The fix is to ask once.
#
# Deliberately NOT a module-level cache, and the distinction is the whole safety
# argument. A process-lifetime cache would answer for a vault that had since
# moved, for a `EXOMEM_STATE_ROOT` that had since changed, in a request that
# never asked for it — and placement is the invariant that keeps machine-local
# state out of the vault, so a stale answer there is not a stale answer about
# content.
#
# What a memoised answer IS, precisely: a resolution taken at one instant. It
# is not a claim that nothing underneath it can change — `Path.resolve()` reads
# symlink topology, which is filesystem evidence, and a symlink can be flipped
# between two calls of one request. That is why the placement REFUSAL is not
# memoised at all: `validate_vault_state_directory` resolves afresh on every
# call, so a state root that has become a descendant of the vault is refused
# the moment it is asked about. What the memo holds is the composed answer to
# "where does this vault's state live", which is re-derived for the next
# request. `ensure_vault_state_dir`, the caller that creates and hardens the
# directory, re-validates live before it acts. The other consumers of the
# composed answer do not: inside a scope they inherit the one validation made
# when the memo was filled, where before each of them validated for itself.
# Validating on every composition would roughly double a warm request's
# filesystem calls, which is the cost this scope exists to remove.
#
# Outside a scope there is no memo and nothing is remembered, so every caller
# that has not opted in behaves byte-identically to before.

_T = TypeVar("_T")

_MISSING = object()

#: One request's memoised resolutions, or `None` when no scope is open.
#: A `ContextVar` rather than thread-local state: it is per-thread already (a
#: new thread starts from the default, so a scope cannot leak into one) and it
#: follows one request through `await` points. It needs no lock: the only
#: operations on it are a `get` and a single `setitem`, and the worst a race
#: can cost is computing one answer twice. See `resolution_scope` for why
#: nothing may hand it to a task of its own.
_RESOLUTION_MEMO: ContextVar[dict[tuple[Any, ...], Any] | None] = ContextVar(
    "exomem_state_resolution_memo", default=None
)


@contextmanager
def resolution_scope() -> Iterator[None]:
    """Memoise this request's pure placement resolutions for its duration.

    What may be memoised inside a scope: the resolution of a configured path to
    its canonical form, and the placement decisions derived from it. Those are
    answers about configuration, and configuration does not change under a
    request.

    What may NOT, and is not: anything read as evidence. A tombstone's stat
    signature, a manifest's mtime, a lock file's state and the existence of a
    directory are observations of a world the request shares with other
    writers, and a request that memoised one would answer from a past it had
    already been told was over.

    Nested scopes reuse the outer memo, so a component that opens its own scope
    inside a request neither starts a second one nor discards the first when it
    leaves. Exceptions are never memoised: a refusal is re-decided every time.

    Nothing may start a thread or an `asyncio` task inside a scope and expect
    the memo to apply to it. A thread gets the default — no memo, full work,
    which is safe. A task created inside a scope inherits the memo by reference
    and would keep answering from it after the scope had exited, which is not:
    the contract is that a memoised answer lives for one request and no longer.
    Nothing does this today (the only opener is synchronous), and a component
    that needs the memo in work of its own opens its own scope there.
    """
    if _RESOLUTION_MEMO.get() is not None:
        yield
        return
    token = _RESOLUTION_MEMO.set({})
    try:
        yield
    finally:
        _RESOLUTION_MEMO.reset(token)


def _memoized(name: str, parts: tuple[Any, ...], compute: Callable[[], _T]) -> _T:
    """`compute()`, once per key per scope — and every time without one.

    The key is assembled HERE, after the memo has been found, so a caller with
    no scope pays only a `ContextVar` read for passing through. Building it at
    the call site instead cost 5.3us against a 15.8us resolution — a third
    more, charged to every caller that cannot use the answer.
    """
    memo = _RESOLUTION_MEMO.get()
    if memo is None:
        return compute()
    key = (name, *parts, _placement_environment())
    hit = memo.get(key, _MISSING)
    if hit is not _MISSING:
        return hit  # type: ignore[return-value]
    # Outside the try/except on purpose: a raised placement refusal is not an
    # answer and must not be remembered as one.
    value = compute()
    memo[key] = value
    return value


def _placement_environment() -> tuple[str, ...]:
    """The environment values that can change where state is placed.

    Part of every memo key, so a scope that spans an environment change (a test
    repointing `EXOMEM_STATE_ROOT`, a process re-reading its configuration) gets
    the new answer rather than the one it happened to ask for first. Eight
    dictionary lookups and no syscalls, and only inside a scope — see
    `_memoized`.
    """
    return (
        os.environ.get(ENV_STATE_ROOT, ""),
        os.environ.get(ENV_HOSTED_STATE_ROOT, ""),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("XDG_STATE_HOME", ""),
        os.environ.get("HOME", ""),
        os.environ.get("USERPROFILE", ""),
        # What `ntpath.expanduser` falls back to when USERPROFILE is unset.
        os.environ.get("HOMEDRIVE", ""),
        os.environ.get("HOMEPATH", ""),
    )


def resolved_vault_path(vault_root: Path | str, *, expanduser: bool = True) -> Path:
    """One canonical path, resolved once per request rather than per caller.

    The shared primitive behind every "which vault is this, canonically?"
    question in the codebase — the state key, the reserved-identity key, the
    mutation identity, the lexical store key. All of them resolve the SAME path
    to the SAME answer, and before this they each paid for it separately, many
    times per request.

    `expanduser` is explicit because the call sites genuinely differ: some
    expand `~` before resolving and some do not, and quietly making them agree
    would change which directory a `~`-spelled path names.
    """

    def compute() -> Path:
        candidate = Path(vault_root)
        if expanduser:
            candidate = candidate.expanduser()
        return candidate.resolve(strict=False)

    return _memoized(
        "resolved_vault_path", (os.fspath(vault_root), expanduser), compute
    )


def _is_windows() -> bool:
    """Return whether the active process uses Windows filesystem semantics."""

    return os.name == "nt"


def _prepare_windows_private_state_root(directory: Path) -> None:
    """Create and harden one Windows state directory, failing closed."""

    from .mutation_lock import prepare_windows_private_state_root

    prepare_windows_private_state_root(directory)


def platform_default_state_root() -> Path:
    """The per-user platform state directory, ignoring the env override.

    Split from :func:`state_store_root` so the test-suite guard fixture can
    watch the REAL user location while every fixture runs under an injected
    ``EXOMEM_STATE_ROOT`` tmpdir.
    """
    if _is_windows():
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "exomem" / "state"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "exomem" / "state"


def state_store_root() -> Path:
    """The state root every machine-local family lives under, resolved fresh.

    Read from the environment on each call (the same way ``kb_dirname`` and
    ``resolve_vault`` read theirs) so it is per-process and test-overridable.
    """
    raw = os.environ.get(ENV_STATE_ROOT)
    if raw is not None and raw != "":
        override = Path(raw)
        if not override.is_absolute():
            raise ValueError("EXOMEM_STATE_ROOT must be an absolute path")
        return override
    return platform_default_state_root()


def _slug(name: str) -> str:
    """A filesystem-safe tail of the vault directory name, for navigability."""
    normalized = unicodedata.normalize("NFC", name)
    safe = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in normalized
    )
    safe = safe.strip("-.") or "vault"
    return safe[-_SLUG_MAX:]


def vault_state_key(vault_root: Path) -> str:
    """``<slug>-<sha256(normalized resolved vault path)[:16]>``.

    The same vault path on the same machine always maps to the same key; a
    *moved* vault maps to a new key and regenerates (or migrates via the
    one-time rule in ``state_migration``). Normalization: ``Path.resolve``,
    casefold on Windows, NFC — so spelling noise in one path never forks the
    key, while genuinely distinct vaults never share one.
    """
    resolved = resolved_vault_path(vault_root)
    text = str(resolved)
    if _is_windows():
        text = text.casefold()
    text = unicodedata.normalize("NFC", text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{_slug(resolved.name)}-{digest}"


def validate_vault_state_directory(vault_root: Path, directory: Path) -> Path:
    """Fail when a resolved state directory is the vault or its descendant.

    This is the single, read-only placement invariant.  It deliberately runs
    before cache lookup, manifest admission, hosted-anchor creation, or owner
    I/O, so a forged complete manifest below the vault is never authority.
    """

    # Resolved UNMEMOISED, every time, including inside a resolution scope.
    # `ensure_vault_state_dir` calls this again after the hosted creation
    # precisely so the check sees the world as it is when it runs, and a
    # memoised answer cannot: a symlinked state root flipped into the vault
    # between two calls was allowed the second time. A resolution reads symlink
    # topology, which is filesystem evidence, and placement is what keeps
    # machine-local state out of the vault — so this refusal is re-decided on
    # every call and only the COMPOSED `vault_state_dir` answer is memoised.
    resolved_vault = Path(vault_root).expanduser().resolve(strict=False)
    resolved_directory = Path(directory).expanduser().resolve(strict=False)
    try:
        resolved_directory.relative_to(resolved_vault)
    except ValueError:
        return Path(directory)
    raise ValueError("EXOMEM_STATE_ROOT must resolve outside the vault")


def vault_state_dir(vault_root: Path) -> Path:
    """THE seam: where one vault's machine-local state lives. Pure — no writes.

    Memoised for the duration of a `resolution_scope`, which is how a request
    that asks this question of a dozen components pays for the answer once.
    The validation below is part of what is memoised, and that is deliberate:
    it decides a placement from a vault path and an environment, both of which
    are in the key, and neither of which a request can change under itself. It
    still runs in full the first time in every scope, and a refusal is never
    memoised.
    """
    return _memoized(
        "vault_state_dir",
        (os.fspath(vault_root),),
        lambda: _compute_vault_state_dir(vault_root),
    )


def _compute_vault_state_dir(vault_root: Path) -> Path:
    directory = state_store_root() / vault_state_key(vault_root)
    return validate_vault_state_directory(vault_root, directory)


def _hosted_state_root_for(directory: Path) -> Path | None:
    """Validate the exact lexical hosted binding for one vault-state leaf."""

    raw = os.environ.get(ENV_HOSTED_STATE_ROOT, "").strip()
    if not raw:
        return None
    hosted_root = Path(raw)
    if not hosted_root.is_absolute():
        raise ValueError("EXOMEM_HOSTED_STATE_ROOT must be an absolute path")
    expected_store = hosted_root / "vault-state"
    actual_store = directory.parent
    expected_text = os.path.normcase(os.path.abspath(expected_store))
    actual_text = os.path.normcase(os.path.abspath(actual_store))
    if actual_text != expected_text:
        raise ValueError("EXOMEM_STATE_ROOT must be the hosted vault-state anchor")
    return hosted_root


def validate_hosted_state_directory(directory: Path) -> None:
    """Read-only, held validation of an existing hosted vault-state leaf.

    Ordinary startup never creates or repairs family state; the readiness
    gate's fresh bootstrap is the one admission that creates anything, and it
    writes only the first empty manifest over proven emptiness.  When hosted
    mode is bound, retain the provisioned root and open both descendant components
    relative to that handle with no-follow semantics.  Missing components are
    reported distinctly so the readiness gate can issue its stable offline
    migration refusal.
    """

    hosted_root = _hosted_state_root_for(Path(directory))
    if hosted_root is None:
        return

    from . import held_fs

    acquired = held_fs.acquire(hosted_root)
    if not acquired.ok:
        if not hosted_root.exists():
            raise FileNotFoundError("hosted state anchor is absent")
        raise OSError("hosted state anchor cannot be safely acquired")
    with acquired.require() as filesystem:
        opened = filesystem.parent(f"vault-state/{Path(directory).name}")
        if not opened.ok:
            if opened.error is not None and opened.error.code == "MISSING":
                raise FileNotFoundError("hosted state directory is absent")
            raise OSError("hosted state anchor is unsafe")
        with opened.require() as retained:
            validated = filesystem.validate_directory(retained)
            if not validated.ok:
                raise OSError("hosted state anchor changed during validation")


def _ensure_hosted_state_directory(directory: Path) -> bool:
    """Open/create a hosted vault-state descendant under a retained root.

    Hosted configuration validates the private root, but a later lexical
    ``mkdir`` would still follow a pre-positioned ``vault-state`` symlink or
    Windows junction.  The held filesystem opens every component relative to
    the validated root and refuses aliases/reparse points before creating the
    per-vault leaf.  Resolution below is an additional invariant check, never
    the authority for traversal.
    """

    hosted_root = _hosted_state_root_for(directory)
    if hosted_root is None:
        return False

    from . import held_fs

    acquired = held_fs.acquire(hosted_root)
    if not acquired.ok:
        raise OSError("hosted state anchor cannot be safely acquired")
    with acquired.require() as filesystem:
        opened = filesystem.parent(
            f"vault-state/{directory.name}",
            create=True,
        )
        if not opened.ok:
            raise OSError("hosted state anchor is unsafe")
        with opened.require() as retained:
            validated = filesystem.validate_directory(retained)
            if not validated.ok:
                raise OSError("hosted state anchor changed during creation")
            resolved_hosted = hosted_root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
            try:
                resolved_directory.relative_to(resolved_hosted)
            except ValueError as error:
                raise OSError("hosted state anchor escaped its private root") from error
    return True


def ensure_vault_state_dir(vault_root: Path) -> Path:
    """Create the vault's state directory with the private-state posture.

    The hosted cell's DACL story moves with the root: on Windows the same
    helper that hardens today's writer-lease state directory is applied.  A
    hardening failure is fatal; silently falling back to a plain directory
    would expose every moved state family.  On POSIX the directory is created
    0o700.

    Deliberately NOT memoised, even inside a scope. Where the directory goes is
    a placement answer and is memoised; whether it exists right now is not one.
    A second caller in the same request must still reach the `mkdir` (and the
    Windows hardening), because a directory that was there when the first
    caller asked can be gone when the second one writes. What the scope saves
    here is the resolution work underneath, which is all of the syscalls and
    none of the guarantee.
    """
    directory = vault_state_dir(vault_root)
    hosted = _ensure_hosted_state_directory(directory)
    validate_vault_state_directory(vault_root, directory)
    if _is_windows():
        # The helper creates missing ancestors itself and applies the DACL
        # only to the leaf its own mkdir created, so it must see the leaf
        # first — a plain mkdir here would hand it an unprotected directory
        # to validate rather than one to create.
        _prepare_windows_private_state_root(directory)
    elif not hosted:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory
