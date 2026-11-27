"""Alternate setting prefixes (EVERMATTER_*, KB_MCP_*) onto EXOMEM_*.

Internal code reads only `EXOMEM_*`. A deployment may name a setting with the
product prefix `EVERMATTER_*` or the pre-rename prefix `KB_MCP_*` instead. Every
source of settings (the process environment, a `.env` file, a service
environment file) renames its own alias names into `EXOMEM_*` before its own
precedence rules apply, and the renamed source no longer holds the alias names.
So a source never lets an alias beat an explicit `EXOMEM_X`, a value that code
writes later wins, a child process inherits one name, and a conflict between two
sources is seen under one name.

Renaming the process environment removes the alias names from it, `KB_MCP_*`
included: a process that read `KB_MCP_X` before the package imported finds it
gone afterwards and reads `EXOMEM_X`. A child process never sees a `KB_MCP_*` name.

A writer persists `EVERMATTER_X` only (`emitted_name`), and removes the other spellings
of a setting it rewrites (`names_for`), so no file holds one setting under two names.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple, TypeVar

log = logging.getLogger(__name__)

V = TypeVar("V")

LEGACY_PREFIX = "KB_MCP_"
PUBLIC_PREFIX = "EVERMATTER_"
CANONICAL_PREFIX = "EXOMEM_"
# Closed by the product's naming history, in precedence order after EXOMEM_; the hooks copy this rule.
ALIAS_PREFIXES = (PUBLIC_PREFIX, LEGACY_PREFIX)
SETTING_PREFIXES = (CANONICAL_PREFIX, *ALIAS_PREFIXES)
# Placement settings select where data lives, so a guess acts on the wrong vault or state root.
# Closed set of setting names (without prefix) that this code itself defines, never a customer's.
# nosemgrep: ep-word-set -- the closed set is stated in the comment above
PLACEMENT_SETTINGS = frozenset({"VAULT_PATH", "STATE_ROOT", "HOSTED_STATE_ROOT"})


class Conflict(NamedTuple):
    """One source sets `alias` (an EVERMATTER_ name) and `canonical` to different values."""

    alias: str
    canonical: str


_advised = False
# The process environment's conflicts, kept because promotion removes the alias
# that held one side: the entry point refuses a placement conflict from here.
_process_conflicts: list[Conflict] = []
# The process environment as the process inherited it, kept because the first rename removes
# its alias names. The supervisor reads its environment files over this copy, by literal name.
_inherited: dict[str, str] | None = None
# A conflict is warned about once per process: the same environment is mapped again
# by every later loader, and one line per pair is enough for the operator.
_reported: set[Conflict] = set()


class SettingsConflict(ValueError):
    """A source sets a product-prefix name and its EXOMEM_ name to different values.

    The message names the variables, never their values: they may be secrets.
    """

    def __init__(
        self,
        conflicts: Iterable[Conflict],
        source: str,
        remedy: str = (
            "Set each value once, under its EVERMATTER_ name, and remove or unset the other "
            "name, then run it again."
        ),
    ) -> None:
        self.conflicts = tuple(conflicts)
        self.source = source
        pairs = "; ".join(f"{c.alias} and {c.canonical}" for c in self.conflicts)
        super().__init__(
            f"{pairs} are set to different values in {source}, so it is unclear which to use. {remedy}"
        )


# The flags a command line uses to identify itself without acting. argparse and the CLI define
# them, so the set is closed; a refusal that would block them only hides the conflict message.
# nosemgrep: ep-word-set -- the three flags are fixed by argparse and the CLI, not by data
IDENTIFICATION_FLAGS = frozenset({"--version", "--help", "-h"})


def asks_only_to_identify(args: Iterable[str]) -> bool:
    """Whether a command line asks only for the version or help, which acts on no setting."""
    return any(arg in IDENTIFICATION_FLAGS for arg in args)


def is_placement(conflict: Conflict) -> bool:
    return conflict.canonical[len(CANONICAL_PREFIX) :] in PLACEMENT_SETTINGS


def rename_settings(settings: Mapping[str, V]) -> tuple[dict[str, V], list[Conflict]]:
    """`settings` with each alias name renamed into its `EXOMEM_X` name, and the alias names removed.

    An `EXOMEM_X` that `settings` sets is never replaced. When it is unset,
    `EVERMATTER_X` then `KB_MCP_X` fills it. A value of None counts as unset. The
    conflicts name each `EVERMATTER_X` whose value differs from a set `EXOMEM_X`;
    a differing `KB_MCP_X` loses to `EXOMEM_X` silently.
    """
    renamed: dict[str, V] = {}
    aliases: dict[str, dict[str, V]] = {}
    for name, value in settings.items():
        prefix = next((p for p in ALIAS_PREFIXES if name.startswith(p)), None)
        if prefix is None:
            renamed[name] = value
        elif value is not None:
            aliases.setdefault(CANONICAL_PREFIX + name[len(prefix) :], {})[prefix] = value
    conflicts: list[Conflict] = []
    for canonical, by_prefix in aliases.items():
        explicit = renamed.get(canonical)
        if explicit is None:
            renamed[canonical] = next(by_prefix[p] for p in ALIAS_PREFIXES if p in by_prefix)
        elif PUBLIC_PREFIX in by_prefix and by_prefix[PUBLIC_PREFIX] != explicit:
            conflicts.append(
                Conflict(PUBLIC_PREFIX + canonical[len(CANONICAL_PREFIX) :], canonical)
            )
    return renamed, conflicts


def _setting_suffix(name: str) -> str | None:
    """`name` without its setting prefix, or None when `name` is not a setting."""
    return next((name[len(p) :] for p in SETTING_PREFIXES if name.startswith(p)), None)


def names_for(name: str) -> tuple[str, ...]:
    """Every name that can set `name`, highest precedence first.

    A setting named under any of the three prefixes has the other two names too; any
    other name stands alone. A writer that rewrites or removes a setting handles all of
    them, or an alias line would bring the old value back.
    """
    suffix = _setting_suffix(name)
    if suffix is None:
        return (name,)
    return tuple(prefix + suffix for prefix in SETTING_PREFIXES)


def emitted_name(name: str) -> str:
    """The name a writer persists for `name`: `EVERMATTER_X` for a setting, else `name` itself.

    Code reads `EXOMEM_X`, so a writer that holds an internal name maps it here before it
    writes a file, a client configuration or an image. A command that a script starts
    from its own environment is not persisted and still takes `EXOMEM_X`.
    """
    suffix = _setting_suffix(name)
    return name if suffix is None else PUBLIC_PREFIX + suffix


def settle_conflicts(
    conflicts: Iterable[Conflict], *, source: str, refuse_all: bool = False
) -> None:
    """Refuse a placement conflict (every conflict with `refuse_all`); warn once about the rest.

    Refusal costs one stopped run and the fix is to delete one line; a guess acts on the wrong vault,
    state root or credential store. `EXOMEM_X` wins every warned conflict.
    """
    conflicts = list(conflicts)
    refused = [c for c in conflicts if refuse_all or is_placement(c)]
    if refused:
        raise SettingsConflict(refused, source)
    for conflict in conflicts:
        _warn(conflict)


def _warn(conflict: Conflict) -> None:
    if conflict in _reported:
        return
    _reported.add(conflict)
    log.warning(
        "%s and %s are both set to different values; using %s. Set the value once, "
        "under %s, and remove or unset %s.",
        conflict.alias,
        conflict.canonical,
        conflict.canonical,
        conflict.alias,
        conflict.canonical,
    )


def canonical_settings(
    settings: Mapping[str, V], *, source: str, refuse_all: bool = False
) -> dict[str, V]:
    """`rename_settings`, with its conflicts settled for the source named `source`."""
    renamed, conflicts = rename_settings(settings)
    settle_conflicts(conflicts, source=source, refuse_all=refuse_all)
    return renamed


def inherited_environment() -> dict[str, str]:
    """A copy of the process environment as it was before the first rename removed its alias names."""
    return dict(os.environ if _inherited is None else _inherited)


def promote_legacy() -> list[str]:
    """Rename the alias names in the process environment into `EXOMEM_X`, and remove them.

    Returns the canonical names that were populated from an alias this call. Safe
    to call repeatedly: the first call removes the aliases, so a later write of
    `EXOMEM_X` wins and a child process inherits one name. Call again after any
    late environment loading. A conflict cannot raise here, because this runs at
    package import; it is recorded for `refuse_process_conflicts` and warned
    about once. Logs one advisory line, once per process, the first time a
    `KB_MCP_*` name is found.
    """
    global _advised, _inherited
    before = dict(os.environ)
    if _inherited is None:
        _inherited = dict(before)
    renamed, conflicts = rename_settings(before)
    removed = [name for name in before if name not in renamed]
    for name in removed:
        os.environ.pop(name, None)
    promoted = [name for name in renamed if name not in before]
    for name in promoted:
        os.environ[name] = renamed[name]
    for conflict in conflicts:
        if conflict not in _process_conflicts:
            _process_conflicts.append(conflict)
        _warn(conflict)
    if not _advised and any(name.startswith(LEGACY_PREFIX) for name in removed):
        _advised = True
        log.info(
            "legacy KB_MCP_* environment variables detected and honored; "
            "prefer the EXOMEM_* or EVERMATTER_* names going forward",
        )
    return promoted


def refuse_process_conflicts(*, every_setting: bool = False) -> None:
    """Raise `SettingsConflict` for a conflict that the process environment held at promotion.

    Only a placement conflict by default; a command that writes a credential passes `every_setting`.
    """
    refused = [c for c in _process_conflicts if every_setting or is_placement(c)]
    if refused:
        raise SettingsConflict(refused, "the process environment")


def program_module(argv: Sequence[str]) -> tuple[str, list[str]] | None:
    """The module and its arguments when `argv` is `python [options] -m module [args]`, else None.

    This is the interpreter's own option grammar: `-m` ends the options, and it may join other
    flags (`-Im`) or its module (`-mexomem.x`). `-W` and `-X` take a value; `-c` runs code, not a module.
    """
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--check-hash-based-pycs":
            index += 2
            continue
        if not arg.startswith("-") or arg in ("-", "--"):
            return None  # a script, standard input or the end of the options
        flags = "" if arg.startswith("--") else arg[1:]
        for position, flag in enumerate(flags, start=1):
            joined = arg[position + 1 :]
            if flag == "m":
                module = joined or (argv[index + 1] if index + 1 < len(argv) else "")
                return (module, list(argv[index + (1 if joined else 2) :])) if module else None
            if flag == "c":
                return None
            if flag in "WX":  # takes a value: the rest of this argument, or the next one
                index += 0 if joined else 1
                break
        index += 1
    return None


def refuse_program_entry_conflicts(orig_argv: Sequence[str] | None = None) -> None:
    """Exit with code 2 when `python -m exomem.<module>` starts on a placement conflict.

    The managed supervisor, the lease coordinator, the upgrade and import jobs and the media
    children are run as modules, so none passes through the CLI's own refusal. They share one
    check at package import, which cannot raise for a library user: it acts only when the
    interpreter was started with `-m exomem.<module>`. `-m exomem` refuses in its own `main`,
    and a version or help request runs.
    """
    program = program_module(sys.orig_argv if orig_argv is None else orig_argv)
    if program is None:
        return
    module, arguments = program
    if not module.startswith("exomem.") or module == "exomem.__main__":
        return
    if asks_only_to_identify(arguments):
        return
    try:
        refuse_process_conflicts()
    except SettingsConflict as conflict:
        print(f"{module}: not run. {conflict}", file=sys.stderr)
        raise SystemExit(2) from conflict


def refuse_placement_conflicts_across(sources: Iterable[Mapping[str, str]], *, source: str) -> None:
    """Treat `sources` that one process reads together as one source for placement conflicts.

    Each source holds its names as written, before any rename. A later source's value for a
    name replaces an earlier one's by that literal spelling, as an environment file replaces
    what its supervisor inherited, so an edit under either spelling takes effect alike. The
    merged view then refuses an `EVERMATTER_X` that disagrees with the `EXOMEM_X` in force:
    a conflict between the supervisor and a file, or between two files, would otherwise give
    two processes of one service two places. Each source's own conflicts are settled by
    `canonical_settings`.
    """
    merged: dict[str, str] = {}
    for settings in sources:
        merged.update(settings)
    _, conflicts = rename_settings(merged)
    refused = [c for c in conflicts if is_placement(c)]
    if refused:
        # Each pair is set under one name in the supervisor's own environment, taken when the unit
        # started, and under the other in a file, so deleting a line from the file may not settle it.
        raise SettingsConflict(
            refused,
            source,
            remedy=(
                "Each pair is set under one name in the supervisor's own environment, taken "
                "when the unit started, and under the other in a service environment file. "
                "Restart the unit so that it takes the file's names, or change the file so "
                "that its value matches."
            ),
        )


def dotenv_settings(
    dotenv_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, str], list[Conflict]]:
    """The values a `.env` file sets, renamed into `EXOMEM_X`, with the file's own conflicts.

    The caller passes a path it has already checked with `dotenv_guard`.
    """
    import dotenv

    renamed, conflicts = rename_settings(dotenv.dotenv_values(dotenv_path))
    return {name: value for name, value in renamed.items() if value is not None}, conflicts


def apply_settings(settings: Mapping[str, str], *, override: bool) -> bool:
    """Write `settings` into the process environment; without `override`, an exported name stays."""
    applied = False
    for name, value in settings.items():
        if override or name not in os.environ:
            os.environ[name] = value
            applied = True
    return applied


def load_dotenv(
    dotenv_path: str | os.PathLike[str] | None = None, *, override: bool = False
) -> bool:
    """Load a `.env` file after renaming its alias names, so only `EXOMEM_X` reaches the environment.

    A file that sets only `EVERMATTER_X` gives the process `EXOMEM_X`. Under
    `override` it beats an exported `EXOMEM_X`, exactly as the file's own
    `EXOMEM_X` would; without it, an exported value stays. A placement conflict
    inside the file raises `SettingsConflict`.
    """
    settings, conflicts = dotenv_settings(dotenv_path)
    settle_conflicts(conflicts, source="the .env file")
    return apply_settings(settings, override=override)
