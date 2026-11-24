"""The inbound-citation reader answers who cites a page, as the caller may see it.

The fixtures reuse the link-values log: a record collection whose `page` field links to pages by ref.
"""

import contextlib

import pytest
from test_collection_link_values import (
    ALPHA,
    BETA,
    HIDDEN,
    K1,
    K2,
    K3,
    LOG,
    _ready,
    append,
    link_warnings,
    log_text,
    page,
)
from test_collection_row_references import collection_text, path
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import SCOPE_ID, _external, write_rule, write_scope
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import freshness, page_citations, record_governance, semantic_contract
from exomem import structured_collections as collections
from exomem.collection_store import connection
from exomem.collection_store import governance as collection_governance
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_resolution, runtime
from exomem.vault import walk_vault_md

OTHER_LOG = "ffffffff-ffff-4fff-8fff-ffffffffffff"
ALPHA_REF = f"exomem://memory/{ALPHA}"
BETA_REF = f"exomem://memory/{BETA}"
AWAY = "exomem://memory/99999999-9999-4999-8999-999999999999"


@pytest.fixture
def world(store, monkeypatch, corpus_cache_on):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    page(store.root, "Notes/Beta.md", "Beta", BETA)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    publish(store.root)
    return store


def publish(root):
    """Publish the corpus generation that a running service keeps current; a citation read borrows it to find pages.

    The watcher's seed comes first, as a service's does, so the generation counts as live.
    """
    freshness.seed(root, "vault", ((str(found), freshness.stat_signature(found)) for found in walk_vault_md(root)))
    semantic_contract.build_corpus_context(root)


def cited(store, who=None, **arguments):
    with request_scope(who or owner_principal()):
        return page_citations.inbound_citations(ALPHA_REF, store.root, **arguments)


def test_a_ready_page_lists_each_citing_row_with_what_a_reader_needs(world):
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": "Alpha"}, key=K2)
    append(world, {"page": BETA_REF}, key=K3)
    _ready(world)
    reply = cited(world, limit=1)
    assert (reply["state"], reply["count"]) == ("ready", 2)
    [first] = reply["items"]
    assert first == {"profile": "records", "collection": LOG, "collection_title": "Log", "label": "c1",
                     "ref": f"exomem://record/{LOG}/{K1}", "field": "page", "relation": "cites"}
    second = cited(world, limit=1, more=reply["more"])
    assert [item["label"] for item in second["items"]] == ["c2"]
    assert second["count"] == 2 and second["more"] is None


def test_a_warming_index_never_reads_as_an_empty_list(world):
    append(world, {"page": ALPHA_REF})
    assert cited(world) == {"state": "warming"}
    _ready(world)
    assert cited(world)["count"] == 1


def test_a_withheld_page_answers_exactly_like_a_missing_one(world):
    page(world.root, "Private/Secret.md", "Secret", HIDDEN)
    publish(world.root)
    append(world, {"page": f"exomem://memory/{HIDDEN}"})
    _ready(world)
    with request_scope(owner_principal()):
        assert page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)["count"] == 1
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret.md"]\n')
    with request_scope(owner_principal()):
        withheld = page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)
        missing = page_citations.inbound_citations(AWAY, world.root)
    assert withheld == missing == {"state": "ready", "items": [], "count": 0, "count_complete": True, "more": None}


def write_tag_scope(root):
    """A scope that selects pages by a tag in their frontmatter, so no path alone places a page in or out of it."""
    target = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'governance_version: 1\nid: {SCOPE_ID}\nname: Patterns\ntags: ["secret"]\n')


def test_a_scope_that_selects_pages_by_tag_leaves_the_owner_s_citations_and_links_unchanged(world):
    """Deciding a linked page by its path alone withheld every page from everyone once any scope read page metadata."""
    append(world, {"page": ALPHA_REF}, key=K1)
    write_tag_scope(world.root)
    publish(world.root)
    stored = append(world, {"page": "Alpha"}, key=K2)
    _ready(world)
    assert link_warnings(stored) == []
    assert cited(world)["count"] == 2


def test_a_page_that_a_tag_scope_withholds_answers_exactly_like_a_missing_one(world):
    secret = world.root / "Knowledge Base/Notes/Secret.md"
    secret.write_text(f"---\nexomem_id: {HIDDEN}\ntitle: Secret\ntags: [secret]\n---\n# Secret\n")
    append(world, {"page": f"exomem://memory/{HIDDEN}"}, key=K1)
    append(world, {"page": ALPHA_REF}, key=K2)
    _ready(world)
    write_tag_scope(world.root)
    write_rule(world.root, ceiling=0)
    publish(world.root)
    with request_scope(_external()):
        withheld = page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)
        missing = page_citations.inbound_citations(AWAY, world.root)
        untagged = page_citations.inbound_citations(ALPHA_REF, world.root)
    assert withheld == missing == {"state": "ready", "items": [], "count": 0, "count_complete": True, "more": None}
    assert untagged["count"] == 1
    with request_scope(owner_principal()):
        assert page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)["count"] == 1


def test_a_row_of_a_collection_the_caller_cannot_read_is_absent_from_items_and_count(world):
    world.create_collection(path("Other"), collection_text(OTHER_LOG, "Other", "label", "    label: {type: string, required: true}\n"
                                                           "    page: {type: link}\n"),
                            why="declare", scaffold=False)
    append(world, {"page": ALPHA_REF}, key=K1)
    with request_scope(owner_principal()):
        world.append_record(OTHER_LOG, item={"label": "o", "page": ALPHA_REF}, item_key=K2, why="record")
    _ready(world)
    _ready(world, OTHER_LOG)
    assert cited(world)["count"] == 2
    write_scope(world.root, paths="Knowledge Base/Records/Other/**")
    write_rule(world.root, ceiling=0)
    seen = cited(world, who=_external())
    assert seen["count"] == 1 and [item["collection"] for item in seen["items"]] == [LOG]


def test_a_withheld_row_of_a_collection_the_caller_can_read_is_absent_from_items_and_count(world):
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": ALPHA_REF}, key=K2)
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, K2)}"]\n')
    write_rule(world.root, ceiling=0)
    seen = cited(world, who=_external())
    assert seen["count"] == 1 and [item["label"] for item in seen["items"]] == ["c1"]


def test_a_reader_whom_no_row_rule_names_counts_every_row_without_deciding_one(world, monkeypatch):
    """The scope that names one row carries a rule for the external audience only, so for the owner no row differs."""
    for key in (K1, K2, K3):
        append(world, {"page": ALPHA_REF}, key=key)
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, K2)}"]\n')
    write_rule(world.root, ceiling=0)
    decided = []
    batch, row_filter = reference_resolution.rows_released, reference_resolution.row_filter

    def counted(session, lookup, row_ids):
        decided.append(len(row_ids))
        return batch(session, lookup, row_ids)

    def filtered(session, lookup):
        found = row_filter(session, lookup)
        if found is not None:
            decided.append("filter")
        return found

    monkeypatch.setattr(reference_resolution, "rows_released", counted)
    monkeypatch.setattr(reference_resolution, "row_filter", filtered)
    assert cited(world)["count"] == 3 and decided == []
    assert cited(world, who=_external())["count"] == 2 and decided == ["filter"]


TAGGED = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SECOND_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FC2"


def _tagged_rows(world):
    """Five rows that scopes tell apart by tag and by ref; their labels, by row id.

    The first scope selects the tag ``secret`` and row 2 by ref, and excludes row 4 by ref, so it holds
    rows 1, 2 and 3. The second scope names rows 0 and 3 by ref, so row 3 lies in both.
    """
    def key(number):
        return f"00000000-0000-4000-8000-{number:012d}"

    world.create_collection(path("Tagged"), collection_text(TAGGED, "Tagged", "label", (
        "    label: {type: string, required: true}\n"
        "    page: {type: link}\n"
        "    tags: {type: array, items: {type: string}}\n")), why="declare", scaffold=False)
    with request_scope(owner_principal()):
        for number, tags in enumerate(([], ["secret"], [], ["secret"], ["secret"])):
            world.append_record(TAGGED, item={"label": f"r{number}", "page": ALPHA_REF, "tags": tags},
                                item_key=key(number), why="record")
    _ready(world)
    _ready(world, TAGGED)
    governance = world.root / "Knowledge Base/_Governance/scopes"
    governance.mkdir(parents=True, exist_ok=True)
    (governance / "second.yaml").write_text(
        f'governance_version: 1\nid: {SECOND_SCOPE}\nname: Second\npaths: ["Unrelated/**"]\n'
        f'refs: ["{collections.record_ref(TAGGED, key(0))}", "{collections.record_ref(TAGGED, key(3))}"]\n')
    return {row_id: f"r{int(item_key[-12:])}" for row_id, item_key in world.handle.connection.execute(
        "SELECT row_id, item_key FROM items WHERE collection_id=?", (TAGGED,))}


def _first_scope(world, extra):
    key = "00000000-0000-4000-8000-{:012d}".format
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + 'tags: ["secret"]\n'
                     + f'refs: ["{collections.record_ref(TAGGED, key(2))}"]\n'
                     + f'exclude:\n  refs: ["{collections.record_ref(TAGGED, key(4))}"]\n' + extra)


@pytest.mark.parametrize("purpose", [None, "audit"])
@pytest.mark.parametrize(("scope_extra", "rule", "withheld"), [
    pytest.param("", {"ceiling": 0}, {"r1", "r2", "r3"}, id="ceiling-0 for the reader"),
    pytest.param("", {"ceiling": 0, "audience": "partner"}, set(), id="a rule for another audience"),
    pytest.param("default_deny: true\n", None, {"r1", "r2", "r3"}, id="a default-deny scope"),
    pytest.param("", {"ceiling": 4, "extra": "kind: org_cap\n"}, {"r1", "r2", "r3"}, id="an org cap"),
    pytest.param("", {"ceiling": 0, "extra": "purpose: audit\npurpose_condition: outside\n"},
                 {"r1", "r2", "r3"}, id="an outside purpose condition"),
    pytest.param("", {"ceiling": 5}, {"r1", "r2", "r3"}, id="ceiling-5 for the reader"),
    # Each scope alone releases the reader, but only the first carries a constraint: their meet differs
    # in that option, which lowers the row that lies in both.
    pytest.param("constraint: Reviewed before release.\n", None, {"r3"}, id="two scopes that combine"),
])
def test_rows_decided_together_release_exactly_the_rows_that_one_by_one_decisions_release(
        world, scope_extra, rule, withheld, purpose):
    labels = _tagged_rows(world)
    _first_scope(world, scope_extra)
    if rule is not None:
        write_rule(world.root, **rule)

    def read(decide):
        with request_scope(_external(purpose)), runtime.read_session(world.root, world.handle.path) as session:
            return decide(session, session.admit_lookup(TAGGED, visit_rows=False))

    keys = dict(world.handle.connection.execute("SELECT row_id, item_key FROM items WHERE collection_id=?", (TAGGED,)))
    one_by_one = read(lambda session, lookup: {
        row_id for row_id, key in keys.items() if reference_resolution.row_released(session, lookup, key)})
    together, grouped, uniform = read(lambda session, lookup: (
        reference_resolution.rows_released(session, lookup, keys),
        session._authorization.released_rows(TAGGED, keys), lookup.uniform))
    assert together == grouped == one_by_one
    assert {labels[row_id] for row_id in keys.keys() - one_by_one} == withheld
    assert uniform == (not withheld)
    with request_scope(_external(purpose)):
        reply = page_citations.inbound_citations(ALPHA_REF, world.root, limit=10)
    assert sorted(item["label"] for item in reply["items"]) == sorted(labels[row_id] for row_id in one_by_one)
    assert (reply["count"], reply["count_complete"]) == (len(one_by_one), True)


def traced_statements(monkeypatch) -> list[str]:
    """The statements every citation read runs from now on, in order."""
    statements: list[str] = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)
    return statements


def test_a_batch_reads_the_index_twice_for_all_its_pages(world, monkeypatch):
    """One probe finds the fields that cite any asked page, and one read lists their entries, however many pages."""
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": BETA_REF, "pages": [ALPHA_REF]}, key=K2)
    _ready(world)
    statements = traced_statements(monkeypatch)
    with request_scope(owner_principal()):
        replies = page_citations.inbound_citations_batch([ALPHA_REF, BETA_REF, AWAY], world.root, per_ref=5)
    assert sum("item_references r" in sql for sql in statements) == 2
    assert {ref: reply["count"] for ref, reply in replies.items()} == {ALPHA_REF: 2, BETA_REF: 1, AWAY: 0}


def test_a_read_runs_the_same_statements_however_many_governed_rows_cite_the_page(world, monkeypatch):
    """A policy that decides rows one by one costs one subject read per collection, never a statement per citing row."""
    def key(number):
        return f"00000000-0000-4000-8000-{number:012d}"

    for number in range(3):
        append(world, {"page": ALPHA_REF}, key=key(number))
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, key(1))}"]\n')
    write_rule(world.root, ceiling=0)
    statements = traced_statements(monkeypatch)

    def measured_read():
        _ready(world)
        cited(world, who=_external(), limit=1)  # warm the caches a running service keeps warm
        statements.clear()
        return cited(world, who=_external(), limit=1)["count"], len(statements)

    few = measured_read()
    for number in range(3, 40):
        append(world, {"page": ALPHA_REF}, key=key(number))
    many = measured_read()
    assert (few[0], many[0]) == (2, 39)
    assert few[1] == many[1]


def _cite_one_by_one(world, monkeypatch):
    """Alpha is cited by rows 00 to 03 and Beta by rows 04 to 06; a ceiling-0 rule withholds row 01 from external readers.

    The rule names one row, and a row filter may decide no metadata value here, so every row of the
    collection is decided on its own. Alpha's withheld row is the only difference between the pages: for
    an external reader each has three visible citing rows.
    """
    monkeypatch.setattr(collection_governance, "MAX_FILTER_METADATA", 0)
    def key(number):
        return f"00000000-0000-4000-8000-{number:012d}"

    for number in range(7):
        append(world, {"page": ALPHA_REF if number < 4 else BETA_REF}, key=key(number))
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, key(1))}"]\n')
    write_rule(world.root, ceiling=0)


def external_read(world, ref, **arguments):
    with request_scope(_external()):
        return page_citations.inbound_citations(ref, world.root, **arguments)


def test_past_the_count_limit_the_count_is_a_lower_bound_and_paging_still_reaches_every_row(world, monkeypatch):
    _cite_one_by_one(world, monkeypatch)
    monkeypatch.setenv(page_citations.COUNT_LIMIT_ENV, "2")
    reply = external_read(world, ALPHA_REF, limit=1)
    assert (reply["count"], reply["count_complete"]) == (2, False)
    labels = [item["label"] for item in reply["items"]]
    while reply["more"] is not None:
        reply = external_read(world, ALPHA_REF, limit=1, more=reply["more"])
        labels += [item["label"] for item in reply["items"]]
    assert labels == ["00", "02", "03"]
    with request_scope(_external()):
        hit = page_citations.for_hits([ALPHA_REF], world.root)[ALPHA_REF]
    assert (hit["cited_by_count"], hit["cited_by_count_complete"]) == (2, False)


@pytest.mark.parametrize(("count_limit", "expected"), [("2", (2, False)), ("3", (3, True))])
def test_a_withheld_row_never_moves_where_the_count_limit_cuts_the_count(world, monkeypatch, count_limit, expected):
    """At a limit of 3 both pages have exactly the limit visible, so both counts are exact.

    A probe past the limit by index entries instead of visible rows would find Alpha's fourth entry and
    call Alpha's count a lower bound, but not Beta's.
    """
    _cite_one_by_one(world, monkeypatch)
    monkeypatch.setenv(page_citations.COUNT_LIMIT_ENV, count_limit)
    alpha, beta = external_read(world, ALPHA_REF), external_read(world, BETA_REF)
    assert (alpha["count"], alpha["count_complete"]) == (beta["count"], beta["count_complete"]) == expected


@pytest.mark.query_deadline
def test_thousands_of_withheld_citing_rows_answer_as_a_page_that_has_none(world):
    """Under the shipped deadline, Alpha's 5,000 withheld citing rows leave its reply equal to Beta's.

    Deciding each withheld row would run past the deadline, so Alpha alone would read as unavailable.
    """
    rows = ([{"label": f"b{number}", "page": BETA_REF} for number in range(3)]
            + [{"label": f"a{number}", "page": ALPHA_REF} for number in range(3)]
            + [{"label": f"w{number:04d}", "page": ALPHA_REF} for number in range(5_000)])
    _evidence(world.root)
    for start in range(0, len(rows), 500):
        guards = world.inspect_collection(LOG)["lifecycle_guards"]
        with request_scope(owner_principal()):
            world.bulk_upsert_records(LOG, rows=[{"item": item} for item in rows[start:start + 500]], why="seed",
                                      source=EVIDENCE, expected_container_hash=guards["expected_container_hash"])
    _ready(world)
    withheld = [key for (key,) in world.handle.connection.execute(
        "SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT -1 OFFSET 6", (LOG,))]
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "refs:\n" + "".join(
        f'  - "{collections.record_ref(LOG, key)}"\n' for key in withheld))
    write_rule(world.root, ceiling=0)
    external_read(world, AWAY)  # the policy's first read compiles it, whichever page it asks about
    alpha, beta = external_read(world, ALPHA_REF), external_read(world, BETA_REF)
    assert [item["label"] for item in alpha.pop("items")] == ["a0", "a1", "a2"]
    assert [item["label"] for item in beta.pop("items")] == ["b0", "b1", "b2"]
    assert alpha == beta == {"state": "ready", "count": 3, "count_complete": True, "more": None}


def test_with_no_published_corpus_to_borrow_a_read_is_unavailable_never_a_walk_or_an_empty_list(world, monkeypatch):
    append(world, {"page": ALPHA_REF})
    _ready(world)
    freshness.clear()
    semantic_contract.reset_corpus_context_cache()

    def walked(*args, **kwargs):
        raise AssertionError("a citation read walked the vault")

    monkeypatch.setattr(record_governance._LinkProjector, "_walk_candidate_index", walked)
    assert cited(world) == {"state": "unavailable"}


def test_a_value_that_is_not_a_ref_is_unavailable_never_an_empty_list(world):
    with request_scope(owner_principal()):
        reply = page_citations.inbound_citations("Alpha", world.root)
    assert reply == {"state": "unavailable"}


def test_a_row_is_cited_by_the_rows_that_link_to_it_and_a_withheld_row_answers_like_a_missing_one(world):
    target = collections.record_ref(LOG, K1)
    append(world, {"pages": [target]}, key=K2)
    _ready(world)

    def asked(ref, who):
        with request_scope(who):
            return page_citations.inbound_citations(ref, world.root)

    append(world, {}, key=K1)
    assert [item["label"] for item in asked(target, owner_principal())["items"]] == ["c2"]
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{target}"]\n')
    write_rule(world.root, ceiling=0)
    missing = collections.record_ref(LOG, "00000000-0000-4000-8000-0000000000ee")
    assert asked(target, _external()) == asked(missing, _external()) == {
        "state": "ready", "items": [], "count": 0, "count_complete": True, "more": None}
