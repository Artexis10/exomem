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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mutation_lock import WindowsRuntimePrincipal

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


def _unsafe_verdict() -> str:
    """The validator's own name for "a principal outside the private set may hold access".

    Read from the validator rather than restated here, so a rename there cannot
    leave this silently matching nothing and quietly stop refusing.
    """
    from .mutation_lock import _WINDOWS_DACL_UNSAFE

    return _WINDOWS_DACL_UNSAFE


@dataclass(frozen=True)
class StateRootPosture:
    """How one vault's state root stands, for BOTH principals that touch it.

    The Windows private-DACL model is relative to the calling token, so a state
    root created by a LocalSystem service is `unsafe` -- and unopenable -- to the
    operator's CLI, and vice versa. Two different questions follow from that, and
    answering one with the other is a defect in each direction:

    * "Can I work here?" is about the CURRENT token (`blocks_current_token`).
    * "Is this cell configured correctly?" is about the RUNTIME principal
      (`runtime_verdict`). A correct LocalSystem root is `unsafe` to every
      operator, so judging cell health by the caller's token makes a healthy
      service install permanently red.
    """

    directory: Path
    #: The root exists on disk. An absent root is a fresh install, not a defect.
    present: bool
    #: This token can open the root.
    accessible: bool
    #: Why the open failed, when it did. `access-denied` is the cross-principal
    #: case; `reparse-point` and `unexpected-path-type` are alias problems with
    #: no `icacls` repair, and must not be reported as principal problems.
    unopenable_reason: str | None
    verdict: str | None
    runtime_verdict: str | None
    #: Worst verdict among a sample of the root's children, judged for the
    #: runtime principal. The root's own descriptor cannot say whether the STATE
    #: inside it is private.
    child_verdict: str | None
    #: How many children that verdict rests on. 0 means the contents were NOT
    #: EVALUATED -- the normal case once a root is sealed, since the operator's
    #: listing is then denied. Reporting privacy without saying so is the
    #: confidently-green claim this whole check exists to stop.
    child_sampled: int
    observed: str | None
    expected: tuple[str, ...]
    runtime_expected: tuple[str, ...]
    #: How the runtime principal was established, or None off Windows.
    runtime_principal_source: str | None
    remediation: str | None

    @property
    def cross_principal(self) -> bool:
        """Present, openable by nobody but another principal. Repairable by ACL."""
        return self.present and not self.accessible and (
            self.unopenable_reason == "access-denied"
        )

    @property
    def contents_unevaluated(self) -> bool:
        """The root is present but nothing inside it was examined."""
        return self.present and self.child_sampled == 0

    @property
    def runtime_principal_unresolved(self) -> bool:
        """The runtime principal fell back to this token instead of being established.

        A remediation rendered for THIS token would then re-introduce the exact
        ping-pong #933 is about, so callers withhold it rather than print it.
        """
        source = self.runtime_principal_source
        return source is not None and source.startswith("current-token (")

    @property
    def unopenable_for_unknown_reason(self) -> bool:
        """Unopenable, and neither a principal nor an alias explains it.

        A sharing violation, a not-ready device, anything outside the causes
        this module names. Narrowing `cross_principal` to `access-denied` was
        right, but it must not leave a residual bucket that simply passes: an
        un-evaluated state is not a safe one, and maintenance that proceeds into
        it fails somewhere further in with a raw error.
        """
        return (
            self.present
            and not self.accessible
            and self.unopenable_reason
            not in {"access-denied", "reparse-point", "unexpected-path-type"}
        )

    @property
    def locally_unsafe(self) -> bool:
        """Openable, but this token's own validator rejects the trustee set.

        The half the first pre-flight missed, and the half the incident's
        service was actually on: LocalSystem holds an `SY` full-access ACE on a
        `user+SY+BA` root, so it opens the root fine and then fails closed on
        every private-state boundary behind it.
        """
        return self.present and self.verdict == _unsafe_verdict()

    @property
    def blocks_current_token(self) -> bool:
        """Any half. Work started here dies; the only question is how far in."""
        return (
            self.cross_principal
            or self.locally_unsafe
            or self.unopenable_for_unknown_reason
        )

    @property
    def alias_path(self) -> bool:
        """The root is a reparse point or the wrong entry type -- not a DACL problem."""
        return self.unopenable_reason in {"reparse-point", "unexpected-path-type"}


class StateRootAccessDenied(OSError):
    """This token cannot do machine-local state work against this root.

    An `OSError` deliberately: every caller that already fails closed on state
    it cannot reach keeps doing so, and the CLI paths that catch `OSError` to
    print a clean error keep catching it -- they just get an actionable message
    instead of a traceback from somewhere further in.
    """

    #: Stable, machine-readable. `upgrade.ps1` consumes the JSON envelope this
    #: reaches, and a message-only envelope cannot be branched on.
    code = "STATE_ROOT_CROSS_PRINCIPAL"

    def __init__(self, posture: StateRootPosture) -> None:
        self.posture = posture
        self.remediation = posture.remediation
        detail = f"; observed {posture.observed!r}" if posture.observed else ""
        if posture.expected:
            detail += (
                "; this process requires full-access trustees "
                + ", ".join(posture.expected)
            )
        # Both halves can hold at once — a SY/BA-only root is unopenable to the
        # operator AND `unsafe` to them. Report the stronger, more concrete fact
        # first: "cannot open it" is checkable, "the validator rejects it" needs
        # the reader to know what the validator is.
        if posture.cross_principal:
            lead = (
                f"the machine-local state root {posture.directory} is owned by another "
                f"principal and cannot be opened by this process{detail}."
            )
        elif posture.unopenable_for_unknown_reason:
            lead = (
                f"the machine-local state root {posture.directory} could not be opened "
                f"({posture.unopenable_reason}), so its posture is unverified{detail}."
            )
        else:
            lead = (
                f"the machine-local state root {posture.directory} carries a DACL this "
                "process's own private-state validator rejects, so every boundary "
                f"behind it fails closed{detail}."
            )
        if posture.runtime_principal_unresolved:
            # Withhold the repair. It renders THIS token's trustees, and the
            # principal that will actually run against the root is unknown --
            # so following it is how #933's day of ACL ping-pong starts.
            super().__init__(
                lead
                + " The principal that runs against this root could not be established "
                f"({posture.runtime_principal_source}), so no repair is offered: "
                "re-ACLing to this token would be a guess that breaks the other "
                "principal. Establish the service account first."
            )
            return
        super().__init__(
            lead
            + " Run it as the principal that owns the root (the service account), "
            "or re-ACL the root with: " + (posture.remediation or "")
        )


def _posture_off_windows(directory: Path) -> StateRootPosture:
    # POSIX privacy is mode-based and the mutation-lock branch no-ops there, so
    # there is no cross-principal DACL condition to report. An unreadable root
    # still fails at its own boundary, as it always has.
    return StateRootPosture(
        directory=directory,
        present=os.path.lexists(directory),
        accessible=True,
        unopenable_reason=None,
        verdict=None,
        runtime_verdict=None,
        child_verdict=None,
        child_sampled=0,
        observed=None,
        expected=(),
        runtime_expected=(),
        runtime_principal_source=None,
        remediation=None,
    )


def inspect_state_root(vault_root: Path) -> StateRootPosture:
    """Read-only posture of one vault's state root. Repairs nothing.

    Reports everything about the directory rather than raising it. It can still
    raise for questions that have no honest answer to report: an
    `EXOMEM_STATE_ROOT` that is relative or inside the vault (`ValueError` from
    :func:`vault_state_dir`), or a current-token identity that cannot be
    established (`OSError`). Callers that must not fail on those catch them --
    doctor turns them into a finding, and the maintenance pre-flight refuses.
    """
    directory = vault_state_dir(vault_root)
    if not _is_windows():
        return _posture_off_windows(directory)

    from .mutation_lock import inspect_windows_private_directory

    posture = inspect_windows_private_directory(directory)
    if posture is None:
        return replace(_posture_off_windows(directory), present=False)
    return StateRootPosture(
        directory=directory,
        present=True,
        accessible=posture.accessible,
        unopenable_reason=posture.unopenable_reason,
        verdict=posture.verdict,
        runtime_verdict=posture.runtime_verdict,
        child_verdict=posture.child_verdict,
        child_sampled=posture.child_sampled,
        observed=posture.observed,
        expected=posture.expected,
        runtime_expected=posture.runtime_expected,
        runtime_principal_source=posture.runtime_principal.source,
        remediation=posture.remediation,
    )


def assert_state_root_accessible(vault_root: Path) -> None:
    """Refuse an unusable state root BEFORE an operation does any work.

    Refuses on BOTH halves. The first version gated only on "cannot open",
    which is the operator's failure; the service's failure is the other one --
    it opens a `user+SY+BA` root through its `SY` ACE and then rejects the
    trustee set, which is manifestation 1 of #933. Gating on one half let
    `maintain` proceed into `ensure_vault_state_dir` and die there with a raw
    `WindowsRuntimeDaclError`, which is the exact failure this exists to remove.

    The alternative is what the incident produced: `maintain --reconcile
    --rebuild-graph` ran far enough to take the mutation hold and census the
    graph lineage, then died on `[Errno 5] cannot safely open .graph.sqlite-wal`
    with the operation half-done and no statement of what to do about it.
    """
    posture = inspect_state_root(vault_root)
    if posture.blocks_current_token:
        raise StateRootAccessDenied(posture)


def seal_state_root_for_runtime_principal(
    vault_root: Path,
) -> WindowsRuntimePrincipal | None:
    """Leave the state root carrying the DACL the RUNTIME principal validates.

    The last act of a user-token flow that created or recreated the root, never
    part of creating it: the creating token must be able to write the state in
    first. A no-op returning None whenever the runtime principal is the current
    token, so a machine with no service install behaves exactly as before -- and
    also whenever this vault is not the one that machine's service is bound to.
    """
    if not _is_windows():
        return None

    from .mutation_lock import seal_windows_state_root_for_runtime_principal

    return seal_windows_state_root_for_runtime_principal(
        vault_state_dir(vault_root), vault_root=Path(vault_root)
    )


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
