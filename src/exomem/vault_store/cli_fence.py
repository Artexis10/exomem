"""Which CLI subcommands touch a vault's pages, and the refusal that a store-mode vault gives them (OpenSpec make-vault-store-canonical, task 3.1).

The registry path refuses a store-mode vault in ``product_invoke`` (``availability.require_servable``). The
subcommands that ``exomem.__main__`` dispatches before that path read or write pages, projections or
file-mode indexes themselves, so a store-mode vault would answer them from the Markdown that the store
replaced. Each subcommand declares here what it touches, and each declared handler calls ``require`` with the
vault it resolved, at the point where it resolves it. The declaration lives here, not in ``__main__``, because
the handlers that resolve a vault sit in other modules, and the lean ``cell-media-*`` routes must not load the CLI.

A ``PAGES`` entry refuses a store-mode vault for good, whether or not the build serves store mode: the command
works outside the service, and no rollout gate changes that. Each entry names what its owner does instead.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

#: The refusal code of a ``PAGES`` command on a store-mode vault.
REFUSED = "VAULT_STORE_CLI_REFUSED"


class VaultAccess(enum.Enum):
    #: Reads and writes no page, projection or file-mode index, so a store-mode vault changes nothing for it.
    NONE = "none"
    #: Reads or writes pages, projections or file-mode indexes outside the service: a store-mode vault refuses it.
    PAGES = "pages"
    #: Reaches pages only through code that refuses a store-mode vault itself, at the steps that need it.
    SELF_FENCED = "self-fenced"


@dataclass(frozen=True)
class Declaration:
    access: VaultAccess
    #: What the owner of a store-mode vault does instead, in a sentence that the refusal repeats. ``PAGES`` only.
    remedy: str = ""


_NONE = Declaration(VaultAccess.NONE)
_SELF_FENCED = Declaration(VaultAccess.SELF_FENCED)
_ROLLBACK = "Run `exomem store-mode rollback` to return the pages to files, then run this command again."


def _pages(remedy: str = _ROLLBACK) -> Declaration:
    """A command that works on pages outside the service. Without a service tool for its job, the owner rolls back."""
    return Declaration(VaultAccess.PAGES, remedy)


# A family of subcommands declares each one as "<command> <subcommand>". The key is the CLI's own parser name.
# nosemgrep: ep-word-set -- This CLI's own parser registers these subcommand names.
ACCESS: dict[str, Declaration] = {
    # Scans the vault, scaffolds the managed root and refreshes its shipped schema files.
    "setup": _pages(),
    # Writes the scaffold into the vault.
    "init": _pages(),
    # Deletes the superseded schema copy from the vault's note namespace.
    "reclaim-schema": _pages(),
    # Writes skill folders into client configuration, never into a vault.
    "install-skill": _NONE,
    # With --vault it reads the vault's real project-keys file; without, it reads no vault.
    "package-skills": _pages(),
    # Scans the vault's top-level folders and writes its access file.
    "personalize": _pages(),
    "install-hook": _NONE,
    # With --vault it runs doctor, find, get and audit on that vault; without, on a bundled sample.
    "demo": _pages("Ask the service instead: `ask_memory` and `read_memory` answer from the store."),
    # Prints a URL.
    "studio": _NONE,
    # The terminal UI backend calls require_servable when it starts its runtime.
    "tui": _SELF_FENCED,
    # Reports; it names store mode itself and stays reachable where a vault is refused.
    "doctor": _NONE,
    "install-info": _NONE,
    # Session administration and client attachment name no vault.
    "auth": _NONE,
    "attach": _NONE,
    # Reports resource posture and the store's own mode record.
    "status": _NONE,
    # With --vault it warms the vault's lexical caches, which are file-mode projections.
    "warm": _pages("Start the service: it reads the store's own rows, so no cache needs warming."),
    # Per-machine settings.
    "mode": _NONE,
    "dreamer": _NONE,
    "prominence": _NONE,
    # Writes sidecar companions and indexes beside the vault's media files.
    "backfill-media": _pages("Ask the service instead: `process_media` without a path extracts governed media from the store."),
    # Builds the file-mode vector index from the vault's Markdown.
    "index": _pages(),
    # The voice-profile store is per-machine state.
    "enroll-speaker": _NONE,
    "list-speakers": _NONE,
    "remove-speaker": _NONE,
    "trace": _NONE,
    "logs": _NONE,
    # The store route: `lease` and `store-mode` are the commands that act on a store-mode vault.
    "lease": _NONE,
    "store-mode": _NONE,
    # Reads the governance store and its custody, not pages.
    "governance-schema status": _NONE,
    # Walks the vault's Markdown to build the catalogue that the target holds.
    "governance-schema plan-migration": _pages(),
    "governance-schema stage-migration": _pages(),
    "governance-schema commit-migration": _pages(),
    # Restore and downmigrate rewrite the governance store from its own backup, not from pages.
    "governance-schema restore-migration-backup": _NONE,
    "governance-schema downmigrate": _NONE,
    # The census asks the service only without `--vault` or `--sample`; the route that this entry fences never
    # does, so no "ask the service" remedy fits it.
    "relations": _pages(),
    # `collections` is one parser whose subcommands differ in what they touch, so each declares its own access.
    "collections backup": _NONE,
    "collections adopt-local": _pages(
        "Ask the service instead: `maintain_memory` with mode `collections-store-adopt-local`."),
    "collections create": _pages(),
    "collections rollback": _pages(),
    "collections migrate": _pages(),
    "collections export": _SELF_FENCED,
    "collections cutover": _pages(),
    # Copies a folder into the vault's import folder.
    "import": _pages(),
    # The backup copies the vault's files and staged state to a repository, and restore writes a separate
    # directory; none answers a question from the files, so a store-mode vault changes nothing for them.
    "backup status": _NONE,
    "backup run": _NONE,
    "backup restore": _NONE,
    # The cloud client talks to the cloud service and reads no local vault.
    "cloud": _NONE,
    # The cell jobs: imports into and exports from the vault, and creates it when absent.
    "cell-import": _pages(),
    "cell-export": _pages(),
    "cell-init": _pages(),
    # Writes a store marker outside the vault and ledger rows beside it; it reads no page, projection or index.
    "media-replicas": _NONE,
    # Reads the cell's media drain record, which the file-mode drain wrote.
    "cell-media-census": _pages(),
    # Reads the vault's companions for the media references that they hold.
    "cell-media-manifest": _pages(),
    # Takes a snapshot and a manifest path, never a vault: it reads manifests and asks the media edge.
    "cell-media-collect": _NONE,
    # An alias of the registry command `activate_context`.
    "activate": _SELF_FENCED,
}


class StoreFenceRefusal(RuntimeError):
    """The refusal that a CLI subcommand gives a vault it must not touch, with its code and reason."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def require(command: str, vault_root: Path) -> None:
    """Refuse a store-mode vault for a subcommand that declares page access, and name what to do instead.

    ``command`` is the declared name, and an undeclared one raises ``KeyError``. A subcommand that creates its
    vault passes because a missing vault is not in store mode. A store-mode vault refuses on the mode itself, not
    on ``availability.require_servable``: that one lifts when a build serves store mode, and these commands
    still work outside the service. A false refusal costs the owner the rollback or the service tool that the
    message names. A file-mode vault keeps ``require_servable``'s one refusal, the store held by another root.
    An unreadable manifest refuses in one line, because an unknown mode must never pass for file mode.
    """
    declared = ACCESS[command]
    if declared.access is not VaultAccess.PAGES:
        return
    from .. import state_migration
    from ..collection_store.connection import CollectionStoreError
    from . import availability, mode

    try:
        in_store_mode = mode.store_mode_active(vault_root)
        if not in_store_mode:
            availability.require_servable(vault_root)
    except CollectionStoreError as refused:
        raise StoreFenceRefusal(refused.code, refused.reason) from None
    except state_migration.StateMigrationManifestError as unreadable:
        raise StoreFenceRefusal(unreadable.code, str(unreadable)) from None
    if in_store_mode:
        raise StoreFenceRefusal(
            REFUSED,
            f"`exomem {command}` works on pages, projections or file-mode indexes outside the service, "
            f"and this vault keeps its pages in the collection store. {declared.remedy}",
        )
