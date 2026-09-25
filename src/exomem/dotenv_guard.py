"""Shared guard: never load or write a working-directory `.env` that lives
inside a vault.

Vault content is writable by remote principals through the file tools and
arrives through sync, so a `.env` found there must never become service or
operator configuration (owner binding, signing key, REST key, OAuth storage
URL, ...). Every place in the package that reads or writes a working-directory
`.env` -- the server's own startup, the CLI's `auth`/`doctor` loaders, the
early native-resource preload, the remote setup wizard's read-back and write,
and the local setup wizard's client-route lookup -- routes through
`dotenv_load_guard` (or its `working_directory_dotenv` convenience wrapper) so
the refusal logic lives in exactly one place.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .vault import _is_vault

log = logging.getLogger(__name__)

_VAULT_PATH_ENV = ("EXOMEM_VAULT_PATH", "KB_MCP_VAULT_PATH")


def dotenv_load_guard(path: Path) -> Path | None:
    """`path`, or None when reading/writing it would touch a vault.

    Both the directory holding `path` (a symlinked working directory, or a
    structural ancestor that is itself a vault) and the directory `path`
    resolves into (a symlinked `.env` may point into a vault) are checked.
    "Inside a vault" is the configured vault (process environment, or the
    vault `path` would itself configure, when it already exists and is
    readable) or any enclosing directory that is structurally a vault.

    A wrong refusal is not free: when the refused file held required settings
    (the vault path, OAuth or signing keys), the caller then fails on the
    missing setting. The log line here says so and names the remedy; the
    ancestor walk is kept because the file it guards can carry service
    secrets.
    """
    directory = path.parent.resolve()
    candidate = directory / path.name
    configured = [os.environ.get(name, "") for name in _VAULT_PATH_ENV]
    if candidate.is_file():
        try:
            from dotenv import dotenv_values

            declared = dotenv_values(candidate)
            configured += [str(declared.get(name) or "") for name in _VAULT_PATH_ENV]
        except (OSError, UnicodeDecodeError):
            pass
    roots: list[Path] = []
    for raw in configured:
        if raw.strip():
            try:
                roots.append(Path(raw.strip()).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
    try:
        resolved_parent = candidate.resolve().parent
    except (OSError, RuntimeError):
        resolved_parent = directory

    def _inside(check: Path) -> bool:
        return any(check == root or root in check.parents for root in roots) or any(
            _is_vault(ancestor) for ancestor in (check, *check.parents)
        )

    if _inside(directory) or _inside(resolved_parent):
        log.warning(
            "event=dotenv_refused reason=inside_vault path=%s remedy=move the .env out "
            "of the vault, or put the settings in service.env",
            candidate,
        )
        return None
    return candidate


def working_directory_dotenv() -> Path | None:
    """`<cwd>/.env`, or None when it would load from inside a vault.

    The convenience form every in-process loader uses: see
    `dotenv_load_guard` for the actual check.
    """
    return dotenv_load_guard(Path.cwd() / ".env")
