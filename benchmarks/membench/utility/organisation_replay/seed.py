"""Build a situation's seed through the arm's public MCP doors (SCHEMA.md section 1).

Three scripted sessions, each its own MCP session: the seed session writes the
established state, the settle session delivers the due-state carrier until no
seed promotion is left open, and the prior-agent session saves each
``recent_promotion_by_prior_agent_session`` entry and stays open for the
capture session. The seed is then read back from files and checked against
the specification by meaning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from membench.utility.switch_eval.arms import ArmSpec
from membench.utility.switch_eval.state import Slot

from .scripted import ScriptedSession
from .situation import ESTABLISHED, PRIOR_AGENT, Situation

SEED_WHY = "organisation replay seed"
#: The relation direction SCHEMA.md gives a relation entry that states none.
DEFAULT_DIRECTION = "directed"
#: The due-state category that carries recent promotions: a product protocol
#: token, the only rows the settle session has to deliver.
PROMOTION_CATEGORY = "recent_promotions"
#: Carrier calls the settle session may spend before the seed is a fault. The
#: carrier delivers five rows a call; a seed's promotions number in the tens.
SETTLE_CALL_LIMIT = 40
#: The product's closed label-resolution statuses for a heading written as a
#: governed kind: a core kind, or a kind the vault registered for the page type.
ACCEPTED_HEADING_STATUSES = ("core", "extension")
#: How a plain seed body's heading is chosen, recorded beside the wraps in every run.
WRAP_RULE = {
    "rule": "the first kind, in the semantic-language registry's declaration order, "
    "that the registry accepts for the note type",
    "limitation": "the note-type registry declares no default unit kind, so every note "
    "type that accepts the first declared kind gets the same heading",
}


class SeedFault(RuntimeError):
    """The seed could not be built or does not match its specification."""


@dataclass
class SeedBuild:
    """What the scripted sessions did, for the check and the run file."""

    relation_keys: dict[str, str] = field(default_factory=dict)
    home_keys: dict[str, str] = field(default_factory=dict)
    entities: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    prior_entries: list[dict] = field(default_factory=list)
    #: Each seed note whose plain body was wrapped: title, note type and heading.
    wraps: list[dict] = field(default_factory=list)
    #: Each refused call that a scripted session resent: session, command,
    #: refusal code and the wait the product asked for.
    retries: list[dict] = field(default_factory=list)
    settle_calls: int = 0
    sessions: dict[str, str] = field(default_factory=dict)


def home_key(folder: str) -> str:
    """The homes registry key for a home: the slug of its last folder segment.

    SCHEMA.md's home entry has no key; the slug is deterministic, so both arms
    save the same key.
    """

    from exomem.vault import slugify_title

    return slugify_title(folder.rstrip("/").rsplit("/", 1)[-1])


async def _content_hash(session: ScriptedSession, subject: str) -> str:
    answer = await session.call("schema_memory", {"operation": "inspect", "subject": subject})
    return answer["content_hash"]


async def _save_entry(session: ScriptedSession, entry: Mapping, build: SeedBuild) -> None:
    # SCHEMA.md fixes the four registry values, and each has its own door.
    registry = entry["registry"]
    key = entry["key"]
    if registry == "domains":
        await session.call(
            "schema_memory",
            {
                "operation": "save",
                "subject": "domains",
                "proposal": {"upsert": {key: {}}},
                "why": SEED_WHY,
                "expected_hash": await _content_hash(session, "domains"),
            },
        )
    elif registry == "entity-types":
        body: dict = {
            "label": entry.get("label") or key,
            "guidance": entry.get("guidance") or "",
            # The door requires a folder, which the schema does not carry and
            # which can never change; the label is the folder the product's
            # own suggestion derives from. Same value in both arms.
            "attributes": {"folder": entry.get("label") or key},
        }
        if entry.get("parent"):
            body["parent"] = entry["parent"]
        await session.call(
            "schema_memory",
            {
                "operation": "save",
                "subject": "entity-types",
                "proposal": {"upsert": {key: body}},
                "why": SEED_WHY,
                "expected_hash": await _content_hash(session, "entity-types"),
            },
        )
    elif registry == "relations":
        parent = entry.get("parent")
        proposal = await session.call(
            "schema_memory",
            {
                "operation": "propose-relation",
                "subject": "relations",
                "proposal": {
                    "requested_label": entry.get("label") or key,
                    # A parent that this seed declared is saved under its canonical key.
                    "parent": build.relation_keys.get(parent, parent),
                    "description": entry.get("description") or "",
                    "direction": entry.get("direction") or DEFAULT_DIRECTION,
                },
            },
        )
        upserts = (proposal.get("delta") or {}).get("upsert") or {}
        if len(upserts) != 1:
            raise SeedFault(f"relation {key}: the proposal named {sorted(upserts)}, not one key")
        await session.call(
            "schema_memory",
            {
                "operation": "save-relations",
                "subject": "relations",
                "proposal": proposal["delta"],
                "why": SEED_WHY,
                "expected_hash": proposal["expected_hash"],
            },
        )
        build.relation_keys[key] = next(iter(upserts))
    elif registry == "projects":
        # No door: a project key registers on its first use by a seed note
        # (SCHEMA.md "Known gaps" 3); the check confirms it registered.
        return
    else:
        raise SeedFault(f"vocabulary registry {registry!r} has no seed door")


def note_body(body: str, note_type: str, vault: Path) -> tuple[str, str | None]:
    """The body a seed note is written with, and the heading wrapped around it, if any.

    A body with a semantic unit is written as given. A plain body goes verbatim under
    one heading: the first kind, in the semantic-language registry's declaration order
    (core kinds, then the vault's own), that the registry accepts for the note type.
    Both arms make this choice by one rule, and the run file records it.

    The note-type registry schema declares no default unit kind (its attributes are
    role, folder, time_bounded and sources), so the declaration order always decides.
    """

    from exomem import semantic_language_registry, semantic_units

    registry = semantic_language_registry.load_registry(vault)

    def units(markdown: str) -> int:
        return len(
            semantic_units.parse_semantic_units(
                markdown, language_registry=registry, page_type=note_type
            ).units
        )

    if units(body):
        return body, None
    kind = next(
        (
            key
            for key in (*registry.core_kinds, *registry.kinds)
            if registry.resolve_heading(key, page_type=note_type).status
            in ACCEPTED_HEADING_STATUSES
        ),
        None,
    )
    if kind is None:
        raise SeedFault(f"the semantic-language registry accepts no heading for {note_type!r}")
    wrapped = f"## {kind}\n\n{body}"
    if units(wrapped) != 1:
        raise SeedFault(f"a {note_type!r} body wrapped under {kind!r} is not one semantic unit")
    return wrapped, kind


async def _create_note(
    session: ScriptedSession, note: Mapping, partition: str | None, body: str
) -> dict:
    arguments: dict = {
        "title": note["title"],
        "content": body,
        "note_type": note["type"],
    }
    if note.get("topics"):
        arguments["topics"] = list(note["topics"])
    # A project home places only a project-partition type, and only through the
    # singular project argument (SCHEMA.md); partition is the registry's closed
    # attribute. Any other type passes the case's projects as given.
    if partition == "project":
        if len(note.get("projects") or []) != 1:
            raise SeedFault(f"note {note['title']!r}: a project-partition note needs one project")
        arguments["project"] = note["projects"][0]
    elif note.get("projects"):
        arguments["projects"] = list(note["projects"])
    draft = await session.call("remember", {**arguments, "validate_only": True})
    committed = await session.call(
        "remember",
        {
            **arguments,
            "draft_id": draft.get("draft_id"),
            "draft_hash": draft.get("draft_hash"),
            "draft_token": draft.get("draft_token"),
            # Seed notes carry no typed relations (SCHEMA.md: edges are what the
            # sessions are measured on), so the review records that none applies.
            "relation_disposition": "reviewed_none",
            "relation_review_hash": draft.get("relation_review_hash"),
            "relation_review_reason": "seed note without typed relations",
        },
    )
    return committed


def _landed_path(answer: Mapping) -> str | None:
    for container in (
        answer,
        answer.get("result") if isinstance(answer.get("result"), dict) else {},
    ):
        for name in ("path", "destination"):
            value = container.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _kb_relative_folder(path: str) -> str:
    from exomem import kbdir

    relative = path.removeprefix(kbdir.kb_prefix())
    return relative.rsplit("/", 1)[0] if "/" in relative else ""


async def seed_session(
    session: ScriptedSession, situation: Situation, build: SeedBuild, vault: Path
) -> None:
    from exomem.vocabulary import registry_spec
    from exomem.vocabulary.registry import load

    vocabulary = situation.vocabulary(ESTABLISHED)
    # Step 1: domains first, then entity types and relations, in file order.
    for entry in sorted(vocabulary, key=lambda e: e["registry"] != "domains"):
        await _save_entry(session, entry, build)
    # Step 2: homes. Project keys need not exist yet (SCHEMA.md "Known gaps" 3).
    for home in situation.seed.get("homes") or []:
        key = home_key(home["folder"])
        build.home_keys[key] = home["folder"]
        await session.call(
            "schema_memory",
            {
                "operation": "save",
                "subject": "homes",
                "proposal": {
                    "upsert": {
                        key: {
                            "attributes": {
                                "folder": home["folder"],
                                "topics": list(home.get("topics") or []),
                                "projects": list(home.get("projects") or []),
                            }
                        }
                    }
                },
                "why": SEED_WHY,
                "expected_hash": await _content_hash(session, "homes"),
            },
        )
    # Step 3: entities.
    for entity in situation.seed.get("entities") or []:
        answer = await session.call(
            "connect_memory",
            {
                "operation": "create-entity",
                "entity_type": entity["type"],
                "name": entity["title"],
                "summary": entity["body"],
            },
        )
        build.entities.append(
            {"ref": entity["ref"], "title": entity["title"], "path": _landed_path(answer)}
        )
    # Step 4: notes, each checked against its expected folder.
    note_types = load(registry_spec("note-types"), vault).entries
    for note in situation.seed.get("notes") or []:
        entry = note_types.get(note["type"])
        partition = entry.attributes.get("partition") if entry is not None else None
        body, heading = note_body(note["body"], note["type"], vault)
        if heading is not None:
            build.wraps.append({"title": note["title"], "type": note["type"], "heading": heading})
        answer = await _create_note(session, note, partition, body)
        landed = _landed_path(answer)
        build.notes.append({"title": note["title"], "folder": note["folder"], "landed": landed})
        if landed is None or _kb_relative_folder(landed) != note["folder"].strip("/"):
            raise SeedFault(
                f"seed note {note['title']!r} landed at {landed!r}, expected {note['folder']!r}"
            )


def _due_promotions(answer: object) -> int:
    """Recent-promotion rows the carrier delivered in one answer."""

    containers = [answer]
    if isinstance(answer, dict) and isinstance(answer.get("result"), dict):
        containers.append(answer["result"])
    rows = 0
    for container in containers:
        due = container.get("due_state") if isinstance(container, dict) else None
        for row in (due or {}).get("top") or []:
            rows += row.get("category") == PROMOTION_CATEGORY
    return rows


async def settle_session(session: ScriptedSession, build: SeedBuild) -> None:
    """Deliver the due-state carrier until it has no recent promotion left to deliver."""

    for _ in range(SETTLE_CALL_LIMIT):
        answer = await session.call("ask_memory", {"query": "organisation replay settle"})
        build.settle_calls += 1
        if not _due_promotions(answer):
            return
    raise SeedFault(f"recent promotions still due after {SETTLE_CALL_LIMIT} carrier calls")


async def prior_session(session: ScriptedSession, situation: Situation, build: SeedBuild) -> None:
    for entry in situation.vocabulary(PRIOR_AGENT):
        await _save_entry(session, entry, build)
        build.prior_entries.append(
            {
                "registry": entry["registry"],
                "key": entry["key"],
                "saved_as": build.relation_keys.get(entry["key"], entry["key"])
                if entry["registry"] == "relations"
                else entry["key"],
            }
        )


def check_seed(situation: Situation, build: SeedBuild, facts: Mapping) -> list[str]:
    """Each way the read-back seed differs in meaning from its specification."""

    problems: list[str] = []
    registries = facts["registries"]
    for entry in situation.seed.get("vocabulary") or []:
        registry, key = entry["registry"], entry["key"]
        if registry == "domains" and key not in registries["domains"]:
            problems.append(f"domain {key} is not registered")
        elif registry == "projects" and key not in registries["projects"]:
            problems.append(f"project key {key} is not registered")
        elif registry == "entity-types":
            found = registries["entity_types"]["vault"].get(key)
            if found is None:
                problems.append(f"entity type {key} is not registered")
            elif (entry.get("parent") or None) != (found["parent_chain"][:1] or [None])[0]:
                problems.append(f"entity type {key} has parent chain {found['parent_chain']}")
        elif registry == "relations":
            saved = build.relation_keys.get(key)
            found = registries["relations"].get(saved) if saved else None
            parent = build.relation_keys.get(entry.get("parent"), entry.get("parent"))
            if found is None:
                problems.append(f"relation {key} is not registered (saved as {saved})")
            elif found["parent_chain"][:1] != [parent]:
                problems.append(f"relation {key} has parent chain {found['parent_chain']}")
    for key, folder in build.home_keys.items():
        home = registries["homes"].get(key)
        spec = next(h for h in situation.seed.get("homes") or [] if h["folder"] == folder)
        if home is None or (
            home["folder"],
            sorted(home["topics"]),
            sorted(home["projects"]),
        ) != (folder, sorted(spec.get("topics") or []), sorted(spec.get("projects") or [])):
            problems.append(f"home {folder} reads back as {home}")
    pages = {page["path"]: page for page in facts["pages"]}
    for entity in build.entities:
        page = pages.get(entity["path"] or "")
        if page is None or page["title"] != entity["title"]:
            problems.append(f"entity {entity['ref']} has no page at {entity['path']}")
    for note in build.notes:
        if note["landed"] not in pages:
            problems.append(f"note {note['title']!r} has no page at {note['landed']}")
    prior = {(e["registry"], e["saved_as"]) for e in build.prior_entries}
    open_prior = set()
    for promotion in facts["promotions"]:
        subjects = {
            (subject.get("registry"), subject.get("key"))
            for subject in promotion.get("subjects") or []
            if isinstance(subject, dict)
        }
        if subjects & prior:
            if promotion["state"] != "open":
                problems.append(f"prior-agent promotion {promotion['id']} is {promotion['state']}")
            open_prior |= subjects & prior
        elif promotion["state"] == "open":
            problems.append(f"seed promotion {promotion['id']} is still open")
    for missing in sorted(prior - open_prior):
        problems.append(f"prior-agent entry {missing} recorded no open promotion")
    return problems


def seed_snapshot(facts: Mapping, *, tree_digest: str, check: list[str]) -> dict:
    return {
        "tree_digest": tree_digest,
        "projection_digest": facts["projection_digest"],
        "registry_overlay_digests": facts["registries"]["overlay_digests"],
        "check": "pass" if not check else "fail",
        "check_problems": check,
        "settled_promotion_ids": sorted(
            p["id"] for p in facts["promotions"] if p["state"] != "open"
        ),
        "open_promotion_ids": sorted(p["id"] for p in facts["promotions"] if p["state"] == "open"),
    }


async def build_seed(
    slot: Slot, spec: ArmSpec, situation: Situation, *, env: Mapping[str, str], log_dir: Path
) -> SeedBuild:
    """Steps 1 to 6 of SCHEMA.md's seed order, each session its own connection."""

    build = SeedBuild()
    async with ScriptedSession(slot, spec, env=env, log_dir=log_dir, name="seed") as session:
        build.sessions["seed"] = str(session.tap_log)
        try:
            await seed_session(session, situation, build, slot.vault)
        finally:
            build.retries += [{"session": "seed", **r} for r in session.retries]
    # The tap's ready gate probes through the carrier too, and warms the index
    # before any measured session, so only this session runs it.
    async with ScriptedSession(
        slot, spec, env=env, log_dir=log_dir, name="settle", ready_gate=True
    ) as session:
        build.sessions["settle"] = str(session.tap_log)
        try:
            await settle_session(session, build)
        finally:
            build.retries += [{"session": "settle", **r} for r in session.retries]
    if situation.vocabulary(PRIOR_AGENT):
        async with ScriptedSession(slot, spec, env=env, log_dir=log_dir, name="prior") as session:
            build.sessions["prior"] = str(session.tap_log)
            try:
                await prior_session(session, situation, build)
            finally:
                build.retries += [{"session": "prior", **r} for r in session.retries]
    return build
