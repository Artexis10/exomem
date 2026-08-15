"""Behavioural acceptance for advisory structural-promotion suggestions.

These tests bind the *mechanism*, not a fixture's prose. The positive case is a
reconstruction of a real dogfood failure: a coherent travel note that accumulated
property finance, farmland, agricultural grants, livestock husbandry and hospitality
across a sequence of individually defensible `observe_memory` writes until it was no
longer the right canonical home for any of them.

Nothing here asserts on the words that made that case memorable. Every assertion is
about structure: whether recurring durable material sits outside the page's own
declared scope, how much of it there is, and whether the page still holds its
original subject. Swap the domain and the tests still hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, structure_promotion, writer_lease

TRAVEL_PAGE = "Knowledge Base/Notes/Research/Travel/seasonal-fare-and-relocation-scouting.md"
TRAVEL_ID = "00000000-0000-4000-8000-0000000005a1"


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _page(
    *,
    page_id: str,
    title: str,
    tags: list[str],
    body: str,
    page_type: str = "research-note",
    project: str = "travel",
) -> str:
    """Render a compiled page with a declared identity and durable units."""
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        f"project: {project}\n"
        "status: active\n"
        f"exomem_id: {page_id}\n"
        "updated: 2026-08-15\n"
        f"tags: [{', '.join(tags)}]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body.rstrip()}\n"
    )


def _unit(category: str, content: str, tags: list[str], anchor: str) -> str:
    return f"- [{category}] {content} {' '.join('#' + tag for tag in tags)} ^{anchor}"


# --- the coherent starting point -------------------------------------------------
# Declared identity: airline fares, seasonality, island destinations, family travel.

_TRAVEL_TITLE = "Seasonal fare portfolio and relocation scouting plan"
_TRAVEL_TAGS = ["carrier-sale", "fares", "relocation", "travel", "islands", "family"]

_COHERENT_UNITS = [
    _unit(
        "decision",
        "Wait for the annual late-summer fare campaign and buy on the first day a "
        "matching itinerary appears rather than committing the budget earlier.",
        ["travel", "carrier-sale", "booking"],
        "wait-for-campaign",
    ),
    _unit(
        "priority",
        "Prioritise one southern-coast journey, one island trip, and one city break "
        "across the sale year rather than compressing them into a single destination.",
        ["travel", "carrier-sale", "islands"],
        "trip-priority",
    ),
    _unit(
        "constraint",
        "Protect midsummer for home and schedule the coastal and island trips in "
        "spring or autumn by default.",
        ["travel", "fares", "islands"],
        "season-constraint",
    ),
    _unit(
        "benchmark",
        "The prior campaign ran for two weeks and offered return benchmarks well "
        "below the ordinary published fares for the same routes.",
        ["carrier-sale", "fares", "travel"],
        "fare-benchmarks",
    ),
    _unit(
        "logistics",
        "Price both three-person and four-person bookings and buy checked baggage "
        "with the tickets, because later addition is normally dearer.",
        ["travel", "family", "carrier-sale"],
        "party-logistics",
    ),
    _unit(
        "decision",
        "Keep the journey a genuine family holiday with scouting embedded lightly "
        "into ordinary neighbourhood stays.",
        ["travel", "relocation", "family"],
        "scout-structure",
    ),
]


def _travel_page(extra_units: list[str] | None = None) -> str:
    units = list(_COHERENT_UNITS) + list(extra_units or [])
    body = "## Observations\n\n" + "\n".join(units) + "\n"
    return _page(
        page_id=TRAVEL_ID,
        title=_TRAVEL_TITLE,
        tags=_TRAVEL_TAGS,
        body=body,
    )


# --- the accumulation, one durable cluster at a time -----------------------------
# Each stage is what a further defensible write would have deposited.

STAGE_PROPERTY = [
    _unit(
        "property_target",
        "The working target is a large rural principal residence of roughly ten "
        "hectares with outbuildings and fenced ground.",
        ["property", "smallholding", "land", "countryside"],
        "property-target",
    ),
]

STAGE_FINANCING = [
    _unit(
        "financing",
        "Do not wait for the full cash target before testing mortgage eligibility; "
        "the practical bottleneck is which income a lender accepts as stable.",
        ["mortgage", "financing", "lending", "deposit"],
        "mortgage-timing",
    ),
    _unit(
        "financing",
        "Treat the deposit, acquisition costs and initial renovation buffer as "
        "short-horizon capital and keep that core liquid.",
        ["financing", "liquidity", "savings", "deposit"],
        "treasury-policy",
    ),
]

STAGE_LAND = [
    _unit(
        "property_candidate",
        "A rural agency listing of an old mill with pasture, wooded meadow and water "
        "plus substantial former farm buildings is a strong fit candidate.",
        ["land", "smallholding", "pasture", "property", "countryside"],
        "land-candidate",
    ),
    _unit(
        "infrastructure",
        "Barn, hangar and stable capacity determines what the holding can carry "
        "before any new building is considered.",
        ["smallholding", "buildings", "pasture", "land"],
        "holding-capacity",
    ),
]

STAGE_GRANTS = [
    _unit(
        "funding_strategy",
        "Treat public support as separate eligible buckets rather than one blanket "
        "renovation grant, because residential and agricultural schemes differ.",
        ["grants", "funding", "agriculture", "subsidy"],
        "funding-stack",
    ),
    _unit(
        "qualification_strategy",
        "Target the minimum recognised qualification that is both practically useful "
        "and accepted for installation aid rather than a full degree.",
        ["qualification", "agriculture", "training", "grants"],
        "qualification-path",
    ),
]

STAGE_LIVESTOCK = [
    _unit(
        "husbandry",
        "The planned operation should optimise for animal welfare rather than "
        "intensive throughput, maximising pasture access and natural behaviours.",
        ["livestock", "husbandry", "pasture", "animal-welfare"],
        "husbandry-principle",
    ),
    _unit(
        "husbandry",
        "Dual-purpose dairy animals can enter the meat system at older ages instead "
        "of being culled early for maximum yield.",
        ["livestock", "dairy", "husbandry", "animal-welfare"],
        "dual-purpose",
    ),
]

STAGE_HOSPITALITY = [
    _unit(
        "business_option",
        "Part of the holding could carry accommodation and experience revenue, "
        "monetising excess buildings without forcing throughput on the livestock.",
        ["hospitality", "accommodation", "business", "smallholding"],
        "hospitality-line",
    ),
]

JOURNEY = [
    ("baseline", []),
    ("property", STAGE_PROPERTY),
    ("financing", STAGE_FINANCING),
    ("land", STAGE_LAND),
    ("grants", STAGE_GRANTS),
    ("livestock", STAGE_LIVESTOCK),
    ("hospitality", STAGE_HOSPITALITY),
]


def _journey_states() -> list[tuple[str, str]]:
    """Cumulative page source after each stage of the accumulation."""
    states: list[tuple[str, str]] = []
    accumulated: list[str] = []
    for name, stage in JOURNEY:
        accumulated.extend(stage)
        states.append((name, _travel_page(accumulated)))
    return states


def _suggest(tmp_path: Path, rel: str, source: str) -> dict | None:
    """Detect over a page exactly as the write path does, via the public helper."""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return structure_promotion.suggest_for_page(tmp_path, rel)


# --------------------------------------------------------------------------------
# Positive journey
# --------------------------------------------------------------------------------


def test_early_accumulation_stays_quiet(tmp_path: Path) -> None:
    """One emerging thread is not yet a separate project."""
    for name, source in _journey_states()[:3]:  # baseline, property, financing
        suggestion = _suggest(tmp_path, TRAVEL_PAGE, source)
        assert suggestion is None, f"stage {name!r} suggested too early: {suggestion}"


def test_sustained_accumulation_reaches_a_strong_suggestion(tmp_path: Path) -> None:
    """By the time several durable threads recur, the page has outgrown its scope."""
    states = dict(_journey_states())
    suggestion = _suggest(tmp_path, TRAVEL_PAGE, states["hospitality"])

    assert suggestion is not None, "the fully accumulated page produced no suggestion"
    assert suggestion["kind"] == "scope_divergence"
    assert suggestion["strength"] == "strong"
    assert set(suggestion["reasons"]) == {
        "cluster_reaches_child_note_mass",
        "declared_scope_mismatch",
        "off_scope_cluster_recurs",
        "page_retains_original_scope",
    }
    assert suggestion["off_scope_units"] >= 5


def test_the_suggestion_arrives_before_the_page_becomes_absurd(tmp_path: Path) -> None:
    """It must fire while a knowledgeable user would still be deciding, not after."""
    fired_at = [
        name
        for name, source in _journey_states()
        if _suggest(tmp_path, TRAVEL_PAGE, source) is not None
    ]

    assert fired_at, "the journey never produced a suggestion"
    # Not before the third cluster recurs, and not later than the sixth.
    assert fired_at[0] in {"land", "grants", "livestock"}, fired_at


# --------------------------------------------------------------------------------
# Negative controls
# --------------------------------------------------------------------------------


def _long_coherent_source() -> str:
    """A deliberately large single-subject note: more units than the positive case."""
    units = []
    for index in range(40):
        units.append(
            _unit(
                ["decision", "finding", "risk", "constraint"][index % 4],
                f"Retrieval behaviour observation number {index} covering ranking, "
                "fusion, and recall of the indexing subsystem under load.",
                ["retrieval", "indexing", ["ranking", "fusion", "recall"][index % 3]],
                f"retrieval-{index}",
            )
        )
    body = (
        "## Question\n\nHow should the retrieval stack rank and fuse candidates?\n\n"
        + "\n\n".join(f"### Section {i}\n\nExtended prose block {i}." for i in range(12))
        + "\n\n## Observations\n\n"
        + "\n".join(units)
        + "\n"
    )
    return _page(
        page_id="00000000-0000-4000-8000-0000000005a2",
        title="Retrieval ranking and fusion research",
        tags=["retrieval", "indexing", "ranking", "fusion", "recall"],
        body=body,
        project="search",
    )


def test_a_long_coherent_research_note_stays_quiet(tmp_path: Path) -> None:
    """Length is not structural debt. This note is larger than the positive case."""
    rel = "Knowledge Base/Notes/Research/Search/retrieval-ranking.md"
    source = _long_coherent_source()
    positive = dict(_journey_states())["hospitality"]

    assert len(source) > len(positive), "the control must be larger to prove the point"
    assert _suggest(tmp_path, rel, source) is None


def test_one_or_two_tangents_do_not_trigger(tmp_path: Path) -> None:
    """An incidental aside is not an emerging project."""
    tangents = [
        _unit(
            "aside",
            "A lender's published rate table was easier to read than expected.",
            ["mortgage", "lending", "rates"],
            "rate-aside",
        ),
        _unit(
            "aside",
            "Rural building conversions appear on regional agency listings.",
            ["property", "buildings", "countryside"],
            "conversion-aside",
        ),
    ]
    assert _suggest(tmp_path, TRAVEL_PAGE, _travel_page(tangents)) is None


def test_source_and_evidence_artifacts_never_enter_the_path(tmp_path: Path) -> None:
    """Non-compiled material is excluded regardless of how heterogeneous it is."""
    accumulated = [unit for _, stage in JOURNEY for unit in stage]
    body = "## Observations\n\n" + "\n".join(_COHERENT_UNITS + accumulated) + "\n"
    for page_type, rel in (
        ("source", "Knowledge Base/Sources/Other/2026-08-15-mixed-capture.md"),
        ("evidence", "Knowledge Base/Evidence/case/mixed-artifact.md"),
    ):
        source = _page(
            page_id="00000000-0000-4000-8000-0000000005a3",
            title="Mixed capture",
            tags=_TRAVEL_TAGS,
            body=body,
            page_type=page_type,
        )
        assert _suggest(tmp_path, rel, source) is None, page_type


def test_a_deliberate_hub_is_not_nagged_into_splitting(tmp_path: Path) -> None:
    """A hub declares its breadth on purpose; breadth is its job."""
    accumulated = [unit for _, stage in JOURNEY for unit in stage]
    body = "## Observations\n\n" + "\n".join(accumulated) + "\n"
    source = _page(
        page_id="00000000-0000-4000-8000-0000000005a4",
        title="Rural holding hub",
        tags=["hub", "smallholding", "land", "livestock", "grants", "hospitality"],
        body=body,
        project="holding",
    )
    rel = "Knowledge Base/Notes/Research/Holding/rural-holding-hub.md"
    assert _suggest(tmp_path, rel, source) is None


def test_advice_stops_once_the_material_is_routed_into_matching_scope(
    tmp_path: Path,
) -> None:
    """The end state of acting on the advice must itself be quiet, with no dismissal."""
    accumulated = [unit for _, stage in JOURNEY for unit in stage]

    # The new destination declares the scope the material actually has.
    destination = _page(
        page_id="00000000-0000-4000-8000-0000000005a5",
        title="Rural holding property, funding and livestock",
        tags=[
            "smallholding",
            "property",
            "land",
            "financing",
            "grants",
            "livestock",
            "husbandry",
            "hospitality",
        ],
        body="## Observations\n\n" + "\n".join(accumulated) + "\n",
        project="holding",
    )
    assert (
        _suggest(
            tmp_path,
            "Knowledge Base/Notes/Research/Holding/property-funding-livestock.md",
            destination,
        )
        is None
    )

    # And the original page, with the material removed, is quiet again.
    assert _suggest(tmp_path, TRAVEL_PAGE, _travel_page()) is None


# --------------------------------------------------------------------------------
# Contract guarantees
# --------------------------------------------------------------------------------


def test_payload_is_bounded_and_deterministic(tmp_path: Path) -> None:
    source = dict(_journey_states())["hospitality"]
    first = _suggest(tmp_path, TRAVEL_PAGE, source)
    second = _suggest(tmp_path, TRAVEL_PAGE, source)

    assert first == second
    assert first is not None
    assert first["reasons"] == sorted(first["reasons"])
    assert len(first["cluster_terms"]) <= structure_promotion.MAX_CLUSTER_TERMS
    assert first["cluster_terms"] == sorted(first["cluster_terms"])
    assert set(first) == {
        "kind",
        "strength",
        "reasons",
        "off_scope_units",
        "cluster_terms",
    }
    assert not isinstance(first["strength"], (int, float))
    assert first["strength"] in {"strong", "moderate"}


def test_evidence_never_names_another_page(tmp_path: Path) -> None:
    """Every reported fact comes from the page the caller just wrote."""
    source = dict(_journey_states())["hospitality"]
    # A neighbour the caller may not be entitled to see.
    (tmp_path / "Knowledge Base/Notes/Research/Secret").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/Notes/Research/Secret/classified-holding.md").write_text(
        _page(
            page_id="00000000-0000-4000-8000-0000000005a6",
            title="Classified holding programme",
            tags=["smallholding", "livestock", "grants"],
            body="## Observations\n\n"
            + "\n".join(unit for _, stage in JOURNEY for unit in stage)
            + "\n",
            project="holding",
        ),
        encoding="utf-8",
    )

    suggestion = _suggest(tmp_path, TRAVEL_PAGE, source)

    assert suggestion is not None
    blob = repr(suggestion)
    for leaked in ("Secret", "classified", "Classified", ".md", "Knowledge Base"):
        assert leaked not in blob, f"{leaked!r} leaked into {blob}"
    assert all(isinstance(term, str) and "/" not in term for term in suggestion["cluster_terms"])


def test_detector_failure_leaves_the_write_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An advisory that raises must not touch the mutation."""
    path = tmp_path / TRAVEL_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dict(_journey_states())["livestock"], encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("structural analysis exploded")

    monkeypatch.setattr(structure_promotion, "suggest_for_state", _boom)

    result = writer_lease.invoke_command(
        _command("observe_memory"),
        tmp_path,
        path=TRAVEL_PAGE,
        operation="add",
        category="business_option",
        content="Events and seasonal lettings could use the larger barn.",
        tags=["hospitality", "accommodation", "business"],
    )

    assert result["ok"] is True
    assert result["status"] == "committed"
    assert result["mutated"] is True
    assert "structure_suggestion" not in result


# --------------------------------------------------------------------------------
# The public surface: every compiled writer, in the default response
# --------------------------------------------------------------------------------


def test_observe_memory_surfaces_the_suggestion_in_the_default_response(
    tmp_path: Path,
) -> None:
    """The motivating dogfood path. Compact is the default; it must be visible there."""
    path = tmp_path / TRAVEL_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dict(_journey_states())["livestock"], encoding="utf-8")

    result = writer_lease.invoke_command(
        _command("observe_memory"),
        tmp_path,
        path=TRAVEL_PAGE,
        operation="add",
        category="business_option",
        content="Part of the holding could carry accommodation and experience revenue.",
        tags=["hospitality", "accommodation", "business", "smallholding"],
    )

    assert result["status"] == "committed"
    suggestion = result["structure_suggestion"]
    assert suggestion["kind"] == "scope_divergence"
    assert suggestion["strength"] == "strong"


def test_edit_memory_surfaces_the_suggestion_in_the_default_response(
    tmp_path: Path,
) -> None:
    path = tmp_path / TRAVEL_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dict(_journey_states())["livestock"], encoding="utf-8")

    result = writer_lease.invoke_command(
        _command("edit_memory"),
        tmp_path,
        path=TRAVEL_PAGE,
        operation={
            "kind": "edit_section",
            "heading": "## Observations",
            "section_position": "append",
            "new_string": _unit(
                "business_option",
                "Guest accommodation could monetise the excess buildings.",
                ["hospitality", "accommodation", "business", "smallholding"],
                "hospitality-append",
            ),
        },
        why="record the emerging hospitality thread",
    )

    assert result["status"] == "committed"
    assert result["structure_suggestion"]["strength"] == "strong"


def test_a_coherent_write_carries_no_suggestion_key(tmp_path: Path) -> None:
    path = tmp_path / TRAVEL_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_travel_page(), encoding="utf-8")

    result = writer_lease.invoke_command(
        _command("observe_memory"),
        tmp_path,
        path=TRAVEL_PAGE,
        operation="add",
        category="logistics",
        content="Confirm the baggage allowance shown for each passenger before payment.",
        tags=["travel", "family", "carrier-sale"],
    )

    assert result["status"] == "committed"
    assert "structure_suggestion" not in result
