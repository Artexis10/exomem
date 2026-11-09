"""A fact held only as a value in a small preserved dataset is found by search, for readers who may read it."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from exomem import commands, find, preserve
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal, request_scope

ACCOUNT = "EE81 7700 1234 5678 9012"
PROFILE = json.dumps(
    {"supplier": {"name": "Brightwater Haulage", "bank": {"account_number": ACCOUNT}}}
).encode()


def _preserve(vault: Path, filename: str, data: bytes):
    return preserve.preserve_stream(
        vault, scope="Test", category="exports", filename=filename, stream=io.BytesIO(data)
    )


def _find(vault: Path, query: str) -> list[dict]:
    found = commands.op_find(vault, query=query, mode="keyword", graph=False)
    return found["hits"] if isinstance(found, dict) else found


def _as_external(vault: Path, *, selector: str, ceiling: int) -> RequestPrincipal:
    """Govern the vault so the external audience reads what `selector` picks at `ceiling`."""
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "datasets.yaml").write_text(
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n{selector}\n", encoding="utf-8"
    )
    (root / "rules" / "datasets.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        f"scope_ids: [01ARZ3NDEKTSV4RRFFQ69G5FAV]\naudience: external\nceiling: {ceiling}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find.clear_cache()
    return RequestPrincipal(audience_id="external", surface="mcp")


def _paths(hits: list[dict]) -> list[str]:
    return [hit["path"] for hit in hits]


def test_full_reader_finds_a_value_held_only_in_a_small_dataset(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    card_before = (vault / result.sidecar_path).read_bytes()

    for query in (ACCOUNT, "ee817700123456789012", "supplier account number"):
        hits = [hit for hit in _find(vault, query) if hit["path"] == result.path]
        assert hits, query
        assert hits[0]["matched_part"] == 1
        assert ACCOUNT in hits[0]["passage"]["text"]
        assert "bank.account_number" in hits[0]["passage"]["text"]

    assert (vault / result.path).read_bytes() == PROFILE
    assert (vault / result.sidecar_path).read_bytes() == card_before


def test_governed_reader_at_full_level_finds_the_value(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    reader = _as_external(vault, selector='paths: ["**"]', ceiling=egress.LEVEL_FULL)
    with request_scope(reader):
        hits = [hit for hit in _find(vault, ACCOUNT) if hit["path"] == result.path]
    assert hits
    assert ACCOUNT in hits[0]["passage"]["text"]


def test_reader_below_full_level_gets_no_value_hit_while_the_card_is_found(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    reader = _as_external(vault, selector='paths: ["**"]', ceiling=egress.LEVEL_EXCERPT)
    with request_scope(reader):
        values = _paths(_find(vault, ACCOUNT))
        by_title = _paths(_find(vault, "supplier-profile"))
    assert result.path not in values
    assert result.sidecar_path in by_title


def test_reader_with_unresolved_membership_gets_no_value_hit_while_the_card_is_found(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    # A selector on page type cannot classify the original, which has no frontmatter.
    reader = _as_external(vault, selector="types: [source]", ceiling=egress.LEVEL_FULL)
    with request_scope(reader):
        values = _paths(_find(vault, ACCOUNT))
        by_title = _paths(_find(vault, "supplier-profile"))
    assert result.path not in values
    assert result.sidecar_path in by_title


def test_a_dataset_over_the_size_bound_keeps_card_only_discovery(vault):
    # Long notes are cut to 256 characters in the rendering, so only the file's size excludes it.
    rows = [{"supplier": f"Supplier {n}", "note": "x" * 1000} for n in range(100)]
    rows.append({"supplier": "Zephyr Needlework", "note": "only-in-the-large-file"})
    data = json.dumps(rows).encode()
    assert len(data) > 64 * 1024
    result = _preserve(vault, "suppliers.json", data)
    assert result.path not in _paths(_find(vault, "only-in-the-large-file"))
    assert result.sidecar_path in _paths(_find(vault, "suppliers"))


def test_a_rendering_cut_at_its_bound_says_so_and_leaves_later_records_unsearched(vault):
    # Long column names repeat on every record of the rendering, so a small file renders large.
    columns = [f"column_with_a_long_descriptive_name_{n:02d}" for n in range(20)]
    lines = [",".join(columns), "first-marker," + ",".join("1" for _ in columns[1:])]
    lines += [",".join("1" for _ in columns) for _ in range(1500)]
    lines.append("last-marker," + ",".join("1" for _ in columns[1:]))
    data = "\n".join(lines).encode()
    assert len(data) <= 64 * 1024
    result = _preserve(vault, "wide.csv", data)
    first = [hit for hit in _find(vault, "first-marker") if hit["path"] == result.path]
    assert first
    assert "truncated" in first[0]["passage"]["text"]
    assert result.path not in _paths(_find(vault, "last-marker"))


def test_a_changed_card_drops_the_value_it_no_longer_vouches_for(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    assert result.path in _paths(_find(vault, ACCOUNT))
    card = vault / result.sidecar_path
    card.write_text(
        card.read_text(encoding="utf-8").replace("inspection_status: ready", "inspection_status: invalid_data"),
        encoding="utf-8",
    )
    assert result.path not in _paths(_find(vault, ACCOUNT))


def test_a_dataset_replaced_together_with_its_card_is_searched_by_its_new_values(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    assert result.path in _paths(_find(vault, ACCOUNT))
    replaced = PROFILE.replace(b"7700", b"7701")
    card = vault / result.sidecar_path
    card_text = card.read_text(encoding="utf-8")
    assert result.hash in card_text
    (vault / result.path).write_bytes(replaced)
    card.write_text(card_text.replace(result.hash, hashlib.sha256(replaced).hexdigest()), encoding="utf-8")
    assert result.path in _paths(_find(vault, "EE81 7701"))
    assert result.path not in _paths(_find(vault, ACCOUNT))


def test_an_original_that_no_longer_matches_its_card_is_not_served(vault):
    result = _preserve(vault, "supplier-profile.json", PROFILE)
    assert result.path in _paths(_find(vault, ACCOUNT))
    replaced = PROFILE.replace(b"7700", b"7701")
    assert hashlib.sha256(replaced).hexdigest() != result.hash
    (vault / result.path).write_bytes(replaced)
    assert result.path not in _paths(_find(vault, ACCOUNT))
    assert result.path not in _paths(_find(vault, "EE81 7701"))
