"""The due-state block a recall carries is built in bounded work.

Measured 2026-09-15 on the personal vault (621 due entries, 355 of them
collection candidates): `ask_memory`'s find took 127 ms and the tool call
25.4 s, all of it in `due_state.served`. Three costs, each pinned here by
mechanism rather than by timing: the candidate detector re-ran over every
term of an entry's units when only the entry's own term is recomposed; refs
were resolved with a fresh sidecar connection per entry; and collection
manifests were re-parsed from YAML on every serve.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import collection_candidate, due_state, review_state, structured_collections


def _candidate_rows() -> list[dict[str, object]]:
    return [
        {
            "page": f"Knowledge Base/Notes/Insights/studio-{index}.md",
            "unit_ref": f"exomem://memory/55555555-5555-4555-8555-55555555555{index}#event",
            "terms": ["studio-licence", "provider-alpha", "workspace-beta"],
            "date": day,
            "text": text,
        }
        for index, (day, text) in enumerate(
            [
                (dt.date(2026, 8, 1), "Purchased a licence for $120"),
                (dt.date(2026, 8, 8), "Renewed the licence"),
                (dt.date(2026, 8, 15), "Cancelled the licence"),
                (dt.date(2026, 8, 22), "Refunded $40"),
            ],
            start=1,
        )
    ]


def test_detecting_one_term_matches_the_full_detection_for_that_term() -> None:
    rows = _candidate_rows()
    full = collection_candidate.detect(rows)
    assert full, "fixture must yield candidates"
    every_term = sorted({term for row in rows for term in row["terms"]})
    for term in every_term:
        restricted = collection_candidate.detect(rows, terms=[term])
        assert restricted == [item for item in full if item.term == term], term
    assert collection_candidate.detect(rows, terms=["never-authored"]) == []
    # Canonicalised like every other term: spacing and case do not fork the key.
    assert collection_candidate.detect(rows, terms=["Studio Licence"]) == [
        item for item in full if item.term == "studio-licence"
    ]


def test_the_survivor_check_recomposes_only_the_entrys_own_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    real = collection_candidate.detect

    def spy(rows, **kwargs):
        captured.update(kwargs)
        return real(rows, **kwargs)

    monkeypatch.setattr(collection_candidate, "detect", spy)
    entry = {
        "path": "Knowledge Base/Notes/Insights/studio-1.md",
        "component": {
            "family": "collection_candidate",
            "term": "studio-licence",
            "units": _candidate_rows(),
            "project_terms": [],
        },
    }
    due_state._survivors_only(
        tmp_path, entry, lambda _path: True, lambda: [], lambda paths: {p: p for p in paths}
    )
    assert tuple(captured.get("terms") or ()) == ("studio-licence",)


def test_refs_are_resolved_once_per_serve_from_the_projections_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_refs(vault_root, paths):
        calls.append(list(paths))
        return {path: f"ref:{path}" for path in paths}

    monkeypatch.setattr(review_state, "refs_for_paths", fake_refs)
    payload = {
        "categories": {
            "collection_candidate": {
                "Knowledge Base/Notes/A.md": {
                    "open": [
                        {
                            "path": "Knowledge Base/Notes/A.md",
                            "component": {
                                "family": "collection_candidate",
                                "units": [{"page": "Knowledge Base/Notes/B.md"}],
                                "joined": [["Knowledge Base/Notes/C.md", "k"]],
                            },
                        }
                    ]
                }
            },
            "unreflected_observations": {
                "Knowledge Base/Notes/D.md": {
                    "pending": [
                        {
                            "path": "Knowledge Base/Notes/D.md",
                            "paths": ["Knowledge Base/Notes/E.md"],
                            "component": {
                                "collection": "Knowledge Base/Records/X/_collection.md",
                                "page_path": "Knowledge Base/Notes/D.md",
                                "reflecting_records": [{"path": "Knowledge Base/Records/X/r1.md"}],
                            },
                        }
                    ]
                }
            },
        }
    }
    resolve = due_state._refs_resolver(tmp_path, payload)
    first = resolve(["Knowledge Base/Notes/A.md", "Knowledge Base/Notes/B.md"])
    second = resolve(["Knowledge Base/Notes/E.md", "Knowledge Base/Records/X/r1.md"])
    assert first == {
        "Knowledge Base/Notes/A.md": "ref:Knowledge Base/Notes/A.md",
        "Knowledge Base/Notes/B.md": "ref:Knowledge Base/Notes/B.md",
    }
    assert second["Knowledge Base/Records/X/r1.md"] == "ref:Knowledge Base/Records/X/r1.md"
    # One batch covering every path the projection names, in one call.
    assert len(calls) == 1
    assert set(calls[0]) == {
        "Knowledge Base/Notes/A.md",
        "Knowledge Base/Notes/B.md",
        "Knowledge Base/Notes/C.md",
        "Knowledge Base/Notes/D.md",
        "Knowledge Base/Notes/E.md",
        "Knowledge Base/Records/X/_collection.md",
        "Knowledge Base/Records/X/r1.md",
    }
    # A path outside the projection falls through to one direct lookup.
    resolve(["Knowledge Base/Notes/outside.md"])
    assert calls[1] == ["Knowledge Base/Notes/outside.md"]


def test_the_batched_refs_lookup_never_names_a_path_the_audience_may_not_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolving a ref is not a pure read for a path the sidecar has not indexed,
    # so a withheld page must not reach the lookup at all -- as before, when only
    # the surviving entries were resolved one by one.
    calls: list[list[str]] = []

    def fake_refs(vault_root, paths):
        calls.append(list(paths))
        return {path: f"ref:{path}" for path in paths}

    monkeypatch.setattr(review_state, "refs_for_paths", fake_refs)
    withheld = "Knowledge Base/Notes/Secret.md"
    payload = {
        "categories": {
            "collection_candidate": {
                "Knowledge Base/Notes/A.md": {
                    "open": [
                        {
                            "path": "Knowledge Base/Notes/A.md",
                            "component": {
                                "family": "collection_candidate",
                                "units": [
                                    {"page": withheld},
                                    {"page": "Knowledge Base/Notes/B.md"},
                                ],
                            },
                        }
                    ]
                },
                withheld: {"open": [{"path": withheld}]},
            }
        }
    }
    resolve = due_state._refs_resolver(tmp_path, payload, lambda path: path != withheld)
    assert resolve(["Knowledge Base/Notes/A.md"]) == {
        "Knowledge Base/Notes/A.md": "ref:Knowledge Base/Notes/A.md"
    }
    assert len(calls) == 1
    assert withheld not in calls[0]
    assert set(calls[0]) == {"Knowledge Base/Notes/A.md", "Knowledge Base/Notes/B.md"}


def test_a_manifest_is_parsed_once_per_content_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault as vault_module

    kb = tmp_path / "Knowledge Base" / "Records" / "Ledger"
    kb.mkdir(parents=True)
    manifest = kb / "_collection.md"
    manifest.write_text(
        """---
type: collection
exomem_id: 7c2f1b0e-6d2a-4c8e-9b1f-3a5d7e9f1c2b
title: Ledger
semantic_profile: records
collection_version: 1
schema_version: 2
lifecycle: active
storage:
  strategy: markdown-items
  source: events.md
  format_version: 1
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
---
""",
        encoding="utf-8",
    )
    parses: list[str] = []
    real = vault_module.parse_frontmatter

    def counting(text, **kwargs):
        parses.append(text)
        return real(text, **kwargs)

    monkeypatch.setattr(vault_module, "parse_frontmatter", counting)
    structured_collections._MANIFEST_PARSE_CACHE.clear()
    rel = "Knowledge Base/Records/Ledger/_collection.md"
    first = structured_collections.load_manifest(tmp_path, rel)
    again = structured_collections.load_manifest(tmp_path, rel)
    assert again is first
    assert len(parses) == 1
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = structured_collections.load_manifest(tmp_path, rel)
    assert changed is not first
    assert len(parses) == 2
