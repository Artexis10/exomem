"""The vault layout: where the managed root sits, and where each page goes in the root layout.

A vault has either of two layouts. In `nested`, the managed root is a folder inside a larger
vault and compiled notes sit under a `Notes/` parent. In `root`, the managed root is the
vault root and each compiled area is a top-level folder. This module holds what depends on
that choice and on no other state:

- `resolve` reads which layout a vault has, from its layout record;
- `area_folder`, `home_folder` and `translate_path` say where an area, a home or an authored
  path selector sits in a layout;
- `place` maps a single page of a `nested` vault to its folder and name class in `root`;
- `corrected_title_name`, `fit_filename` and `allocate` make the file names of derived pages:
  a corrected title, cut to the byte cap, unique across the vault.

No area or folder name is spelled here. Areas come from the note-type and home registries,
the collection profiles and the managed-root helpers in `kbdir`. Nothing in the read or write
paths calls this module yet: only the read-only relayout plan and `layout_report`, which feeds
status and doctor, do.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml

from . import collection_profiles, extraction_parts, homes, kbdir, note_types, vault
from .cli_ops import OpError
from .held_fs import vault_name_key
from .vault import corrected_title_name, fit_filename


class Layout(StrEnum):
    """Where the managed root sits. The vault-layout schema fixes these two values."""

    NESTED = "nested"
    ROOT = "root"


class LayoutError(OpError):
    """The layout of a vault cannot be read from its record and folders."""


# --------------------------------------------------------------------------- #
# The layout record
# --------------------------------------------------------------------------- #

#: The record's file name inside the managed root's schema folder.
RECORD_NAME = "vault-layout.yaml"


@dataclass(frozen=True, slots=True)
class LayoutRecord:
    """A vault's record: its layout and the top-level entries that sit outside Evermatter's care.

    `path` is where the record was read, relative to the vault root.
    """

    layout: Layout
    unmanaged: tuple[str, ...] = ()
    path: str = ""


def _schema_folders() -> tuple[str, str]:
    """The two places a schema folder can be, relative to the vault root: the vault root, then the managed root."""
    return vault.SCHEMA_OVERLAY_DIRNAME, kbdir.managed_join(vault.SCHEMA_OVERLAY_DIRNAME)


def _record_rels() -> tuple[str, str]:
    """The two places a record can live, in reading order: a `root` vault's, then a `nested` vault's."""
    root_folder, wrapper_folder = _schema_folders()
    return f"{root_folder}/{RECORD_NAME}", f"{wrapper_folder}/{RECORD_NAME}"


def _parse_record(text: str, where: str) -> LayoutRecord:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise LayoutError(
            "LAYOUT_RECORD_INVALID",
            f"{where} is not valid YAML.",
            f"Rewrite {where} as a mapping with the key `layout` (nested or root).",
        ) from error
    layout = value.get("layout") if isinstance(value, dict) else None
    unmanaged = value.get("unmanaged", []) if isinstance(value, dict) else None
    if (
        layout not in tuple(Layout)
        or not isinstance(unmanaged, list)
        or not all(isinstance(entry, str) for entry in unmanaged)
    ):
        raise LayoutError(
            "LAYOUT_RECORD_INVALID",
            f"{where} needs `layout` set to nested or root, and `unmanaged` as a list of names.",
            f"Fix {where}: set `layout`, and list each top-level entry that sits outside the managed root under `unmanaged`.",
        )
    return LayoutRecord(Layout(layout), tuple(unmanaged), where)


def _read_record_at(root: Path, rel: str) -> LayoutRecord | None:
    """The record at `rel` below `root`, or None when nothing is there.

    A store-mode vault keeps the record as a stored page whose file may be absent, so the
    store answers first for a path it serves, as `vault._is_vault` does for its sentinel.
    """
    from .vault_store import read

    try:
        stored = read.owned_bytes(root, rel)
    except FileNotFoundError:
        return None  # the store serves this path and holds no page
    if stored is None:
        try:
            stored = (root / rel).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LayoutError(
                "LAYOUT_RECORD_UNREADABLE",
                f"{rel} cannot be read: {error.strerror or error}.",
                f"Make {rel} a readable file, or remove it.",
            ) from error
    return _parse_record(stored.decode("utf-8", errors="replace"), rel)


def read_record(vault_root: Path) -> LayoutRecord | None:
    """The layout record of the vault at `vault_root`, or None when the vault has none.

    The record sits in the managed root's schema folder: at the vault root for a `root`
    vault, inside the wrapper for a `nested` vault. The vault root is read first, then the wrapper.
    """
    root = Path(vault_root)
    for rel in _record_rels():
        record = _read_record_at(root, rel)
        if record is not None:
            return record
    return None


@dataclass(frozen=True, slots=True)
class Resolved:
    """A vault's layout, with the schema folders that its record leaves unused.

    `leftover_schema_folders` lists the schema folders, relative to the vault root, other
    than the folder that holds the record. A plan reports them; they never change the layout.
    """

    layout: Layout
    record: LayoutRecord | None = None
    leftover_schema_folders: tuple[str, ...] = ()


def inspect(vault_root: Path) -> Resolved:
    """The layout of the vault at `vault_root`, and what the folders add to it.

    A record decides, and a second schema folder beside it is reported, never refused. Without
    a record, a schema folder only inside the managed root (or none at all) means `nested`, as
    for every vault that exists today. A schema folder at the vault root without a record, or
    in both places, is refused: the folders alone cannot say which folder is the managed root.
    """
    root = Path(vault_root)
    present = tuple(folder for folder in _schema_folders() if (root / folder).is_dir())
    record = read_record(root)
    if record is not None:
        holder = record.path.rpartition("/")[0]
        return Resolved(
            record.layout, record, tuple(folder for folder in present if folder != holder)
        )
    root_folder, wrapper_folder = _schema_folders()
    root_record, wrapper_record = _record_rels()
    if root_folder in present and wrapper_folder in present:
        raise LayoutError(
            "LAYOUT_AMBIGUOUS",
            f"Both {root_folder}/ and {wrapper_folder}/ exist and the vault has no layout record.",
            f"Write the record for the layout the pages are in: {wrapper_record} with "
            f"`layout: nested` when the pages sit under {wrapper_folder.rpartition('/')[0]}/, or "
            f"{root_record} with `layout: root` when they sit at the vault root. A record that "
            "names the wrong layout hides every page.",
        )
    if root_folder in present:
        raise LayoutError(
            "LAYOUT_RECORD_MISSING",
            f"{root_folder}/ sits at the vault root and the vault has no layout record.",
            f"Write {root_record} with `layout: root`: a schema folder only at the vault root "
            "means the vault root is the managed root.",
        )
    return Resolved(Layout.NESTED)


def resolve(vault_root: Path) -> Layout:
    """The layout of the vault at `vault_root`; see `inspect` for the rules and for what else they find."""
    return inspect(vault_root).layout


# --------------------------------------------------------------------------- #
# Areas, homes and path selectors
# --------------------------------------------------------------------------- #


def _compiled_folder(folder: str, layout: Layout) -> str:
    """A compiled folder under `layout`; `folder` is read with or without its `Notes/` parent."""
    parts = PurePosixPath(folder).parts
    below = parts[1:] if parts and parts[0] == note_types.NOTES_FOLDER else parts
    return "/".join(below if layout is Layout.ROOT else (note_types.NOTES_FOLDER, *below))


def area_folder(note_type: note_types.NoteType, layout: Layout | str) -> str | None:
    """The folder a note type's pages sit in under `layout`, relative to the managed root.

    None when the type declares no folder. A compiled type's folder is an area below the
    `Notes/` parent in `nested` and a top-level folder in `root`. Another role keeps its folder.
    """
    if note_type.folder is None:
        return None
    if not note_types.compiled(note_type):
        return note_type.folder
    return _compiled_folder(note_type.folder, Layout(layout))


def home_folder(home: homes.Home, layout: Layout | str) -> str:
    """The folder of a home under `layout`, relative to the managed root."""
    return _compiled_folder(home.folder, Layout(layout))


def _area_name(folder: str) -> str:
    """The first folder below the `Notes/` parent of a compiled folder, or "" when there is none."""
    parts = PurePosixPath(_compiled_folder(folder, Layout.ROOT)).parts
    return parts[0] if parts else ""


@dataclass(eq=False)
class Registry:
    """What the layout reads from the vault's registries, loaded once for a plan.

    `areas` holds the exact spelling of the first folder below `Notes/` for every compiled
    note type and every home. `findings` names each registry that could not be read in full:
    the areas and types of an unreadable registry are then missing, and the plan says so.
    """

    basis: note_types.Basis
    areas: frozenset[str]
    findings: tuple[str, ...] = ()
    _folded: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._folded = frozenset(name.casefold() for name in self.areas)

    def has_area(self, name: str, *, exact: bool = False) -> bool:
        """Whether `name` is a registered area; `exact` compares the spelling, else letter case is ignored."""
        return name in self.areas if exact else name.casefold() in self._folded

    def note_type(self, value: object) -> note_types.NoteType | None:
        """The registered type that a page's authored `type:` value names, or None."""
        from . import activation  # imported here so activation can use this module later

        return self.basis.resolve(activation.normalized_page_type(value)).note_type


def load_registry(vault_root: Path) -> Registry:
    """Read the note-type and home registries of the vault, as its owner."""
    root = Path(vault_root)
    basis = note_types.Basis(root, owner_local=True)
    compiled = [basis.resolve(key).note_type for key in basis.keys(note_types.compiled)]
    areas = {
        _area_name(note_type.folder) for note_type in compiled if note_type and note_type.folder
    }
    findings: list[str] = []
    if basis.dependency[0] != "effective":
        findings.append("note_types_overlay_unavailable")
    home_set = homes.load(root)
    if home_set.findings:
        findings.append("homes_overlay_invalid")
    areas.update(_area_name(home.folder) for home in home_set.homes)
    areas.discard("")
    return Registry(basis, frozenset(areas), tuple(findings))


def translate_path(value: str, target: Layout | str, registry: Registry) -> str:
    """An authored path selector, read in the `target` layout.

    In `root`, a leading managed-root segment drops, and `Notes/<Area>/...` reads as
    `<Area>/...` for a registered area or home. A value whose area slot is a glob, or names
    no registered area, stays as written: the member set of the selector is the proof of a
    translation, not the text. In `nested` the value is returned as it is. Spellings compare
    exactly, because a selector matches letter case exactly.
    """
    if Layout(target) is Layout.NESTED:
        return value
    inside = kbdir.managed_split(value)
    rest = value if inside is None else inside
    head, separator, tail = rest.partition("/")
    area, _, _ = tail.partition("/")
    if separator and head == note_types.NOTES_FOLDER and registry.has_area(area, exact=True):
        return tail
    return rest


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

# A date prefix is a field format, not a meaning: four digits, a month and optionally a day.
_DAY_PREFIX = re.compile(r"(\d{4})-(\d{2})-(\d{2})-", re.ASCII)
_MONTH_PREFIX = re.compile(r"(\d{4})-(\d{2})-", re.ASCII)


def date_prefix(stem: str) -> str:
    """The `YYYY-MM-DD-` or `YYYY-MM-` that starts `stem` when it parses as a date, else ""."""
    for pattern in (_DAY_PREFIX, _MONTH_PREFIX):
        found = pattern.match(stem)
        if found is None:
            continue
        year, month, *day = (int(part) for part in found.groups())
        try:
            date(year, month, day[0] if day else 1)
        except ValueError:
            continue
        return found.group(0)
    return ""


# --------------------------------------------------------------------------- #
# The per-page mapping
# --------------------------------------------------------------------------- #


class NameClass(StrEnum):
    """Whether a page's file name follows its title (derived) or stays as it is (fixed)."""

    DERIVED = "derived"
    FIXED = "fixed"


class FixedBecause(StrEnum):
    """Why a page keeps its name. The closed reasons `place` can give, for a plan to count."""

    NOT_MARKDOWN = "not_markdown"
    MACHINERY = "machinery_folder"
    NAVIGATION = "navigation_page"
    COLLECTION = "collection_page"
    UNTYPED = "no_registered_type"
    ROLE_KEEPS_NAME = "role_keeps_name"
    OUTSIDE_AREA = "outside_registered_area"
    NO_TITLE = "no_frontmatter_title"
    NO_USABLE_NAME = "no_usable_name"
    EXTRACTION_PART = "extraction_part"
    # The writer, the access kernel and the backfills bind a page named `<file>.md` to the file beside it by that name.
    FILE_SIDECAR = "sidecar_of_a_file"


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a single page of a `nested` vault goes in `root`, before names are made unique.

    `stem` is the corrected title name of a derived page and the current stem of a fixed page.
    `holds_name` says whether the page takes part in vault-wide name uniqueness: a Markdown
    page outside the machinery folders.
    """

    current_path: str
    new_dir: str
    name_class: NameClass
    stem: str
    extension: str
    date_prefix: str = ""
    holds_name: bool = True
    corrections: tuple[str, ...] = ()
    fixed_because: FixedBecause | None = None
    #: The `<X>` of a `Notes/<X>` folder that names no registered area: its pages keep the `Notes/` level.
    unregistered_folder: str | None = None


def _area_below_notes(folders: list[str], registry: Registry) -> tuple[bool, str | None]:
    """Whether the folders lead into a registered area below `Notes/`, and else the unregistered `Notes/<X>` they enter."""
    if len(folders) < 2 or folders[0].casefold() != note_types.NOTES_FOLDER.casefold():
        return False, None
    return (True, None) if registry.has_area(folders[1]) else (False, folders[1])


def in_collection_layer(managed_relative: str) -> bool:
    """Whether a path below the managed root lies in a collection layer, the folders that the collection store owns."""
    head, separator, _rest = managed_relative.partition("/")
    return bool(separator) and vault_name_key(head) in {
        vault_name_key(profile.placement_layer) for profile in collection_profiles.PROFILES.values()
    }


def place(
    current_path: str,
    *,
    type_value: object,
    title: object,
    registry: Registry,
    files: AbstractSet[str] = frozenset(),
) -> Placement | None:
    """Where the page at `current_path` goes in `root`, or None when it is outside the managed root.

    `type_value` and `title` are the page's frontmatter values, or None for a file with no
    frontmatter. The folder follows a prefix rewrite: the managed root drops, and `Notes/`
    drops before a registered area. A `Notes/<X>` folder with no registered area keeps its
    level and is named in `unregistered_folder`. The name class follows the page's note type,
    and the first rule that keeps a name wins. `files` holds the path of every file the plan
    lists, so that a page named after the file beside it is found by that file's existence.
    """
    from . import semantic_contract
    from .vault_store import ownership

    below = kbdir.managed_split(current_path)
    if not below:
        return None
    *folders, filename = below.split("/")
    in_area, unregistered = _area_below_notes(folders, registry)
    new_dir = "/".join(folders[1:] if in_area else folders)
    markdown = ownership.is_markdown(filename)
    stem, extension = (
        (filename[: -len(".md")], filename[-len(".md") :]) if markdown else (filename, "")
    )
    machinery = vault.in_excluded_scan_dir(current_path)

    def keep(because: FixedBecause) -> Placement:
        return Placement(
            current_path,
            new_dir,
            NameClass.FIXED,
            stem,
            extension,
            # A part page is named by its position and shares that name with the parts of every other page.
            holds_name=markdown and not machinery and because is not FixedBecause.EXTRACTION_PART,
            fixed_because=because,
            unregistered_folder=unregistered,
        )

    if not markdown:
        return keep(FixedBecause.NOT_MARKDOWN)
    if machinery:
        return keep(FixedBecause.MACHINERY)
    if extraction_parts.source_page_for(PurePosixPath(current_path)) is not None:
        return keep(FixedBecause.EXTRACTION_PART)
    if current_path[: -len(extension)] in files:
        return keep(FixedBecause.FILE_SIDECAR)
    # The write gate's own list of generated navigation names; a bare Markdown name matches no folder.
    if semantic_contract.compiled_route_exempt(filename):
        return keep(FixedBecause.NAVIGATION)
    if in_collection_layer(below):
        return keep(FixedBecause.COLLECTION)
    note_type = registry.note_type(type_value)
    if note_type is None:
        return keep(FixedBecause.UNTYPED)
    if not note_types.titled_name(note_type):
        return keep(FixedBecause.ROLE_KEEPS_NAME)
    if note_types.compiled(note_type) and not in_area:
        return keep(FixedBecause.OUTSIDE_AREA)
    if not isinstance(title, str) or not title.strip():
        return keep(FixedBecause.NO_TITLE)
    corrected = corrected_title_name(title)
    if not corrected.name:
        return keep(FixedBecause.NO_USABLE_NAME)
    # A source is named by its capture date, and the pack declares no stem for it yet.
    dated = (
        note_type.stem == "dated"
        or note_type.stem == "started"
        or note_types.captured_source(note_type)
    )
    return Placement(
        current_path,
        new_dir,
        NameClass.DERIVED,
        corrected.name,
        extension,
        date_prefix=date_prefix(stem) if dated else "",
        corrections=corrected.corrections,
        unregistered_folder=unregistered,
    )


# --------------------------------------------------------------------------- #
# Vault-wide uniqueness
# --------------------------------------------------------------------------- #

UNMAPPABLE_SAME_PATH = "same_new_path"
UNMAPPABLE_NO_NAME_FITS = "no_name_fits"


@dataclass(frozen=True, slots=True)
class Allocation:
    """The final path of a page in `root`. `number` is 1 for the plain name, else the suffix ` (<number>)`."""

    current_path: str
    new_path: str
    number: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class Unmappable:
    """A page that cannot be given its own new path, with the other pages that want its path."""

    current_path: str
    reason: str
    also: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Allocations:
    """The outcome of `allocate`: placed pages and unmappable pages, each sorted by current path."""

    placed: tuple[Allocation, ...]
    unmappable: tuple[Unmappable, ...]


def _join(new_dir: str, filename: str) -> str:
    return f"{new_dir}/{filename}" if new_dir else filename


def allocate(placements: Iterable[Placement]) -> Allocations:
    """Give every page a final path, with derived names unique across the vault.

    Names compare under `vault_name_key`. Pages that hold a name, fixed and derived alike,
    share a single namespace. A fixed name never changes, so a derived name that equals it takes a
    suffix whatever the order. Among derived names that collide, the first by current path
    keeps the plain name, and each later page takes the lowest ` (n)` that no page holds, from
    ` (2)`. A page that keeps its plain name is never pushed off it by another page's suffix.
    Two pages that would take the same new path, and a derived page whose name leaves no room
    under the byte cap, are unmappable. The result depends only on the input set.
    """
    ordered = sorted(placements, key=lambda placement: placement.current_path)
    taken = {
        vault_name_key(placement.stem)
        for placement in ordered
        if placement.name_class is NameClass.FIXED and placement.holds_name
    }
    chosen: dict[str, Allocation] = {}
    unmappable: list[Unmappable] = []
    later: list[tuple[Placement, str]] = []
    plain_names: set[str] = set()
    for placement in ordered:
        if placement.name_class is NameClass.FIXED:
            chosen[placement.current_path] = Allocation(
                placement.current_path,
                _join(placement.new_dir, placement.stem + placement.extension),
                1,
                False,
            )
            continue
        fitted = fit_filename(
            placement.stem, date_prefix=placement.date_prefix, extension=placement.extension
        )
        if fitted is None:
            unmappable.append(Unmappable(placement.current_path, UNMAPPABLE_NO_NAME_FITS))
            continue
        key = vault_name_key(fitted[0])
        if key in taken or key in plain_names:
            later.append((placement, key))
            continue
        plain_names.add(key)
        chosen[placement.current_path] = Allocation(
            placement.current_path,
            _join(placement.new_dir, fitted[0] + placement.extension),
            1,
            fitted[1],
        )
    # Every plain name is held before any suffix is chosen, so a suffix never takes a name that a page keeps.
    taken |= plain_names
    next_number: dict[str, int] = {}
    for placement, key in later:
        number = next_number.get(key, 2)
        while True:
            fitted = fit_filename(
                placement.stem,
                date_prefix=placement.date_prefix,
                number=number,
                extension=placement.extension,
            )
            if fitted is None:
                unmappable.append(Unmappable(placement.current_path, UNMAPPABLE_NO_NAME_FITS))
                break
            if vault_name_key(fitted[0]) not in taken:
                taken.add(vault_name_key(fitted[0]))
                next_number[key] = number + 1
                chosen[placement.current_path] = Allocation(
                    placement.current_path,
                    _join(placement.new_dir, fitted[0] + placement.extension),
                    number,
                    fitted[1],
                )
                break
            number += 1
    by_path: dict[str, list[str]] = {}
    for allocation in chosen.values():
        by_path.setdefault(vault_name_key(allocation.new_path), []).append(allocation.current_path)
    for group in by_path.values():
        if len(group) > 1:
            unmappable.extend(
                Unmappable(
                    path,
                    UNMAPPABLE_SAME_PATH,
                    tuple(sorted(other for other in group if other != path)),
                )
                for path in group
            )
    clashing = {item.current_path for item in unmappable}
    return Allocations(
        tuple(chosen[path] for path in sorted(chosen) if path not in clashing),
        tuple(sorted(unmappable, key=lambda item: item.current_path)),
    )


def final_paths(placements: Iterable[Placement], allocations: Allocations) -> dict[str, str]:
    """The new path of every placed file. A part page follows its source page's new folder, never its own."""
    moved = {allocation.current_path: allocation.new_path for allocation in allocations.placed}
    for placement in placements:
        if placement.fixed_because is not FixedBecause.EXTRACTION_PART:
            continue
        if placement.current_path not in moved:
            continue
        source = extraction_parts.source_page_for(PurePosixPath(placement.current_path))
        new_source = moved.get(source.as_posix()) if source is not None else None
        if new_source is not None:
            folder = extraction_parts.folder_for(PurePosixPath(new_source))
            moved[placement.current_path] = (
                folder / PurePosixPath(placement.current_path).name
            ).as_posix()
    return moved
