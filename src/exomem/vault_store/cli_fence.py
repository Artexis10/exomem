"""Which CLI subcommands touch a vault's pages, and the refusal that a store-mode vault gives them (OpenSpec make-vault-store-canonical, task 3.1).

The registry path refuses a store-mode vault in ``product_invoke`` (``availability.require_servable``). The
subcommands that ``exomem.__main__`` dispatches before that path read or write pages, projections or
file-mode indexes themselves, so a store-mode vault would answer them from the Markdown that the store
replaced. Each subcommand declares here what it touches, and each declared handler calls ``require`` with the
vault it resolved, at the point where it resolves it. The declaration lives here, not in ``__main__``, because
the handlers that resolve a vault sit in other modules, and the lean ``cell-media-*`` routes must not load the CLI.
"""

from __future__ import annotations

import enum
from pathlib import Path


class VaultAccess(enum.Enum):
    #: Reads and writes no page, projection or file-mode index, so a store-mode vault changes nothing for it.
    NONE = "none"
    #: Reads or writes pages, projections or file-mode indexes outside the service: a store-mode vault refuses it.
    PAGES = "pages"
    #: Reaches pages only through code that refuses a store-mode vault itself, at the steps that need it.
    SELF_FENCED = "self-fenced"


# A family of subcommands declares each one as "<command> <subcommand>". The key is the CLI's own parser name.
# nosemgrep: ep-word-set -- This CLI's own parser registers these subcommand names.
ACCESS: dict[str, VaultAccess] = {
    # Scans the vault, scaffolds the managed root and refreshes its shipped schema files.
    "setup": VaultAccess.PAGES,
    # Writes the scaffold into the vault.
    "init": VaultAccess.PAGES,
    # Deletes the superseded schema copy from the vault's note namespace.
    "reclaim-schema": VaultAccess.PAGES,
    # Writes skill folders into client configuration, never into a vault.
    "install-skill": VaultAccess.NONE,
    # With --vault it reads the vault's real project-keys file; without, it reads no vault.
    "package-skills": VaultAccess.PAGES,
    # Scans the vault's top-level folders and writes its access file.
    "personalize": VaultAccess.PAGES,
    "install-hook": VaultAccess.NONE,
    # With --vault it runs doctor, find, get and audit on that vault; without, on a bundled sample.
    "demo": VaultAccess.PAGES,
    # Prints a URL.
    "studio": VaultAccess.NONE,
    # The terminal UI backend calls require_servable when it starts its runtime.
    "tui": VaultAccess.SELF_FENCED,
    # Reports; it names store mode itself and stays reachable where a vault is refused.
    "doctor": VaultAccess.NONE,
    "install-info": VaultAccess.NONE,
    # Session administration and client attachment name no vault.
    "auth": VaultAccess.NONE,
    "attach": VaultAccess.NONE,
    # Reports resource posture and the store's own mode record.
    "status": VaultAccess.NONE,
    # With --vault it warms the vault's lexical caches, which are file-mode projections.
    "warm": VaultAccess.PAGES,
    # Per-machine settings.
    "mode": VaultAccess.NONE,
    "dreamer": VaultAccess.NONE,
    "prominence": VaultAccess.NONE,
    # Writes sidecar companions and indexes beside the vault's media files.
    "backfill-media": VaultAccess.PAGES,
    # Builds the file-mode vector index from the vault's Markdown.
    "index": VaultAccess.PAGES,
    # The voice-profile store is per-machine state.
    "enroll-speaker": VaultAccess.NONE,
    "list-speakers": VaultAccess.NONE,
    "remove-speaker": VaultAccess.NONE,
    "trace": VaultAccess.NONE,
    "logs": VaultAccess.NONE,
    # The store route: `lease` and `store-mode` are the commands that act on a store-mode vault.
    "lease": VaultAccess.NONE,
    "store-mode": VaultAccess.NONE,
    # Reads the governance store and its custody, not pages.
    "governance-schema status": VaultAccess.NONE,
    # Walks the vault's Markdown to build the catalogue that the target holds.
    "governance-schema plan-migration": VaultAccess.PAGES,
    "governance-schema stage-migration": VaultAccess.PAGES,
    "governance-schema commit-migration": VaultAccess.PAGES,
    # Restore and downmigrate rewrite the governance store from its own backup, not from pages.
    "governance-schema restore-migration-backup": VaultAccess.NONE,
    "governance-schema downmigrate": VaultAccess.NONE,
    # Reads the local graph snapshot when the service does not answer.
    "relations": VaultAccess.PAGES,
    # `collections` is one parser whose subcommands differ in what they touch, so each declares its own access.
    "collections backup": VaultAccess.NONE,
    "collections adopt-local": VaultAccess.PAGES,
    "collections create": VaultAccess.PAGES,
    "collections rollback": VaultAccess.PAGES,
    "collections migrate": VaultAccess.PAGES,
    "collections export": VaultAccess.SELF_FENCED,
    "collections cutover": VaultAccess.PAGES,
    # Copies a folder into the vault's import folder.
    "import": VaultAccess.PAGES,
    # The cloud client talks to the cloud service and reads no local vault.
    "cloud": VaultAccess.NONE,
    # The cell jobs: imports into and exports from the vault, and creates it when absent.
    "cell-import": VaultAccess.PAGES,
    "cell-export": VaultAccess.PAGES,
    "cell-init": VaultAccess.PAGES,
    # Reads the cell's media drain record, which the file-mode drain wrote.
    "cell-media-census": VaultAccess.PAGES,
    # Reads the vault's companions for the media references that they hold.
    "cell-media-manifest": VaultAccess.PAGES,
    # Takes a snapshot and a manifest path, never a vault: it reads manifests and asks the media edge.
    "cell-media-collect": VaultAccess.NONE,
    # An alias of the registry command `activate_context`.
    "activate": VaultAccess.SELF_FENCED,
}


class StoreFenceRefusal(RuntimeError):
    """``availability.require_servable``'s refusal, with its code and reason, raised for a CLI subcommand."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def require(command: str, vault_root: Path) -> None:
    """Refuse a store-mode vault for a subcommand that declares page access, with the registry path's refusal.

    ``command`` is the declared name, and an undeclared one raises ``KeyError``. A subcommand that creates its
    vault passes because a missing vault is not in store mode. A false refusal costs the owner a rollback to
    run that single command, which the message names.
    """
    if ACCESS[command] is not VaultAccess.PAGES:
        return
    from ..collection_store.connection import CollectionStoreError
    from . import availability

    try:
        availability.require_servable(vault_root)
    except CollectionStoreError as refused:
        raise StoreFenceRefusal(refused.code, refused.reason) from None
