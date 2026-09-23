"""Counts-only relation-quality census over one published graph snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import (
    commands,
    doctor,
    epistemic_graph,
    find_corpus,
    relation_census,
    relation_registry,
    semantic_units,
)
from exomem.__main__ import main
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope

NOTES = "Knowledge Base/Notes"
PEOPLE = "Knowledge Base/Entities/People"
ORGS = "Knowledge Base/Entities/Organizations"
SOURCE = "Knowledge Base/Sources/Articles/source-one"

_REGISTRY = """\
schema_version: 1
extensions:
  vault.applies_to:
    parent: relates_to
    description: A synthetic applicability relation.
    direction: directed
    aliases: [applies_to]
  vault.retired:
    parent: relates_to
    description: A retired synthetic relation.
    direction: directed
    status: deprecated
    replaced_by: vault.applies_to
  vault.scoped:
    parent: supports
    description: A synthetic claim-only support relation.
    direction: directed
    source_kinds: [claim]
  vault.unused_example:
    parent: relates_to
    description: A synthetic relation nobody uses.
    direction: symmetric
"""


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _page(page_type: str, title: str, body: str, *, created: str | None = None, extra: str = "") -> str:
    created_line = f"created: {created}\n" if created else ""
    return f"---\ntype: {page_type}\n{created_line}{extra}---\n# {title}\n\n{body}\n"


def _seed_census_vault(vault: Path) -> Path:
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", _REGISTRY)
    _write(
        vault,
        f"{SOURCE}.md",
        "---\ntype: source\ncaptured: 2026-01-01\n---\n# Source one\n\nSource text.\n",
    )
    _write(
        vault,
        f"{NOTES}/alpha.md",
        _page(
            "insight",
            "Alpha Quokka Note",
            f"See [[{NOTES}/delta]].\n\n"
            f"- supports [[{NOTES}/beta]]\n"
            f"- relates_to [[{NOTES}/gamma]]\n"
            f"- vault.applies_to [[{NOTES}/beta]]\n"
            f"- applies_to [[{NOTES}/gamma]]",
            created="2026-01-10",
        ),
    )
    _write(
        vault,
        f"{NOTES}/beta.md",
        _page(
            "insight",
            "Beta Note",
            f"- relates_to [[{NOTES}/alpha]]\n"
            f"- zebra.label: [[{NOTES}/gamma]]\n"
            f"- vault.retired [[{NOTES}/alpha]]\n"
            f"- vault.scoped [[{NOTES}/gamma]]",
            created="2026-02-10",
        ),
    )
    _write(
        vault,
        f"{NOTES}/gamma.md",
        _page(
            "insight",
            "Gamma Note",
            "Plain body.",
            created="2026-03-10",
            extra=f'sources:\n  - "[[{SOURCE}]]"\n',
        ),
    )
    _write(
        vault,
        f"{NOTES}/delta.md",
        _page("insight", "Delta Note", f"Links [[{NOTES}/alpha]] and [[{NOTES}/target-only]]."),
    )
    _write(vault, f"{NOTES}/target-only.md", _page("insight", "Target Only", "Nothing outbound."))
    _write(vault, f"{NOTES}/lonely.md", _page("insight", "Lonely Note", "No links here."))
    _write(vault, f"{NOTES}/index.md", _page("insight", "Index", f"[[{NOTES}/alpha]]"))
    _write(
        vault,
        f"{PEOPLE}/ada-example.md",
        "---\ntype: entity\nentity_type: person\n---\n# Ada Example\n\n"
        f"- relates_to [[{ORGS}/example-org]]\n",
    )
    _write(
        vault,
        f"{ORGS}/example-org.md",
        "---\ntype: entity\nentity_type: organization\n---\n# Example Org\n\n"
        f"- depends_on [[{ORGS}/other-org]]\n",
    )
    _write(
        vault,
        f"{ORGS}/other-org.md",
        "---\ntype: entity\nentity_type: organization\n---\n# Other Org\n\nPlain.\n",
    )
    return vault


def _built(vault: Path) -> Path:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault


@pytest.fixture
def census_vault(tmp_path: Path) -> Path:
    return _built(_seed_census_vault(tmp_path / "vault"))


def test_census_reads_one_snapshot_and_parses_no_markdown(
    census_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    original = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def counting(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        connection = original(self, *args, **kwargs)
        opened.append(connection)
        return connection

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the census must not parse Markdown")

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", counting)
    monkeypatch.setattr(semantic_units, "parse_semantic_units", refuse)
    monkeypatch.setattr(find_corpus, "parse_page", refuse)

    result = relation_census.census(census_vault)

    assert result["available"] is True
    assert len(opened) == 1


def test_generic_share_typed_and_specific_coverage_on_fixture(census_vault: Path) -> None:
    result = relation_census.census(census_vault)
    metrics = result["metrics"]

    assert result["cohort"]["eligible_pages"] == 9
    assert metrics["authored_edges"] == 10
    assert metrics["by_status"] == {
        "core_specific": 2,
        "core_generic": 3,
        "extension": 1,
        "alias": 1,
        "deprecated": 1,
        "unregistered": 1,
        "scope_violation": 1,
    }
    # 3 generic of the 9 registered authored edges.
    assert metrics["generic_share"] == pytest.approx(0.3333)
    assert metrics["typed_coverage"] == {"pages": 5, "ratio": pytest.approx(0.5556)}
    assert metrics["specific_coverage"] == {"pages": 4, "ratio": pytest.approx(0.4444)}
    assert metrics["predicate_utilisation"] == {
        "core_keys_used": 3,
        "core_keys": 28,
        "extension_keys_used": 3,
        "extension_keys": 4,
        "top3_edges": 6,
        "registered_edges": 9,
        "top3_share": pytest.approx(0.6667),
    }
    assert metrics["extension_use"] == {
        "standing": "unmeasured",
        "registered": 4,
        "deprecated": 1,
        "used": 3,
        "edges": 4,
        "aliases_in_use": 1,
    }
    assert metrics["unregistered_pressure"] == {
        "distinct_labels": 1,
        "edges": 1,
        "labels_at_three_or_more_pages": 0,
    }
    assert metrics["inverse_duplicates"] == {"applicable": 0, "pairs": 0}
    assert metrics["near_duplicate_groups"] == "unmeasured"
    assert metrics["false_precision_judged"] == "unmeasured"


def test_disconnected_counts_typed_wikilink_and_sources_origins(census_vault: Path) -> None:
    # delta is connected only by wikilinks and gamma only by frontmatter
    # sources; target-only, lonely and other-org author nothing outbound, and
    # only lonely has nothing inbound either.
    metrics = relation_census.census(census_vault)["metrics"]

    assert metrics["disconnected"] == {"pages": 3, "isolated": 1}


def test_entity_coverage_counts_persons_linked_only_by_relates_to(census_vault: Path) -> None:
    result = relation_census.census(census_vault)

    assert result["cohort"]["entity_pages"] == 3
    assert result["metrics"]["entity_coverage"] == {
        "entity_pages": 3,
        "with_specific_entity_edge": 1,
        "entity_edges": {
            "core_specific": 1,
            "core_generic": 1,
            "extension": 0,
            "alias": 0,
            "deprecated": 0,
            "unregistered": 0,
            "scope_violation": 0,
        },
        "only_generic_entity_edges": 1,
        "persons": 1,
        "persons_without_affiliation": 1,
        "without_inbound": 1,
    }


def _seed_check_vault(vault: Path) -> Path:
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", _REGISTRY)
    _write(
        vault,
        f"{SOURCE}.md",
        "---\ntype: source\ncaptured: 2026-01-01\n---\n# Source one\n\nSource text.\n",
    )
    _write(vault, f"{NOTES}/older.md", _page("insight", "Older", "Text.", created="2026-01-01"))
    _write(
        vault,
        f"{NOTES}/newer.md",
        _page(
            "insight",
            "Newer",
            "Text.",
            created="2026-06-01",
            extra=f'supersedes: "[[{NOTES}/older]]"\n',
        ),
    )
    _write(
        vault, f"{NOTES}/late-original.md", _page("insight", "Late", "Text.", created="2026-05-01")
    )
    _write(
        vault,
        f"{NOTES}/early-replacement.md",
        _page(
            "insight",
            "Early",
            "Text.",
            created="2026-02-01",
            extra=f'supersedes: "[[{NOTES}/late-original]]"\n',
        ),
    )
    _write(vault, f"{NOTES}/plain.md", _page("insight", "Plain", "Text.", created="2026-01-01"))
    _write(
        vault,
        f"{NOTES}/question-page.md",
        _page("insight", "Question", "## Open Question\n\nWhat holds?", created="2026-01-01"),
    )
    _write(
        vault,
        f"{NOTES}/evidence-user.md",
        _page(
            "insight",
            "Evidence user",
            f"- evidenced_by [[{SOURCE}]]\n"
            f"- evidenced_by [[{NOTES}/plain]]\n"
            f"- evidenced_by [[{NOTES}/missing-page]]\n"
            f"- answers [[{NOTES}/question-page]]\n"
            f"- answers [[{NOTES}/plain]]\n"
            f"- depends_on [[{NOTES}/mutual]]\n"
            f"- contradicts [[{NOTES}/plain]]\n"
            f"- vault.scoped [[{NOTES}/plain]]",
            created="2026-01-01",
        ),
    )
    _write(
        vault,
        f"{NOTES}/mutual.md",
        _page(
            "insight", "Mutual", f"- depends_on [[{NOTES}/evidence-user]]", created="2026-01-01"
        ),
    )
    _write(
        vault,
        f"{NOTES}/self-ref.md",
        _page(
            "insight",
            "Self ref",
            "## Claim\n\n"
            f"- relations: supports: [[{NOTES}/self-ref]]\n"
            f"- relations: vault.scoped: [[{NOTES}/plain]]\n\n"
            "A claim body.",
            created="2026-01-01",
        ),
    )
    _write(vault, f"{NOTES}/chain-v1.md", _page("insight", "Chain v1", "Text.", created="2026-01-01"))
    _write(
        vault,
        f"{NOTES}/chain-v2.md",
        _page(
            "insight",
            "Chain v2",
            f"- contradicts [[{NOTES}/chain-v1]]",
            created="2026-07-01",
            extra=f'supersedes: "[[{NOTES}/chain-v1]]"\n',
        ),
    )
    return vault


def test_each_structural_check_reports_its_denominator(tmp_path: Path) -> None:
    vault = _built(_seed_check_vault(tmp_path / "vault"))

    checks = relation_census.census(vault)["checks"]

    assert checks == {
        # evidence-user's vault.scoped from a page violates source_kinds=[claim];
        # self-ref's claim-block use of it is in scope.
        "signature_mismatch": {"applicable": 2, "violations": 1},
        # early-replacement (2026-02-01) claims to supersede a newer page.
        "supersedes_backwards": {"applicable": 3, "violations": 1},
        # A Source page passes, a plain page fails, a missing page cannot apply.
        "evidence_target_not_evidential": {"applicable": 2, "violations": 1},
        "answers_without_question": {"applicable": 2, "violations": 1},
        # Three supersessions, two contradictions and three support-family
        # edges; self-ref's claim supports its own page.
        "same_page_epistemic": {"applicable": 8, "violations": 1},
        "directed_both_ways": {"applicable": 11, "violations": 2, "inspection_only": True},
        # chain-v2 contradicts the version it supersedes.
        "contradicts_within_chain": {"applicable": 2, "violations": 1},
    }


def test_inverse_duplicates_count_registered_inverse_pairs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault, f"{NOTES}/whole.md", _page("insight", "Whole", f"- contains [[{NOTES}/piece]]"))
    _write(vault, f"{NOTES}/piece.md", _page("insight", "Piece", f"- part_of [[{NOTES}/whole]]"))
    _write(vault, f"{NOTES}/other.md", _page("insight", "Other", f"- part_of [[{NOTES}/whole]]"))

    metrics = relation_census.census(_built(vault))["metrics"]

    # Three inverse-bearing edges; whole/piece is one fact stated twice.
    assert metrics["inverse_duplicates"] == {"applicable": 3, "pairs": 1}


def test_counts_mode_emits_no_path_title_or_vault_key(census_vault: Path) -> None:
    counts = json.dumps(relation_census.census(census_vault), sort_keys=True)
    keys = json.dumps(relation_census.census(census_vault, detail="keys"), sort_keys=True)

    for token in (
        "Knowledge Base",
        "Notes/",
        ".md",
        "Quokka",
        "Alpha",
        "vault.",
        "applies_to",
        "zebra",
        "ada-example",
        "example-org",
    ):
        assert token not in counts, token
    assert "vault.applies_to" in keys
    assert "vault.unused_example" in keys
    assert "Knowledge Base" not in keys
    assert "Quokka" not in keys
    assert "zebra" not in keys


def test_census_is_byte_identical_for_one_generation_and_registry(census_vault: Path) -> None:
    first = relation_census.census(census_vault)
    second = relation_census.census(census_vault)
    registry = relation_registry.load_registry(census_vault)

    assert json.dumps(first) == json.dumps(second)
    assert first["census_version"] == relation_census.CENSUS_VERSION
    assert isinstance(first["graph_generation"], int)
    assert first["registry"] == {
        "core_version": registry.core_version,
        "extension_hash": registry.extension_hash,
    }


_WITHHELD = f"{NOTES}/Withheld/secret"


def _governed_twins(tmp_path: Path) -> tuple[Path, Path]:
    """Two vaults identical except for one withheld page and its edges."""
    twins = []
    for name, with_secret in (("visible-only", False), ("with-secret", True)):
        vault = _seed_census_vault(tmp_path / name)
        _write(
            vault,
            f"{NOTES}/pointer.md",
            _page("insight", "Pointer", f"- supports [[{_WITHHELD}]]", created="2026-04-01"),
        )
        if with_secret:
            _write(
                vault,
                f"{_WITHHELD}.md",
                _page(
                    "insight",
                    "Secret",
                    f"Mentions [[{NOTES}/lonely]].\n\n"
                    f"- supports [[{NOTES}/alpha]]\n"
                    f"- zebra.label: [[{NOTES}/lonely]]\n"
                    f"- relates_to [[{PEOPLE}/ada-example]]",
                    created="2026-04-02",
                ),
            )
        twins.append(_built(vault))
    return twins[0], twins[1]


def _withhold(vault: Path) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Withheld\n"
        'paths: ["Notes/Withheld/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "withheld-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )


def _reset_governance_caches() -> None:
    from exomem import find as find_module
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def test_counts_follow_the_callers_walk_filter(tmp_path: Path) -> None:
    visible_only, with_secret = _governed_twins(tmp_path)

    def keep(path: str) -> bool:
        return "/Withheld/" not in path

    restricted_twin = relation_census.census(visible_only, keep=keep)
    restricted = relation_census.census(with_secret, keep=keep)
    owner = relation_census.census(with_secret)

    # Nothing the withheld page authored, names or receives changes the view.
    assert json.dumps(restricted) == json.dumps(restricted_twin)
    assert json.dumps(
        relation_census.census(with_secret, keep=keep, detail="keys")
    ) == json.dumps(relation_census.census(visible_only, keep=keep, detail="keys"))
    # pointer's only edge names the withheld page, so it reads as isolated.
    assert restricted["metrics"]["disconnected"] == {"pages": 4, "isolated": 2}
    assert restricted["graph_generation"] is None
    assert owner["metrics"]["authored_edges"] > restricted["metrics"]["authored_edges"]
    assert owner["cohort"]["eligible_pages"] == restricted["cohort"]["eligible_pages"] + 1

    # The tool applies the caller's own release filter, inside the walk.
    for vault in (visible_only, with_secret):
        _withhold(vault)
    _reset_governance_caches()
    external = RequestPrincipal(audience_id="external", surface="mcp")
    views = []
    for vault in (visible_only, with_secret):
        with request_scope(external):
            views.append(
                commands.op_schema_memory(vault, operation="census", subject="relations")
            )
        _reset_governance_caches()
    assert json.dumps(views[0]) == json.dumps(views[1])
    assert views[1]["metrics"] == restricted["metrics"]


def test_a_withheld_target_reads_exactly_like_a_missing_one(tmp_path: Path) -> None:
    """A visible page names a target by bare title. When the target exists it
    resolves into the withheld folder; when it does not, the graph keeps a
    placeholder at another path. A restricted caller must not tell them apart."""
    views = []
    for name, exists in (("missing", False), ("withheld", True)):
        vault = _seed_census_vault(tmp_path / name)
        _write(
            vault,
            f"{NOTES}/pointer.md",
            _page("insight", "Pointer", "- supports [[secret-plan]]", created="2026-04-01"),
        )
        if exists:
            _write(vault, f"{_WITHHELD}-plan.md", _page("insight", "Secret plan", "Text."))
        views.append(
            relation_census.census(_built(vault), keep=lambda path: "/Withheld/" not in path)
        )

    assert json.dumps(views[0]) == json.dumps(views[1])
    # Neither view counts the row as a connection; both count it as a row whose
    # target lies outside the caller's view.
    assert views[0]["metrics"]["unresolved_target_edges"] == 1
    assert views[0]["metrics"]["disconnected"] == {"pages": 4, "isolated": 2}


def test_unavailable_graph_reports_unavailable_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unbuilt = _seed_census_vault(tmp_path / "unbuilt")

    result = relation_census.census(unbuilt)

    assert result["available"] is False
    assert result["reason"] == "graph_unavailable"
    assert "metrics" not in result and "cohort" not in result and "checks" not in result
    assert not epistemic_graph.sidecar_path(unbuilt).exists()

    built = _built(_seed_census_vault(tmp_path / "built"))
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    disabled = commands.op_schema_memory(built, operation="census", subject="relations")
    assert disabled["available"] is False
    assert "metrics" not in disabled


def test_census_operation_accepts_only_detail_and_date_scope(census_vault: Path) -> None:
    scoped = commands.op_schema_memory(
        census_vault,
        operation="census",
        subject="relations",
        detail="keys",
        date_from="2026-02-01",
        date_to="2026-12-31",
    )
    assert scoped["detail"] == "keys"
    # beta and gamma are dated inside the window; alpha is older and the
    # remaining six pages are undated.
    assert scoped["cohort"]["eligible_pages"] == 2
    assert scoped["cohort"]["outside_scope"] == 1
    assert scoped["cohort"]["undated"] == 6

    with pytest.raises(ValueError, match="INVALID_RELATION_ARGUMENT"):
        commands.op_schema_memory(
            census_vault, operation="census", subject="relations", save=True
        )
    with pytest.raises(ValueError, match="INVALID_SCHEMA_ARGUMENT"):
        commands.op_schema_memory(
            census_vault, operation="infer", subject="relations", detail="keys"
        )


def test_cli_census_reads_the_local_snapshot_without_a_service(
    census_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)

    exit_code = main(["relations", "census", "--json", "--vault", str(census_vault)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["served_by"] == "local-snapshot"
    assert payload["metrics"]["authored_edges"] == 10


def test_cli_census_asks_a_running_managed_service_first(
    census_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served = relation_census.census(census_vault, detail="keys")
    calls: list[str] = []

    def from_service(detail: str) -> dict:
        calls.append(detail)
        return served

    def refuse_local(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a running service answers; the CLI must not open the sidecar")

    monkeypatch.setattr(relation_census, "service_census", from_service)
    monkeypatch.setattr(relation_census, "census", refuse_local)

    exit_code = main(["relations", "census", "--json", "--keys"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls == ["keys"]
    assert payload["served_by"] == "service"
    assert payload["metrics"] == served["metrics"]


def test_service_census_falls_back_on_any_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from exomem import install_info

    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    assert relation_census.service_census("counts") is None

    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-key")
    monkeypatch.setattr(
        install_info, "report", lambda: {"managed_service_target": "http://127.0.0.1:9"}
    )
    posted: list[tuple[str, dict]] = []

    class Response:
        def __init__(self, status: int, body: dict) -> None:
            self.status_code = status
            self._body = body

        def json(self) -> dict:
            return self._body

    answers = iter(
        [
            Response(400, {"success": False}),
            Response(200, {"success": False, "error": {"code": "INVALID_SCHEMA_OPERATION"}}),
            Response(200, {"success": True, "data": {"census_version": 1, "available": True}}),
        ]
    )

    def post(url: str, **kwargs):  # noqa: ANN003, ANN202
        posted.append((url, kwargs["json"]))
        return next(answers)

    monkeypatch.setattr(httpx, "post", post)

    assert relation_census.service_census("counts") is None
    assert relation_census.service_census("counts") is None
    assert relation_census.service_census("keys") == {"census_version": 1, "available": True}
    assert posted[-1] == (
        "http://127.0.0.1:9/api/schema_memory",
        {"subject": "relations", "operation": "census", "detail": "keys"},
    )


def test_doctor_reports_one_census_line(census_vault: Path) -> None:
    check = doctor._check_relation_census(census_vault)

    assert check.id == "relations.census"
    assert check.status == "pass"
    assert "\n" not in check.message
    assert "generic share 33.3%" in check.message
    assert "3 of 9 eligible pages disconnected" in check.message


def test_doctor_census_line_reports_an_unavailable_graph(tmp_path: Path) -> None:
    check = doctor._check_relation_census(_seed_census_vault(tmp_path / "vault"))

    assert check.id == "relations.census"
    assert check.status == "warn"
    assert "unavailable" in check.message
