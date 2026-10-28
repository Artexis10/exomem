"""Task templates: a seeded vault, a user prompt and an end-state key, from one parameter set.

A template is four pure functions of a case's JSON parameters. ``draw`` picks
the parameters from a seed, ``seed_steps`` lists the product calls that build
the case's vault, ``prompt`` is the one user turn the session sees, and ``key``
is the answer. The key comes from the parameters alone, never from the
product's output, and the session never receives it.

Templates name Records and Planning product commands only as the identities
that a key expects (``target_ops``), not as tool spellings: scoring resolves
what the session called through an injected resolver (see ``resolve``).
"""

from __future__ import annotations

import datetime as dt
import json
import random
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

POOLS: Mapping[str, Any] = json.loads((Path(__file__).parent / "pools.json").read_text("utf-8"))

READ, WRITE = "read", "write"
RECORDS, PLANNING = "records", "planning"

#: The product commands a key expects a session to reach. These are the grouped
#: command identities the resolver returns, not any rendering's tool name.
RECORD_COMMAND, PLAN_COMMAND = "record_memory", "plan_memory"

PLAN_PATH = "Knowledge Base/Planning/Work/_collection.md"
_BASE_DATE = dt.date(2026, 3, 1)
_ANSWER_TAIL = ' Reply with only a JSON object of the form {{"answer": {shape}}} and nothing else.'
_NAMESPACE = uuid.UUID("5d0a8e5e-0c0b-4f79-9f0e-2a6b0f6f4d11")


# --------------------------------------------------------------------------
# Keys.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Op:
    """A product operation a key accepts as the right one: ``(command, action, mode)``."""

    command: str
    action: str
    mode: str | None = None
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileKey:
    """Authored facts for one requested page; generated identities remain fixture guards."""

    path: str
    body: str | None = None
    units: tuple[Mapping[str, Any], ...] | None = None


@dataclass(frozen=True)
class CollectionKey:
    """The exact rows a collection must hold at the end.

    Each row asserts the fields it names. Rows match actual items by the
    collection's natural key, so item identity never depends on a generated id.
    """

    manifest: str
    natural_key: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ManifestKey:
    """A collection the session had to create: its profile, title, key and fields."""

    manifest: str
    profile: str
    title: str
    natural_key: tuple[str, ...]
    #: Field name to the manifest field types that satisfy the request.
    fields: Mapping[str, tuple[str, ...]]
    required: tuple[str, ...]


@dataclass(frozen=True)
class Key:
    kind: str
    target_ops: tuple[Op, ...]
    collections: tuple[CollectionKey, ...] = ()
    manifests: tuple[ManifestKey, ...] = ()
    #: Reads only: the expected JSON answer and how it is compared.
    answer: Any = None
    answer_mode: str | None = None
    files: tuple[FileKey, ...] = ()
    preference: Mapping[str, Any] | None = None
    question: Mapping[str, Any] | None = None
    registry: Mapping[str, Any] | None = None
    preview: Mapping[str, Any] | None = None
    adoption: Mapping[str, Any] | None = None


# --------------------------------------------------------------------------
# Seed steps.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedStep:
    """One product call that builds a case's vault.

    ``match`` names the existing item an ``update`` or ``triage`` targets by
    field values; the seeder reads its id, version and the container guard
    from the collection, as a caller must.
    """

    command: str
    action: str
    kwargs: Mapping[str, Any]
    match: Mapping[str, Any] | None = None
    #: Invented original bytes materialized before an adoption scan, outside the managed knowledge base.
    originals: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Template:
    template_id: str
    kind: str
    profile: str
    draw: Callable[[random.Random], dict]
    seed_steps: Callable[[dict], list[SeedStep]]
    prompt: Callable[[dict], str]
    key: Callable[[dict], Key]
    #: What the template's group exercises, for the report only.
    summary: str = field(default="")


def _frontmatter(document: Mapping[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True) + "---\n"


def _manifest_text(
    *, title: str, profile: str, source: str, natural_key: Sequence[str], fields: Mapping[str, Any]
) -> str:
    collection_id = uuid.uuid5(_NAMESPACE, f"{profile}/{title}")
    return _frontmatter(
        {
            "type": "collection",
            "exomem_id": str(collection_id),
            "title": title,
            "semantic_profile": profile,
            "collection_version": 1,
            "schema_version": 1,
            "lifecycle": "active",
            "storage": {"strategy": "markdown-items", "source": source, "format_version": 1},
            "item_schema": {"natural_key": list(natural_key), "fields": dict(fields)},
        }
    )


def records_manifest(tracker: Mapping[str, str]) -> str:
    return _manifest_text(
        title=tracker["title"],
        profile=RECORDS,
        source="Events",
        natural_key=["occurred_on"],
        fields={
            "occurred_on": {"type": "date", "required": True},
            tracker["measure"]: {"type": "number"},
            "note": {"type": "string"},
        },
    )


def planning_manifest() -> str:
    return _manifest_text(
        title="Planning work",
        profile=PLANNING,
        source="Items",
        natural_key=["title"],
        fields={
            name: ({"type": "string", "required": True} if name == "title" else {"type": "string"})
            for name in (
                "title",
                "kind",
                "status",
                "lifecycle",
                "priority",
                "commitment",
                "horizon",
                "health",
                "area",
                "parent",
            )
        },
    )


def records_path(tracker: Mapping[str, str]) -> str:
    return f"Knowledge Base/Records/{tracker['dir']}/_collection.md"


def _date(offset: int) -> str:
    return (_BASE_DATE + dt.timedelta(days=offset)).isoformat()


# --------------------------------------------------------------------------
# Records templates.
# --------------------------------------------------------------------------


def _tracker_rows(rng: random.Random, count: int, span: int) -> list[dict]:
    offsets = sorted(rng.sample(range(span), count))
    return [
        {"date": _date(offset), "value": rng.randint(1, 9), "note": rng.choice(POOLS["notes"])}
        for offset in offsets
    ]


def _records_seed(params: dict) -> list[SeedStep]:
    tracker = params["tracker"]
    path = records_path(tracker)
    steps = [
        SeedStep(
            RECORD_COMMAND,
            "create",
            {"manifest_path": path, "manifest_text": records_manifest(tracker)},
        )
    ]
    for row in params["rows"]:
        steps.append(
            SeedStep(RECORD_COMMAND, "append", {"collection": path, "item": _record_item(tracker, row)})
        )
    return steps


def _record_item(tracker: Mapping[str, str], row: Mapping[str, Any]) -> dict:
    return {"occurred_on": row["date"], tracker["measure"]: row["value"], "note": row["note"]}


def _records_collection_key(params: dict, rows: Sequence[Mapping[str, Any]]) -> CollectionKey:
    tracker = params["tracker"]
    return CollectionKey(
        records_path(tracker),
        ("occurred_on",),
        tuple(_record_item(tracker, row) for row in rows),
    )


def _draw_log_entry(rng: random.Random) -> dict:
    tracker = rng.choice(POOLS["trackers"])
    rows = _tracker_rows(rng, 3, 20)
    last = (dt.date.fromisoformat(rows[-1]["date"]) - _BASE_DATE).days
    target = {
        "date": _date(last + rng.randint(1, 3)),
        "value": rng.randint(1, 9),
        "note": rng.choice(POOLS["notes"]),
    }
    return {"tracker": tracker, "rows": rows, "target": target}


def _log_entry_prompt(params: dict) -> str:
    tracker, target = params["tracker"], params["target"]
    return (
        f"Add this to my {tracker['title']}: on {target['date']} I logged {target['value']} "
        f"{tracker['measure']}, with the note \"{target['note']}\"."
    )


def _log_entry_key(params: dict) -> Key:
    return Key(
        WRITE,
        (Op(RECORD_COMMAND, "append"),),
        (_records_collection_key(params, [*params["rows"], params["target"]]),),
    )


def _draw_update_row(rng: random.Random) -> dict:
    tracker = rng.choice(POOLS["trackers"])
    rows = _tracker_rows(rng, 4, 20)
    index = rng.randrange(len(rows))
    new = rng.choice([value for value in range(1, 10) if value != rows[index]["value"]])
    return {"tracker": tracker, "rows": rows, "index": index, "new_value": new}


def _update_row_prompt(params: dict) -> str:
    tracker, row = params["tracker"], params["rows"][params["index"]]
    return (
        f"In my {tracker['title']}, the entry for {row['date']} is wrong: it should say "
        f"{params['new_value']} {tracker['measure']}, not {row['value']}. Fix it."
    )


def _update_row_key(params: dict) -> Key:
    rows = [dict(row) for row in params["rows"]]
    rows[params["index"]]["value"] = params["new_value"]
    return Key(
        WRITE, (Op(RECORD_COMMAND, "update"),), (_records_collection_key(params, rows),)
    )


def _draw_range_total(rng: random.Random) -> dict:
    tracker = rng.choice(POOLS["trackers"])
    rows = _tracker_rows(rng, 7, 14)
    first = rng.randrange(0, 3)
    last = first + rng.randint(2, 4)
    return {
        "tracker": tracker,
        "rows": rows,
        "from_date": rows[first]["date"],
        "to_date": rows[last]["date"],
    }


def _range_total_prompt(params: dict) -> str:
    tracker = params["tracker"]
    return (
        f"In my {tracker['title']}, what is the total {tracker['measure']} logged from "
        f"{params['from_date']} to {params['to_date']}, both days included?"
        + _ANSWER_TAIL.format(shape="<number>")
    )


def _range_total_key(params: dict) -> Key:
    total = sum(
        row["value"]
        for row in params["rows"]
        if params["from_date"] <= row["date"] <= params["to_date"]
    )
    return Key(READ, (Op(RECORD_COMMAND, "query"),), answer=total, answer_mode="number")


def _draw_create_collection(rng: random.Random) -> dict:
    return {"collection": rng.choice(POOLS["new_collections"])}


def _create_collection_prompt(params: dict) -> str:
    spec = params["collection"]
    kind = "whole-number" if spec["number_kind"] == "whole" else "decimal-number"
    return (
        f"Create a new Records collection called \"{spec['title']}\" for tracking {spec['topic']}. "
        f"Each entry has a required date field named `{spec['date_field']}` that identifies the "
        f"entry, a {kind} field named `{spec['number_field']}`, and a short text field named "
        f"`{spec['text_field']}`. Put its manifest at `Knowledge Base/Records/{spec['dir']}/_collection.md`."
    )


def _create_collection_key(params: dict) -> Key:
    spec = params["collection"]
    number = ("integer",) if spec["number_kind"] == "whole" else ("number",)
    return Key(
        WRITE,
        (Op(RECORD_COMMAND, "create"),),
        manifests=(
            ManifestKey(
                f"Knowledge Base/Records/{spec['dir']}/_collection.md",
                RECORDS,
                spec["title"],
                (spec["date_field"],),
                {spec["date_field"]: ("date",), spec["number_field"]: number, spec["text_field"]: ("string",)},
                (spec["date_field"],),
            ),
        ),
    )


def _create_collection_seed(params: dict) -> list[SeedStep]:
    # The vault starts with no collection at all; the session creates the one.
    return []


# --------------------------------------------------------------------------
# Planning templates.
# --------------------------------------------------------------------------


def _plan_titles(rng: random.Random, count: int) -> list[str]:
    combos = [f"{verb} {thing}" for verb in POOLS["plan_verbs"] for thing in POOLS["plan_objects"]]
    return rng.sample(combos, count)


def _plan_state(rng: random.Random, state: Mapping[str, Any]) -> dict:
    return {
        "status": state["status"],
        "commitment": state["commitment"],
        "horizon": rng.choice(state["horizons"]),
    }


def _plan_item(item: Mapping[str, Any]) -> dict:
    # Every seeded item is an outcome: a committed active work item needs a parent.
    return {
        "title": item["title"],
        "kind": "outcome",
        "status": item["status"],
        "commitment": item["commitment"],
        "horizon": item["horizon"],
        "priority": item["priority"],
    }


def _plan_seed(params: dict) -> list[SeedStep]:
    steps = [
        SeedStep(
            PLAN_COMMAND,
            "create",
            {"manifest_path": PLAN_PATH, "manifest_text": planning_manifest()},
        )
    ]
    for item in params["items"]:
        steps.append(SeedStep(PLAN_COMMAND, "add", {"collection": PLAN_PATH, "item": _plan_item(item)}))
    return steps


def _plan_collection_key(rows: Sequence[Mapping[str, Any]]) -> CollectionKey:
    return CollectionKey(PLAN_PATH, ("title",), tuple(rows))


def _draw_plan_items(rng: random.Random, count: int) -> list[dict]:
    titles = _plan_titles(rng, count)
    return [
        {
            "title": title,
            **_plan_state(rng, rng.choice(POOLS["plan_states"])),
            "priority": rng.choice(POOLS["priorities"]),
        }
        for title in titles
    ]


def _draw_plan_add(rng: random.Random) -> dict:
    items = _draw_plan_items(rng, 4)
    return {"items": items[:3], "new_title": items[3]["title"], "new_priority": rng.choice(POOLS["priorities"])}


def _plan_add_prompt(params: dict) -> str:
    return (
        f"Add a new plan item called \"{params['new_title']}\" to my plan, "
        f"with {params['new_priority']} priority."
    )


def _plan_add_key(params: dict) -> Key:
    rows = [_plan_item(item) for item in params["items"]]
    rows.append({"title": params["new_title"], "priority": params["new_priority"]})
    return Key(WRITE, (Op(PLAN_COMMAND, "add"),), (_plan_collection_key(rows),))


def _planned_state(rng: random.Random) -> dict:
    planned = next(state for state in POOLS["plan_states"] if state["status"] == "planned")
    return {
        "status": planned["status"],
        "commitment": planned["commitment"],
        "horizon": rng.choice([h for h in planned["horizons"] if h != "week"]),
    }


def _draw_plan_triage(rng: random.Random) -> dict:
    items = _draw_plan_items(rng, 4)
    items[3] = {**items[3], **_planned_state(rng)}
    return {"items": items, "target": 3, "horizon": rng.choice(["week", "month"])}


def _plan_triage_prompt(params: dict) -> str:
    target = params["items"][params["target"]]
    return (
        f"I have committed to \"{target['title']}\": make it active for this {params['horizon']}."
    )


def _plan_triage_key(params: dict) -> Key:
    rows = [_plan_item(item) for item in params["items"]]
    rows[params["target"]] = {
        **rows[params["target"]],
        "status": "active",
        "commitment": "committed",
        "horizon": params["horizon"],
    }
    return Key(
        WRITE, (Op(PLAN_COMMAND, "triage"), Op(PLAN_COMMAND, "update")), (_plan_collection_key(rows),)
    )


def _state_named(status: str) -> Mapping[str, Any]:
    return next(state for state in POOLS["plan_states"] if state["status"] == status)


def _draw_committed_week(rng: random.Random) -> dict:
    titles = _plan_titles(rng, 7)
    hits = rng.randint(2, 3)
    committed = [_state_named("active"), _state_named("blocked")]
    items: list[dict] = []
    for index, title in enumerate(titles):
        if index < hits:
            state = {**committed[index % 2], "horizon": "week"}
        elif index == hits:
            state = {**_state_named("active"), "horizon": rng.choice(["month", "quarter"])}
        elif index == hits + 1:
            state = {**_state_named("planned"), "horizon": "week"}
        else:
            state = {**_state_named("candidate"), "horizon": "inbox"}
        items.append({"title": title, **{k: state[k] for k in ("status", "commitment", "horizon")}, "priority": rng.choice(POOLS["priorities"])})
    rng.shuffle(items)
    return {"items": items}


def _committed_week_prompt(params: dict) -> str:
    return (
        "Which of my plan items are committed to this week (commitment committed, horizon week)? "
        "List their titles." + _ANSWER_TAIL.format(shape='["<title>", ...]')
    )


def _committed_week_key(params: dict) -> Key:
    titles = [
        item["title"]
        for item in params["items"]
        if item["commitment"] == "committed" and item["horizon"] == "week"
    ]
    return Key(READ, (Op(PLAN_COMMAND, "query"),), answer=titles, answer_mode="set")


def _draw_plan_history(rng: random.Random) -> dict:
    *items, base = _draw_plan_items(rng, 4)
    if rng.random() < 0.5:
        prior = {**_state_named("candidate"), "horizon": "inbox"}
        after = {"status": "planned", "commitment": "considering", "horizon": "month"}
    else:
        prior = {**_state_named("planned"), "horizon": rng.choice(["month", "quarter"])}
        after = {"status": "active", "commitment": "committed", "horizon": "week"}
    target = {**base, **{k: prior[k] for k in ("status", "commitment", "horizon")}}
    return {
        "items": items,
        "target": target,
        "after": after,
        "asked_field": rng.choice(["status", "horizon"]),
    }


def _plan_history_seed(params: dict) -> list[SeedStep]:
    steps = _plan_seed({"items": [*params["items"], params["target"]]})
    steps.append(
        SeedStep(
            PLAN_COMMAND,
            "triage",
            {"collection": PLAN_PATH, "transition": dict(params["after"])},
            match={"title": params["target"]["title"]},
        )
    )
    return steps


def _plan_history_prompt(params: dict) -> str:
    title, asked = params["target"]["title"], params["asked_field"]
    return (
        f"Before \"{title}\" was last triaged, what was its {asked}?"
        + _ANSWER_TAIL.format(shape=f'"<{asked}>"')
    )


def _plan_history_key(params: dict) -> Key:
    return Key(
        READ,
        (Op(PLAN_COMMAND, "history"),),
        answer=params["target"][params["asked_field"]],
        answer_mode="text",
    )


TEMPLATES: Mapping[str, Template] = {
    template.template_id: template
    for template in (
        Template(
            "records-log-entry", WRITE, RECORDS, _draw_log_entry, _records_seed,
            _log_entry_prompt, _log_entry_key, "log a new entry in an existing tracker",
        ),
        Template(
            "records-update-row", WRITE, RECORDS, _draw_update_row, _records_seed,
            _update_row_prompt, _update_row_key, "correct one existing row",
        ),
        Template(
            "records-range-total", READ, RECORDS, _draw_range_total, _records_seed,
            _range_total_prompt, _range_total_key, "answer a question about recent rows from a query",
        ),
        Template(
            "records-create-collection", WRITE, RECORDS, _draw_create_collection,
            _create_collection_seed, _create_collection_prompt, _create_collection_key,
            "create a collection from a description",
        ),
        Template(
            "plan-add-item", WRITE, PLANNING, _draw_plan_add, _plan_seed, _plan_add_prompt,
            _plan_add_key, "add a plan item",
        ),
        Template(
            "plan-triage-item", WRITE, PLANNING, _draw_plan_triage, _plan_seed,
            _plan_triage_prompt, _plan_triage_key, "commit to and activate a planned item",
        ),
        Template(
            "plan-committed-week", READ, PLANNING, _draw_committed_week, _plan_seed,
            _committed_week_prompt, _committed_week_key, "read-only plan query: what is committed this week",
        ),
        Template(
            "plan-item-history", READ, PLANNING, _draw_plan_history, _plan_history_seed,
            _plan_history_prompt, _plan_history_key, "history of one plan item",
        ),
    )
}
