"""What fixes a world's bytes: digests, and a record of each artefact that a manifest pins.

The manifest of a plan lists the generator version, the digest of the catalogue and of the pack, a
commitment to the seed and a hash of every artefact. A hash covers an artefact, its carried statements and its resolved relation facts with endpoint provenance, so two runs that agree on every hash agree on the world's evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from .core import EdgeRef, World
from .loader import CATALOGUE_DIR
from .validator import check_relation_refs

GENERATOR_VERSION = "2"


def _plain(value: Any) -> Any:
    """A JSON form for the values that a world holds and JSON does not."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, frozenset | set):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} has no JSON form")


def canonical(value: Any) -> str:
    """The one JSON text of a value: keys sorted, no spare space, dates as ISO text."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_plain, ensure_ascii=False
    )


def dump(value: Any) -> str:
    """The same value as ``canonical``, indented for a person to read."""
    return json.dumps(value, sort_keys=True, indent=1, default=_plain, ensure_ascii=False) + "\n"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def catalogue_digest(directory: Path = CATALOGUE_DIR) -> str:
    """The digest of every file of the catalogue, by relative path. The projection sits outside."""
    parts = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*.yaml"))
    }
    return digest(parts)


def artefact_hashes(world: World) -> list[dict[str, Any]]:
    """Hash each document, its carried statements and its resolved relation facts and provenance."""
    relations = world.relation_facts()
    originals = check_relation_refs(
        world, ((a.id, ref) for a in world.artefacts.values() for ref in a.relations), relations
    )
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "hash": digest(
                {
                    "artefact": asdict(a),
                    "carries": [asdict(world.statements[s]) for s in a.carries],
                    "relations": [
                        {
                            "fact": asdict(relations[ref]),
                            "endpoints": [
                                {
                                    "statement": asdict(world.statements[s]),
                                    "document": asdict(originals[s]),
                                }
                                for s in (ref.source, ref.target)
                            ]
                            if isinstance(ref, EdgeRef)
                            else [],
                        }
                        for ref in a.relations
                    ],
                }
            ),
        }
        for a in sorted(world.artefacts.values(), key=lambda a: a.id)
    ]


def world_record(world: World) -> dict[str, Any]:
    """The world's records as plain data, for a file that a person or another tool can read."""
    record = {
        field: [asdict(item) for item in sorted(getattr(world, field).values(), key=lambda i: i.id)]
        for field in (
            "entities",
            "attributes",
            "events",
            "contexts",
            "audiences",
            "statements",
            "artefacts",
        )
    } | {
        field: [asdict(item) for item in getattr(world, field)]
        for field in (
            "assignments",
            "withdrawals",
            "corrections",
            "edges",
            "namings",
            "merges",
            "tellings",
            "draws",
            "causes",
            "restrictions",
            "removals",
        )
    }
    # Which record each announcing document stands for: the operation's name and its `seq`.
    record["operations"] = [
        {"id": id_, "operation": name, "seq": r.seq}
        for id_, (name, r) in sorted(world.operations.items())
    ]
    return record
