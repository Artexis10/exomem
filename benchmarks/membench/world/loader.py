"""Load the phenomenon catalogue, refuse what it cannot hold, and list what is buildable.

The catalogue is YAML under ``catalogue/``. ``attributes.yaml`` declares the closed sets that code
implements; every other file is an open registry. ``check_catalogue`` collects every refusal, each
naming the offending item, and raises them together. On a clean catalogue it returns the entries
with their family defaults applied, and says which are buildable and what the others lack.

Code here branches on closed values and schema field names only: never on a phenomenon, family,
behaviour, norm, hedge or statement kind. The few names below are field names of the schema or
closed values that this module implements.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CATALOGUE_DIR = Path(__file__).resolve().parent / "catalogue"

# Closed values that this module implements. `PIPELINE` is the `truth` marker that the computed
# key class carries; `SILENT` and `STATED` are the implicitness level and the norm basis that
# the silent-level rule reads.
PIPELINE = "pipeline"
SILENT = "silent"
STATED = "stated"

# Schema field names. An entry may carry only these fields; a family's defaults the first five.
_ENTRY_FIELDS = (
    "id",
    "stage",
    "says",
    "from",
    "implicit",
    "twin_of",
    "truth",
    "key",
    "norms",
    "probes",
    "expect",
)
_INHERITED = ("truth", "key", "norms", "probes", "expect")

# The arguments whose value comes from a closed set or an open registry. Every other argument of a
# step, a role constraint or a reading is a reference to a role or a step id, or to a slot.
_CLOSED_ARGUMENT = {
    "basis": "statement_bases",
    "mode": "defeat_modes",
    "shape": "rule_shapes",
    "volatility": "volatility_classes",
}
_REGISTRY_ARGUMENT = {"kind": "statement_kinds", "hedge": "hedges"}
_SLOT_ARGUMENT = ("at", "until", "when")
_KEY_FIELD_AT = "at"


@dataclass(frozen=True)
class Refusal:
    code: str
    item: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.item}: {self.detail}"


class CatalogueRefused(Exception):
    """The catalogue holds at least one refusal; every one is in ``refusals``."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


@dataclass(frozen=True)
class Entry:
    id: str
    family: str
    stage: str
    implicit: tuple[str, ...]
    truth: tuple[str, ...]
    key: str
    norms: tuple[str, ...]
    probes: tuple[str, ...]
    expect: tuple[str, ...]
    twin_of: str | None


@dataclass(frozen=True)
class Catalogue:
    entries: dict[str, Entry]
    recipes: dict[str, Any]
    buildable: tuple[str, ...]
    lacking: dict[str, tuple[str, ...]]  # each non-buildable entry, with what it lacks


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that records a mapping key given twice, which plain YAML silently overwrites."""

    def __init__(self, stream: str):
        super().__init__(stream)
        self.duplicates: list[str] = []

    def construct_mapping(self, node, deep=False):  # noqa: ANN001
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                self.duplicates.append(str(key))
            seen.add(key)
        return super().construct_mapping(node, deep)


def _read(path: Path, duplicates: list[Refusal]) -> dict[str, Any]:
    loader = _StrictLoader(path.read_text(encoding="utf-8"))
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    duplicates.extend(
        Refusal("duplicate-id", key, f"given twice in {path.name}") for key in loader.duplicates
    )
    return data


def load_catalogue(
    directory: Path = CATALOGUE_DIR, implemented: Mapping[str, Collection[str]] | None = None
) -> Catalogue:
    """Read ``directory`` and check it. Raises ``CatalogueRefused`` on any refusal."""
    refusals: list[Refusal] = []
    raw: dict[str, Any] = {"attributes": _read(directory / "attributes.yaml", refusals)}
    for key, name, field in (
        ("behaviours", "behaviours", "behaviours"),
        ("hedges", "hedges", "hedges"),
        ("lineage", "lineage", "lineage"),
        ("norms", "norms", "norms"),
        ("statement_kinds", "statement-kinds", "statement_kinds"),
        ("recipes", "recipes", "recipes"),
    ):
        raw[key] = _read(directory / f"{name}.yaml", refusals)[field]
    families = _read(directory / "families.yaml", refusals)
    raw["families"], raw["realms"] = families["families"], families["realms"]
    raw["phenomena"] = [
        entry
        for path in sorted((directory / "phenomena").glob("*.yaml"))
        for entry in _read(path, refusals)["phenomena"]
    ]
    return check_catalogue(raw, implemented, prior=refusals)


def check_catalogue(
    raw: Mapping[str, Any],
    implemented: Mapping[str, Collection[str]] | None = None,
    prior: list[Refusal] | None = None,
) -> Catalogue:
    """Check a catalogue read into one mapping, and compute what is buildable.

    ``implemented`` maps a closed set of ``attributes.yaml`` to the members that the generator
    implements. A set that it omits counts as fully implemented, until the generator says otherwise.
    """
    checker = _Checker(raw, list(prior or []))
    return checker.run(implemented or {})


def _names(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


class _Checker:
    def __init__(self, raw: Mapping[str, Any], refusals: list[Refusal]):
        self.raw = raw
        self.attrs: Mapping[str, Any] = raw["attributes"]
        self.refusals = refusals
        self.entries: dict[str, Entry] = {}
        self.used: dict[
            str, set[tuple[str, str]]
        ] = {}  # per entry or recipe: closed values it names

    # -- refusals and value checks -------------------------------------------------------------

    def refuse(self, code: str, item: str, detail: str) -> None:
        refusal = Refusal(code, item, detail)
        if refusal not in self.refusals:
            self.refusals.append(refusal)

    def closed(
        self, item: str, field: str, value: Any, set_name: str, used: set | None = None
    ) -> None:
        if isinstance(value, str) and value in (self.attrs.get(set_name) or {}):
            if used is not None:
                used.add((set_name, value))
        else:
            self.refuse(
                "undeclared-value", item, f"{field} `{value}` is not declared in {set_name}"
            )

    def registry(self, item: str, field: str, value: Any, registry: str) -> None:
        if not (isinstance(value, str) and value in (self.raw.get(registry) or {})):
            self.refuse(
                "unknown-reference", item, f"{field} `{value}` is not in the {registry} registry"
            )

    def check_fields(self, item: str, fields: Mapping[str, Any], used: set) -> None:
        """Check the values that an entry or a family default names."""
        for value in _names(fields.get("truth")):
            self.closed(item, "truth", value, "truth_mechanisms", used)
        if "key" in fields:
            self.closed(item, "key", fields["key"], "key_classes", used)
        for value in _names(fields.get("implicit")):
            self.closed(item, "implicit", value, "implicitness_levels", used)
        for value in _names(fields.get("probes")):
            self.closed(item, "probes", value, "probe_forms", used)
        if "stage" in fields:
            self.closed(item, "stage", fields["stage"], "stages")
        for value in _names(fields.get("norms")):
            self.registry(item, "norms", value, "norms")
        for value in _names(fields.get("expect")):
            self.registry(item, "expect", value, "behaviours")
        for value in _names(fields.get("from")):
            self.registry(item, "from", value, "lineage")
        for field in ("truth", "norms", "probes", "expect", "implicit", "from"):
            if field in fields and not isinstance(fields[field], list):
                self.refuse("undeclared-value", item, f"{field} must be a list")

    # -- the run -------------------------------------------------------------------------------

    def run(self, implemented: Mapping[str, Collection[str]]) -> Catalogue:
        self.check_schema()
        families = self.raw["families"]
        for name, family in families.items():
            used: set = set()
            self.registry(f"family {name}", "realm", family.get("realm"), "realms")
            self.check_fields(
                f"family {name}",
                {**family.get("defaults", {}), "from": family.get("from", [])},
                used,
            )
        for entry in self.raw["phenomena"]:
            self.check_entry(entry, families)
        for entry in self.entries.values():
            if entry.twin_of is not None and entry.twin_of not in self.entries:
                self.refuse(
                    "unknown-reference",
                    f"phenomenon {entry.id}",
                    f"twin_of `{entry.twin_of}` is no phenomenon",
                )
        for recipe_id, recipe in self.raw["recipes"].items():
            self.check_recipe(recipe_id, recipe)
        if self.refusals:
            raise CatalogueRefused(self.refusals)
        return self.list_buildable(implemented)

    def check_schema(self) -> None:
        for name in _names(self.attrs.get("pipeline_order")):
            self.closed("pipeline_order", "mechanism", name, "truth_mechanisms")
        for name, cls in (self.attrs.get("key_classes") or {}).items():
            rule = cls.get("truth")
            if rule != PIPELINE:
                self.closed(f"key class {name}", "truth", rule, "truth_mechanisms")
        for name, kind in self.raw["statement_kinds"].items():
            self.closed(
                f"statement kind {name}", "commitment", kind.get("commitment"), "commitments"
            )
        for name, norm in self.raw["norms"].items():
            self.closed(f"norm {name}", "basis", norm.get("basis"), "norm_bases")
        for name, behaviour in self.raw["behaviours"].items():
            self.closed(
                f"behaviour {name}", "polarity", behaviour.get("polarity"), "behaviour_polarities"
            )

    # -- entries -------------------------------------------------------------------------------

    def check_entry(self, entry: Mapping[str, Any], families: Mapping[str, Any]) -> None:
        eid = entry.get("id")
        item = f"phenomenon {eid}"
        for field in entry:
            if field not in _ENTRY_FIELDS:
                self.refuse("undeclared-value", item, f"field `{field}` is not an entry field")
        if eid in self.entries:
            self.refuse("duplicate-id", item, "the id is used by an earlier entry")
            return
        family_name = str(eid).split(".")[0]
        family = families.get(family_name)
        if family is None:
            self.refuse(
                "unknown-reference", item, f"family `{family_name}` is not in the families registry"
            )
            return
        used: set = set()
        self.check_fields(item, entry, used)
        merged = {**family.get("defaults", {}), **{k: entry[k] for k in _INHERITED if k in entry}}
        resolved = Entry(
            id=eid,
            family=family_name,
            stage=entry.get("stage"),
            implicit=tuple(entry.get("implicit", [])),
            truth=tuple(merged.get("truth", [])),
            key=merged.get("key"),
            norms=tuple(merged.get("norms", [])),
            probes=tuple(merged.get("probes", [])),
            expect=tuple(merged.get("expect", [])),
            twin_of=entry.get("twin_of"),
        )
        self.entries[eid] = resolved
        self.used[eid] = used | {("truth_mechanisms", m) for m in resolved.truth}
        self.check_key_class(item, resolved)

    def check_key_class(self, item: str, entry: Entry) -> None:
        key_class = (self.attrs.get("key_classes") or {}).get(entry.key)
        if key_class is None:
            return  # already refused as an undeclared value
        rule = key_class.get("truth")
        pipeline = _names(self.attrs.get("pipeline_order"))
        if rule == PIPELINE:
            fits = bool(entry.truth) and all(m in pipeline for m in entry.truth)
            admits = "mechanisms of pipeline_order"
        else:
            fits = list(entry.truth) == [rule]
            admits = f"`{rule}` alone"
        if not fits:
            self.refuse(
                "key-class-contradicts-mechanisms",
                item,
                f"key class `{entry.key}` admits {admits}, and the entry names {list(entry.truth)}",
            )
        stated = any((self.raw["norms"].get(n) or {}).get("basis") == STATED for n in entry.norms)
        if rule == PIPELINE and SILENT in entry.implicit and not stated:
            self.refuse(
                "silent-without-stated-norm",
                item,
                "a computed entry at the silent level names no stated norm",
            )

    # -- recipes -------------------------------------------------------------------------------

    def check_recipe(self, recipe_id: str, recipe: Mapping[str, Any]) -> None:
        item = f"recipe {recipe_id}"
        entry = self.entries.get(recipe_id)
        if entry is None:
            self.refuse("unknown-reference", item, "no phenomenon has this id")
            return
        used = self.used.setdefault(f"recipe {recipe_id}", set())
        roles = self.check_roles(item, recipe.get("needs") or {}, used)
        slots = _names(recipe.get("slots"))
        ids = self.check_steps(item, recipe.get("steps") or [], roles, slots, used)
        scope = (item, roles, slots, ids, used)
        # A key of the recipe is read in both worlds of a thread: the planted one and the mutation one.
        self.check_keys(
            scope, recipe.get("keys") or {}, entry.probes, entry.truth, ("planted", "mutation"), ""
        )
        twin = recipe.get("twin")
        if twin is not None:
            self.closed(item, "twin world", twin.get("world"), "step_worlds")
            twin_world = twin.get("world")
            worlds = (
                ("planted", "mutation")
                if twin_world == "both"
                else tuple(w for w in ("planted", "mutation") if w == twin_world)
            )
            twins = [t for t in self.entries.values() if t.twin_of == recipe_id] or [entry]
            for twin_entry in twins:
                probes = (
                    twin_entry.probes if twin_entry is not entry else tuple(twin.get("keys") or {})
                )
                self.check_keys(
                    scope,
                    twin.get("keys") or {},
                    probes,
                    twin_entry.truth,
                    worlds,
                    "twin-",
                    twin_entry.id,
                )

    def check_roles(self, item: str, needs: Mapping[str, Any], used: set) -> set[str]:
        roles = set(needs)
        for name, need in needs.items():
            self.closed(item, f"role {name} kind", need.get("is"), "role_kinds", used)
            for constraint, value in need.items():
                if constraint == "is":
                    continue
                self.closed(item, f"role {name} constraint", constraint, "role_constraints")
                if constraint in _CLOSED_ARGUMENT:
                    self.closed(
                        item, f"role {name} {constraint}", value, _CLOSED_ARGUMENT[constraint]
                    )
                elif value not in roles:
                    self.refuse(
                        "unknown-reference",
                        item,
                        f"role {name} constraint {constraint} names `{value}`, which is no role",
                    )
        return roles

    def check_steps(
        self,
        item: str,
        steps: list[Mapping[str, Any]],
        roles: set[str],
        slots: list[str],
        used: set,
    ) -> dict[str, set[str]]:
        """Check each step. Returns, for each world, the step ids that exist there."""
        ids: dict[str, set[str]] = {"planted": set(), "mutation": set()}
        every_id = {s["id"] for s in steps if "id" in s}
        for index, step in enumerate(steps):
            op = step.get("op")
            label = (
                f"{item}, step `{step['id']}`"
                if "id" in step
                else f"{item}, step {index + 1} ({op})"
            )
            world = step.get("world", "both")
            self.closed(label, "world", world, "step_worlds")
            if "id" in step:
                for w in ("planted", "mutation") if world == "both" else (world,):
                    if w in ids and step["id"] in ids[w]:
                        self.refuse(
                            "duplicate-id", label, f"the step id is used twice in the {w} world"
                        )
                    elif w in ids:
                        ids[w].add(step["id"])
            declared = (self.attrs.get("world_operations") or {}).get(op)
            self.closed(label, "operation", op, "world_operations", used)
            if declared is None:
                continue
            for arg, value in step.items():
                if arg in ("op", "id", "world"):
                    continue
                if arg not in declared.get("args", []):
                    self.refuse(
                        "undeclared-value",
                        label,
                        f"argument `{arg}` is not declared for operation `{op}`",
                    )
                elif arg in _CLOSED_ARGUMENT:
                    self.closed(label, arg, value, _CLOSED_ARGUMENT[arg], used)
                elif arg in _REGISTRY_ARGUMENT:
                    self.registry(label, arg, value, _REGISTRY_ARGUMENT[arg])
                else:
                    self.check_references(label, arg, value, roles | every_id, slots)
        return ids

    def check_references(
        self, item: str, arg: str, value: Any, known: set[str], slots: list[str]
    ) -> None:
        pool, what = (set(slots), "slot") if arg in _SLOT_ARGUMENT else (known, "role or step id")
        for ref in value if isinstance(value, list) else [value]:
            if not (isinstance(ref, str) and ref in pool):
                self.refuse("unknown-reference", item, f"{arg} `{ref}` is no {what} of the recipe")

    # -- keys ----------------------------------------------------------------------------------

    def check_keys(
        self,
        scope: tuple,
        keys: Mapping[str, Any],
        declared_forms: tuple[str, ...],
        mechanisms: tuple[str, ...],
        worlds: tuple[str, ...],
        prefix: str,
        owner: str | None = None,
    ) -> None:
        item, roles, slots, ids, used = scope
        owner = owner or item.removeprefix("recipe ")
        for form in declared_forms:
            if form not in keys:
                self.refuse(
                    f"{prefix}form-unkeyed",
                    owner,
                    f"the recipe leaves the probe form `{form}` unkeyed",
                )
        for form, key in keys.items():
            if form not in declared_forms:
                self.refuse(
                    f"{prefix}form-undeclared",
                    owner,
                    f"the recipe keys the probe form `{form}`, which the entry does not declare",
                )
            self.closed(item, "key form", form, "probe_forms", used)
            label = f"{item}, key `{form}`"
            for part, value in key.items():
                if part == _KEY_FIELD_AT:
                    self.check_references(label, "at", value, set(), slots)
                    continue
                self.closed(label, "key part", part, "key_parts")
                for reading in value if isinstance(value, list) else [value]:
                    self.check_reading(label, reading, scope, mechanisms, worlds)

    def check_reading(
        self,
        item: str,
        reading: Any,
        scope: tuple,
        mechanisms: tuple[str, ...],
        worlds: tuple[str, ...],
    ) -> None:
        _, roles, slots, ids, used = scope
        if not isinstance(reading, dict):
            self.refuse("undeclared-value", item, "a key part must be a reading or a role")
            return
        if "role" in reading:
            if reading["role"] not in roles:
                self.refuse(
                    "unknown-reference", item, f"role `{reading['role']}` is no role of the recipe"
                )
            return
        mechanism, _, name = str(reading.get("read")).partition(".")
        readings = ((self.attrs.get("truth_mechanisms") or {}).get(mechanism) or {}).get(
            "readings"
        ) or {}
        if name not in readings:
            self.refuse(
                "undeclared-value", item, f"reading `{reading.get('read')}` is not declared"
            )
        else:
            used.add(("truth_mechanisms", mechanism))
            if mechanism not in mechanisms:
                self.refuse(
                    "reading-not-named",
                    item,
                    f"reading `{reading['read']}` is of a mechanism that the phenomenon does not name",
                )
        every_id = ids["planted"] | ids["mutation"]
        for arg, value in reading.items():
            if arg == "read":
                continue
            self.closed(item, "reading argument", arg, "reading_arguments")
            self.check_references(item, arg, value, roles | every_id, slots)
            for ref in value if isinstance(value, list) else [value]:
                if ref in every_id:
                    for world in worlds:
                        if ref not in ids[world]:
                            self.refuse(
                                "step-absent-in-world",
                                item,
                                f"reads step `{ref}`, which is absent in the {world} world",
                            )

    # -- what is buildable ---------------------------------------------------------------------

    def list_buildable(self, implemented: Mapping[str, Collection[str]]) -> Catalogue:
        recipes = self.raw["recipes"]
        buildable: list[str] = []
        lacking: dict[str, tuple[str, ...]] = {}
        for entry in self.entries.values():
            lacks: list[str] = []
            base = entry.twin_of or entry.id
            recipe = recipes.get(base)
            if recipe is None:
                lacks.append(
                    "no recipe" if base == entry.id else f"its base `{base}` has no recipe"
                )
            elif entry.twin_of and "twin" not in recipe:
                lacks.append(f"its base `{base}` has no twin part")
            if recipe is not None and not lacks:
                names = self.used.get(entry.id, set()) | self.used.get(f"recipe {base}", set())
                lacks += [
                    f"the generator does not implement {set_name} `{value}`"
                    for set_name, value in sorted(names)
                    if set_name in implemented and value not in implemented[set_name]
                ]
            if lacks:
                lacking[entry.id] = tuple(lacks)
            else:
                buildable.append(entry.id)
        return Catalogue(dict(self.entries), dict(recipes), tuple(buildable), lacking)


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else CATALOGUE_DIR
    try:
        catalogue = load_catalogue(directory)
    except CatalogueRefused as refused:
        print(f"refused: {len(refused.refusals)}")
        print(refused)
        return 1
    print(f"buildable: {len(catalogue.buildable)} of {len(catalogue.entries)}")
    for entry_id in catalogue.buildable:
        print(f"  {entry_id}")
    print(f"not buildable: {len(catalogue.lacking)}")
    for entry_id, lacks in catalogue.lacking.items():
        print(f"  {entry_id}: {'; '.join(lacks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
