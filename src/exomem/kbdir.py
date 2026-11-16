"""The governed-folder name inside a vault — configurable via ``EXOMEM_KB_DIRNAME``.

Defaults to ``"Knowledge Base"``. This is the single source of truth for the KB
subtree name so it isn't hardcoded across the codebase. Read from the environment on
each call (the same way ``vault.resolve_vault`` reads ``EXOMEM_VAULT_PATH``), so it is
per-process and test-overridable — set ``EXOMEM_KB_DIRNAME`` and the whole engine
resolves, indexes, and wikilinks against that folder name instead.

Everything outside this module reaches the folder through the ``managed_*`` helpers
below, never through ``kb_dirname``/``kb_prefix``. The helpers own how a managed-root-relative
path (``Sources/x.md``) becomes a vault-relative path (``Knowledge Base/Sources/x.md``) and
back, so a different root layout changes here only.

The helpers take an optional layout. ``None`` and ``nested`` return today's strings exactly. In
``root`` the vault root is the managed root: there is no wrapper to add or remove, and a path
whose first component sits outside Exomem's care (a dot folder, the import staging folder or an
entry that the layout record lists as unmanaged) is outside the managed root.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Sequence
from enum import StrEnum
from pathlib import Path

_DEFAULT = "Knowledge Base"

#: The vault folder that holds staged imports, with a folder for each `<import_id>`.
IMPORT_STAGE_DIRNAME = "_Imports"


class Layout(StrEnum):
    """Where the managed root sits. The vault-layout schema fixes these two values."""

    NESTED = "nested"
    ROOT = "root"


def _is_root(layout: Layout | str | None) -> bool:
    """Whether ``layout`` is `root`. ``None`` is `nested`; a name that is neither refuses, so a typo never reads as `nested`."""
    return layout is not None and Layout(layout) is Layout.ROOT


def _outside_root(
    head: str, unmanaged: Collection[str], key: Callable[[str], str] | None
) -> bool:
    """Whether the top-level entry ``head`` sits outside the managed root of a `root` vault.

    A name that starts with a dot is the filesystem and Obsidian convention for an entry that no
    tool reads as content. The staging folder holds uploads that are not pages yet.
    """
    if head.startswith("."):
        return True
    if key is None:
        return head == IMPORT_STAGE_DIRNAME or head in unmanaged
    folded = key(head)
    return folded == key(IMPORT_STAGE_DIRNAME) or any(folded == key(name) for name in unmanaged)


def kb_dirname() -> str:
    """The KB folder name, a single folder (``EXOMEM_KB_DIRNAME`` override, else default).

    A leading or trailing slash is dropped, a backslash like a slash (native Windows). A separator
    inside the name is refused: the helpers below compare the root as the first component of a
    path, which a nested name never equals.
    """
    name = os.environ.get("EXOMEM_KB_DIRNAME", "").strip().strip("/\\")
    if "/" in name or "\\" in name:
        raise ValueError(
            f"EXOMEM_KB_DIRNAME={name!r} names a nested path. Set it to a single folder name "
            "inside the vault, such as 'Knowledge Base', or unset it."
        )
    return name or _DEFAULT


def kb_prefix() -> str:
    """The KB folder name with a trailing slash, for prefix ops (e.g. ``"Knowledge Base/"``)."""
    return kb_dirname() + "/"


def managed_join(rel: str, *, layout: Layout | str | None = None) -> str:
    """The vault-relative path of ``rel``, a path relative to the managed root.

    ``managed_join("")`` is the managed root's own prefix (``"Knowledge Base/"``): the
    result is always the prefix followed by ``rel``, and a trailing slash on ``rel`` is kept.
    In ``root`` the vault root is the managed root, so the result is ``rel`` itself.
    """
    return rel if _is_root(layout) else kb_prefix() + rel


def managed_split(
    vault_rel: str,
    *,
    key: Callable[[str], str] | None = None,
    layout: Layout | str | None = None,
    unmanaged: Collection[str] = frozenset(),
) -> str | None:
    """The managed-root-relative part of ``vault_rel``, or ``None`` outside the managed root.

    The inverse of ``managed_join``: the path must start with the root's prefix, so the
    root's bare name is outside and ``"Knowledge Base/"`` splits to ``""``. A ``key`` (such as
    ``str.casefold``) compares the root's name through it, for callers whose own rule is looser
    than byte equality; the remainder keeps the caller's spelling.

    In ``root`` the path is already managed-root-relative, so it comes back as it is, unless its
    first component is a dot name, the import staging folder or a name in ``unmanaged`` (the names
    that the layout record lists, compared through ``key`` when given): those are outside.
    """
    if _is_root(layout):
        return None if _outside_root(vault_rel.partition("/")[0], unmanaged, key) else vault_rel
    if key is not None:
        head, separator, tail = vault_rel.partition("/")
        return tail if separator and key(head) == key(kb_dirname()) else None
    prefix = kb_prefix()
    return vault_rel[len(prefix):] if vault_rel.startswith(prefix) else None


def managed_splitter(
    *, layout: Layout | str | None = None, unmanaged: Collection[str] = frozenset()
) -> Callable[[str], str | None]:
    """``managed_split`` with the root read once, for a loop over many paths.

    Reading the root costs an environment lookup, so a loop over a corpus binds it here and
    calls the result per path, as it would hoist a prefix string. ``layout`` and ``unmanaged``
    are those of ``managed_split``.
    """
    if _is_root(layout):
        names = frozenset(unmanaged)
        return lambda vault_rel: (
            None if _outside_root(vault_rel.partition("/")[0], names, None) else vault_rel
        )
    prefix = kb_prefix()
    size = len(prefix)
    return lambda vault_rel: vault_rel[size:] if vault_rel.startswith(prefix) else None


def managed_split_parts(
    parts: Sequence[str],
    *,
    key: Callable[[str], str] | None = None,
    layout: Layout | str | None = None,
    unmanaged: Collection[str] = frozenset(),
) -> tuple[str, ...] | None:
    """The components below the managed root, or ``None`` when ``parts`` is outside it.

    The components form of ``managed_split`` for paths already split on ``/``. The root
    itself (its name alone) gives an empty tuple. ``key``, ``layout`` and ``unmanaged`` are those of
    ``managed_split``; in ``root`` the empty tuple is the root itself.
    """
    if _is_root(layout):
        return None if parts and _outside_root(parts[0], unmanaged, key) else tuple(parts)
    if parts and is_managed_root_name(parts[0], key=key):
        return tuple(parts[1:])
    return None


def is_managed_root_name(segment: str, *, key: Callable[[str], str] | None = None) -> bool:
    """Whether a path segment is the managed root's name, compared through ``key`` when given."""
    name = kb_dirname()
    return segment == name if key is None else key(segment) == key(name)


def is_in_managed_root(vault_rel: str) -> bool:
    """Whether ``vault_rel`` is the managed root itself or lies below it."""
    name = kb_dirname()
    return vault_rel == name or vault_rel.startswith(name + "/")


def managed_form(path: str) -> str:
    """``path`` as a vault-relative path, adding the managed root when ``path`` lacks it.

    For text that may or may not carry the root already. It changes nothing else about
    ``path``; ``kb_relative_form`` is the variant that also cleans separators.
    """
    prefix = kb_prefix()
    return path if path.startswith(prefix) else prefix + path


def managed_relative(path: str) -> str:
    """``path`` without the managed root when it starts with it, else ``path`` unchanged."""
    return path.removeprefix(kb_prefix())


def managed_respell(rel: str) -> str:
    """``rel`` with a leading root segment that differs from the configured name only in letter case re-spelled.

    On a case-insensitive filesystem the real on-disk casing of the managed root may differ from
    the configured name (``knowledge base`` against ``Knowledge Base``). The identity census keys
    the first segment literally and ``managed_split`` is byte-exact, so a canonicalised rel-form
    must still lead with the configured spelling.
    """
    head, separator, tail = rel.partition("/")
    if head != kb_dirname() and is_managed_root_name(head, key=str.casefold):
        return managed_join(tail) if separator else managed_root_rel()
    return rel


def managed_root(vault_root: Path, *, layout: Layout | str | None = None) -> Path:
    """The managed root directory of the vault at ``vault_root``: the vault root itself in ``root``."""
    return vault_root if _is_root(layout) else vault_root / kb_dirname()


def managed_root_rel() -> str:
    """The vault-relative name of the managed root directory, with no trailing slash.

    For callers that hand the directory itself to a held-filesystem operation, a cache key
    or a report field. Path text under the root goes through ``managed_join``.
    """
    return kb_dirname()


def kb_relative_form(path: str) -> str:
    """The KB-relative rel-form a page write leaf will use for ``path``.

    The single source of truth for how caller text becomes a vault-relative
    page path: separators normalised, leading separators dropped, and the KB
    prefix supplied when the caller omitted it (which the tool descriptions
    explicitly invite). ``edit._resolve`` and ``replace._resolve_kb_path`` both
    call it, and so does the hosted protected-tree guard.

    That last caller is the reason this lives here rather than in either leaf.
    A guard that decides "is this target inside a protected tree?" and an
    executor that decides "which file do I open?" must derive the same
    rel-form, or the guard is answering about a path the executor never
    touches. Three separate bypasses of the hosted guard traced to exactly that
    disagreement, the last one because the guard joined at the *vault* root
    while this joins at the *KB* root. Copying this logic into the guard
    would be the same defect with a longer fuse, so there is one function and
    every caller uses it.
    """

    return managed_form(path.strip().replace("\\", "/").lstrip("/"))


def kb_page_relative_form(path: str) -> str:
    """The KB-relative rel-form of a *page* target, Markdown suffix included.

    Extensionless input is a supported shape -- the leaves supply the suffix --
    so the rel-form a page write actually uses is this one, not
    ``kb_relative_form``. A guard that judges the unsuffixed form treats the
    final component as a directory the caller might be entering, when the leaf
    will make it a file.
    """

    rel = kb_relative_form(path)
    if not rel.endswith(".md"):
        rel = rel + ".md"
    return rel


def kb_page_target(vault_root: Path, path: str) -> tuple[Path, str]:
    """Return ``(absolute candidate, rel-form)`` for a caller-supplied page path.

    No filesystem access and no validation: the caller still has to decide
    whether the result exists, escapes the KB, or is allowed. This answers only
    "which file does this text name?".
    """

    rel = kb_page_relative_form(path)
    return Path(vault_root) / rel, rel


def resolve_stored_page_target(vault_root: Path, rel: str) -> tuple[Path, str] | None:
    """Resolve an existing authoring parent from the store, or leave file-mode resolution alone."""
    from . import vault
    from .vault_store import read

    if not read.serves(vault_root, rel):
        return None
    try:
        return vault.resolve_under_vault(
            vault_root, rel, must_exist=True, must_be_under_kb=True, refuse_withheld=True,
        )
    except vault.VaultPathError as error:
        if error.code == "NOT_FOUND":
            raise vault.VaultPathError("NOT_FOUND", f"file does not exist: {rel}") from None
        raise
