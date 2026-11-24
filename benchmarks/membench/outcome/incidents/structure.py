"""The checks of agent-managed structure: what a restructure left in the vault and what the user was told.

Three checks read a unit whose session may have reshaped a collection (``__main__.py`` wires them in):

- ``restructure``: the shape history of the named collections (or of the collections the session made, with
  ``new``) holds an entry with each expected origin, step kind and undo, and each undoable entry has its undo call.
  With ``none`` it holds no entry with steps that the seed did not leave, and the collections keep their title, key,
  fields and lifecycle. ``automatic_steps_among`` says every automatic entry holds only steps of those kinds.
- ``rows_preserved``: every value that the seed put in a collection is readable where the history says its rows
  went, and the collections that hold them hold ``equals`` rows together. A rename carries a value to its new field
  name. A merge moves rows to its target under the step's ``mapping`` ({source field: target field}; a field that it
  does not name keeps its name). An entry that a later entry undid moves nothing. ``added`` names values that the
  session's own rows must hold, ``corrected`` edits the expected seed by an answer the user gave, and ``retired``
  counts the named collections that are retired and still read by their path.
- ``plain_reply``: no reply meant for the user contains a path form of the vault (its root, and each collection's
  path and directory). A blind judge then grades whether the replies name the user's things in their terms with no
  internal names, how many questions the session asked, and whether each was about the user's things.

The history, the rows and the path forms are structural facts that the harness reads from the arm. What counts as
an internal name is the judge's decision and never a pattern in this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from exomem import restructure as product

#: The facts of a collection that "keeps its shape" compares, as the arm's own manifest reports them.
SHAPE_FACTS = ("title", "natural_key", "fields", "lifecycle")
RESTRUCTURE_KEYS = frozenset({"collections", "new", "entries", "none", "automatic_steps_among"})
ENTRY_KEYS = frozenset({"origin", "step", "undone"})
ROWS_PRESERVED_KEYS = frozenset({"equals", "added", "corrected", "retired"})
PLAIN_REPLY_KEYS = frozenset({"questions"})
QUESTION_BOUNDS = frozenset({"equals", "at_most"})

PLAIN_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "names_the_users_things_in_their_terms": {
            "type": "boolean",
            "description": "True if every reply that says what the assistant keeps for the user, or what changed for them, says it in the user's own words and in terms of their things. False if a reply says it in terms of how the assistant stores things.",
        },
        "no_internal_names": {
            "type": "boolean",
            "description": "True if no reply names the assistant's own machinery: how things are stored or organised (collections, tables, schemas, fields, keys, identifiers, files, paths, tools, restructuring steps). The user's own words for their things are fine even when they sound technical. False if any reply names such machinery.",
        },
        "questions_asked": {
            "type": "integer",
            "description": "How many questions the assistant put to the user to get a decision or a fact from them, over all the replies together. An offer that waits for a yes or no counts. A rhetorical question, an explanation, and an answer to the user's own question do not. Zero when it asked none.",
        },
        "every_question_about_the_users_things": {
            "type": "boolean",
            "description": "True if each question the assistant asked is about the user's own things (what they keep, what they want, which value is right), and none is about how the assistant stores them. True when it asked no question.",
        },
        "reason": {"type": "string", "description": "One short paragraph that cites the replies."},
    },
    "required": [
        "names_the_users_things_in_their_terms",
        "no_internal_names",
        "questions_asked",
        "every_question_about_the_users_things",
        "reason",
    ],
    "additionalProperties": False,
}
#: The verdict fields that must be true; the question count is compared with the case's bound.
PLAIN_REPLY_REQUIRE = (
    "names_the_users_things_in_their_terms",
    "no_internal_names",
    "every_question_about_the_users_things",
)
PLAIN_REPLY_PROMPT = (
    "You grade what an assistant said to a user. The user is not technical: they talk about their own things "
    "(their lists, plants, services, quotes, books) in their own words, and the assistant keeps those things for "
    "them. Read the user's turns and the assistant's replies, then fill the schema. Grade the replies only, not "
    "what was saved.\n\nUser turns: {turn}\n\nAssistant replies:\n<<<\n{answer}\n>>>"
)


# --- the case's rules -------------------------------------------------------------------------------------------


def validate(case: dict) -> None:
    """Refuse a structure rule that could pass on nothing or never be read, before any session starts."""
    rule, seeded = case["pass"], case.get("seed_collections", ())
    paths = {collection["manifest_path"] for collection in seeded}
    if "restructure" in rule:
        validate_restructure(case, paths)
    if "rows_preserved" in rule:
        validate_rows_preserved(case, seeded, paths)
    if "plain_reply" in rule:
        validate_plain_reply(case)
    if "step_order" in rule:
        validate_step_order(case)
    # The store's collections are made in a process that serves the store, so the case needs the unit's coordinator.
    if "store_collections" in case and (
        case["store_collections"] is not True
        or not case.get("store")
        or not seeded
        or any(not isinstance(c["manifest"], dict) for c in seeded)
    ):
        raise SystemExit(
            f"case {case['id']}: `store_collections` is true, of a `store` case whose "
            "`seed_collections` give their manifests as mappings"
        )


def validate_restructure(case: dict, paths: set[str]) -> None:
    spec = case["pass"]["restructure"]
    name = f"case {case['id']}: restructure"
    if not isinstance(spec, dict) or set(spec) - RESTRUCTURE_KEYS:
        raise SystemExit(f"{name} takes {sorted(RESTRUCTURE_KEYS)}")
    named = spec.get("collections")
    if ("new" in spec) == ("collections" in spec) or spec.get("new", True) is not True:
        raise SystemExit(
            f"{name} names `collections` (seeded paths) or `new: true`, not both or neither"
        )
    # A path the seed does not create reads nothing, so "no change" would hold of a collection that is not there.
    if named is not None and (not isinstance(named, list) or not named or set(named) - paths):
        raise SystemExit(
            f"{name} `collections` is a non-empty list of paths that `seed_collections` creates"
        )
    if ("none" in spec) == ("entries" in spec):
        raise SystemExit(f"{name} gives `entries` or `none: true`, not both or neither")
    if "none" in spec and (
        spec["none"] is not True or "new" in spec or "automatic_steps_among" in spec
    ):
        raise SystemExit(
            f"{name} `none` is true, reads seeded `collections` and takes no step kinds"
        )
    entries = spec.get("entries", [])
    if not isinstance(entries, list) or ("entries" in spec and not entries):
        raise SystemExit(f"{name} `entries` is a non-empty list")
    for entry in entries:
        # The closed sets are the product's own (its operation schema fixes them), so no second list can drift.
        if (
            not isinstance(entry, dict)
            or set(entry) - ENTRY_KEYS
            or entry.get("origin") not in product.ORIGINS
            or ("step" in entry and entry["step"] not in product.STEP_PARAMETERS)
            or ("undone" in entry and entry["undone"] is not True)
        ):
            raise SystemExit(
                f"{name} entries hold an `origin` among {product.ORIGINS}, a `step` among "
                f"{sorted(product.STEP_PARAMETERS)} and `undone: true`"
            )
    kinds = spec.get("automatic_steps_among")
    if kinds is not None and (
        not isinstance(kinds, list) or not kinds or set(kinds) - set(product.STEP_PARAMETERS)
    ):
        raise SystemExit(
            f"{name} `automatic_steps_among` is step kinds of {sorted(product.STEP_PARAMETERS)}"
        )


def validate_rows_preserved(case: dict, seeded: Sequence[dict], paths: set[str]) -> None:
    spec = case["pass"]["rows_preserved"]
    name = f"case {case['id']}: rows_preserved"
    if (
        not isinstance(spec, dict)
        or set(spec) - ROWS_PRESERVED_KEYS
        or type(spec.get("equals")) is not int
    ):
        raise SystemExit(
            f"{name} takes {sorted(ROWS_PRESERVED_KEYS)} and needs an integer `equals`"
        )
    # The check follows the seed's rows by the identity its manifest gives, so there must be rows and an identity.
    if not any(c.get("rows") for c in seeded) or any(
        not isinstance(c["manifest"], dict) or "exomem_id" not in c["manifest"] for c in seeded
    ):
        raise SystemExit(
            f"{name} reads seeded rows, and each seeded manifest is a mapping with an `exomem_id`"
        )
    if spec["equals"] < 1:
        raise SystemExit(f"{name} `equals` is an integer, 1 or more")
    for values in spec.get("added", ()):
        if not isinstance(values, list) or not values:
            raise SystemExit(
                f"{name} `added` is a list of non-empty value lists, one per row the session adds"
            )
    by_path = {c["manifest_path"]: c.get("rows", ()) for c in seeded}
    for fix in spec.get("corrected", ()):
        if not isinstance(fix, dict) or set(fix) != {"collection", "row", "field", "value"}:
            raise SystemExit(
                f"{name} `corrected` items hold a collection, a row, a field and a value"
            )
        # The row that an answer corrects must be one seeded row, or the correction would edit nothing or many.
        if (
            not isinstance(fix["row"], dict)
            or len(matching(by_path.get(fix["collection"], ()), fix["row"])) != 1
        ):
            raise SystemExit(
                f"{name} `corrected` row {fix['row']} matches one seeded row of {fix['collection']}"
            )
    retired = spec.get("retired")
    if retired is not None and (
        not isinstance(retired, dict)
        or set(retired) != {"collections", "equals"}
        or not isinstance(retired["collections"], list)
        or not retired["collections"]
        or set(retired["collections"]) - paths
        or type(retired["equals"]) is not int
        or retired["equals"] < 0
    ):
        raise SystemExit(f"{name} `retired` is {{collections: seeded paths, equals: count}}")


def validate_plain_reply(case: dict) -> None:
    spec = case["pass"]["plain_reply"]
    bound = spec.get("questions") if isinstance(spec, dict) else None
    if (
        not isinstance(spec, dict)
        or set(spec) - PLAIN_REPLY_KEYS
        or not isinstance(bound, dict)
        or len(bound) != 1
        or set(bound) - QUESTION_BOUNDS
        or any(type(count) is not int or count < 0 for count in bound.values())
    ):
        raise SystemExit(
            f"case {case['id']}: plain_reply takes `questions`, one of {{equals: n}} or {{at_most: n}}"
        )


def validate_step_order(case: dict) -> None:
    spec, turns = case["pass"]["step_order"], len(case.get("turns") or ())
    if (
        not isinstance(spec, dict)
        or not spec
        or set(spec) - set(product.STEP_PARAMETERS)
        or any(type(turn) is not int or not 1 < turn <= turns for turn in spec.values())
    ):
        raise SystemExit(
            f"case {case['id']}: step_order maps step kinds of {sorted(product.STEP_PARAMETERS)} to the turn, "
            f"from 2 to {turns}, before which no call may carry them"
        )


# --- step_order -------------------------------------------------------------------------------------------------


def steps_of(value: object) -> list[str]:
    """The step kinds in a tool call's structured input, wherever its route nests the steps.

    A door may carry its arguments as a JSON string, which is parsed, never matched.
    """
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, Mapping):
        own = [value["step"]] if isinstance(value.get("step"), str) else []
        return own + [step for nested in value.values() for step in steps_of(nested)]
    if isinstance(value, list):
        return [step for nested in value for step in steps_of(nested)]
    return []


def step_order(turn_calls: Sequence[Sequence[Mapping]], spec: Mapping[str, int]) -> bool:
    """No call carries a named step before the turn the case names (turns count from 1).

    An attempt counts, refused or not: the case asks the agent not to make the change until the user answers.
    A step that no call carries passes here; whether it happened is the ``restructure`` check's question.
    """
    return not any(
        step in spec and number < spec[step]
        for number, calls in enumerate(turn_calls, 1)
        for call in calls
        for step in steps_of(call.get("input"))
    )


# --- values -----------------------------------------------------------------------------------------------------


def as_number(value: object) -> Decimal | None:
    """The value as a number when it is one or reads as one; a retyped field keeps the number, not its spelling."""
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def same_value(held: object, wanted: object, *, fold: bool = False) -> bool:
    """Whether a stored value is the wanted one: numbers by value, anything else as trimmed text.

    A date or a retyped number reads back as its text, so text is how two spellings of one value compare. ``fold``
    ignores case, for a value that the session typed from the user's words.
    """
    if held is None or wanted is None:
        return held is wanted
    numbers = as_number(held), as_number(wanted)
    if None not in numbers:
        return numbers[0] == numbers[1]
    held, wanted = str(held).strip(), str(wanted).strip()
    return held.casefold() == wanted.casefold() if fold else held == wanted


def matching(rows: Sequence[Mapping], where: Mapping) -> list[Mapping]:
    """The rows that hold every value of ``where``."""
    return [row for row in rows if all(same_value(row.get(k), v) for k, v in where.items())]


# --- restructure ------------------------------------------------------------------------------------------------


def shape_facts(read: Mapping) -> dict:
    """What a collection held before the session: its entry ids and the facts that "keeps its shape" compares."""
    history = read["history"] or ()
    return {
        "entry_ids": [entry["id"] for entry in history],
        **{fact: read[fact] for fact in SHAPE_FACTS},
    }


def has_undo_call(entry: Mapping) -> bool:
    """The entry carries the call that undoes it, or is one that no undo reverses and so carries none.

    The arm builds the call from the entry's id on every read. A creation and a declined proposal changed nothing
    that an undo could reverse, so the product offers them no call (``product.undoable``).
    """
    if not product.undoable(entry):
        return "undo_call" not in entry
    call = entry.get("undo_call")
    return isinstance(call, dict) and call.get("undo") == entry["id"]


def entry_matches(entry: Mapping, history: Sequence[Mapping], want: Mapping) -> bool:
    return (
        entry["origin"] == want["origin"]
        and ("step" not in want or any(step.get("step") == want["step"] for step in entry["steps"]))
        and has_undo_call(entry)
        and (not want.get("undone") or any(other["undoes"] == entry["id"] for other in history))
    )


def restructure(reads: Sequence[Mapping], befores: Sequence[Mapping], spec: Mapping) -> bool | None:
    """The collections' shape history holds each expected entry, or holds none and the shapes are unchanged.

    ``reads`` are the arm's ``shape`` answers and ``befores`` the ``shape_facts`` of the same collections before the
    session (``{"entry_ids": []}`` for a collection that the session made). An entry counts only when the seed did
    not leave it. An arm whose inspection reports no shape history holds no entry, so an expected one is missing
    there. None when a collection could not be read: unknown, never unchanged.
    """
    if any("error" in read for read in reads):
        return None
    history = [entry for read in reads for entry in read["history"] or ()]
    fresh = [
        entry
        for read, before in zip(reads, befores, strict=True)
        for entry in read["history"] or ()
        if entry["id"] not in before["entry_ids"]
    ]
    if spec.get("none"):
        kept = all(
            read[fact] == before[fact]
            for read, before in zip(reads, befores, strict=True)
            for fact in SHAPE_FACTS
        )
        # A creation entry (no steps) records a collection the session made, not a change of shape.
        return not [entry for entry in fresh if entry["steps"]] and kept
    kinds = spec.get("automatic_steps_among")
    return all(
        any(entry_matches(entry, history, want) for entry in fresh) for want in spec["entries"]
    ) and (
        kinds is None
        or all(
            step.get("step") in kinds
            for entry in fresh
            if entry["origin"] == "automatic"
            for step in entry["steps"]
        )
    )


# --- rows_preserved ---------------------------------------------------------------------------------------------


def standing(history: Sequence[Mapping]) -> list[Mapping]:
    """The entries that still stand once every undo is applied, in the order given.

    An entry stands unless a standing entry undid it, so an undo that was itself undone brings the first entry back.
    An undo entry carries the inverse of what it undid, so it adds no step of its own to follow.
    """
    undoers: dict[str, list[Mapping]] = {}
    for entry in history:
        if entry["undoes"]:
            undoers.setdefault(entry["undoes"], []).append(entry)
    verdicts: dict[str, bool] = {}

    def stands(entry: Mapping) -> bool:
        if entry["id"] not in verdicts:
            verdicts[entry["id"]] = not any(stands(undo) for undo in undoers.get(entry["id"], ()))
        return verdicts[entry["id"]]

    return [entry for entry in history if not entry["undoes"] and stands(entry)]


def timeline(reads: Sequence[Mapping]) -> list[Mapping]:
    """Every standing entry of the given collections' histories once, oldest first.

    A read lists its history newest first, so it is reversed; the sort is stable, so entries of one second keep their order.
    """
    seen: dict[tuple[str, str], Mapping] = {}
    for read in reads:
        for entry in reversed(read["history"] or ()):
            seen.setdefault((entry["collection_id"], entry["id"]), entry)
    return sorted(standing(list(seen.values())), key=lambda entry: entry["at"])


def locate(
    collection_id: str, fields: Sequence[str], events: Sequence[Mapping], refs: Mapping[str, str]
) -> tuple[str, dict[str, str]] | None:
    """Where a seeded collection's rows are now, and what each original field is called there.

    It follows the recorded steps in order: a rename changes a field's name, and a merge moves the rows to the step's
    ``target`` under its ``mapping``. None when a merge names a target that no collection answers to: the rows went
    somewhere the harness cannot find, which is unknown and never a loss.
    """
    here, names = collection_id, {field: field for field in fields}
    for event in events:
        if event["collection_id"] != here:
            continue
        for step in event["steps"]:
            kind = step.get("step")
            if kind == "rename_field":
                names = {k: step["to"] if v == step["field"] else v for k, v in names.items()}
            elif kind == "merge_into":
                target = refs.get(step["target"])
                if target is None:
                    return None
                mapping = step.get("mapping") or {}
                names = {k: mapping.get(v, v) for k, v in names.items()}
                here = target
    return here, names


def expected_rows(
    seeded: Sequence[Mapping], corrections: Sequence[Mapping]
) -> dict[str, list[dict]]:
    """The seed's rows by collection path, edited by the answers the user gave."""
    expected = {c["manifest_path"]: [dict(row) for row in c.get("rows", ())] for c in seeded}
    for fix in corrections:
        (row,) = matching(expected[fix["collection"]], fix["row"])
        row[fix["field"]] = fix["value"]
    return expected


def rows_preserved(
    reads: Mapping[str, Mapping], seeded: Sequence[Mapping], refs: Mapping[str, str], spec: Mapping
) -> bool | None:
    """Every seeded value is readable where the history says it went, and the holders hold ``equals`` rows together.

    ``reads`` are the arm's ``shape`` answers by knowledge-base path and ``refs`` map each name that a merge step may
    use for a collection (``refs_of``) to its id. A holder that did not read, or a merge target that resolves to no
    collection, makes the check unknown.
    """
    by_id = {read["collection_id"]: read for read in reads.values() if "error" not in read}
    events = timeline(list(by_id.values()))
    holders: set[str] = set()
    for path, rows in expected_rows(seeded, spec.get("corrected", ())).items():
        source = next(c for c in seeded if c["manifest_path"] == path)["manifest"]["exomem_id"]
        found = locate(source, sorted({field for row in rows for field in row}), events, refs)
        if found is None or not isinstance(by_id.get(found[0], {}).get("rows"), list):
            return None
        holder, names = found
        holders.add(holder)
        for row in rows:
            if not any(
                all(
                    same_value(held.get(names[field]), value)
                    for field, value in row.items()
                    if value is not None
                )
                for held in by_id[holder]["rows"]
            ):
                return False
    held = [
        [row[field] for field in by_id[holder]["fields"] if field in row]
        for holder in holders
        for row in by_id[holder]["rows"]
    ]
    return (
        len(held) == spec["equals"]
        and all(
            any(row_holds(values, wanted) for values in held) for wanted in spec.get("added", ())
        )
        and (retired_hold(reads, spec["retired"]) if "retired" in spec else True)
    )


def row_holds(values: Sequence[object], wanted: Sequence[object]) -> bool:
    """Whether the row's values include each wanted value, as the session typed it from the user's words."""
    return all(any(same_value(value, one, fold=True) for value in values) for one in wanted)


def retired_hold(reads: Mapping[str, Mapping], spec: Mapping) -> bool | None:
    """Exactly ``equals`` of the named collections are retired, and each still reads by its path."""
    named = [reads.get(path, {"error": "not read"}) for path in spec["collections"]]
    if any("error" in read for read in named):
        return None
    return sum(read["lifecycle"] == "retired" for read in named) == spec["equals"]


def refs_of(reads: Mapping[str, Mapping]) -> dict[str, str]:
    """The names a merge step may call a collection (its id, title, or either path), each to its id.

    A name that two collections answer to is left out: it names no one collection, so a step that uses it stays unresolved.
    """
    names: dict[str, set[str]] = {}
    for kb_path, read in reads.items():
        if "error" not in read:
            for name in (read["collection_id"], read["title"], kb_path, read["path"]):
                names.setdefault(name, set()).add(read["collection_id"])
    return {name: next(iter(ids)) for name, ids in names.items() if len(ids) == 1}


# --- plain_reply ------------------------------------------------------------------------------------------------


def path_forms(vault: str, collections: Sequence[Mapping]) -> list[str]:
    """The strings that make a reply name the vault's own layout: its root, and each collection's paths and directories.

    A collection gives its vault path and its knowledge-base path, each with the directory that holds its manifest and
    items. The harness knows the vault and every path in it, so no word decides it.
    """
    forms = {vault}
    for found in collections:
        for path in (found["path"], found.get("kb_path")):
            if path:
                forms |= {path, PurePosixPath(path).parent.as_posix()}
    return sorted(form for form in forms if form not in ("", "."))


def leaked_paths(replies: Sequence[str], forms: Sequence[str]) -> list[str]:
    """The path forms that any reply contains."""
    return [form for form in forms if any(form in reply for reply in replies)]


def plain_reply(
    leaked: Sequence[str], verdict: Mapping | None, spec: Mapping
) -> tuple[bool | None, list[str]]:
    """The check and why it failed: a path in a reply fails it, then the judge's findings and the question count.

    A judge with no usable verdict leaves it unknown, unless a path already failed it.
    """
    problems = [f"a reply names the vault path {form!r}" for form in leaked]
    if verdict is None:
        return (False if leaked else None), problems
    problems += [
        f"the judge found `{field}` false"
        for field in PLAIN_REPLY_REQUIRE
        if verdict[field] is not True
    ]
    asked, (bound, limit) = verdict["questions_asked"], next(iter(spec["questions"].items()))
    if type(asked) is not int or (asked != limit if bound == "equals" else asked > limit):
        problems.append(
            f"the session asked {asked} questions, and the case allows {bound.replace('_', ' ')} {limit}"
        )
    return not problems, problems
