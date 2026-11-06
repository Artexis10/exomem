"""A large extracted document stays searchable and readable end to end as one document.

Real preserve, real text extraction through the job runtime, real find and real
read_memory. Nothing is replaced: the original is plain UTF-8 text, which needs no engine.
"""

import os
import random
from pathlib import Path

import pytest

from exomem import (
    audit,
    commands,
    delete_file,
    extract,
    extraction_parts,
    find_parts,
    media_processing,
    media_worker,
    preserve,
    reconcile,
    vault_portability,
)
from exomem import find as find_module
from exomem.vault import parse_frontmatter

PLANTED = "zorblax quillfeather"


def _document(size: int) -> str:
    """Paragraphs of invented words, `size` characters in all, with `PLANTED` in the last 4 KB."""
    rng = random.Random(7)
    vocabulary = [f"w{rng.randrange(10**6):06d}" for _ in range(500)]
    paragraphs: list[str] = []
    total = 0
    while total < size:
        paragraph = " ".join(rng.choice(vocabulary) for _ in range(rng.randrange(40, 120)))
        paragraphs.append(paragraph)
        total += len(paragraph) + 2
    text = "\n\n".join(paragraphs)[:size].rstrip()
    near_end = len(text) - 2000
    return text[:near_end] + f" {PLANTED} " + text[near_end:]


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)


def _extract(vault, filename: str, text: str):
    stub = preserve.preserve_bytes(
        vault, scope="Rental", category="docs", filename=filename, data=text.encode("utf-8")
    )
    binary = vault / stub.path
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()
    return stub


def test_a_five_mebibyte_document_is_one_findable_document_read_in_windows(vault) -> None:
    text = _document(5 * 1024 * 1024)
    stub = _extract(vault, "big-report.txt", text)

    hits = find_module.find(vault, query=PLANTED)

    assert [hit.path for hit in hits] == [stub.sidecar_path]
    hit = hits[0]
    assert hit.matched_part is not None and hit.matched_part > 1
    assert PLANTED in hit.passage["text"]

    found = commands.op_read_memory(vault, path=stub.sidecar_path, offset=hit.passage["offset"])
    assert PLANTED in found["body"]
    assert found["window"]["part"] == hit.matched_part

    walked: list[str] = []
    offset: int | None = 0
    while offset is not None:
        window = commands.op_read_memory(vault, path=stub.sidecar_path, offset=offset)
        assert window["window"]["offset"] == offset
        assert len(window["body"]) <= 48 * 1024
        assert window["body_truncated"] and "content_hash" not in window  # a window cannot guard a write
        walked.append(window["body"])
        offset = window["window"]["next_offset"]
    assert "".join(walked) == text

    listing = commands.op_browse_memory(vault, path=os.path.dirname(stub.path), mode="list")
    assert sorted(entry["name"] for entry in listing["entries"]) == [
        os.path.basename(stub.path),
        os.path.basename(stub.sidecar_path),
    ]


def test_a_log_without_word_breaks_returns_a_bounded_passage_that_still_opens_the_match(vault) -> None:
    # One compact JSON record per line has no spaces, so each line is one "word": a passage of
    # MAX_WORDS_PER_CHUNK words around the match ran to hundreds of kilobytes in every hit.
    # The phrase sits inside a record longer than the bound, far from where its word starts.
    record = '{"level":"INFO","logger":"exomem.mutation_lock","fields":{"wait_ms":1.33,"holder":"x"}}' * 6
    lines = [f'{{"line":{number}}}{record}' for number in range(400)]
    lines[300] = f'{lines[300] * 10}{{"message":"{PLANTED}"}}{lines[300]}'
    text = "\n".join(lines)
    stub = _extract(vault, "ci-shard.log.txt", text)

    [hit] = find_module.find(vault, query=PLANTED)

    assert PLANTED in hit.passage["text"]
    assert len(hit.passage["text"]) <= find_parts.MAX_PASSAGE_CHARS
    found = commands.op_read_memory(vault, path=stub.sidecar_path, offset=hit.passage["offset"])
    assert PLANTED in found["body"]


def _small_parted_source(vault, monkeypatch: pytest.MonkeyPatch, filename: str = "note.txt"):
    """A Source whose text needs several parts at a 4 KB page, written the way the worker writes it."""
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 4000)
    stub = preserve.preserve_bytes(
        vault, scope="Test", category="docs", filename=filename, data=b"original bytes"
    )
    text = "\n\n".join(
        f"paragraph {number} " + "alpha beta gamma " * 20 + (PLANTED if number == 55 else "")
        for number in range(60)
    ).strip()
    preserve.update_sidecar_extraction(vault, vault / stub.sidecar_path, text=text, engine="text")
    return stub, text


def test_reading_a_part_page_returns_its_source_as_one_document(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    part = extraction_parts.part_path(page, 3).relative_to(vault).as_posix()

    from_part = commands.op_read_memory(vault, path=part, offset=0)
    from_source = commands.op_read_memory(vault, path=stub.sidecar_path, offset=0)

    assert from_part["path"] == stub.sidecar_path
    assert from_part["frontmatter"]["exomem_id"] == from_source["frontmatter"]["exomem_id"]
    assert from_part["body"] == from_source["body"]


@pytest.mark.parametrize("past_end", [True, False], ids=["past-the-end", "not-a-number"])
def test_an_offset_outside_the_text_is_refused(vault, monkeypatch, past_end) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)

    with pytest.raises(ValueError, match="INVALID_OFFSET"):
        commands.op_read_memory(vault, path=stub.sidecar_path, offset=len(text) if past_end else True)


def test_a_walk_after_an_edit_reads_the_text_as_it_is_now(vault, monkeypatch) -> None:
    page_rel, _text = _adopted_parted_source(vault, monkeypatch)
    page = vault / page_rel
    commands.op_edit(
        vault, page_rel, "shorten the first paragraph",
        old_string="paragraph 0 " + "alpha beta gamma " * 20, new_string="paragraph 0 ",
    )
    walked: list[str] = []
    offset: int | None = 0
    while offset is not None and len(walked) < 100:
        read = commands.op_read_memory(vault, path=page_rel, offset=offset)
        assert read["window"]["next_offset"] is None or read["window"]["next_offset"] > offset
        walked.append(read["body"])
        offset = read["window"]["next_offset"]

    assert "".join(walked) == extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8"))


def test_a_whitespace_run_longer_than_a_page_splits_into_pages_at_least_half_full(monkeypatch) -> None:
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 4000)
    text = "a" * 3000 + " " * 20_000 + "\n\n\n" + "b" * 9000

    pieces = extraction_parts.split(text)

    assert "".join(pieces) == text
    assert all(len(piece.encode("utf-8")) <= 4000 for piece in pieces)
    assert all(len(piece) >= 2000 for piece in pieces[1:-1])


def test_a_shorter_extraction_leaves_no_stale_part(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    assert extraction_parts.folder_for(page).is_dir()

    preserve.update_sidecar_extraction(vault, page, text="a short replacement", engine="text")

    assert not extraction_parts.folder_for(page).exists()
    assert "extraction_parts" not in page.read_text(encoding="utf-8")
    assert find_module.find(vault, query=PLANTED) == []


def test_moving_a_source_moves_its_parts_with_it(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    old_page = vault / stub.sidecar_path

    commands.op_move_file(vault, old_path=stub.path, new_path=stub.path.replace("docs", "moved"))

    new_page = vault / stub.sidecar_path.replace("docs", "moved")
    assert not extraction_parts.folder_for(old_page).exists()
    assert extraction_parts.whole_text(vault, new_page, new_page.read_text(encoding="utf-8")) == text
    hits = find_module.find(vault, query=PLANTED)
    assert [hit.path for hit in hits] == [new_page.relative_to(vault).as_posix()]


def _adopted_parted_source(vault, monkeypatch: pytest.MonkeyPatch, beside_planted: str = "") -> tuple[str, str]:
    """A text file dropped into a project folder and extracted by the worker into several parts.

    Outside Sources/ and Evidence/, so its page may be edited, trashed and moved alone, and its
    origin carriers are withheld from other audiences.
    """
    monkeypatch.setattr(preserve, "_MAX_EXTRACT_BYTES", 4000)
    text = "\n\n".join(
        f"paragraph {number} " + "alpha beta gamma " * 20 + (f"{PLANTED} {beside_planted}" if number == 55 else "")
        for number in range(60)
    ).strip()
    binary = vault / "Knowledge Base" / "Projects" / "demo" / "report.txt"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(text, encoding="utf-8")
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    media_processing.reconcile_media(vault, binary, explicit=False)
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
    worker._drain_parent_results()
    page = binary.with_name("report.txt.md")
    assert extraction_parts.count_of(parse_frontmatter(page.read_text(encoding="utf-8"))[0]) > 2
    return page.relative_to(vault).as_posix(), text


def test_trashing_a_source_takes_its_parts_and_recovery_brings_them_back(vault, monkeypatch) -> None:
    page_rel, text = _adopted_parted_source(vault, monkeypatch)
    page = vault / page_rel

    trashed = delete_file.delete_file(vault, path=page_rel, confirm=True)

    assert not extraction_parts.folder_for(page).exists()
    assert find_module.find(vault, query=PLANTED) == []
    commands.op_recover_from_trash(vault, trash_path=trashed.trash_path)
    assert extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8")) == text
    assert [hit.path for hit in find_module.find(vault, query=PLANTED)] == [page_rel]


def test_moving_a_page_without_its_original_moves_its_parts(vault, monkeypatch) -> None:
    page_rel, text = _adopted_parted_source(vault, monkeypatch)
    (vault / page_rel.removesuffix(".md")).unlink()  # the page no longer pairs with an original
    new_rel = page_rel.replace("report.txt.md", "renamed.md")

    commands.op_move_file(vault, old_path=page_rel, new_path=new_rel)

    new_page = vault / new_rel
    assert not extraction_parts.folder_for(vault / page_rel).exists()
    assert extraction_parts.whole_text(vault, new_page, new_page.read_text(encoding="utf-8")) == text
    assert [hit.path for hit in find_module.find(vault, query=PLANTED)] == [new_rel]


def test_a_later_part_withholds_an_origin_carrier_from_its_passage_and_window(vault, monkeypatch) -> None:
    from test_episode_recovery import _write_source_rule

    from exomem.governance.principal import RequestPrincipal, request_scope

    page_rel, _text = _adopted_parted_source(vault, monkeypatch, beside_planted="<!-- exomem-origin:v1 SECRETNOTE -->")
    [hit] = find_module.find(vault, query=PLANTED)
    _write_source_rule(vault, ceiling=0)  # a configured policy makes client-a a restricted audience

    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        window = commands.op_read_memory(vault, path=page_rel, offset=hit.passage["offset"])

    assert hit.matched_part > 1 and PLANTED in hit.passage["text"] and PLANTED in window["body"]
    assert "SECRETNOTE" not in hit.passage["text"] and "SECRETNOTE" not in window["body"]


def test_reconcile_removes_a_leftover_part_keeps_a_hand_made_file_and_rebuilds_a_missing_part(
    vault, monkeypatch
) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    (vault / stub.path).write_text(text, encoding="utf-8")  # the original now holds the text itself
    folder = extraction_parts.folder_for(page)
    count = extraction_parts.count_of(parse_frontmatter(page.read_text(encoding="utf-8"))[0])
    last = (folder / f"part-{count:02d}.md").read_text(encoding="utf-8")
    leftover = folder / f"part-{count + 1:02d}.md"  # as a longer, earlier extraction wrote it
    leftover.write_text(last.replace(f"part: {count}\n", f"part: {count + 1}\n") + "plumquorx\n", encoding="utf-8")
    hand_made = folder / f"part-{count + 2:02d}.md"
    hand_made.write_text("my own notes\n", encoding="utf-8")
    (folder / "part-03.md").unlink()
    find_module.clear_cache()

    assert find_module.find(vault, query="plumquorx") == []  # a leftover is no text of its Source
    report = reconcile.reconcile(vault).as_dict()

    assert (report["part_orphans_removed"], report["parts_rebuilt"], report["parts_missing"]) == (1, 1, [])
    assert report["parts_unowned"] == [hand_made.relative_to(vault).as_posix()]
    assert not leftover.exists() and hand_made.exists()
    assert extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8")) == text


def test_reconcile_rebuilds_a_parts_folder_that_is_gone(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    (vault / stub.path).write_text(text, encoding="utf-8")  # the original now holds the text itself
    folder = extraction_parts.folder_for(page)
    for part in folder.iterdir():
        part.unlink()
    folder.rmdir()

    report = reconcile.reconcile(vault).as_dict()

    assert (report["parts_rebuilt"], report["parts_missing"]) == (1, [])
    assert extraction_parts.whole_text(vault, page, page.read_text(encoding="utf-8")) == text


def test_reconcile_reports_a_missing_part_of_a_recording_without_running_its_engine(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch, filename="call.mp3")
    page = vault / stub.sidecar_path
    extraction_parts.part_path(page, 3).unlink()
    engine_runs: list[object] = []
    monkeypatch.setattr(extract, "extract_text", lambda path, **_options: engine_runs.append(path))

    report = reconcile.reconcile(vault).as_dict()

    assert engine_runs == []
    assert report["parts_missing"] == [stub.sidecar_path]
    with pytest.raises(ValueError, match="PART_MISSING"):
        offset: int | None = 0
        while offset is not None:
            offset = commands.op_read_memory(vault, path=stub.sidecar_path, offset=offset)["window"]["next_offset"]


def test_a_part_belongs_to_the_scopes_of_its_source(vault, monkeypatch) -> None:
    from exomem import find_corpus, memory_refs
    from exomem.governance import membership, policy

    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    identity = parse_frontmatter(page.read_text(encoding="utf-8"))[0]["exomem_id"]
    scope_file = vault / "Knowledge Base" / "_Governance" / "scopes" / "client.yaml"
    scope_file.parent.mkdir(parents=True)
    scope_file.write_text(
        f'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nrefs: ["{memory_refs.memory_ref(identity)}"]\n',
        encoding="utf-8",
    )
    part = extraction_parts.part_path(page, 3)

    scopes = membership.evaluate(find_corpus.parse_page(part, part.stat().st_mtime, vault), policy.load(vault))

    assert scopes == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})


def test_a_page_at_a_part_path_that_is_not_a_part_is_governed_as_itself(vault) -> None:
    from exomem import find_corpus
    from exomem.governance import membership, policy

    kb = vault / "Knowledge Base" / "Notes"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "garden-plan.md").write_text("---\ntype: insight\n---\nBeds and paths.\n", encoding="utf-8")
    planted = kb / ".garden-plan.parts" / "part-02.md"
    planted.parent.mkdir()
    planted.write_text("---\ntype: insight\ntags: [secret]\n---\nSalary review, confidential.\n", encoding="utf-8")
    scope_file = vault / "Knowledge Base" / "_Governance" / "scopes" / "secret.yaml"
    scope_file.parent.mkdir(parents=True)
    scope_file.write_text('governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAW\ntags: ["secret"]\n', encoding="utf-8")
    rel = planted.relative_to(vault).as_posix()

    scopes = membership.evaluate(find_corpus.parse_page(planted, planted.stat().st_mtime, vault), policy.load(vault))

    assert scopes == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAW"})
    # Never served as its neighbour note: an unlisted dot-folder page is not a readable memory.
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_read_memory(vault, path=rel)


def test_a_source_page_with_crlf_line_endings_still_reads_in_windows(vault, monkeypatch) -> None:
    stub, text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    page.write_bytes(page.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

    pieces, offset = [], 0
    while offset is not None:
        result = commands.op_read_memory(vault, path=stub.sidecar_path, offset=offset)
        pieces.append(result["body"])
        offset = result["window"]["next_offset"]

    assert "".join(pieces).replace("\r\n", "\n") == text


def test_export_classifies_part_pages_as_canonical_vault_content(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    part = extraction_parts.part_path(page, 2).relative_to(vault).as_posix()
    other_hidden = part.replace("part-02.md", "cache.bin")

    assert vault_portability.classify_artifact(part).artifact_class.value == "canonical"
    assert vault_portability.classify_artifact(other_hidden).artifact_class.value == "disposable-runtime"


def test_parts_are_not_counted_as_unprocessed_sources(vault, monkeypatch) -> None:
    stub, _text = _small_parted_source(vault, monkeypatch)
    page = vault / stub.sidecar_path
    parts = {p.relative_to(vault).as_posix() for p in extraction_parts.folder_for(page).glob("part-*.md")}
    assert parts

    report = audit.audit(vault, categories=["unprocessed_source"])

    flagged = {finding.path for finding in report.findings}
    assert stub.sidecar_path in flagged
    assert not parts & flagged


# Governed projection search: an enrolled vault serves `find` from its catalog, where a part's text
# is content of its Source's full-text row and nothing else.

#: Two invented words that no page of a governed vault holds.
ABSENT = "quorvane plinthwhistle"
_RESTRICTED_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_VAULTED_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


@pytest.fixture
def governed_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Offline custody for a vault that the test enrolls itself; query models stay off."""
    from contextlib import contextmanager

    import test_governance_schema_migration_plan as migration_tests

    for name in ("EXOMEM_DISABLE_EMBEDDINGS", "EXOMEM_DISABLE_CLIP", "EXOMEM_DISABLE_RANKING"):
        monkeypatch.setenv(name, "1")
    vault = tmp_path / "vault"
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir()
    (governance / "scopes" / "restricted.yaml").write_text(
        f"governance_version: 1\nid: {_RESTRICTED_SCOPE}\ntags: [restricted]\n", encoding="utf-8"
    )
    (governance / "scopes" / "vaulted.yaml").write_text(
        f'governance_version: 1\nid: {_VAULTED_SCOPE}\npaths: ["Knowledge Base/Vaulted/**"]\n', encoding="utf-8"
    )
    # "external" may not read either scope at all; "partner" reads only an excerpt of it.
    for rule_id, audience, ceiling in (("01ARZ3NDEKTSV4RRFFQ69G5FB0", "external", 0), ("01ARZ3NDEKTSV4RRFFQ69G5FB1", "partner", 5)):
        (governance / "rules" / f"{audience}.yaml").write_text(
            f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{_RESTRICTED_SCOPE}", "{_VAULTED_SCOPE}"]\n'
            f"audience: {audience}\nceiling: {ceiling}\n",
            encoding="utf-8",
        )
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    (notes / "plain.md").write_text("---\ntype: insight\n---\n\n# Plain\n\nAn ordinary note about gardens.\n", encoding="utf-8")
    with contextmanager(migration_tests._offline_state.__wrapped__)(tmp_path, monkeypatch):
        yield vault


def _enroll(vault) -> None:
    """Migrate the vault to the governed store, which publishes its first catalog."""
    import time

    import test_governance_schema_migration_plan as migration_tests

    from exomem import state_migration
    from exomem.governance import schema_migration, store

    state_migration.migrate_vault_state_offline(
        vault, authority=state_migration.assert_offline_migration_authority(source="full text fixture")
    )
    store.open_connection(vault).close()
    now = int(time.time()) - 10  # custody refuses a control record issued in the future
    plan = migration_tests._stage_reviewed_migration(vault, now=now)
    schema_migration.commit_forward_migration(vault, expected_plan_digest=plan.plan_digest, now=now + 2)


@pytest.fixture
def governed(governed_state):
    _enroll(governed_state)
    return governed_state


def _owner():
    from exomem.governance.principal import owner_principal

    return owner_principal(surface="mcp")


def _audience(audience_id: str):
    from exomem.governance.principal import RequestPrincipal

    return RequestPrincipal(audience_id=audience_id, surface="mcp")


def _projected_find(vault, query: str, who) -> dict:
    """`find` as `who` asks it, served from the catalog the last write published."""
    from exomem.governance import projection_runtime
    from exomem.governance.principal import request_scope

    projection_runtime.preactivate_projection_runtime(vault)
    with request_scope(who):
        return commands.op_find(vault, query=query, limit=5, graph=False, rerank=False)


def _as_owner(call, *args, **kwargs):
    from exomem.governance.principal import request_scope

    with request_scope(_owner()):
        return call(*args, **kwargs)


def _part_count(vault, page_rel: str) -> int:
    return extraction_parts.count_of(parse_frontmatter((vault / page_rel).read_text(encoding="utf-8"))[0])


def test_a_restricted_source_matches_in_its_last_part_only_for_an_audience_that_reads_it_in_full(
    governed, monkeypatch
) -> None:
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)
    _as_owner(commands.op_edit, governed, path=page_rel, why="Restrict the report", tags=["restricted"])

    for audience in ("external", "partner"):
        who = _audience(audience)
        assert _projected_find(governed, PLANTED, who) == _projected_find(governed, ABSENT, who)
    [hit] = _projected_find(governed, PLANTED, _owner())["hits"]
    window = _as_owner(commands.op_read_memory, governed, path=page_rel, offset=hit["passage"]["offset"])

    assert (hit["path"], hit["matched_part"]) == (page_rel, _part_count(governed, page_rel))
    assert PLANTED in hit["passage"]["text"] and PLANTED in window["body"]


def test_a_tag_edit_takes_the_parts_of_a_source_out_of_an_audience_and_back(governed, monkeypatch) -> None:
    partner = _audience("partner")
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)

    readable = _projected_find(governed, PLANTED, partner)["hits"]
    _as_owner(commands.op_edit, governed, path=page_rel, why="Restrict the report", tags=["restricted"])
    restricted = _projected_find(governed, PLANTED, partner)
    _as_owner(commands.op_edit, governed, path=page_rel, why="Release the report", tags=[])
    released = _projected_find(governed, PLANTED, partner)["hits"]

    assert [hit["path"] for hit in readable] == [hit["path"] for hit in released] == [page_rel]
    assert restricted == _projected_find(governed, ABSENT, partner)


def test_moving_a_source_into_a_restricted_folder_takes_its_parts_with_it(governed, monkeypatch) -> None:
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)
    count = _part_count(governed, page_rel)
    # A governed catalog publishes Markdown only, so the page moves without its original.
    (governed / page_rel.removesuffix(".md")).unlink()

    _as_owner(commands.op_move_file, governed, old_path=page_rel, new_path="Knowledge Base/Vaulted/report.md")

    owner_hits = _projected_find(governed, PLANTED, _owner())["hits"]
    assert [(hit["path"], hit["matched_part"]) for hit in owner_hits] == [("Knowledge Base/Vaulted/report.md", count)]
    partner = _audience("partner")
    assert _projected_find(governed, PLANTED, partner) == _projected_find(governed, ABSENT, partner)


def test_a_shorter_extraction_replaces_the_parts_in_the_catalog(governed, monkeypatch) -> None:
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)
    shorter = "\n\n".join(
        f"paragraph {number} " + "delta epsilon " * 30 + ("plumquorx" if number == 19 else "") for number in range(20)
    )

    _as_owner(preserve.update_sidecar_extraction, governed, governed / page_rel, text=shorter, engine="text")

    [hit] = _projected_find(governed, "plumquorx", _owner())["hits"]
    assert 1 < hit["matched_part"] == _part_count(governed, page_rel)
    assert _projected_find(governed, PLANTED, _owner())["hits"] == []


def test_trashing_a_source_and_recovering_it_takes_its_parts_out_and_back(governed, monkeypatch) -> None:
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)

    trashed = _as_owner(delete_file.delete_file, governed, path=page_rel, confirm=True)
    trashed_hits = _projected_find(governed, PLANTED, _owner())["hits"]
    _as_owner(commands.op_recover_from_trash, governed, trash_path=trashed.trash_path)
    [hit] = _projected_find(governed, PLANTED, _owner())["hits"]

    assert trashed_hits == []
    assert (hit["path"], hit["matched_part"]) == (page_rel, _part_count(governed, page_rel))


def test_a_catalog_published_before_parts_gains_them_when_the_projector_refresh_runs(
    governed_state, monkeypatch
) -> None:
    import os
    import sqlite3

    from exomem import state_paths
    from exomem.governance import projections, projector_refresh

    vault = governed_state
    # As the release before parts published the catalog: the projector before this one, no part
    # text in any row, and stores without the parts table.
    with monkeypatch.context() as previous_release:
        previous_release.setattr(projections, "PROJECTOR_SCHEMA_VERSION", projections.PROJECTOR_SCHEMA_VERSION - 1)
        previous_release.setattr(extraction_parts, "stored_parts", lambda *_args, **_options: ())
        _enroll(vault)
        page_rel, _text = _as_owner(_adopted_parted_source, vault, monkeypatch)
        assert _projected_find(vault, PLANTED, _owner())["hits"] == []
    for rows in state_paths.vault_state_dir(vault).rglob("rows.sqlite"):
        os.chmod(rows, 0o600)
        with sqlite3.connect(rows) as connection:
            for operation in ("insert", "update", "delete"):
                connection.execute(f"DROP TRIGGER projection_variant_parts_no_{operation}")
            connection.execute("DROP TABLE projection_variant_parts")
            connection.execute("PRAGMA user_version=2")

    assert projector_refresh.converge(vault) == "refreshed"

    [hit] = _projected_find(vault, PLANTED, _owner())["hits"]
    assert (hit["path"], hit["matched_part"]) == (page_rel, _part_count(vault, page_rel))


def test_a_projector_refresh_measures_no_unchanged_projection_text_again(vault, monkeypatch) -> None:
    from dataclasses import replace

    from governance_projection_support import verified_namespace
    from test_governance_clip_successor_publication import _vector_family, _vector_measurements

    from exomem import embeddings
    from exomem.governance import (
        catalog_publication,
        projection_measurement_store,
        projection_store,
        projections,
        schema_v4,
    )
    from exomem.governance.policy import Policy

    stub, _text = _small_parted_source(vault, monkeypatch)
    old_key = projections.ProjectionNamespaceKey("a" * 64, projections.PROJECTOR_SCHEMA_VERSION - 1, 1)
    policy = Policy(fingerprint=old_key.policy_fingerprint)
    current = catalog_publication.catalog_item(
        vault_root=vault, path=stub.sidecar_path, source=(vault / stub.sidecar_path).read_text(encoding="utf-8"),
        policy=policy, target_key=old_key,
    )
    # The row as the release before parts published it: the same projection text, no parts.
    active_items = (replace(current, variants=tuple(replace(variant, parts=()) for variant in current.variants)),)
    vector_family = _vector_family(old_key)
    vectors = projection_measurement_store.stage_measurement_store(
        vault, namespace=verified_namespace(old_key, active_items), family=vector_family,
        measurements=_vector_measurements(vector_family, *active_items),
    )
    manifest = projection_store.stage_variant_store(vault, key=old_key, items=active_items)
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            "fixture-vault", "fixture-store", 1, "e" * 64, "fixture-policy", old_key.policy_fingerprint,
            old_key.projector_schema_version, old_key.catalog_generation, old_key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(old_key, active_items),
        projection_namespace_evidence=projection_store.projection_namespace_evidence_bytes(
            manifest, required_measurement_roots=(projection_measurement_store.measurement_root(vectors),)
        ),
    )
    measured: list[str] = []
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, **_options: measured.extend(texts) or [(0.0, 1.0) for _ in texts]
    )

    prepared = catalog_publication.prepare_policy_projection(
        vault, active_snapshot=snapshot, target_policy=policy, ready_at=19
    )

    [variant] = prepared.items[0].variants
    assert measured == [] and PLANTED in variant.parts[-1].text
    [root] = projection_store.decode_projection_namespace_evidence(
        prepared.evidence, expected_key=prepared.manifest.namespace_key
    ).required_measurement_roots
    _manifest, rows = projection_measurement_store.load_measurement_store(
        vault,
        namespace=projection_store.prepare_projection_namespace(
            key=prepared.manifest.namespace_key, manifest=prepared.manifest, items=prepared.items
        ),
        family=projection_measurement_store.MeasurementFamilyKey(
            root.namespace_key, root.lane, root.extractor_version, root.model_version
        ),
        expected_rows_digest=root.rows_digest,
    )
    assert [row.vector for row in rows] == [row.vector for row in _vector_measurements(vector_family, *active_items)]


def test_a_rebuilt_missing_part_becomes_searchable(governed, monkeypatch) -> None:
    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)
    count = _part_count(governed, page_rel)
    extraction_parts.part_path(governed / page_rel, count).unlink()  # the part that holds PLANTED
    _as_owner(commands.op_edit, governed, path=page_rel, why="Tag the report", tags=["report"])
    missing = _projected_find(governed, PLANTED, _owner())["hits"]

    _as_owner(reconcile.reconcile, governed)

    [hit] = _projected_find(governed, PLANTED, _owner())["hits"]
    assert missing == [] and hit["matched_part"] == count


def test_a_full_catalog_publishes_a_source_without_the_parts_that_do_not_fit(governed, monkeypatch, caplog) -> None:
    from exomem.governance import projection_runtime, projections

    projection_runtime.preactivate_projection_runtime(governed)
    held = len(projection_runtime.load_active_projection_runtime(governed).namespace.items)
    monkeypatch.setattr(projections, "MAX_GOVERNED_CATALOG_ITEMS", held + 2)  # the Source and one part

    page_rel, _text = _as_owner(_adopted_parted_source, governed, monkeypatch)

    assert [hit["path"] for hit in _projected_find(governed, "paragraph 0", _owner())["hits"]] == [page_rel]
    assert _projected_find(governed, PLANTED, _owner())["hits"] == []
    first_left_out = extraction_parts.part_path(Path(page_rel), 3).as_posix()
    assert any(first_left_out in record.getMessage() for record in caplog.records)
