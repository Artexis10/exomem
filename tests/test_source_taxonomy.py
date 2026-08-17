"""Acceptance for the open source taxonomy and its filesystem projection.

The motivating defect: source classification was a closed vocabulary scraped by
regex out of a markdown table whose token pattern could not express a hyphen. An
artifact whose kind and subject were both obvious — a research report about
travel — had exactly one legal destination, the `other` fallback, and real vaults
then grew subject folders underneath that fallback. This suite binds the fix:

- kind and domain are independently open, and a previously unseen meaningful
  value works with no code change and no migration;
- the location is a deterministic projection of the canonical keys, so the
  directory tree is derived from the model rather than being the model;
- `other` means low confidence, never missing vocabulary;
- every legacy kind and every already-captured path keeps working untouched;
- open vocabulary does not weaken filesystem safety.

Every fixture here is synthetic.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import commands, mutation_terminal
from exomem import schema as schema_module
from exomem import source_taxonomy as st

TODAY = dt.date(2026, 8, 17)
KB = "Knowledge Base"


def _capture(
    vault: Path, source_schema: schema_module.SourceSchema, **kwargs: object
) -> add_module.AddResult:
    kwargs.setdefault("content", "Body text for a captured source.")
    kwargs.setdefault("today", TODAY)
    return add_module.add(vault, source_schema, **kwargs)  # type: ignore[arg-type]


def _committed_envelope(leaf: object) -> dict:
    """The compact envelope an MCP/REST/CLI caller actually receives."""
    terminal = mutation_terminal.committed_terminal(
        leaf, request_id="r", receipt_id=None, idempotency_key=None
    )
    return mutation_terminal.project_terminal(terminal, "compact")


def _frontmatter(vault: Path, rel_path: str) -> dict:
    text = (vault / rel_path).read_text(encoding="utf-8")
    front, _, _ = text.partition("\n---\n")
    return yaml.safe_load(front.removeprefix("---\n"))


# ---------------------------------------------------------------------------
# Positive journeys — the artifacts that used to be forced into `other`
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source_type", "domain", "title", "expected_dir", "url"),
    [
        (
            "research-report",
            "travel",
            "Autumn airfare investigation",
            f"{KB}/Sources/Reports/Travel",
            None,
        ),
        (
            "paper",
            "health",
            "Sleep latency meta-analysis",
            f"{KB}/Sources/Papers/Health",
            "https://example.com/sleep-latency",
        ),
        (
            "invoice-receipt",
            "equipment",
            "Microphone stand order",
            f"{KB}/Sources/Invoices/Equipment",
            None,
        ),
        (
            "official-guidance",
            "travel",
            "Entry requirements bulletin",
            f"{KB}/Sources/Official Guidance/Travel",
            None,
        ),
    ],
)
def test_classified_captures_never_land_in_the_fallback(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    source_type: str,
    domain: str,
    title: str,
    expected_dir: str,
    url: str | None,
) -> None:
    result = _capture(
        vault, source_schema, source_type=source_type, domain=domain, title=title, url=url
    )
    assert result.path.startswith(f"{expected_dir}/")
    assert f"{KB}/Sources/Other" not in result.path
    assert (vault / result.path).exists()


def test_the_motivating_case_end_to_end(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A research report about travel, belonging to a user project."""
    result = _capture(
        vault,
        source_schema,
        source_type="research-report",
        domain="travel",
        projects=["california-trip-2026"],
        title="Autumn airfare investigation",
    )
    assert result.path == (
        f"{KB}/Sources/Reports/Travel/2026-08-17-autumn-airfare-investigation.md"
    )
    front = _frontmatter(vault, result.path)
    assert front["type"] == "source"
    assert front["source_type"] == "research-report"
    assert front["domain"] == "travel"
    assert front["projects"] == ["california-trip-2026"]
    assert front["ingested_into"] == []
    # No advisory: this capture is classified, so there is nothing to advise on.
    assert result.structure_suggestion is None


# ---------------------------------------------------------------------------
# Open vocabulary
# ---------------------------------------------------------------------------
def test_a_previously_unseen_kind_and_domain_both_work(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault,
        source_schema,
        source_type="field-notebook",
        domain="agroforestry",
        title="Hedgerow establishment notes",
    )
    assert result.path == (
        f"{KB}/Sources/Field Notebook/Agroforestry/"
        "2026-08-17-hedgerow-establishment-notes.md"
    )
    front = _frontmatter(vault, result.path)
    assert front["source_type"] == "field-notebook"
    assert front["domain"] == "agroforestry"


def test_an_unseen_value_registers_itself_in_the_capture_batch(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _capture(
        vault,
        source_schema,
        source_type="field-notebook",
        domain="marine-biology",
        title="Kelp canopy survey",
    )
    registry = yaml.safe_load(st.registry_path(vault).read_text(encoding="utf-8"))
    assert "field-notebook" in registry["source_kinds"]
    assert "marine-biology" in registry["domains"]
    # And it resolves as registered on the next capture rather than unregistered.
    assert st.load_taxonomy(vault).resolve_kind("field-notebook").status == "registered"


def test_an_unseen_kind_on_an_established_domain_works(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault,
        source_schema,
        source_type="research-report",
        domain="marine-biology",
        title="Kelp canopy survey synthesis",
    )
    assert result.path.startswith(f"{KB}/Sources/Reports/Marine Biology/")


def test_registered_aliases_resolve_to_the_canonical_key(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault, source_schema, source_type="deep-research", title="Alias routed report"
    )
    assert result.path.startswith(f"{KB}/Sources/Reports/")
    assert _frontmatter(vault, result.path)["source_type"] == "research-report"


def test_a_near_miss_kind_is_refused_and_names_its_correction(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    taxonomy = st.core_taxonomy()
    with pytest.raises(st.TaxonomyTypoError) as exc:
        taxonomy.resolve_kind("articl")
    assert exc.value.close_match == "article"
    assert "source-taxonomy.yaml" in str(exc.value)
    with pytest.raises(add_module.AddError):
        _capture(vault, source_schema, source_type="articl", title="Typo kind")


def test_a_near_miss_domain_warns_instead_of_refusing(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """`wealth` beside `health` is a distinct word, not a typo.

    Kinds are a small, product-seeded vocabulary where a one-character
    difference is nearly always a mistake. Domains are an open single-word space
    where near-misses are frequently legitimate, and refusing a capture over one
    would make preserving material burdensome.
    """
    result = _capture(
        vault, source_schema, source_type="article", domain="wealth",
        title="Household balance sheet primer", url="https://example.com/primer",
    )
    assert result.path.startswith(f"{KB}/Sources/Articles/Wealth/")
    assert any("DOMAIN_NEAR_MISS" in warning for warning in result.warnings)
    assert any("health" in warning for warning in result.warnings)


def test_registration_is_always_reported(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault, source_schema, source_type="field-notebook", title="Quiet new kind"
    )
    assert any("NEW_SOURCE_KIND" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def test_canonical_key_and_path_segment_are_separate_concerns() -> None:
    resolution = st.core_taxonomy().resolve_kind("research-report")
    assert resolution.key == "research-report"
    assert resolution.label == "Research report"
    assert resolution.path_label == "Reports"


def test_omitting_the_domain_omits_a_level(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with_domain = _capture(
        vault, source_schema, source_type="book", domain="food", title="Ferment primer"
    )
    without = _capture(
        vault, source_schema, source_type="book", title="Bread primer"
    )
    assert with_domain.path.startswith(f"{KB}/Sources/Books/Food/")
    assert without.path.startswith(f"{KB}/Sources/Books/2026-")


def test_projection_is_deterministic_and_bounded() -> None:
    taxonomy = st.core_taxonomy()
    kind = taxonomy.resolve_kind("research-report")
    domain = taxonomy.resolve_domain("travel")
    assert st.source_segments(kind, domain) == st.source_segments(kind, domain)
    assert st.source_segments(kind, domain) == ("Sources", "Reports", "Travel")
    assert len(st.source_segments(kind, domain)) - 1 == st.MAX_PROJECTION_DEPTH
    assert len(st.source_segments(kind)) - 1 == 1


def test_an_unregistered_key_derives_its_own_labels() -> None:
    resolution = st.core_taxonomy().resolve_domain("marine-biology")
    assert resolution.status == "unregistered"
    assert resolution.label == "Marine biology"
    assert resolution.path_label == "Marine Biology"


# ---------------------------------------------------------------------------
# The fallback contract
# ---------------------------------------------------------------------------
def test_an_unclassified_capture_still_succeeds(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(vault, source_schema, title="A loose thought")
    assert result.path.startswith(f"{KB}/Sources/Other/")
    assert _frontmatter(vault, result.path)["source_type"] == "other"


def test_a_confidently_supplied_unknown_kind_is_never_demoted(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The defect in one assertion.

    `research-report` was not in the closed vocabulary, so it became `other`.
    Being unfamiliar is not the same as being unclassifiable.
    """
    result = _capture(
        vault, source_schema, source_type="research-report", domain="travel",
        title="Autumn airfare investigation",
    )
    assert _frontmatter(vault, result.path)["source_type"] != st.FALLBACK_KIND
    assert f"{KB}/Sources/Other" not in result.path


def test_the_fallback_with_a_domain_still_nests_under_the_fallback(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """Legacy-compatible by construction: this is the shape vaults already grew."""
    result = _capture(
        vault, source_schema, source_type="other", domain="travel", title="Loose travel note"
    )
    assert result.path.startswith(f"{KB}/Sources/Other/Travel/")


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("legacy_kind", "folder", "url"),
    [
        ("article", "Articles", "https://example.com/a"),
        ("session", "Sessions", None),
        ("book", "Books", None),
        ("paper", "Papers", "https://example.com/p"),
        ("video", "Videos", "https://example.com/v"),
        ("other", "Other", None),
    ],
)
def test_every_legacy_kind_routes_exactly_where_it_used_to(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    legacy_kind: str,
    folder: str,
    url: str | None,
) -> None:
    result = _capture(
        vault, source_schema, source_type=legacy_kind, title=f"Legacy {legacy_kind}", url=url
    )
    assert result.path == f"{KB}/Sources/{folder}/2026-08-17-legacy-{legacy_kind}.md"
    assert add_module.SOURCE_TYPE_TO_FOLDER[legacy_kind] == folder


def test_an_existing_source_is_not_required_to_match_the_projection(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A pre-existing fallback capture stays valid, indexed, and retrievable.

    Compiled notes enforce a path/type correspondence; sources deliberately do
    not, because that invariant would mark every legacy `Sources/Other/<Subject>/`
    page as violating and manufacture pressure to move append-only,
    provenance-bearing files.
    """
    legacy_dir = vault / KB / "Sources" / "Other" / "Travel"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy = legacy_dir / "2026-01-05-legacy-fallback-capture.md"
    legacy.write_text(
        "---\ntype: source\nsource_type: other\ncaptured: 2026-01-05\n"
        "tags: []\ningested_into: []\n---\n\n# Legacy fallback capture\n\n## Capture\n\nBody.\n",
        encoding="utf-8",
    )
    before = legacy.read_text(encoding="utf-8")

    # A later, unrelated capture rewrites the indexes; the legacy page must be
    # untouched and must still appear.
    _capture(vault, source_schema, source_type="research-report", domain="travel",
             title="A newer report")
    assert legacy.exists()
    assert legacy.read_text(encoding="utf-8") == before
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "Sources/Other/|Other]]" in index

    hits = commands.op_ask_memory(vault, query="legacy fallback capture", limit=20)
    paths = [hit["path"] if isinstance(hit, dict) else hit.path for hit in hits]
    assert any(path.endswith("2026-01-05-legacy-fallback-capture.md") for path in paths)


# ---------------------------------------------------------------------------
# Parameter aliasing
# ---------------------------------------------------------------------------
def test_either_parameter_name_is_accepted(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    by_type = commands.op_capture_source(
        vault, source_schema, content="Body.", title="Named by type",
        source_type="research-report",
    )
    by_kind = commands.op_capture_source(
        vault, source_schema, content="Body.", title="Named by kind",
        source_kind="research-report",
    )
    assert by_type["source"]["path"].startswith(f"{KB}/Sources/Reports/")
    assert by_kind["source"]["path"].startswith(f"{KB}/Sources/Reports/")


def test_the_same_value_under_both_names_is_accepted(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    out = commands.op_capture_source(
        vault, source_schema, content="Body.", title="Agreed pair",
        source_type="research-report", source_kind="Research Report",
    )
    assert out["source"]["path"].startswith(f"{KB}/Sources/Reports/")


def test_a_conflicting_pair_of_names_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(ValueError, match="same axis"):
        commands.op_capture_source(
            vault, source_schema, content="Body.", title="Conflicting pair",
            source_type="book", source_kind="research-report",
        )


def test_neither_name_resolves_to_the_fallback(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    out = commands.op_capture_source(
        vault, source_schema, content="Body.", title="Unclassified capture"
    )
    assert out["source"]["path"].startswith(f"{KB}/Sources/Other/")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
# The drive-qualified and UNC forms are assembled rather than written literally.
# The repository's public-artifact privacy guard scans every tracked file for
# absolute local paths, and spelling one out here reads to it exactly like a
# leaked machine path. Composing them keeps the guard strict instead of teaching
# it an exception — see `public_artifact_privacy._CONTENT_RULES`.
_DRIVE_PATH = "C:" + chr(92) + "foo"
_UNC_PATH = chr(92) * 2 + "server" + chr(92) + "share"

_TRAVERSAL_AND_PATH_FORMS = [
    "../../foo",
    "../travel",
    _DRIVE_PATH,
    "/foo",
    _UNC_PATH,
    "travel/reports",
    "travel\\reports",
    "travel...",
    "  travel  ",
    "tra\x00vel",
    "\u202etravel",
    "中文",
]

_REFUSED_VALUES = [
    "",
    "   ",
    ".",
    "..",
    "...",
    "con",
    "CON",
    "nul",
    "aux",
    "prn",
    "com1",
    "LPT9",
    "x" * 60,
]


@pytest.mark.parametrize("raw", _TRAVERSAL_AND_PATH_FORMS)
def test_hostile_values_normalize_to_a_safe_segment(raw: str) -> None:
    """Safety is structural: a segment only ever comes from a validated key."""
    taxonomy = st.core_taxonomy()
    key = st.normalize(raw, axis="domain")
    resolution = taxonomy.resolve_domain(raw)
    segments = st.source_segments(taxonomy.resolve_kind("article"), resolution)
    for segment in segments:
        assert "/" not in segment
        assert "\\" not in segment
        assert ".." not in segment
        assert segment not in {".", ".."}
        assert segment == segment.strip()
        assert not segment.endswith(".")
        assert all(ord(char) >= 32 for char in segment)
        assert ":" not in segment
    assert st.MAX_PROJECTION_DEPTH >= len(segments) - 1
    assert key == key.strip()


@pytest.mark.parametrize("raw", _REFUSED_VALUES)
def test_degenerate_and_reserved_values_are_refused(raw: str) -> None:
    for axis in ("source_kind", "domain"):
        with pytest.raises(st.InvalidTaxonomyValue):
            st.normalize(raw, axis=axis)


@pytest.mark.parametrize("raw", _TRAVERSAL_AND_PATH_FORMS + _REFUSED_VALUES)
def test_no_hostile_value_can_escape_the_sources_root(
    vault: Path, source_schema: schema_module.SourceSchema, raw: str
) -> None:
    sources_root = (vault / KB / "Sources").resolve()
    try:
        result = _capture(
            vault, source_schema, source_type="article", domain=raw,
            title=f"Safety probe {abs(hash(raw))}", url="https://example.com/x",
        )
    except add_module.AddError:
        return  # refused, which is an equally acceptable outcome
    written = (vault / result.path).resolve()
    assert written.is_relative_to(sources_root)


def test_a_colliding_path_segment_is_refused(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """Two keys must not project to one directory on a case-insensitive filesystem."""
    st.registry_path(vault).parent.mkdir(parents=True, exist_ok=True)
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  ledger:\n    label: Ledger\n"
        "    path_label: reports\ndomains: {}\n",
        encoding="utf-8",
    )
    taxonomy = st.load_taxonomy(vault)
    assert any("project to" in finding for finding in taxonomy.findings)


def test_a_registry_declared_segment_must_be_a_safe_directory_name(
    vault: Path,
) -> None:
    st.registry_path(vault).parent.mkdir(parents=True, exist_ok=True)
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  ledger:\n    label: Ledger\n"
        "    path_label: ../escape\ndomains: {}\n",
        encoding="utf-8",
    )
    taxonomy = st.load_taxonomy(vault)
    assert taxonomy.resolve_kind("ledger").path_label == "Ledger"
    assert taxonomy.findings


def test_a_malformed_registry_degrades_to_the_built_ins(vault: Path) -> None:
    st.registry_path(vault).parent.mkdir(parents=True, exist_ok=True)
    st.registry_path(vault).write_text("::not: valid: yaml: [", encoding="utf-8")
    taxonomy = st.load_taxonomy(vault)
    assert taxonomy.resolve_kind("research-report").path_label == "Reports"
    assert taxonomy.findings


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
@pytest.fixture
def classified_corpus(
    vault: Path, source_schema: schema_module.SourceSchema
) -> dict[str, str]:
    rows = [
        ("research-report", "travel", ["california-trip-2026"], "Airfare investigation", None),
        ("research-report", "marine-biology", [], "Kelp canopy synthesis", None),
        ("paper", "health", [], "Sleep latency meta-analysis", "https://example.com/p"),
        ("invoice-receipt", "equipment", ["studio-refit"], "Stand order", None),
        ("official-guidance", "travel", ["california-trip-2026"], "Entry bulletin", None),
    ]
    out: dict[str, str] = {}
    for kind, domain, projects, title, url in rows:
        result = _capture(
            vault, source_schema, source_type=kind, domain=domain,
            projects=projects, title=title, url=url,
        )
        out[title] = result.path
    return out


def _recall(vault: Path, **kwargs: object) -> set[str]:
    hits = commands.op_ask_memory(vault, query="", limit=30, **kwargs)  # type: ignore[arg-type]
    return {
        (hit["path"] if isinstance(hit, dict) else hit.path).rsplit("/", 1)[-1]
        for hit in hits
    }


def test_each_axis_filters_independently(
    vault: Path, classified_corpus: dict[str, str]
) -> None:
    def stem(title: str) -> str:
        return classified_corpus[title].rsplit("/", 1)[-1]

    assert _recall(vault, source_kinds=["research-report"]) == {
        stem("Airfare investigation"),
        stem("Kelp canopy synthesis"),
    }
    assert _recall(vault, domains=["travel"]) == {
        stem("Airfare investigation"),
        stem("Entry bulletin"),
    }
    assert _recall(vault, projects=["california-trip-2026"]) == {
        stem("Airfare investigation"),
        stem("Entry bulletin"),
    }


def test_axes_combine(vault: Path, classified_corpus: dict[str, str]) -> None:
    def stem(title: str) -> str:
        return classified_corpus[title].rsplit("/", 1)[-1]

    assert _recall(vault, source_kinds=["research-report"], domains=["travel"]) == {
        stem("Airfare investigation")
    }
    assert _recall(
        vault,
        source_kinds=["official-guidance"],
        domains=["travel"],
        projects=["california-trip-2026"],
    ) == {stem("Entry bulletin")}
    assert _recall(vault, source_kinds=["paper"], domains=["travel"]) == set()


def test_classification_is_filterable_without_any_tags(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault, source_schema, source_type="dataset-export", domain="media",
        title="Untagged export", tags=None,
    )
    assert _frontmatter(vault, result.path)["tags"] == []
    assert result.path.rsplit("/", 1)[-1] in _recall(
        vault, source_kinds=["dataset-export"]
    )


def test_the_frontmatter_pointer_form_still_works(
    vault: Path, classified_corpus: dict[str, str]
) -> None:
    """The general escape hatch keeps working alongside the first-class fields."""
    expected = classified_corpus["Stand order"].rsplit("/", 1)[-1]
    assert _recall(
        vault, filters={"page.frontmatter:/domain": {"$eq": "equipment"}}
    ) == {expected}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def test_capture_then_compile_preserves_provenance(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault, source_schema, source_type="research-report", domain="travel",
        projects=["california-trip-2026"], title="Airfare investigation",
    )
    front = _frontmatter(vault, result.path)
    assert front["exomem_id"]
    assert front["ingested_into"] == []

    proposal = commands.op_compile_source(vault, sources=[result.path])
    assert result.path.removesuffix(".md") in proposal["suggested_sources"]


def test_the_session_note_type_heuristic_still_fires(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A kind whose downstream behaviour is special-cased keeps that behaviour."""
    session = _capture(
        vault, source_schema, source_type="session", title="Design conversation"
    )
    proposal = commands.op_compile_source(vault, sources=[session.path])
    assert "research-note" in str(proposal)


# ---------------------------------------------------------------------------
# Advisory classification suggestion
# ---------------------------------------------------------------------------
def test_recurring_fallback_captures_in_one_domain_suggest_a_real_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    suggestions = [
        _capture(
            vault, source_schema, source_type="other", domain="media",
            title=f"Loose media item {index}",
        ).structure_suggestion
        for index in range(3)
    ]
    assert suggestions[0]["strength"] == "moderate"
    final = suggestions[-1]
    assert final["kind"] == "source_classification_debt"
    assert final["strength"] == "strong"
    assert final["reasons"] == sorted(final["reasons"])
    assert "fallback_captures_recur_in_domain" in final["reasons"]
    assert final["domain"] == "media"
    # Advisory, never a score.
    assert "confidence" not in final
    assert "score" not in final
    assert "probability" not in final


def test_a_single_unclassified_capture_stays_quiet(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(vault, source_schema, title="One loose thought")
    assert result.structure_suggestion is None


def test_a_coherently_classified_capture_stays_quiet(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _capture(
        vault, source_schema, source_type="invoice-receipt", domain="equipment",
        title="Stand order",
    )
    assert result.structure_suggestion is None


def test_advisory_failure_leaves_the_capture_committed(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("sensor exploded")

    monkeypatch.setattr(add_module, "_count_capped", explode)
    result = _capture(
        vault, source_schema, source_type="other", domain="media", title="Still saved"
    )
    assert result.structure_suggestion is None
    assert (vault / result.path).exists()
    assert result.path.startswith(f"{KB}/Sources/Other/Media/")


def test_the_suggestion_surfaces_on_the_public_capture_result(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    for index in range(3):
        out = commands.op_capture_source(
            vault, source_schema, content="Body.", title=f"Loose item {index}",
            source_kind="other", domain="media",
        )
    assert out["source"]["structure_suggestion"]["strength"] == "strong"


def test_a_classified_capture_omits_the_key_entirely(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    out = commands.op_capture_source(
        vault, source_schema, content="Body.", title="Classified item",
        source_kind="research-report", domain="travel",
    )
    assert "structure_suggestion" not in out["source"]


def test_the_advisory_survives_the_committed_mutation_envelope(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """Producing the advisory is not the same as an agent receiving it.

    The envelope re-validates every advisory against the bounds declared for its
    kind, so an advisory whose payload the terminal does not recognise is dropped
    silently — which is correct for a malformed one and useless for a real one.
    """
    for index in range(3):
        leaf = commands.op_capture_source(
            vault, source_schema, content="Body.", title=f"Loose item {index}",
            source_kind="other", domain="media",
        )
    envelope = _committed_envelope(leaf)
    advisory = envelope["structure_suggestion"]
    assert advisory["kind"] == "source_classification_debt"
    assert advisory["strength"] == "strong"
    assert advisory["domain"] == "media"
    assert advisory["fallback_captures"] == 3
    # The compiled-write shape is not smuggled in beside it.
    assert "off_scope_units" not in advisory
    assert "cluster_terms" not in advisory


def test_a_malformed_classification_advisory_is_dropped_not_forwarded(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    leaf = {
        "source": {
            "path": "Knowledge Base/Sources/Other/Media/x.md",
            "structure_suggestion": {
                "kind": "source_classification_debt",
                "strength": "strong",
                "reasons": ["fallback_kind_with_declared_domain"],
                "domain": "media",
                "fallback_captures": "three",  # not an int
            },
        }
    }
    assert "structure_suggestion" not in _committed_envelope(leaf)


# ---------------------------------------------------------------------------
# Human browsing
# ---------------------------------------------------------------------------
def test_an_unshipped_kind_appears_in_the_index_with_a_description(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _capture(vault, source_schema, source_type="field-notebook", title="Notebook entry")
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "Sources/Field Notebook/|Field Notebook]] — captured material (1)" in index


def test_a_shipped_kind_gets_its_registry_description(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _capture(vault, source_schema, source_type="research-report", title="A report")
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "Sources/Reports/|Reports]] — a compiled investigation or research report" in index


def test_legacy_index_descriptions_are_unchanged(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """Upgrading must not rewrite an existing index row's text."""
    _capture(vault, source_schema, source_type="research-report", title="A report")
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "|Articles]] — captured web/PDF content" in index
    assert "|Sessions]] — pasted Claude/conversation transcripts" in index


def test_nested_sources_count_under_their_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _capture(vault, source_schema, source_type="research-report", title="Flat report")
    _capture(
        vault, source_schema, source_type="research-report", domain="travel",
        title="Nested report",
    )
    index = (vault / KB / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "|Reports]] — a compiled investigation or research report (2)" in index


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------
def test_registration_is_idempotent(vault: Path) -> None:
    assert st.register(vault, kind="field-notebook")
    assert st.register(vault, kind="field-notebook") == ()


def test_a_registry_override_changes_where_future_captures_land(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The registry owns display and path metadata; the key stays canonical."""
    st.registry_path(vault).parent.mkdir(parents=True, exist_ok=True)
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  research-report:\n"
        "    label: Investigation\n    path_label: Investigations\ndomains: {}\n",
        encoding="utf-8",
    )
    result = _capture(
        vault, source_schema, source_type="research-report", title="Relabelled report"
    )
    assert result.path.startswith(f"{KB}/Sources/Investigations/")
    assert _frontmatter(vault, result.path)["source_type"] == "research-report"


def test_a_deprecated_key_still_works_and_says_so(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    st.registry_path(vault).parent.mkdir(parents=True, exist_ok=True)
    st.registry_path(vault).write_text(
        "schema_version: 1\nsource_kinds:\n  memo:\n    label: Memo\n"
        "    status: deprecated\n    replaced_by: correspondence\ndomains: {}\n",
        encoding="utf-8",
    )
    result = _capture(vault, source_schema, source_type="memo", title="Old memo")
    assert any("DEPRECATED_SOURCE_KIND" in warning for warning in result.warnings)
    assert any("correspondence" in warning for warning in result.warnings)


def test_the_published_tool_schema_never_carries_a_vaults_own_vocabulary(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """The live set is published through bootstrap, never through the schema.

    Bootstrap is already per-vault and stays on the machine. A tool description
    is serialized to whichever model provider is connected and, when the schema
    fixture is regenerated, committed into this public repository — so a vault's
    own kinds and domains must never be reachable from it.
    """
    _capture(vault, source_schema, source_type="field-notebook",
             domain="agroforestry", title="Notebook entry")

    fixture = Path(__file__).parent / "fixtures" / "mcp_tool_schemas.json"
    serialized = fixture.read_text(encoding="utf-8")
    for vault_added in ("field-notebook", "agroforestry"):
        assert vault_added not in serialized

    # It states the open contract instead, so the frozen text never teaches an
    # agent that the set is closed.
    capture = json.loads(serialized)["capture_source"]
    assert "open vocabularies" in capture["description"].lower()
    properties = capture["inputSchema"]["properties"]
    assert "not a closed set" in properties["source_type"]["description"].lower()

    # The current set reaches the agent through bootstrap, which is per-vault.
    block = commands.op_bootstrap(vault, profile="full")["source_taxonomy"]
    assert "field-notebook" in block["source_kinds_known"]
    assert block["exhaustive"] is False


def test_built_in_vocabulary_is_pinned_so_additions_stay_reviewable() -> None:
    """The shipped defaults are a public artifact and must stay generic.

    Pinned rather than pattern-matched: a denylist cannot recognise a private
    label it has never seen, whereas an exact set turns any addition into a
    deliberate, visible diff that review can judge.
    """
    assert set(st.builtin_kinds()) == {
        "article",
        "book",
        "contract-legal-document",
        "correspondence",
        "dataset-export",
        "invoice-receipt",
        "manual-documentation",
        "official-guidance",
        "other",
        "paper",
        "research-report",
        "session",
        "video",
        "webpage-snapshot",
    }
    assert set(st.builtin_domains()) == {
        "business",
        "equipment",
        "finance",
        "food",
        "health",
        "legal",
        "media",
        "photography",
        "research",
        "software",
        "travel",
    }
    for definition in (*st.builtin_kinds().values(), *st.builtin_domains().values()):
        assert st.normalize(definition.key) == definition.key
        assert definition.builtin


# ---------------------------------------------------------------------------
# Agent contract
# ---------------------------------------------------------------------------
def test_bootstrap_teaches_the_open_vocabulary_contract(vault: Path) -> None:
    """Compact carries the contract; the byte-starved profile can drop the rest.

    The lists are discovery convenience — the contract itself says the listed
    set is not the permitted one — so an agent that only ever sees the compact
    profile still has everything it needs to classify correctly.
    """
    block = commands.op_bootstrap(vault, profile="compact")["source_taxonomy"]
    for axis in ("source_kind", "domain", "projects"):
        assert axis in block["contract"]
    assert "open" in block["contract"].lower()
    assert "even if unfamiliar" in block["contract"]
    assert "could not be determined" in block["fallback_rule"]
    assert "never that no familiar label matched" in block["fallback_rule"]


def test_richer_profiles_add_the_live_vocabulary_and_the_mechanics(
    vault: Path,
) -> None:
    block = commands.op_bootstrap(vault, profile="full")["source_taxonomy"]
    assert block["exhaustive"] is False
    assert "source_kinds=" in block["recall"]
    assert "research-report" in block["source_kinds_known"]
    assert "travel" in block["domains_known"]


def test_bootstrap_no_longer_publishes_the_fallback_as_the_capture_default(
    vault: Path,
) -> None:
    """The contract used to hand every agent `source_type: "other"` as the route."""
    payload = commands.op_bootstrap(vault, profile="compact")
    capture = payload["simple_actions"]["capture"]
    assert capture["route"]["tool"] == "capture_source"
    assert capture["route"]["args"] == {}
    assert st.FALLBACK_KIND not in str(capture["route"])
    # The capture action names all three axes in the prose it already publishes
    # and points at the block carrying the openness rule, so the compact payload
    # never says it twice and never pays for a key of its own.
    intent = capture["intent"]
    for axis in ("source_kind", "domain", "projects"):
        assert axis in intent
    assert "source_taxonomy" in intent
    assert "open" in payload["source_taxonomy"]["contract"]


def test_bootstrap_registers_the_classification_suggestion_kind(vault: Path) -> None:
    post_write = commands.op_bootstrap(vault, profile="compact")["authoring_contract"][
        "post_write"
    ]
    assert "source_classification_debt" in post_write["structure_suggestion"]
    assert "source_classification_debt" in post_write["structure_suggestion_handling"]


def test_bootstrap_reflects_vault_added_vocabulary(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    _capture(vault, source_schema, source_type="field-notebook",
             domain="agroforestry", title="Notebook entry")
    block = commands.op_bootstrap(vault, profile="full")["source_taxonomy"]
    assert "field-notebook" in block["source_kinds_known"]
    assert "agroforestry" in block["domains_known"]


def test_full_profile_carries_the_definitions_and_registry_path(vault: Path) -> None:
    block = commands.op_bootstrap(vault, profile="full")["source_taxonomy"]
    assert block["registry"].endswith("source-taxonomy.yaml")
    by_key = {item["key"]: item for item in block["source_kinds"]}
    assert by_key["research-report"]["path_label"] == "Reports"
    assert by_key["article"]["requires_url"] is True
    assert "tags_rule" in block and "path_rule" in block


# ---------------------------------------------------------------------------
# Knowledge packs
# ---------------------------------------------------------------------------
def test_every_built_in_pack_suggests_resolvable_generic_labels() -> None:
    from exomem import knowledge_packs

    taxonomy = st.core_taxonomy()
    for pack in knowledge_packs.list_builtin_packs():
        assert pack["suggested_source_kinds"]
        assert pack["suggested_domains"]
        for key in pack["suggested_source_kinds"]:
            assert taxonomy.resolve_kind(key).key == key
        for key in pack["suggested_domains"]:
            assert taxonomy.resolve_domain(key).key == key


def test_pack_labels_resolve_against_the_one_shared_vocabulary() -> None:
    """Packs surface labels; they never define a competing model."""
    from exomem import knowledge_packs

    for pack in knowledge_packs.list_builtin_packs():
        for key in pack["suggested_source_kinds"]:
            assert key in st.builtin_kinds()
        for key in pack["suggested_domains"]:
            assert key in st.builtin_domains()


def test_a_pack_may_not_suggest_an_unusable_label() -> None:
    from exomem import knowledge_packs

    raw = dict(knowledge_packs.list_builtin_packs()[0])
    raw["suggested_domains"] = ["Not A Slug"]
    with pytest.raises(knowledge_packs.PackValidationError) as exc:
        knowledge_packs.validate_pack_dict(raw)
    assert exc.value.code == "INVALID_TAXONOMY_LABEL"


def test_classification_works_with_no_pack_selected(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    from exomem import knowledge_packs

    assert not (
        vault / KB / knowledge_packs.PACK_SELECTION_DIR
        / knowledge_packs.PACK_SELECTION_FILE
    ).exists()
    result = _capture(
        vault, source_schema, source_type="research-report", domain="travel",
        title="Report without a pack",
    )
    assert result.path.startswith(f"{KB}/Sources/Reports/Travel/")


def test_selecting_a_pack_creates_no_taxonomy_entry(vault: Path) -> None:
    from exomem import knowledge_packs

    knowledge_packs.write_selected_packs(vault, ["technical"], source="test")
    assert not st.registry_path(vault).exists()


# ---------------------------------------------------------------------------
# Capture surfaces
# ---------------------------------------------------------------------------
def _capture_alias_argv(argv: list[str]) -> list[str]:
    """The core-op argv the `exomem capture` alias builds, without executing it."""
    from unittest import mock

    from exomem import __main__ as cli

    with mock.patch.object(cli, "_core_op_main", return_value=0) as core:
        assert cli._simple_capture_main(argv) == 0
    return list(core.call_args.args[0])


def test_the_capture_alias_never_supplies_the_fallback_on_the_callers_behalf() -> None:
    """The CLI hard-defaulted `--source-type` to the fallback.

    That is the same defect the bootstrap route carried: a surface that fills in
    "unclassified" for you makes every capture through it look like a deliberate
    low-confidence classification rather than an absent one.
    """
    argv = _capture_alias_argv(["Body text.", "--title", "Untyped capture"])
    assert st.FALLBACK_KIND not in argv
    assert "--source-type" not in argv
    assert "--source-kind" not in argv


def test_the_capture_alias_forwards_all_three_axes() -> None:
    argv = _capture_alias_argv(
        [
            "Body text.",
            "--title",
            "Autumn airfare investigation",
            "--source-kind",
            "research-report",
            "--domain",
            "travel",
            "--project",
            "california-trip-2026",
            "--project",
            "client-project",
        ]
    )
    assert argv[argv.index("--source-kind") + 1] == "research-report"
    assert argv[argv.index("--domain") + 1] == "travel"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--projects"] == [
        "california-trip-2026",
        "client-project",
    ]
