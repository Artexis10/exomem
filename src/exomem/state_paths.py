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
from pathlib import Path

ENV_STATE_ROOT = "EXOMEM_STATE_ROOT"
ENV_HOSTED_STATE_ROOT = "EXOMEM_HOSTED_STATE_ROOT"

#: Length cap for the human-navigability slug in a vault state key.
_SLUG_MAX = 24


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
    resolved = Path(vault_root).expanduser().resolve(strict=False)
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

    resolved_vault = Path(vault_root).expanduser().resolve(strict=False)
    resolved_directory = Path(directory).expanduser().resolve(strict=False)
    try:
        resolved_directory.relative_to(resolved_vault)
    except ValueError:
        return Path(directory)
    raise ValueError("EXOMEM_STATE_ROOT must resolve outside the vault")


def vault_state_dir(vault_root: Path) -> Path:
    """THE seam: where one vault's machine-local state lives. Pure — no writes."""

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

    Ordinary startup must never create or repair state.  When hosted mode is
    bound, retain the provisioned root and open both descendant components
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
