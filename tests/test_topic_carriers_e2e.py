"""An agent files undeclared entries under topics from the owner's considerations, and the maps show them.

One fixture vault holds an undeclared project key, a topic folder and a tag on notes in a note-type
folder. The considerations are read through `schema_memory`, the carriers are saved with inferred
origins (the agent's choice, standing in), and the next maps packet and the domain maps read through
`read_memory` file those entries with no page write. Per-rule checks live in `test_topic_carriers.py`.
"""

from __future__ import annotations

from pathlib import Path

from test_context_catalogue import KB, _built, _page, _write
from test_topic_carriers import AREA, _carrier, _inspect, _save

from exomem import commands
from exomem import context_views as cv
from exomem.governance.principal import library_scope

TAG = "quay-works"


def _vault_of_unfiled_entries(vault: Path) -> list[str]:
    _write(
        vault,
        f"{KB}/_Schema/source-taxonomy.yaml",
        "schema_version: 1\ndomains:\n"
        "  harbour: {label: Harbour, path_label: Harbour, description: Quays and moorings.}\n"
        "  fleet: {label: Fleet, path_label: Fleet}\n",
    )
    _write(vault, f"{KB}/_Schema/project-keys.yaml", "projects:\n  fleet-ops: {folder: Fleet Ops, category: domain}\n")
    paths = [_page(vault, f"berth-{index}", folder=AREA) for index in range(3)]
    paths += [_page(vault, f"quay-{index}", f"tags: [{TAG}]\n") for index in range(3)]
    paths += [_page(vault, f"engine-{index}", "project: fleet-ops\n") for index in range(3)]
    return paths


def _topic_refs(vault: Path) -> set[str]:
    with library_scope():
        packet = cv.activation_packet(vault, 40_000, generation={})
    return {item["ref"] for item in packet["map"]["items"] if item["kind"] == "topic"}


def _members(vault: Path, topic: str) -> set[str]:
    with library_scope():
        page = commands.op_read_memory(vault, path=cv.topic_ref(topic))
    return {item["path"] for item in page["items"] if "path" in item}


def test_inferred_carriers_put_unfiled_entries_on_the_map_and_domain_maps_return_them(vault: Path) -> None:
    pages = _vault_of_unfiled_entries(vault)
    _built(vault)
    before = {path: (vault / path).read_bytes() for path in pages}

    # The considerations name each carrier that would file unfiled entries.
    considered = {item["carrier"] for item in _inspect(vault)["considerations"]}
    assert {"folder:areas/harbour", f"tag:{TAG}", "project:fleet-ops"} <= considered
    assert not _topic_refs(vault)

    # The agent infers a topic from each carrier's evidence and saves it as `inferred`.
    result = _save(
        vault,
        {
            "berths": _carrier("folder", "Areas/Harbour", ["harbour"]),
            "quays": _carrier("tag", TAG, ["harbour"]),
            "fleet": _carrier("project", "fleet-ops", ["fleet"]),
        },
    )
    assert result["valid"] is True
    carriers = {entry["key"]: entry for entry in _inspect(vault)["entries"]}
    assert {entry["attributes"]["origin"]["harbour"] for key, entry in carriers.items() if key != "fleet"} == {"inferred"}

    # The next packet shows both topics; each domain map returns exactly its carriers' members.
    assert _topic_refs(vault) == {cv.topic_ref("harbour"), cv.topic_ref("fleet")}
    assert _members(vault, "harbour") == {path for path in pages if "berth-" in path or "quay-" in path}
    assert _members(vault, "fleet") == {path for path in pages if "engine-" in path}
    assert {path: (vault / path).read_bytes() for path in pages} == before


def _review_action(vault: Path) -> dict[str, str]:
    report = commands.op_adopt(vault)
    return next(action for action in report["next_actions"] if action["action"] == "review-topic-carriers")


def test_adoption_names_the_carrier_review_with_the_unfiled_count_and_writes_nothing(vault: Path) -> None:
    _vault_of_unfiled_entries(vault)
    _built(vault)
    before = sorted(path.read_bytes() for path in (vault / KB).rglob("*.md"))

    action = _review_action(vault)

    assert action["status"] == "available"
    assert 'subject="topic-carriers"' in action["description"]
    assert sorted(path.read_bytes() for path in (vault / KB).rglob("*.md")) == before


def test_adoption_says_unknown_when_the_catalogue_is_not_built(vault: Path) -> None:
    assert _review_action(vault)["status"] == "unknown"
