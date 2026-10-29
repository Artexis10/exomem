"""Reusable synthetic, densely-wikilinked vault generator for perf benchmarks.

Extracted from `tests/test_graph_lane_perf.py` (which used to carry its own copy)
so three callers generate a byte-identical corpus from ONE place:

- `tests/test_graph_lane_perf.py` — the graph-lane event-maintenance regression.
- `tests/test_latency_gate.py`    — the per-lane latency ceiling gate at scale.
- `scripts/latency_curve.py`      — the latency-vs-corpus-size curve harness.

The generator is pure stdlib (`random` + `pathlib`) — it does NOT import exomem
or torch — so importing it is cheap and side-effect-free from both tests and
scripts. `gen_dense_vault` is deterministic for a fixed `(n, links_per_note,
seed)`: same names, same folders, same link topology every run, so a benchmark
number is reproducible and a regression is attributable to code, not corpus jitter.

Why "dense": the graph lane resolves every `[[wikilink]]` on strong candidates
through the whole-vault `WikilinkResolver`. A realistic cross-linked corpus
(~25 links/note, a mix of full-path and bare-stem link forms) is what makes a
resolver-rebuild regression show up as a real per-query cost at scale, instead of
staying hidden the way a 10-file fixture did.
"""

from __future__ import annotations

import random
import uuid
from pathlib import Path

# KB sub-folders the synthetic notes are spread across — a realistic slice of the
# page-type tree so the walk/parse cost resembles a real vault's shape.
FOLDERS: tuple[str, ...] = (
    "Notes/Insights", "Notes/Patterns", "Notes/Failures",
    "Entities/Concepts", "Entities/People", "Sources", "Experiments",
)


#: Namespace for the deterministic note ids `gen_dense_vault(ids=True)` writes.
_NOTE_IDS = uuid.UUID("2d0b6c1e-8f4a-4c3e-9b1d-7a5e3f2c1b0d")


def gen_dense_vault(
    root: Path,
    n: int,
    links_per_note: int = 25,
    seed: int = 7,
    *,
    sources: int = 0,
    relations: bool = False,
    ids: bool = False,
) -> list[str]:
    """Write `n` densely cross-linked KB notes under `root`; return their rels.

    Each note gets `links_per_note` outbound `[[wikilinks]]` to random other
    notes, alternating between full-path and bare-stem link forms so the resolver
    exercises both resolution paths. Returns the vault-relative paths (with the
    leading ``Knowledge Base/`` and trailing ``.md``) in creation order, so a
    caller can address specific notes (e.g. to edit/rename/delete one).

    Deterministic in `(n, links_per_note, seed)`. Writes only under
    ``root/Knowledge Base/`` — the caller owns `root` (typically a tmp dir).

    Three opt-in shapes real vaults have, off by default so every existing
    caller writes byte-identical notes: `sources=k` writes k Source pages and
    has note i cite ``Sources/report-(i % k)`` (one-Source clusters of n/k
    notes); `relations` adds a ``## Relations`` line to the next note; `ids`
    gives every note a deterministic ``exomem_id``.
    """
    rng = random.Random(seed)
    kb = root / "Knowledge Base"
    for f in FOLDERS:
        (kb / f).mkdir(parents=True, exist_ok=True)
    if sources:
        (kb / "Sources").mkdir(parents=True, exist_ok=True)
        for s in range(sources):
            (kb / "Sources" / f"report-{s:03d}.md").write_text(
                "---\n"
                "type: source\n"
                f"title: Report {s:03d}\n"
                "captured: 2026-01-15\n"
                "---\n\n"
                f"# Report {s:03d}\n\nRaw notes from the field.\n",
                encoding="utf-8",
            )
    rels: list[str] = []
    names: list[str] = []
    for i in range(n):
        folder = FOLDERS[i % len(FOLDERS)]
        name = f"note-{i:05d}-topic-{rng.randint(0, 99999)}"
        names.append(name)
        rels.append(f"Knowledge Base/{folder}/{name}.md")
    for i, rel in enumerate(rels):
        targets = rng.sample(range(n), min(links_per_note, n))
        link_lines = []
        for t in targets:
            if t % 3 == 0:  # mix full-path and bare-stem link forms
                link_lines.append(f"- see [[{rels[t][:-3]}]] for context")
            else:
                link_lines.append(f"- ref [[{names[t]}]] inline")
        identity = f"exomem_id: {uuid.uuid5(_NOTE_IDS, rel)}\n" if ids else ""
        cited = f'sources:\n  - "[[Sources/report-{i % sources:03d}]]"\n' if sources else ""
        authored = (
            f"\n## Relations\n\n- relates_to [[{rels[(i + 1) % n][:-3]}]]\n" if relations else ""
        )
        (root / rel).write_text(
            "---\n"
            f"{identity}"
            "type: insight\n"
            f"title: Note {i} about topic {names[i]}\n"
            "tags: [synthetic, graph, dense]\n"
            f"updated: 2026-02-{(i % 28) + 1:02d}\n"
            f"{cited}"
            "---\n\n"
            f"# Note {i}\n\n"
            "Prose paragraph so the note is realistically sized and body text "
            "gives BM25 something to rank on. topic topic topic.\n\n"
            "## Related\n\n" + "\n".join(link_lines) + "\n" + authored,
            encoding="utf-8",
        )
    return rels


def gen_entity_overlay(root: Path, n_entities: int, seed: int = 17) -> list[str]:
    """Add deterministic synthetic person entities without changing dense fixtures."""
    rng = random.Random(seed)
    folder = root / "Knowledge Base" / "Entities" / "People"
    folder.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    for index in range(n_entities):
        token = rng.randint(0, 999999)
        rel = f"Knowledge Base/Entities/People/synthetic-person-{index:05d}-{token:06d}.md"
        (root / rel).write_text(
            "---\n"
            "type: entity\n"
            f"title: Synthetic Person {index:05d}\n"
            "entity_type: person\n"
            "status: active\n"
            "relationship: friend\n"
            "tags: [synthetic, scale]\n"
            "updated: 2026-02-01\n"
            "---\n\n"
            f"# Synthetic Person {index:05d}\n\n"
            "A deterministic synthetic identity used only for scale measurement.\n",
            encoding="utf-8",
        )
        rels.append(rel)
    return rels


def reference_source(profile: dict, directory: Path) -> tuple[list[str], str]:
    """Require approved public prose bytes; never replace them with a word bank."""
    import hashlib
    import string

    source = profile["source"]
    path = directory / source["path"]
    if not source.get("sha256") or not path.is_file():
        raise ValueError("missing-approved-prose-fixture")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source["sha256"]:
        raise ValueError("prose-fixture-digest-mismatch")
    words = raw.decode("utf-8").split()
    terms = {word.strip(string.punctuation).casefold() for word in words}
    terms.discard("")
    if len(terms) < profile["vocabulary_min"]:
        raise ValueError("prose-fixture-vocabulary-below-profile")
    return words, digest


def gen_reference_vault(root: Path, profile: dict, words: list[str]) -> list[str]:
    """Generate the latency profile from public prose and current registries.

    Existing dense fixture APIs keep their original bytes. This reference uses
    registry roles and folders; no copied note or category vocabulary lives here.
    """
    import json

    from exomem import note_types, semantic_language_registry
    from exomem.governance import egress
    from exomem.vocabulary import load

    types = [
        entry for entry in load(note_types.SPEC, root).active
        if entry.attributes.get("role") == "compiled" and entry.attributes.get("folder")
    ]
    categories = tuple(semantic_language_registry.core_registry().core_categories)
    if not types or not categories:
        raise ValueError("reference-registry-unavailable")
    rng = random.Random(profile["seed"])
    rels = [
        f"Knowledge Base/{types[i % len(types)].attributes['folder']}/sample-{i:05d}.md"
        for i in range(profile["pages"])
    ]

    def passage(offset: int, count: int) -> str:
        return " ".join(words[(offset + j) % len(words)] for j in range(count))

    for index, rel in enumerate(rels):
        paragraphs = [
            passage(index * 37 + paragraph * 53, profile["paragraph_words"])
            for paragraph in range(profile["paragraphs"])
        ]
        if index % round(1 / profile["unit_page_fraction"]) == 0:
            paragraphs.extend(
                f"- [{categories[j % len(categories)]}] {passage(index * 37 + j * 97, 18)}"
                for j in range(profile["units_per_page"])
            )
        targets = rng.sample(rels, rng.randint(profile["links_min"], profile["links_max"]))
        paragraphs.append("\n".join(f"- [[{target[:-3]}]]" for target in targets))
        frontmatter = (
            "---\n"
            f"type: {types[index % len(types)].key}\n"
            f"title: Sample {index:05d}\n"
            "projects: [reference]\n"
            f"updated: 2026-02-{index % 28 + 1:02d}\n"
            "---\n\n"
        )
        target = (
            profile["transcript_target_bytes"]
            if index % round(1 / profile["transcript_fraction"]) == 0
            else profile["page_target_bytes"]
        )
        if target == profile["transcript_target_bytes"]:
            frontmatter = frontmatter.replace("---\n\n", "media_type: audio\n---\n\n")
        content = frontmatter + "\n\n".join(paragraphs) + "\n"
        step = 0
        while len(content.encode("utf-8")) < target:
            content += "\n" + passage(index * 37 + 313 + step * 53, 20) + "\n"
            step += 1
        destination = root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    # This policy fixture is generated only in disposable benchmark vaults.
    # The rule releases in full: below egress.RELEASE_FLOOR every admitted hit
    # becomes a withheld notice and units are omitted, so the admitted series
    # would time notices instead of the owner's results.
    governance = root / "Knowledge Base/_Governance"
    scope_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    for kind, name, document in (
        ("scopes", "reference", {"governance_version": 1, "id": scope_id,
         "name": "Reference", "paths": [f"{entry.attributes['folder']}/**" for entry in types]}),
        ("rules", "reference", {"governance_version": 1,
         "id": "01ARZ3NDEKTSV4RRFFQ69G5FB0", "scope_ids": [scope_id],
         "audience": "benchmark-admitted", "ceiling": egress.LEVEL_FULL}),
    ):
        path = governance / kind / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        # JSON is a YAML subset; use its maintained serializer for fixture syntax.
        path.write_text(json.dumps(document), encoding="utf-8")
    return rels


# --- MCP read latency fixture --------------------------------------------------
#
# `gen_collection_fixture` and the two case builders turn
# benchmarks/mcp-read-latency/profile.json into Records and Planning collections
# and the canary cases that read them. A case's expected values come from the
# generator's own input (row counts, keys, titles, passages), never from a
# measured response.

#: Namespace of the collection ids; changing it changes every id in the fixture.
_COLLECTION_NAMESPACE = uuid.UUID("5b1c0d3e-7a42-4f6b-9c18-2e3d4a5b6c7d")


def _skewed_sizes(spec: dict, rng: random.Random) -> list[int]:
    """Row counts that fall off as a power law, so a few collections hold most rows."""
    weights = [(rank + 1) ** -spec["skew"] for rank in range(spec["collections"])]
    sizes = [max(spec["minimum_rows"], round(spec["rows"] * weight / sum(weights))) for weight in weights]
    rng.shuffle(sizes)
    return sizes


def _manifest_source(profile_name: str, identity: str, title: str, fields: dict, key: str, source: str) -> dict:
    import yaml

    frontmatter = {
        # These tokens are the collection manifest contract's fixed values, not vault vocabulary.
        "type": "collection", "exomem_id": identity, "title": title, "semantic_profile": profile_name,
        "collection_version": 1, "schema_version": 1, "lifecycle": "active",
        "storage": {"strategy": "markdown-items", "source": source, "format_version": 1},
        "item_schema": {"natural_key": [key], "fields": fields},
    }
    description = title + "."
    return {"manifest_text": "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + description + "\n",
            "description": description}


def _record_item(fields: list[dict], rng: random.Random, ordinal: str) -> dict:
    import datetime

    item: dict = {}
    for field in fields:
        if field.get("key"):
            item[field["name"]] = ordinal
        elif "distinct" in field:
            item[field["name"]] = f"{field['name']}-{rng.randrange(field['distinct'])}"
        elif "range" in field:
            item[field["name"]] = rng.randint(*field["range"])
        else:
            start = datetime.date.fromisoformat(field["from"])
            item[field["name"]] = (start + datetime.timedelta(days=rng.randrange(field["span_days"]))).isoformat()
    return item


def _planning_item(title: str, kind: str, governed: list[str], values: dict[str, list[str]],
                   rng: random.Random, tries: int = 500) -> dict:
    """Draw governed values until the product's own validator accepts the combination.

    Status, commitment and horizon constrain each other, so independent draws are often refused.
    The shipped validator is the oracle; the generator holds no copy of its rules.
    """
    from exomem import planning
    from exomem.structured_collections import CollectionError

    for _ in range(tries):
        item = {"title": title, "kind": kind, **{name: rng.choice(values[name]) for name in governed}}
        try:
            planning.normalize_item(item, vault_root=None)
        except CollectionError:
            continue
        return item
    raise ValueError("planning-item-unsatisfiable")


def _planning_layers(parents: dict[str, list[str]], governed: list[str], values: dict[str, list[str]],
                     rng: random.Random) -> list[list[str]]:
    """Registry kinds by depth in their `parents` graph, roots first.

    A kind is usable when the validator accepts it with delivery-state values and, unless it is a root,
    when one of its parent kinds is usable at a shallower depth. Both facts come from the registry and
    the validator at run time, so the generator names no kind.
    """
    carriers = set()
    for kind in parents:
        try:
            _planning_item("probe", kind, governed, values, rng, tries=100)
        except ValueError:
            continue
        carriers.add(kind)
    depth = {kind: 0 for kind in carriers if not parents[kind]}
    placed = True
    while placed:
        placed = False
        for kind in carriers - depth.keys():
            shallower = [depth[parent] for parent in parents[kind] if parent in depth]
            if shallower:
                depth[kind] = 1 + min(shallower)
                placed = True
    return [sorted(kind for kind, level in depth.items() if level == wanted) for wanted in range(max(depth.values()) + 1)]


def _planning_hierarchy(size: int, layers: list[list[str]], parents: dict[str, list[str]], fanout: int,
                        rng: random.Random) -> list[tuple[str, int | None]]:
    """`(kind, parent index)` for each of `size` items, parents before children, roots without parent.

    Level sizes grow by `fanout` per level; each child takes the next parent of an allowed kind in turn.
    """
    levels = min(len(layers), size)
    weights = [fanout ** level for level in range(levels)]
    sizes = [max(1, round(size * weight / sum(weights))) for weight in weights[:-1]]
    sizes.append(size - sum(sizes))
    placed: list[tuple[str, int | None]] = []
    turn: dict[str, int] = {}
    for level, count in enumerate(sizes):
        for _ in range(count):
            options = [kind for kind in layers[level]
                       if level == 0 or any(prior in parents[kind] for prior, _ in placed)]
            kind = rng.choice(options)
            if level == 0:
                placed.append((kind, None))
                continue
            candidates = [index for index, (prior, _) in enumerate(placed) if prior in parents[kind]]
            turn[kind] = turn.get(kind, -1) + 1
            placed.append((kind, candidates[turn[kind] % len(candidates)]))
    return placed


def gen_collection_fixture(profile: dict) -> dict:
    """Records and Planning collections with skewed row counts, plus manifest-less trackers.

    Planning values (status, commitment, horizon, priority) come from the shipped
    planning-values registry at run time. Only statuses of the registry's `open`
    class are used, so every row stays in the default query view. Planning items form
    the hierarchy the registry's kind `parents` rules require.
    """
    from exomem.planning_values import PlanningValues

    rng = random.Random(f"{profile['seed']}:collections")
    registry = PlanningValues(None)
    records_spec, planning_spec = profile["records"], profile["planning"]
    records = []
    for index, size in enumerate(_skewed_sizes(records_spec, rng)):
        title = f"{records_spec['title']} {index:02d}"
        identity = str(uuid.uuid5(_COLLECTION_NAMESPACE, f"{profile['seed']}:records:{index}"))
        key = next(field["name"] for field in records_spec["fields"] if field.get("key"))
        fields = {field["name"]: {"type": field["type"], **({"required": True} if field.get("key") else {})}
                  for field in records_spec["fields"]}
        records.append({
            "id": identity, "title": title, "manifest_path": f"Knowledge Base/Records/{title}/_collection.md",
            **_manifest_source("records", identity, title, fields, key, "Entries"),
            "items": [_record_item(records_spec["fields"], rng, f"{index:02d}-{row:04d}") for row in range(size)],
        })
    governed = planning_spec["governed_fields"]
    if set(governed) - set(registry.fields()):
        raise ValueError("planning-field-unregistered")
    values = {name: [value for value in registry.values(name)
                     if name != "status" or registry.planning_class(value) == "open"] for name in governed}
    parents = {kind: registry.find("kind", kind).attributes["parents"] for kind in registry.values("kind")}
    layers = _planning_layers(parents, governed, values, rng)
    planning = []
    for index, size in enumerate(_skewed_sizes(planning_spec, rng)):
        title = f"{planning_spec['title']} {index:02d}"
        identity = str(uuid.uuid5(_COLLECTION_NAMESPACE, f"{profile['seed']}:planning:{index}"))
        # `lifecycle` is a core Planning field that the manifest must declare (planning.require_planning_profile);
        # the registry governs the others.
        fields = {name: {"type": "string", **({"required": True} if name == "title" else {})}
                  for name in dict.fromkeys(("title", "lifecycle", *registry.fields(), *planning_spec["free_fields"]))}
        shape = _planning_hierarchy(size, layers, parents, planning_spec["fanout"], rng)
        planning.append({
            "id": identity, "title": title, "manifest_path": f"Knowledge Base/Planning/{title}/_collection.md",
            **_manifest_source("planning", identity, title, fields, "title", "Items"),
            "items": [_planning_item(f"{title} item {row:04d}", kind, governed, values, rng)
                      for row, (kind, _parent) in enumerate(shape)],
            # Index of each item's parent among `items` (None for a root); the loader turns it into a plan
            # reference from the add response, because the product assigns the item key.
            "parents": [parent for _kind, parent in shape],
        })
    trackers = []
    for index in range(profile["legacy_trackers"]["count"]):
        rows = "\n".join(f"| 2026-02-{row % 28 + 1:02d} | note {index}-{row} |"
                         for row in range(profile["legacy_trackers"]["rows"]))
        trackers.append({"path": f"Knowledge Base/Records/Legacy/tracker-{index}.md",
                         # `tracker` is the frontmatter type the legacy reader accepts.
                         "text": f"---\ntype: tracker\ntitle: Legacy tracker {index}\n---\n\n"
                                 f"| date | note |\n| --- | --- |\n{rows}\n"})
    # bulk_upsert needs one preserved page under the Evidence lane to name as the rows' provenance.
    source_page = {"path": "Knowledge Base/Evidence/latency-fixture-source.md",
                   "text": "---\ntitle: Latency fixture source\n---\n\nGenerated rows for the latency gate.\n"}
    return {"records": records, "planning": planning, "legacy_trackers": trackers, "source_page": source_page}


def _case(group: str, number: int, tool: str, arguments: dict, *, state_path: list, state,
          references: list[tuple[list, str]], material: list[list]) -> dict:
    return {"id": f"{group}-{number:02d}", "group": group, "tool": tool, "arguments": arguments,
            "expected": {"state_path": state_path, "state": state,
                         "references": [{"path": path, "value": value} for path, value in references],
                         "material_paths": material}}


def gen_collection_cases(fixture: dict, profile: dict) -> list[dict]:
    """Query, read and list cases whose answers follow from the fixture's own rows."""
    spec = profile["cases"]
    cases = []
    # Filter on the key (one row) and on each low-cardinality field (many rows).
    record_columns = [field["name"] for field in profile["records"]["fields"]
                      if field.get("key") or "distinct" in field]
    for kind, tool, name, filter_names in (
        ("records", "read_records", "records", record_columns),
        ("planning", "read_plans", "planning", profile["planning"]["governed_fields"]),
    ):
        rng = random.Random(f"{profile['seed']}:{kind}-cases")
        chosen = rng.sample(fixture[kind], min(spec["per_group"], len(fixture[kind])))
        for number, collection in enumerate(chosen):
            row = rng.choice(collection["items"])
            column = filter_names[number % len(filter_names)]
            matched = sum(1 for item in collection["items"] if item[column] == row[column])
            query = {"kind": "query", "collection": collection["manifest_path"],
                     "filters": [{"column": column, "op": "eq", "value": row[column]}]}
            cases.append(_case(f"{name}-query", number, tool, {"operation": query},
                               state_path=["total_matched"], state=matched,
                               references=[(["collection_id"], collection["id"])], material=[["snapshot"]]))
        for number, collection in enumerate(chosen):
            inspect = {"kind": "inspect", "collection": collection["manifest_path"]}
            cases.append(_case(f"{name}-read", number, tool, {"operation": inspect},
                               state_path=["contract", "collection_id"], state=collection["id"],
                               references=[(["contract", "title"], collection["title"])],
                               material=[["snapshot"]]))
        for number in range(spec["list_cases"]):
            listing = {"collections": {"type": "array", "minItems": len(fixture[kind])}}
            if kind == "records":
                listing["legacy_trackers"] = {"type": "array", "minItems": len(fixture["legacy_trackers"])}
            cases.append({"id": f"{name}-list-{number:02d}", "group": f"{name}-list", "tool": tool,
                          "arguments": {"operation": {"kind": "inspect"}},
                          "expected": {"response_schema": {"type": "object", "required": list(listing),
                                                           "properties": listing}}})
    return cases


def gen_note_cases(root: Path, rels: list[str], profile: dict, fixture: dict) -> list[dict]:
    """Read, search and recall cases for pages drawn from the generated vault.

    The shared synthetic prose repeats across pages, so no query names one page.
    The read case pins the exact page; the search case requires a non-empty ranked answer, and the
    recall case requires source-bound collection material from the default map.
    """
    from exomem import vault

    spec = profile["cases"]
    size = spec["per_group"]
    chosen = random.Random(f"{profile['seed']}:notes").sample(rels, size * 3)
    pages = []
    for rel in chosen:
        frontmatter, body, _ = vault.parse_frontmatter((root / rel).read_text(encoding="utf-8"))
        pages.append((rel, frontmatter["title"], " ".join(body.split("\n\n")[0].split()[: spec["passage_words"]])))
    search_schema = {"type": "object", "required": ["hits"],
                     "properties": {"hits": {"type": "array", "minItems": 1,
                                             "items": {"type": "object", "required": ["path", "title"]}}}}
    cases = []
    for number, (rel, title, _passage) in enumerate(pages[:size]):
        cases.append(_case("note-read", number, "read_memory", {"path": rel}, state_path=["path"], state=rel,
                           references=[(["frontmatter", "title"], title)], material=[["body"]]))
    for number, (_rel, _title, passage) in enumerate(pages[size:2 * size]):
        cases.append({"id": f"search-{number:02d}", "group": "search", "tool": "search_memory",
                      "arguments": {"query": passage}, "expected": {"response_schema": search_schema}})
    # Maps are turn-independent; bind each collection's identity and lede from the authored fixture at any rank.
    collections = [*fixture["records"], *fixture["planning"]]
    material = [{"type": "object", "required": ["path", "title", "description"],
                 "properties": {"path": {"const": collection["manifest_path"]},
                                "title": {"const": collection["title"]},
                                "description": {"const": collection["description"]}}}
                for collection in collections]
    recall_schema = {
        "type": "object", "required": ["abstained", "map"],
        "properties": {
            "abstained": {"const": False},
            "map": {"type": "object", "required": ["ref", "state", "items"],
                    "properties": {"ref": {"const": "exomem://map"}, "state": {"const": "ready"},
                                   "items": {"type": "array", "contains": {"anyOf": material}}}},
        },
    }
    for number, (_rel, title, _passage) in enumerate(pages[2 * size:]):
        cases.append({"id": f"recall-{number:02d}", "group": "recall", "tool": "recall_context",
                      "arguments": {"turn": title},
                      "expected": {"outcome": "ok", "response_schema": recall_schema}})
    return cases
