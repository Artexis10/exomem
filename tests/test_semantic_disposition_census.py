"""Relation-disposition census over every evaluated page.

Satisfied pages emit no finding, so a rate derived from findings alone reports a
reviewed-none share of 100% on a healthy vault. The census must therefore count
every evaluation, and must not be shrinkable under the payload byte budget.
"""

from __future__ import annotations

from pathlib import Path

from exomem import semantic_writes


def _page(page_id: str, *, title: str, relations: str = "") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "project: alpha\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"# {title}\n\n"
        "- [config] Session duration is fixed.\n\n"
        "## Relations\n"
        f"{relations}"
    )


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _corpus(tmp_path: Path) -> list[Path]:
    target = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/target.md",
        _page("00000000-0000-4000-8000-000000000201", title="Target"),
    )
    connected = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/connected.md",
        _page(
            "00000000-0000-4000-8000-000000000202",
            title="Connected",
            relations="- refines [[Knowledge Base/Notes/Insights/target]]\n",
        ),
    )
    bare = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/bare.md",
        _page("00000000-0000-4000-8000-000000000203", title="Bare"),
    )
    return [target, connected, bare]


def test_census_counts_every_evaluation_including_satisfied_pages(
    tmp_path: Path,
) -> None:
    paths = _corpus(tmp_path)

    batch = semantic_writes.evaluate_posthoc_batch(
        tmp_path, paths=paths, operation="watcher"
    )
    payload = batch.as_dict()
    census = payload["relation_disposition_summary"]

    assert sum(census.values()) == len(batch.evaluations) == 3
    # The typed-relation page is satisfied and emits no finding of its own, so it
    # is only visible through the census.
    assert census.get("qualifying_relation_typed", 0) >= 1
    assert len(payload["semantic_contract_findings"]) < len(batch.evaluations)


def test_census_is_not_shrinkable_under_the_byte_budget(tmp_path: Path) -> None:
    paths = _corpus(tmp_path)

    payload = semantic_writes.evaluate_posthoc_batch(
        tmp_path, paths=paths, operation="watcher"
    ).as_dict()

    # Only variable-length collections are shrunk; a fixed-size census must never
    # be registered as one, or the rate would silently become partial.
    assert "relation_disposition_summary" not in payload["omitted_counts"]
    assert payload["relation_disposition_summary"]


def test_census_is_present_in_the_full_projection(tmp_path: Path) -> None:
    paths = _corpus(tmp_path)

    payload = semantic_writes.evaluate_posthoc_batch(
        tmp_path, paths=paths, operation="audit"
    ).as_dict(detail="full")

    assert sum(payload["relation_disposition_summary"].values()) == 3
