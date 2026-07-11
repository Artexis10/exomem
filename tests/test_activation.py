from __future__ import annotations

import json
from pathlib import Path

from exomem import activation, attention, commands, review_state


def _write_page(
    vault: Path,
    name: str,
    body: str,
    *,
    page_type: str = "insight",
    status: str = "active",
    folder: str = "Notes/Insights",
) -> Path:
    path = vault / "Knowledge Base" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\nstatus: {status}\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_activation_corpus(vault: Path) -> dict[str, Path]:
    paths = {
        "disconnected": _write_page(vault, "disconnected", "## Finding\n\nMeasured result."),
        "generic": _write_page(
            vault,
            "generic",
            "## Overview\n\nSee [[Knowledge Base/Notes/Insights/typed]].",
        ),
        "typed": _write_page(
            vault,
            "typed",
            "## Relations\n\n- supports [[Knowledge Base/Notes/Insights/generic]]",
        ),
        "provenance": _write_page(
            vault,
            "provenance",
            "## Claim\n\n- relations: evidenced_by: [[Knowledge Base/Sources/source]]\n\nSupported claim.",
        ),
        "unknown": _write_page(
            vault,
            "unknown",
            "## Relations\n\n- science.replicates [[Knowledge Base/Notes/Insights/typed]]",
        ),
    }
    _write_page(
        vault,
        "raw",
        "## Finding\n\nRaw input.",
        page_type="source",
        folder="Sources",
    )
    _write_page(vault, "archived", "## Finding\n\nOld.", status="archived")
    _write_page(vault, "readonly", "## Finding\n\nProtected.", folder="Reference")
    (vault / "Knowledge Base/_access.yaml").write_text(
        "readonly:\n  - Reference\n", encoding="utf-8"
    )
    return paths


def test_scan_measures_coverage_and_four_structural_deficits(tmp_path: Path) -> None:
    paths = _seed_activation_corpus(tmp_path)

    report = activation.scan(tmp_path)
    by_category = {category: [] for category in activation.ACTIVATION_CATEGORIES}
    for finding in report.findings:
        by_category[finding.category].append(finding.path)

    assert report.coverage == {
        "eligible_pages": 5,
        "connected_pages": 4,
        "typed_relation_pages": 2,
        "generic_only_pages": 2,
        "disconnected_pages": 1,
        "provenance_candidate_pages": 2,
        "provenance_linked_pages": 1,
        "unregistered_relation_observations": 1,
    }
    assert paths["disconnected"].relative_to(tmp_path).as_posix() in by_category["relation_debt"]
    assert paths["generic"].relative_to(tmp_path).as_posix() in by_category["typed_relation_debt"]
    assert paths["disconnected"].relative_to(tmp_path).as_posix() in by_category["provenance_debt"]
    assert paths["unknown"].relative_to(tmp_path).as_posix() in by_category["unregistered_relation"]
    assert not any("Sources/" in finding.path or "Reference/" in finding.path for finding in report.findings)


def test_scan_is_deterministic_non_mutating_and_routes_existing_tools(tmp_path: Path) -> None:
    _seed_activation_corpus(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    first = activation.scan(tmp_path)
    second = activation.scan(tmp_path)
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert first == second
    assert after == before
    for finding in first.findings:
        assert finding.meta and finding.meta["signal_version"]
        assert finding.meta["next_actions"]
        assert {action["tool"] for action in finding.meta["next_actions"]} <= {
            "connect_memory",
            "read_memory",
            "schema_memory",
        }


def test_activation_queue_ranks_dedups_and_preserves_default_attention(tmp_path: Path) -> None:
    _seed_activation_corpus(tmp_path)

    default_categories = attention.ATTENTION_CATEGORIES
    activation_report = attention.activation(tmp_path, limit=0)

    assert attention.ATTENTION_CATEGORIES == default_categories
    assert activation_report.coverage["eligible_pages"] == 5
    assert len({item.path for item in activation_report.items}) == len(activation_report.items)
    disconnected = next(item for item in activation_report.items if item.path.endswith("disconnected.md"))
    assert disconnected.categories == ["provenance_debt", "relation_debt"]
    assert disconnected.score > next(
        item.score for item in activation_report.items if item.path.endswith("generic.md")
    )
    assert "quality" in disconnected.proposed_fix.lower()


def test_activation_only_item_supports_item_lookup_and_triage(tmp_path: Path) -> None:
    paths = _seed_activation_corpus(tmp_path)
    item = next(
        item
        for item in attention.activation(tmp_path, limit=0).items
        if item.path == paths["generic"].relative_to(tmp_path).as_posix()
    )
    assert item.categories == ["typed_relation_debt"]
    assert commands.op_review_memory(tmp_path, mode="item", ref=item.ref)["ref"] == item.ref

    dismissed = commands.op_triage_memory(tmp_path, ref=item.ref, action="dismiss")
    assert dismissed["state"] == "dismissed"
    assert item.ref not in {
        current.ref for current in attention.activation(tmp_path, limit=0).items
    }
    assert item.ref in {
        current.ref
        for current in attention.activation(tmp_path, limit=0, state="dismissed").items
    }

    paths["generic"].write_text(
        paths["generic"].read_text(encoding="utf-8") + "\nChanged context.\n",
        encoding="utf-8",
    )
    resurfaced = next(
        current
        for current in attention.activation(tmp_path, limit=0).items
        if current.ref == item.ref
    )
    assert resurfaced.fingerprint != item.fingerprint
    assert resurfaced.state == "open"
    assert review_state.state_path(tmp_path).exists()


def test_review_memory_activation_response_is_json_serializable(tmp_path: Path) -> None:
    _seed_activation_corpus(tmp_path)

    result = commands.op_review_memory(tmp_path, mode="activation", limit=2)

    assert result["shown"] == len(result["items"]) == 2
    assert result["truncated"] > 0
    assert result["coverage"]["eligible_pages"] == 5
    assert json.loads(json.dumps(result))["coverage"] == result["coverage"]


def test_expected_fingerprint_disambiguates_same_target_across_review_modes(
    tmp_path: Path, monkeypatch
) -> None:
    target_ref = "exomem://vault/shared"
    review_ref = review_state.review_ref(review_state.item_id(target_ref))
    attention_item = attention.AttentionItem(
        path="shared.md",
        score=1.0,
        severity="info",
        categories=["stale_review"],
        reasons=[],
        proposed_fix="Review.",
        item_id=review_state.item_id(target_ref),
        ref=review_ref,
        target_ref=target_ref,
        fingerprint="attention-fingerprint",
        state="open",
    )
    activation_item = attention.AttentionItem(
        path="shared.md",
        score=1.0,
        severity="info",
        categories=["relation_debt"],
        reasons=[],
        proposed_fix="Review.",
        item_id=review_state.item_id(target_ref),
        ref=review_ref,
        target_ref=target_ref,
        fingerprint="activation-fingerprint",
        state="open",
    )
    monkeypatch.setattr(
        attention,
        "attention",
        lambda *args, **kwargs: attention.AttentionReport(
            items=[attention_item],
            summary={},
            shown=1,
            total=1,
            truncated=0,
            upstream_truncated=0,
            note=None,
        ),
    )
    monkeypatch.setattr(
        attention,
        "activation",
        lambda *args, **kwargs: attention.AttentionReport(
            items=[activation_item],
            summary={},
            shown=1,
            total=1,
            truncated=0,
            upstream_truncated=0,
            note=None,
        ),
    )

    resolved = attention.item_by_ref(
        tmp_path,
        review_ref,
        expected_fingerprint="activation-fingerprint",
    )

    assert resolved.categories == ["relation_debt"]
