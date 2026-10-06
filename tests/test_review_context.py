from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from exomem import attention, epistemic_graph, review_context
from exomem import find as find_module

TARGET = "Knowledge Base/Notes/Insights/review-target.md"
RELATED = "Knowledge Base/Notes/Insights/related-note.md"
SOURCE = "Knowledge Base/Sources/reference-source.md"
EVIDENCE = "Knowledge Base/Evidence/Research/proof.md"
EXCLUDED = "Knowledge Base/Private/secret.md"


def _write(vault: Path, rel: str, content: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def review_item_vault(tmp_path: Path) -> tuple[Path, object]:
    vault = tmp_path / "vault"
    long_body = "Measured review context. " * 40
    _write(
        vault,
        TARGET,
        "---\n"
        "type: insight\n"
        "status: active\n"
        "updated: 2026-07-11\n"
        "api_token: must-not-leak\n"
        f'sources: ["[[{SOURCE.removesuffix(".md")}]]"]\n'
        f'evidence: ["[[{EVIDENCE.removesuffix(".md")}]]"]\n'
        "---\n"
        "# Review target\n\n"
        f"See [[{RELATED.removesuffix('.md')}]] and [[{EXCLUDED.removesuffix('.md')}]].\n\n"
        f"## Relations\n\n- research.echoes [[{RELATED.removesuffix('.md')}]]\n\n"
        f"{long_body}\n",
    )
    _write(
        vault,
        RELATED,
        "---\ntype: insight\nstatus: active\n---\n"
        "# Related note\n\n## Relations\n\n"
        f"- supports [[{TARGET.removesuffix('.md')}]]\n",
    )
    _write(vault, SOURCE, "---\ntype: source\n---\n# Source\n\nRaw source.\n")
    _write(vault, EVIDENCE, "---\ntype: evidence\n---\n# Proof\n\nProof text.\n")
    _write(vault, EXCLUDED, "---\ntype: insight\n---\n# Secret\n\nNever disclose.\n")
    _write(vault, "Knowledge Base/_access.yaml", "excluded:\n  - Private\n")
    _write(
        vault,
        "Knowledge Base/log.md",
        "# Activity log\n\n"
        f"## [2026-07-11] edit | {TARGET.removesuffix('.md')}\n"
        "Added measured context.\n",
    )
    find_module.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    item = next(
        candidate
        for candidate in attention.activation(vault, limit=0).items
        if candidate.path == TARGET
    )
    return vault, item


def test_review_context_contract_is_bounded_and_json_serializable(
    review_item_vault: tuple[Path, object],
) -> None:
    vault, item = review_item_vault

    result = review_context.assemble(
        vault,
        ref=item.ref,
        expected_fingerprint=item.fingerprint,
        max_body_chars=180,
        max_related_pages=1,
        max_graph_nodes=2,
        max_graph_edges=2,
        max_history=1,
        max_evolution_versions=2,
    )

    assert set(result) == {
        "item",
        "target",
        "related",
        "provenance",
        "graph",
        "history",
        "evolution",
        "availability",
        "truncation",
    }
    assert result["item"]["ref"] == item.ref
    assert result["target"]["path"] == TARGET
    assert result["target"]["body_truncated"] is True
    assert len(result["target"]["body"]) <= 180
    assert "api_token" not in result["target"]["frontmatter"]
    assert result["related"]["shown"] <= 1
    assert result["graph"]["shown_nodes"] <= 2
    assert result["graph"]["shown_edges"] <= 2
    assert all(node.get("ref") for node in result["graph"]["nodes"] if node.get("path"))
    assert all(
        edge.get("source_ref") for edge in result["graph"]["edges"] if edge.get("source_path")
    )
    assert result["history"]["shown"] <= 1
    assert SOURCE in {row["path"] for row in result["provenance"]["sources"]}
    assert EVIDENCE in {row["path"] for row in result["provenance"]["evidence"]}
    assert EXCLUDED not in json.dumps(result)
    assert json.loads(json.dumps(result))["item"]["ref"] == item.ref


def test_review_context_excludes_managed_attribution_before_clipping(
    review_item_vault: tuple[Path, object],
) -> None:
    from exomem import commands
    from exomem.governance import egress

    vault, _item = review_item_vault
    carrier = "<!-- exomem-origin:v2 " + "PRIVATE-ASSESSMENT " * 60 + "-->\n"
    snapshots = {}
    for path, heading in ((TARGET, "# Review target"), (RELATED, "# Related note")):
        content = (vault / path).read_text(encoding="utf-8")
        content = content.replace(heading, carrier + heading)
        content = content.replace(
            "status: active", "status: 'active <!-- exomem-origin:v2 PRIVATE-FIELD -->'"
        )
        _write(vault, path, content)
        snapshots[path] = (vault / path).read_bytes()
    find_module.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    item = next(
        candidate for candidate in attention.activation(vault, limit=0).items
        if candidate.path == TARGET
    )

    response = commands.op_review_item_context(vault, ref=item.ref, max_body_chars=180)
    response = egress.postfilter("review_item_context", response, vault)
    wire = json.dumps(response, default=str)
    assert "PRIVATE-ASSESSMENT" not in wire and "PRIVATE-FIELD" not in wire
    assert "Review target" in response["target"]["body"]
    assert any("Related note" in row["excerpt"] for row in response["related"]["items"])
    assert len(response["target"]["body"]) <= 180
    assert all((vault / path).read_bytes() == content for path, content in snapshots.items())


def test_review_context_rejects_a_stale_expected_fingerprint(
    review_item_vault: tuple[Path, object],
) -> None:
    vault, item = review_item_vault

    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        review_context.assemble(
            vault,
            ref=item.ref,
            expected_fingerprint="0" * 24,
        )


def test_review_context_soft_fails_one_optional_section(
    review_item_vault: tuple[Path, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, item = review_item_vault

    def _broken_graph(*args, **kwargs):
        raise RuntimeError("graph offline")

    monkeypatch.setattr(epistemic_graph, "graph_context", _broken_graph)
    result = review_context.assemble(vault, ref=item.ref)

    assert result["target"]["path"] == TARGET
    assert result["graph"] == {
        "available": False,
        "reason": "graph offline",
        "nodes": [],
        "edges": [],
        "shown_nodes": 0,
        "shown_edges": 0,
        "truncated_nodes": 0,
        "truncated_edges": 0,
    }
    assert result["availability"]["graph"] is False


def test_review_context_does_not_mutate_vault(
    review_item_vault: tuple[Path, object],
) -> None:
    vault, item = review_item_vault
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    first = review_context.assemble(vault, ref=item.ref)
    second = review_context.assemble(vault, ref=item.ref)
    after = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    assert first == second
    assert after == before


def test_review_context_rejects_an_excluded_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _write(vault, EXCLUDED, "---\ntype: insight\n---\n# Secret\n\nNever disclose.\n")
    _write(vault, "Knowledge Base/_access.yaml", "excluded:\n  - Private\n")
    item = attention.AttentionItem(
        path=EXCLUDED,
        score=1.0,
        severity="info",
        categories=["relation_debt"],
        reasons=[],
        proposed_fix="Review only.",
        item_id="a" * 24,
        ref=f"exomem://review/{'a' * 24}",
        target_ref="exomem://vault/Knowledge%20Base/Private/secret.md",
        fingerprint="b" * 24,
        state="open",
    )
    monkeypatch.setattr(attention, "item_by_ref", lambda *args, **kwargs: item)

    with pytest.raises(ValueError, match="PERMISSION_DENIED"):
        review_context.assemble(vault, ref=item.ref)


def test_review_context_default_latency_and_target_read_budget(
    review_item_vault: tuple[Path, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, item = review_item_vault
    review_context.assemble(vault, ref=item.ref)  # warm parsed and graph caches

    original = review_context.get_page.get_page
    reads: list[str] = []

    def _counted_get(vault_root: Path, *, path: str):
        reads.append(path)
        return original(vault_root, path=path)

    monkeypatch.setattr(review_context.get_page, "get_page", _counted_get)
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        review_context.assemble(vault, ref=item.ref)
        samples.append((time.perf_counter() - started) * 1000)

    assert reads.count(TARGET) == 5
    assert len(reads) <= 5 * (1 + 8), reads
    assert statistics.median(samples) < 500, samples


def test_review_context_resolves_each_reference_path_once_per_assembly(
    review_item_vault: tuple[Path, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, item = review_item_vault
    calls: list[str] = []
    ref_for_path = review_context.memory_refs.ReferenceIndex.ref_for_path

    def counted_ref_for_path(self, path: str):
        calls.append(path)
        return ref_for_path(self, path)

    monkeypatch.setattr(
        review_context.memory_refs.ReferenceIndex,
        "ref_for_path",
        counted_ref_for_path,
    )

    review_context.assemble(vault, ref=item.ref)

    assert len(calls) == len(set(calls)), calls


@pytest.mark.parametrize("audience", ["client-a", "owner"])
def test_review_context_withholds_a_code_pushed_carrier_only_from_a_restricted_reader(
    review_item_vault: tuple[Path, object], audience: str
) -> None:
    """A restricted reader never gets a carrier's payload; the owner reads its code as written."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands, writer_lease
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    vault, _item = review_item_vault
    carrier = "<!-- exomem-origin:v1 PRIVATE-ASSESSMENT -->"
    content = (vault / TARGET).read_text(encoding="utf-8")
    _write(vault, TARGET, content.replace("# Review target\n\n", f"# Review target\n\nPara.\n\n    {carrier}\n\n", 1))
    _write_source_rule(vault, ceiling=0)  # a configured policy makes client-a a restricted audience
    find_module.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    item = next(c for c in attention.activation(vault, limit=0).items if c.path == TARGET)
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "review_item_context")
    who = (
        owner_principal(surface="mcp")
        if audience == "owner"
        else RequestPrincipal(audience_id=audience, surface="mcp")
    )

    with request_scope(who):
        result = writer_lease.invoke_command(command, vault, ref=item.ref)

    assert "Review target" in result["target"]["body"]
    assert (carrier in result["target"]["body"]) is (audience == "owner")
    if audience != "owner":
        assert "PRIVATE-ASSESSMENT" not in json.dumps(result, default=str)
