"""Capture date and position as search filters, and who may see the position.

A media sidecar records when its original was captured and where. Search filters on both through
the catalogue. The position is as sensitive as the original: a caller who may not download the
original never sees it, on any surface, and a filter reads such a page as one with no position.

These tests build sidecars through the real preserve path and state their capture fields directly,
so they need no image codecs: reading the fields from a file is `test_capture_metadata.py`'s job.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, freshness, lexstore, media_jobs, preserve
from exomem import find as find_module
from exomem import structured_filters as sf
from exomem.capture_metadata import CaptureFacts
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

pytestmark = pytest.mark.skipif(not lexstore.fts5_available(), reason="SQLite build lacks FTS5")

EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")

# name -> (date, latitude, longitude); None stands for a photo that states no such value.
PHOTOS: dict[str, tuple[str | None, float | None, float | None]] = {
    "helsinki": ("2024-06-01", 60.175, 24.9375),
    "sydney": ("2023-01-15", -33.87, 151.21),
    "fiji": ("2022-03-04", -17.7, 179.9),
    "tonga": ("2021-07-07", -21.1, -175.2),
    "no-position": ("2024-02-02", None, None),
    "scan": (None, None, None),
}
COORDINATES = ("60.175", "24.9375", "-33.87", "151.21", "179.9", "-175.2", "-17.7", "-21.1")

WINDOW = {"page.capture_date": {"$between": ["2024-01-01", "2024-12-31"]}}
BOX = {"$and": [{"page.capture_lat": {"$between": [59, 61]}}, {"page.capture_lon": {"$between": [24, 25]}}]}
ACROSS_THE_ANTIMERIDIAN = {
    "$and": [
        {"page.capture_lat": {"$between": [-30, -10]}},
        {"$or": [{"page.capture_lon": {"$gte": 170}}, {"page.capture_lon": {"$lte": -170}}]},
    ]
}
NORTH_OF_THE_TROPICS = {"page.capture_lat": {"$gte": 23.5}}
HAS_A_POSITION = {"page.frontmatter:/capture_lat": {"$exists": True}}
HAS_NO_POSITION = {"page.frontmatter:/capture_lat": {"$exists": False}}
WINDOW_WITHOUT_THE_BOX = {"$and": [WINDOW, {"$not": BOX}]}


def _write_photo(vault: Path, name: str) -> tuple[str, str]:
    date, lat, lon = PHOTOS[name]
    stored = preserve.preserve_bytes(
        vault, scope="Photos", category="image", filename=f"{name}.jpg",
        data=b"\xff\xd8" + name.encode(), content_type="image/jpeg",
    )
    sidecar = vault / stored.sidecar_path
    text = sidecar.read_text(encoding="utf-8").rstrip("\n") + "\nA harbour at dusk.\n"
    facts = CaptureFacts(
        time=None if date is None else f"{date}T09:00:00", date=date, lat=lat, lon=lon,
        make=None if date is None else "Acme", width=64, height=48,
    )
    sidecar.write_text(preserve.set_capture_fields(text, facts), encoding="utf-8")
    return stored.path, stored.sidecar_path


def _govern(vault: Path, *, pages: int, originals: int) -> None:
    """External callers read the sidecar pages up to `pages` and the original files up to `originals`."""
    gov = vault / "Knowledge Base" / "_Governance"
    for index, (name, glob, ceiling) in enumerate(
        (("pages", "Evidence/**/*.md", pages), ("files", "Evidence/**/*.jpg", originals))
    ):
        scope_id, rule_id = f"01ARZ3NDEKTSV4RRFFQ69G5FB{index * 2 + 1}", f"01ARZ3NDEKTSV4RRFFQ69G5FB{index * 2 + 2}"
        (gov / "scopes").mkdir(parents=True, exist_ok=True)
        (gov / "rules").mkdir(parents=True, exist_ok=True)
        (gov / "scopes" / f"{name}.yaml").write_text(
            f'governance_version: 1\nid: {scope_id}\nname: {name}\npaths: ["{glob}"]\n', encoding="utf-8"
        )
        (gov / "rules" / f"{name}.yaml").write_text(
            f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\naudience: external\nceiling: {ceiling}\n',
            encoding="utf-8",
        )


def _reset(vault: Path) -> None:
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)


@pytest.fixture
def photos(vault: Path) -> dict[str, tuple[str, str]]:
    written = {name: _write_photo(vault, name) for name in PHOTOS}
    _reset(vault)
    return written


def _names(vault: Path, photos: dict[str, tuple[str, str]], filters: dict[str, Any]) -> set[str]:
    """The photos a search with `filters` returns, through the scan route a command-line caller reads."""
    by_sidecar = {sidecar: name for name, (_original, sidecar) in photos.items()}
    hits = find_module.find(
        vault, query="harbour", scope="kb-only", mode="keyword", graph=False, result_level="page",
        filters=filters, limit=20,
    )
    return {by_sidecar[hit.path] for hit in hits}


def _managed_names(
    vault: Path, photos: dict[str, tuple[str, str]], filters: dict[str, Any], *, timed: bool = True
) -> tuple[set[str], str | None]:
    """The same search as the live cell serves it, with where its eligibility came from (owner only)."""
    by_sidecar = {sidecar: name for name, (_original, sidecar) in photos.items()}
    result = commands.op_find(
        vault, query="harbour", mode="keyword", scope="kb-only", graph=False, result_level="page",
        filters=filters, limit=20, include_timings=timed,
    )
    hits = result["hits"] if timed else result
    source = result["timings"]["stages"]["filter_eligibility"]["source"] if timed else None
    return {by_sidecar[hit["path"]] for hit in hits}, source


OWNER_ANSWERS = [
    pytest.param(WINDOW, {"helsinki", "no-position"}, id="date-window"),
    pytest.param(BOX, {"helsinki"}, id="box"),
    pytest.param(ACROSS_THE_ANTIMERIDIAN, {"fiji", "tonga"}, id="box-across-the-antimeridian"),
    pytest.param(WINDOW_WITHOUT_THE_BOX, {"no-position"}, id="window-without-the-box"),
]


@pytest.mark.parametrize(("filters", "expected"), OWNER_ANSWERS)
def test_owner_filters_photos_by_capture_date_and_position(vault, photos, filters, expected):
    assert _names(vault, photos, filters) == expected


@pytest.mark.parametrize(("filters", "expected"), OWNER_ANSWERS)
def test_live_cell_answers_capture_filters_from_the_catalogue(vault, photos, warm_managed_cell, filters, expected):
    warm_managed_cell(vault)

    names, source = _managed_names(vault, photos, filters)

    assert (names, source) == (expected, "index")


def test_a_photo_with_no_date_or_position_matches_no_window_and_no_box(vault, photos):
    assert "scan" not in _names(vault, photos, {"page.capture_date": {"$gte": "1900-01-01"}})
    assert "scan" not in _names(vault, photos, {"page.capture_lat": {"$gte": -90}})


def test_a_position_outside_the_globe_is_not_a_position(vault, photos):
    sidecar = vault / photos["helsinki"][1]
    sidecar.write_text(sidecar.read_text(encoding="utf-8").replace("capture_lat: 60.175", "capture_lat: 600.175"))
    _reset(vault)

    assert "helsinki" not in _names(vault, photos, {"page.capture_lat": {"$gte": -90}})


# --- a caller who may read the page but not download its original -----------------------------------


def _as(principal: RequestPrincipal | None):
    return library_scope() if principal is None else request_scope(principal)


def _raw_keys(content: str) -> list[str]:
    head = content.split("\n---\n", 1)[0]
    return [line.split(":", 1)[0] for line in head.splitlines() if line and not line.startswith((" ", "-"))]


@pytest.mark.parametrize(
    ("principal", "originals", "sees_position"),
    [
        pytest.param(None, 5, True, id="owner"),
        pytest.param(EXTERNAL, 6, True, id="original-released"),
        pytest.param(EXTERNAL, 5, False, id="original-withheld"),
    ],
)
def test_page_read_shows_the_position_only_to_a_caller_who_may_download_the_original(
    vault, photos, principal, originals, sees_position
):
    _govern(vault, pages=6, originals=originals)
    _reset(vault)

    with _as(principal):
        page = commands.op_get(vault, photos["helsinki"][1], include_raw=True)

    shown = {*page["frontmatter"], *_raw_keys(page["content"])} & {"capture_lat", "capture_lon"}
    assert shown == ({"capture_lat", "capture_lon"} if sees_position else set())
    assert page["frontmatter"]["capture_date"] == "2024-06-01"
    assert page["frontmatter"]["camera_make"] == "Acme"
    assert json.dumps(page, default=str).count("60.175") == (2 if sees_position else 0)


SPELLINGS = [
    pytest.param([("capture_lat:", '"capture_lat":'), ("capture_lon:", '"capture_lon":')], id="quoted-keys"),
    pytest.param([("capture_lat:", "capture_lat :"), ("capture_lon:", "capture_lon :")], id="spaced-keys"),
    pytest.param([('camera_make: "Acme"', 'camera_make: "Acme\\q"')], id="unparseable-frontmatter"),
]


@pytest.mark.parametrize("respelled", SPELLINGS)
def test_a_restricted_raw_read_shows_no_position_however_the_page_spells_it(vault, photos, respelled):
    """Catches raw text that keeps a position the parsed page no longer shows: a key the line check does not
    recognise, or frontmatter that does not parse, so the page reads as having none to withhold."""
    sidecar = vault / photos["helsinki"][1]
    text = sidecar.read_text(encoding="utf-8")
    for old, new in respelled:
        assert old in text
        text = text.replace(old, new)
    sidecar.write_text(text, encoding="utf-8")
    _govern(vault, pages=6, originals=5)
    _reset(vault)

    with library_scope():
        owner = commands.op_get(vault, photos["helsinki"][1], include_raw=True)
    with request_scope(EXTERNAL):
        external = commands.op_get(vault, photos["helsinki"][1], include_raw=True)

    assert "60.175" in owner["content"]
    assert not [digits for digits in ("60.175", "24.9375") if digits in json.dumps(external, default=str)]


def test_search_hits_carry_no_coordinates_for_anyone(vault, photos):
    _govern(vault, pages=6, originals=6)
    _reset(vault)

    with library_scope():
        owner = json.dumps(commands.op_find(vault, query="harbour", mode="keyword", scope="kb-only", graph=False))
        by_coordinate = commands.op_find(vault, query="60.175", mode="keyword", scope="kb-only", graph=False)
    with request_scope(EXTERNAL):
        external = json.dumps(commands.op_find(vault, query="harbour", mode="keyword", scope="kb-only", graph=False))

    assert not [text for text in COORDINATES if text in owner or text in external]
    assert not by_coordinate


WITHHELD_ANSWERS = [
    pytest.param(NORTH_OF_THE_TROPICS, set(), id="latitude-range"),
    pytest.param(HAS_A_POSITION, set(), id="pointer-exists"),
    pytest.param(HAS_NO_POSITION, set(PHOTOS), id="pointer-does-not-exist"),
    pytest.param(WINDOW_WITHOUT_THE_BOX, {"helsinki", "no-position"}, id="not-in-the-box"),
    pytest.param(WINDOW, {"helsinki", "no-position"}, id="date-still-filters"),
]
RELEASED_ANSWERS = [
    pytest.param(NORTH_OF_THE_TROPICS, {"helsinki"}, id="latitude-range"),
    pytest.param(HAS_A_POSITION, {"helsinki", "sydney", "fiji", "tonga"}, id="pointer-exists"),
    pytest.param(HAS_NO_POSITION, {"no-position", "scan"}, id="pointer-does-not-exist"),
    pytest.param(WINDOW_WITHOUT_THE_BOX, {"no-position"}, id="not-in-the-box"),
    pytest.param(WINDOW, {"helsinki", "no-position"}, id="date-still-filters"),
]


@pytest.mark.parametrize(("filters", "expected"), WITHHELD_ANSWERS)
def test_a_withheld_position_filters_exactly_as_if_the_page_had_none(vault, photos, filters, expected):
    _govern(vault, pages=6, originals=5)
    _reset(vault)

    with request_scope(EXTERNAL):
        assert _names(vault, photos, filters) == expected


@pytest.mark.parametrize(("filters", "expected"), RELEASED_ANSWERS)
def test_a_position_the_caller_may_see_filters_like_the_owners(vault, photos, filters, expected):
    _govern(vault, pages=6, originals=6)
    _reset(vault)

    with request_scope(EXTERNAL):
        assert _names(vault, photos, filters) == expected


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param(BOX, set(), id="box"),
        pytest.param(ACROSS_THE_ANTIMERIDIAN, set(), id="box-across-the-antimeridian"),
        pytest.param(WINDOW_WITHOUT_THE_BOX, {"helsinki", "no-position"}, id="not-in-the-box"),
        pytest.param(WINDOW, {"helsinki", "no-position"}, id="date-still-filters"),
    ],
)
def test_live_cell_reads_a_withheld_position_as_none_from_its_catalogue(
    vault, photos, warm_managed_cell, filters, expected
):
    _govern(vault, pages=6, originals=5)
    _reset(vault)
    warm_managed_cell(vault)

    with request_scope(EXTERNAL):
        assert _managed_names(vault, photos, filters, timed=False)[0] == expected


def test_the_hot_cache_never_hands_the_owners_positioned_answer_to_a_withheld_caller(vault, photos):
    _govern(vault, pages=6, originals=5)
    _reset(vault)

    with library_scope():
        assert _names(vault, photos, NORTH_OF_THE_TROPICS) == {"helsinki"}
    with request_scope(EXTERNAL):
        assert _names(vault, photos, NORTH_OF_THE_TROPICS) == set()
    with library_scope():
        assert _names(vault, photos, NORTH_OF_THE_TROPICS) == {"helsinki"}


def _receipt_lines(vault: Path) -> int:
    events = vault / "Knowledge Base" / "_Governance" / "events"
    return sum(len(path.read_text(encoding="utf-8").splitlines()) for path in events.rglob("*.jsonl")) if events.exists() else 0


@pytest.mark.parametrize("originals", [0, 5, 6])
def test_position_access_follows_the_decision_that_hands_over_the_original(vault, photos, originals):
    _govern(vault, pages=6, originals=originals)
    _reset(vault)
    original = photos["helsinki"][0]

    allowed = egress.original_download_filter(vault, principal=EXTERNAL)
    receipts_before = _receipt_lines(vault)
    quiet = allowed(original)
    receipts_after_quiet = _receipt_lines(vault)
    with request_scope(EXTERNAL):
        handed_over = egress.release_allows_download(vault, original, principal=EXTERNAL)

    assert quiet is handed_over is (originals == 6)
    assert receipts_after_quiet == receipts_before
    assert allowed(None) is False
    assert egress.original_download_filter(vault, principal=None) is None


# --- the extraction result that a media job hands back ---------------------------------------------


def test_a_job_result_with_an_out_of_range_position_is_discarded():
    good = {"text": "words", "engine": "ocr", "capture": {"capture_lat": 60.175, "capture_lon": 24.9375}}
    bad = {"text": "words", "engine": "ocr", "capture": {"capture_lat": 91.0, "capture_lon": 24.9375}}

    assert media_jobs._validated_result_payload("extraction", good) == good
    assert media_jobs._validated_result_payload("extraction", bad) is None


# --- a catalogue that the previous release built ------------------------------------------------------


def test_a_catalogue_built_before_the_capture_columns_is_rebuilt_with_them(vault, photos):
    import sqlite3

    database = lexstore.lexical_path(vault)
    connection = sqlite3.connect(database)
    try:
        for index in ("pages_capture_day", "pages_capture_lat", "pages_capture_lon"):
            connection.execute(f"DROP INDEX {index}")
        for column in ("capture_day", "capture_date_json", "capture_lat", "capture_lon"):
            connection.execute(f"ALTER TABLE pages DROP COLUMN {column}")
        connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(lexstore.SCHEMA_VERSION - 1),))
        connection.commit()
    finally:
        connection.close()

    assert lexstore.ensure_fresh(vault)

    connection = sqlite3.connect(database)
    try:
        in_the_box = {
            path
            for (path,) in connection.execute("SELECT path FROM pages WHERE capture_lat BETWEEN 59 AND 61")
        }
    finally:
        connection.close()
    assert in_the_box == {photos["helsinki"][1]}


@pytest.mark.parametrize(
    ("filters", "index"),
    [
        (WINDOW, "pages_capture_day"),
        (BOX, "pages_capture_lon"),
        ({"$and": [WINDOW, BOX]}, "pages_capture_"),
    ],
)
def test_a_capture_filter_scans_a_capture_index_not_the_whole_scope(filters, index):
    # Every page is in the scope, so a scan driven from the scope flag reads the whole catalogue: the
    # cost that grows with the library. SQLite has no statistics here, as in a freshly built catalogue.
    conn = sqlite3.connect(":memory:")
    lexstore.LexicalStore._create_catalog_tables(conn)
    lexstore.LexicalStore._create_catalog_indexes(conn)
    eligibility = sf.plan_index_eligibility(sf.compile_filter(filters))
    predicate, params = lexstore._eligibility_predicate(eligibility, "in_kb", None)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT p.path FROM pages p WHERE "
        f"{lexstore._scope_term(eligibility, 'in_kb')} AND ({predicate})", params
    ).fetchall()
    assert index in " ".join(str(row[3]) for row in plan)


def test_a_store_catalogue_built_before_the_capture_columns_reads_as_not_current_until_the_owner_pass_adds_them():
    """Catches a store that stays "current" over a page table with no capture columns, so its capture filters fail or answer nothing."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(":memory:")) as catalogue:
        lexstore.create_catalogue_schema(catalogue)
        lexstore.stamp_catalogue_schema(catalogue)
        assert lexstore.catalogue_schema_current(catalogue)
        for index in ("pages_capture_day", "pages_capture_lat", "pages_capture_lon"):
            catalogue.execute(f"DROP INDEX {index}")
        for column in ("capture_day", "capture_date_json", "capture_lat", "capture_lon"):
            catalogue.execute(f"ALTER TABLE pages DROP COLUMN {column}")

        assert not lexstore.catalogue_schema_current(catalogue)  # the stamp is current; the page shape is not

        lexstore.create_catalogue_schema(catalogue)  # the owner pass

        assert lexstore.catalogue_schema_current(catalogue)
        kinds = {row[1]: row[2] for row in catalogue.execute("PRAGMA table_info(pages)") if row[1].startswith("capture_")}
        assert kinds == {"capture_day": "TEXT", "capture_date_json": "TEXT", "capture_lat": "REAL", "capture_lon": "REAL"}
        indexes = {row[1] for row in catalogue.execute("PRAGMA index_list(pages)")}
        assert {"pages_capture_day", "pages_capture_lat", "pages_capture_lon"} <= indexes


@pytest.mark.parametrize("coordinate", [0.00005, -0.00003])
def test_a_position_that_python_prints_in_scientific_notation_stays_a_number_in_the_sidecar(coordinate):
    """Catches a coordinate written as `5e-05`, which YAML reads as text: the page then has no position to filter."""
    from exomem.vault import parse_frontmatter

    text = preserve.set_capture_fields("---\ntitle: x\n---\nbody\n", CaptureFacts(lat=coordinate, lon=coordinate))

    stated = parse_frontmatter(text, custody=True)[0]

    assert (stated["capture_lat"], stated["capture_lon"]) == (coordinate, coordinate)
