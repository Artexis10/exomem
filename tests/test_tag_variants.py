import json
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import commands, lexstore, tag_variants, vault, writer_lease


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    tag_variants._INDEX_CACHE.clear()
    yield
    tag_variants._INDEX_CACHE.clear()


def _page(root: Path, rel: str, tags_line: str, body: str = "Plain body text.\n") -> Path:
    path = root / "Knowledge Base" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        # A path-derived id keeps twin vaults byte-identical page for page.
        f"---\ntype: insight\ntitle: {path.stem}\nexomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, rel)}\n"
        f"{tags_line}\n---\n{body}",
        encoding="utf-8",
    )
    return path


def _log(root: Path) -> None:
    log = root / "Knowledge Base" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text("# Log\n", encoding="utf-8")


def _catalogue(root: Path) -> None:
    assert lexstore.get_store(root).rebuild_atomic()


def _rows(*pages: list[str]):
    """A fake catalogue: one row per page, members stored casefolded."""
    rows = [
        (f"Knowledge Base/Notes/p{index}.md", [tag.casefold() for tag in tags])
        for index, tags in enumerate(pages)
    ]
    return lambda _root: list(rows)


def _usage(*pages: list[str]) -> "tag_variants.Usage":
    return tag_variants.Usage(_rows(*pages)(None))


# ---------------- counting and canonical choice ----------------


def test_the_canonical_is_the_most_used_normal_form():
    usage = _usage(*[["failures"]] * 4, *[["failure"]] * 2)
    assert usage.target("failure") == ("failures", 4)
    assert usage.target("failures") is None
    assert usage.target("Failures") is None  # case alone is not a variant


def test_the_canonical_is_what_writers_produce_never_a_raw_spelling():
    usage = _usage(*[["Machine_Learning"]] * 3, ["machine-learning"])
    assert usage.target("machine_learning") == ("machine-learning", 4)
    assert usage.target("Machine_Learning") == ("machine-learning", 4)
    assert usage.target("machine-learning") is None
    only_raw = _usage(*[["Machine_Learning"]] * 3, ["machine-learnings"])
    assert only_raw.target("machine-learnings") == ("machine-learning", 3)


@pytest.mark.parametrize("uses", [1, 3])
def test_a_tie_never_names_a_canonical(uses):
    usage = _usage(*[["failure"]] * uses, *[["failures"]] * uses)
    assert usage.target("failure") is None
    assert usage.target("failures") is None


def test_different_words_are_never_variants():
    usage = _usage(*[["training"]] * 5, ["trains"], *[["records"]] * 4, ["recording"])
    for tag in ("trains", "training", "recording", "records"):
        assert usage.target(tag) is None


def test_a_new_authored_tag_is_compared_against_existing_uses():
    usage = _usage(*[["failures"]] * 3)
    assert usage.target("failure") == ("failures", 3)
    assert usage.target("fresh") is None


def test_catalogue_counts_tags_per_page(tmp_path):
    _log(tmp_path)
    for index in range(3):
        _page(tmp_path, f"Notes/d{index}.md", "tags: [failures]")
    _page(tmp_path, "Notes/e.md", "tags: [failure, failure]")
    _catalogue(tmp_path)
    usage = tag_variants.usage(tmp_path)
    assert usage.spellings["failures"] == 3
    assert usage.spellings["failure"] == 1


# ---------------- write time: advice only ----------------


def test_maximal_advises_and_never_rewrites(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3, ["policy"]))
    notes = tag_variants.advise_authored(
        tmp_path, ["failure", "policy", "fresh"], level="maximal"
    )
    assert len(notes) == 1
    assert "'failure'" in notes[0] and "'failures'" in notes[0] and "3 pages" in notes[0]
    assert "\n" not in notes[0]
    assert not hasattr(tag_variants, "reconcile_authored")


@pytest.mark.parametrize("level", ["off", "light", "balanced"])
def test_other_levels_leave_advice_to_the_committed_response(tmp_path, monkeypatch, level):
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    assert tag_variants.advise_authored(tmp_path, ["failure"], level=level) == []


def test_unavailable_catalogue_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "_catalogue_rows", lambda _root: None)
    assert tag_variants.advise_authored(tmp_path, ["failure"], level="maximal") == []


def test_advisory_names_the_canonical_tag_on_one_line(tmp_path, monkeypatch):
    path = _page(tmp_path, "Notes/a.md", "tags: [failure, other]")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 5, ["failure"]))
    notice = tag_variants.advisory_for_page(tmp_path, path.relative_to(tmp_path).as_posix())
    assert notice["canonical"] == "failures" and notice["tag"] == "failure"
    assert "\n" not in notice["message"]
    assert tag_variants.valid_advisory(notice)


def test_write_time_index_is_cached_briefly(tmp_path, monkeypatch):
    calls = []

    def rows(_root):
        calls.append(1)
        return [("Knowledge Base/Notes/a.md", ["failures"])]

    monkeypatch.setattr(tag_variants, "_catalogue_rows", rows)
    for _ in range(3):
        tag_variants.advise_authored(tmp_path, ["failure"], level="maximal")
    assert len(calls) == 1
    monkeypatch.setattr(tag_variants, "INDEX_TTL_SECONDS", 0.0)
    tag_variants.advise_authored(tmp_path, ["failure"], level="maximal")
    assert len(calls) == 2


def test_an_empty_index_is_cached_too(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tag_variants, "_catalogue_rows", lambda _root: calls.append(1) or [])
    for _ in range(3):
        assert tag_variants.advise_authored(tmp_path, ["failure"], level="maximal") == []
    assert len(calls) == 1


_UNIT = "## Observations\n- [operating constraint] Keep retries bounded #reliability\n"


def test_maximal_write_keeps_the_authored_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "remember")
    from exomem import note as note_module

    captured = {}
    original = note_module._render_note

    def spy(**kwargs):
        captured["tags"] = kwargs["tags"]
        return original(**kwargs)

    monkeypatch.setattr(note_module, "_render_note", spy)
    _log(tmp_path)
    try:
        writer_lease.invoke_command(
            command, tmp_path, idempotency_key="max", content=_UNIT, title="Max",
            slug="max", tags=["failure", "fresh"],
        )
    except Exception:  # noqa: BLE001 - the semantic contract may still refuse; tags are what matter
        pass
    assert captured["tags"] == ["failure", "fresh"]


def test_add_surfaces_tag_advice(vault, source_schema, monkeypatch):
    from exomem import add as add_module

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    result = add_module.add(
        vault,
        source_schema,
        content="An article body about a failure.",
        source_type="article",
        title="Failure article",
        url="https://example.com/failure",
        tags=["failure"],
    )
    from exomem import vault as vault_module

    written = vault / result.path
    assert vault_module.parse_frontmatter(written.read_text(encoding="utf-8"))[0]["tags"] == [
        "failure"
    ]
    assert any("'failures'" in warning for warning in result.warnings)


def test_edit_never_rewrites_authored_tags(vault, monkeypatch):
    from exomem import edit as edit_module
    from exomem import vault as vault_module

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    target = _page(vault, "Notes/Insights/tagged.md", "tags: [keep]")
    rel = target.relative_to(vault).as_posix()
    result = edit_module.edit(vault, path=rel, tags=["failure", "keep"], why="retag")
    fm = vault_module.parse_frontmatter(target.read_text(encoding="utf-8"))[0]
    assert fm["tags"] == ["failure", "keep"]
    assert any("'failures'" in warning for warning in result.warnings)


def test_link_surfaces_tag_advice(vault, monkeypatch):
    from exomem import link as link_module
    from exomem import vault as vault_module

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    result = link_module.link(
        vault,
        entity_type="concept",
        name="Failure Budget",
        summary="How much failure a service may spend.",
        tags=["failure"],
    )
    fm = vault_module.parse_frontmatter((vault / result.path).read_text(encoding="utf-8"))[0]
    assert fm["tags"] == ["failure"]
    assert any("'failures'" in warning for warning in result.warnings)


def test_balanced_write_keeps_the_authored_tag_and_advises(tmp_path, monkeypatch):
    from exomem import mutation_terminal, vocabulary_delivery
    from exomem.governance.principal import library_scope

    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    path = _page(tmp_path, "Notes/new.md", "tags: [failure]", _UNIT)
    rel = path.relative_to(tmp_path).as_posix()
    terminal = mutation_terminal.committed_terminal(
        {"path": rel}, request_id="r", receipt_id="receipt-r", idempotency_key="k"
    )
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal)
    notice = result["vocabulary_advisory"]
    assert notice["family"] == "tag-variant/v1"
    assert notice["canonical"] == "failures"
    assert vault.parse_frontmatter(path.read_text(encoding="utf-8"))[0]["tags"] == ["failure"]
    public = vocabulary_delivery.public_projection(result)
    assert public["vocabulary_advisory"] == notice


def test_tag_advisory_respects_an_off_envelope(tmp_path, monkeypatch):
    from exomem import mutation_terminal, vocabulary_delivery
    from exomem.governance.principal import library_scope

    monkeypatch.setenv("EXOMEM_PROMINENCE", "off")
    monkeypatch.setattr(tag_variants, "_catalogue_rows", _rows(*[["failures"]] * 3))
    path = _page(tmp_path, "Notes/quiet.md", "tags: [failure]", _UNIT)
    terminal = mutation_terminal.committed_terminal(
        {"path": path.relative_to(tmp_path).as_posix()},
        request_id="r", receipt_id="receipt-r", idempotency_key="k",
    )
    with library_scope():
        result = vocabulary_delivery.after_commit(tmp_path, terminal)
    assert "vocabulary_advisory" not in result


def test_relation_review_notice_keeps_the_single_advisory_slot():
    from exomem import vocabulary_delivery

    tag_notice = tag_variants.advisory("failure", "failures", 3)
    merged = vocabulary_delivery._with_tag_advisory(
        {"vocabulary_advisory": {"ref": "exomem://review/vocabulary/x"}}, tag_notice
    )
    assert merged["vocabulary_advisory"]["ref"] == "exomem://review/vocabulary/x"


# ---------------- frontmatter splice ----------------


@pytest.mark.parametrize(
    "tags_line",
    [
        "tags: [failure, keep]",
        "tags:\n  - failure\n  - keep",
        "tags: failure, keep",
    ],
)
def test_rewrite_changes_only_the_tags_key(tags_line):
    body = "Body mentions failure and tags: [failure] verbatim.\r\n\n  - failure\n"
    text = f"---\ntitle: T\n{tags_line}\nstatus: active\n---\n{body}"
    updated = tag_variants._rewrite(text, ["failures", "keep"])
    assert updated is not None
    assert updated.endswith(body)
    fm, after_body, _ = vault.parse_frontmatter(updated)
    assert fm["tags"] == ["failures", "keep"]
    assert fm["status"] == "active" and fm["title"] == "T"


def test_rewrite_quotes_tags_that_yaml_would_misread():
    tags = ["#hash", "a: b", "true", "null", "123", "1.5", "yes", "on", "2024-01-01", "0x1f"]
    updated = tag_variants._rewrite("---\ntags: [a]\n---\nx\n", tags)
    assert vault.parse_frontmatter(updated)[0]["tags"] == tags


def test_rewrite_keeps_an_inline_comment_on_the_tags_line():
    text = "---\ntitle: T\ntags: [failure, keep]  # topic tags\n---\nx\n"
    updated = tag_variants._rewrite(text, ["failures", "keep"])
    assert updated is not None
    assert "tags: [failures, keep]  # topic tags\n" in updated


def test_rewrite_refuses_to_drop_a_comment_inside_the_tags_block():
    text = "---\ntitle: T\ntags:\n  - failure  # the old name\n  - keep\n---\nx\n"
    assert tag_variants._rewrite(text, ["failures", "keep"]) is None
    commented = "---\ntitle: T\ntags:\n  # the old name\n  - failure\n---\nx\n"
    assert tag_variants._rewrite(commented, ["failures"]) is None


def test_rewrite_refuses_when_another_key_would_change(monkeypatch):
    text = "---\ntitle: T\ntags: [failure]\nstatus: active\n---\nx\n"
    original = tag_variants._replace_tags_key
    monkeypatch.setattr(
        tag_variants,
        "_replace_tags_key",
        lambda fm_text, tags: original(fm_text, tags).replace("status: active", "status: gone"),
    )
    assert tag_variants._rewrite(text, ["failures"]) is None


# ---------------- maintenance ----------------


def _maintain(root: Path, key: str, **kwargs):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")
    return writer_lease.invoke_command(command, root, idempotency_key=key, **kwargs)


def _variant_vault(root: Path, *, catalogue: bool = True) -> dict[str, str]:
    _log(root)
    bodies = {}
    for index in range(4):
        path = _page(root, f"Notes/canon-{index}.md", "tags: [failures, policies]")
        bodies[path.name] = path.read_text(encoding="utf-8").split("---\n", 2)[2]
    for index in range(2):
        body = f"This body says failure and policy {index} and must not change.\n"
        path = _page(root, f"Notes/variant-{index}.md", "tags: [failure, policy, Kept]", body)
        bodies[path.name] = body
    _page(root, "Sources/Articles/raw.md", "tags: [failure]")
    if catalogue:
        _catalogue(root)
    return bodies


def test_preview_lists_groups_with_counts_and_writes_nothing(tmp_path):
    _variant_vault(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    result = _maintain(tmp_path, "preview", mode="tag-variants")
    groups = {group["canonical"]: group for group in result["groups"]}
    assert groups["failures"]["variants"] == [
        {"tag": "failures", "uses": 4},
        {"tag": "failure", "uses": 2},
    ]
    assert groups["policies"]["uses"] == 6
    assert groups["failures"]["tied"] is False
    assert result["pages_pending"] == 2
    assert {entry["path"] for entry in result["batch"]} == {
        "Knowledge Base/Notes/variant-0.md",
        "Knowledge Base/Notes/variant-1.md",
    }
    assert result["apply"]["args"]["plan_id"] == result["plan_id"]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before


def test_confirmed_apply_rewrites_minority_tags_and_is_idempotent(tmp_path):
    bodies = _variant_vault(tmp_path)
    source = (tmp_path / "Knowledge Base/Sources/Articles/raw.md").read_bytes()
    preview = _maintain(tmp_path, "preview-1", mode="tag-variants")
    applied = _maintain(
        tmp_path,
        "apply-1",
        mode="tag-variants",
        apply=True,
        plan_id=preview["plan_id"],
        why="Reconcile plural variants.",
    )
    assert applied["state"] == "committed"
    for index in range(2):
        path = tmp_path / f"Knowledge Base/Notes/variant-{index}.md"
        fm, body, _ = vault.parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["tags"] == ["failures", "policies", "Kept"]
        assert body == bodies[path.name]
    assert (tmp_path / "Knowledge Base/Sources/Articles/raw.md").read_bytes() == source
    assert "Reconciled tag variants" in (tmp_path / "Knowledge Base/log.md").read_text()

    again = _maintain(tmp_path, "preview-2", mode="tag-variants")
    assert again["pages_pending"] == 0 and "apply" not in again
    with pytest.raises(Exception, match="STALE_TAG_VARIANT_PLAN"):
        _maintain(
            tmp_path,
            "apply-2",
            mode="tag-variants",
            apply=True,
            plan_id=preview["plan_id"],
            why="Reconcile plural variants.",
        )


def _logged_entry(root: Path) -> dict:
    text = (root / "Knowledge Base/log.md").read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "Reconciled tag variants " in line)
    return json.loads(line.split("Reconciled tag variants ", 1)[1])


def test_apply_logs_a_rollback_record(tmp_path):
    _variant_vault(tmp_path)
    preview = _maintain(tmp_path, "preview", mode="tag-variants")
    _maintain(
        tmp_path, "apply", mode="tag-variants", apply=True,
        plan_id=preview["plan_id"], why="Reconcile.",
    )
    entry = _logged_entry(tmp_path)
    assert entry["inverse"] == {"failures": ["failure"], "policies": ["policy"]}
    pages = {page["path"]: page for page in entry["pages"]}
    assert pages["Knowledge Base/Notes/variant-0.md"]["before"] == ["failure", "policy", "Kept"]
    assert pages["Knowledge Base/Notes/variant-0.md"]["after"] == ["failures", "policies", "Kept"]
    after = (tmp_path / "Knowledge Base/Notes/variant-0.md").read_text(encoding="utf-8")
    assert pages["Knowledge Base/Notes/variant-0.md"]["after_hash"] == vault.content_hash(after)


def test_apply_refuses_when_the_log_cannot_be_written(tmp_path):
    _variant_vault(tmp_path)
    preview = _maintain(tmp_path, "preview", mode="tag-variants")
    (tmp_path / "Knowledge Base/log.md").unlink()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    with pytest.raises(Exception, match="TAG_VARIANT_AUDIT_UNAVAILABLE"):
        _maintain(
            tmp_path, "apply", mode="tag-variants", apply=True,
            plan_id=preview["plan_id"], why="Reconcile.",
        )
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before


def test_apply_refuses_a_stale_plan(tmp_path):
    _variant_vault(tmp_path)
    preview = _maintain(tmp_path, "preview", mode="tag-variants")
    page = tmp_path / "Knowledge Base/Notes/variant-0.md"
    page.write_text(page.read_text(encoding="utf-8") + "Edited.\n", encoding="utf-8")
    with pytest.raises(Exception, match="STALE_TAG_VARIANT_PLAN"):
        _maintain(
            tmp_path, "apply", mode="tag-variants", apply=True,
            plan_id=preview["plan_id"], why="Reconcile.",
        )


def test_apply_is_batched(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "BATCH_PAGES", 1)
    _variant_vault(tmp_path)
    first = _maintain(tmp_path, "p1", mode="tag-variants")
    assert first["batch_pages"] == 1 and first["pages_pending"] == 2
    done = _maintain(
        tmp_path, "a1", mode="tag-variants", apply=True, plan_id=first["plan_id"], why="Batch one."
    )
    assert done["paths"] == ["Knowledge Base/Notes/variant-0.md"]
    second = _maintain(tmp_path, "p2", mode="tag-variants")
    assert second["pages_pending"] == 1
    assert second["batch"][0]["path"] == "Knowledge Base/Notes/variant-1.md"


def _leaf_manager(tmp_path: Path, monkeypatch) -> writer_lease.LeaseManager:
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with manager.mutation_guard(tmp_path, operation="test"):
        pass
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    return manager


def test_apply_leaf_reports_what_remains(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "BATCH_PAGES", 1)
    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    _leaf_manager(tmp_path, monkeypatch)
    result = tag_variants.apply(tmp_path, plan_id=plan_id, why="Batch one.")
    assert result["pages_remaining"] == 1
    assert result["next"] == {"tool": "maintain_memory", "args": {"mode": "tag-variants"}}


def _guard_hook(manager, monkeypatch, on_enter):
    """Run ``on_enter`` as the mutation guard is entered, and track whether it is held."""
    held = {"now": False}
    original = manager.mutation_guard

    @contextmanager
    def guard(*args, **kwargs):
        with original(*args, **kwargs) as mutation:
            held["now"] = True
            try:
                on_enter()
                yield mutation
            finally:
                held["now"] = False

    monkeypatch.setattr(manager, "mutation_guard", guard)
    return held


def test_apply_plans_outside_the_mutation_guard(tmp_path, monkeypatch):
    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    manager = _leaf_manager(tmp_path, monkeypatch)
    held = _guard_hook(manager, monkeypatch, lambda: None)
    planned_while_held = []
    original_plan = tag_variants._plan
    monkeypatch.setattr(
        tag_variants, "_plan", lambda root: planned_while_held.append(held["now"]) or original_plan(root)
    )
    result = tag_variants.apply(tmp_path, plan_id=plan_id, why="Outside.")
    assert result["outcome"] == "committed"
    assert planned_while_held == [False]


def test_apply_refuses_a_page_that_changes_before_the_guard(tmp_path, monkeypatch):
    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    manager = _leaf_manager(tmp_path, monkeypatch)
    page = tmp_path / "Knowledge Base/Notes/variant-1.md"
    _guard_hook(
        manager,
        monkeypatch,
        lambda: page.write_text(page.read_text(encoding="utf-8") + "Late.\n", encoding="utf-8"),
    )
    untouched = (tmp_path / "Knowledge Base/Notes/variant-0.md").read_bytes()
    with pytest.raises(ValueError, match="STALE_TAG_VARIANT_PLAN"):
        tag_variants.apply(tmp_path, plan_id=plan_id, why="Late edit.")
    assert (tmp_path / "Knowledge Base/Notes/variant-0.md").read_bytes() == untouched


def test_apply_refuses_when_a_batch_group_changes_before_the_guard(tmp_path, monkeypatch):
    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    manager = _leaf_manager(tmp_path, monkeypatch)
    flipped = [(f"Knowledge Base/Notes/x{index}.md", ["failure"]) for index in range(9)]
    original_rows = tag_variants._catalogue_rows

    def flip():
        monkeypatch.setattr(
            tag_variants, "_catalogue_rows", lambda root: original_rows(root) + flipped
        )

    _guard_hook(manager, monkeypatch, flip)
    with pytest.raises(ValueError, match="STALE_TAG_VARIANT_PLAN"):
        tag_variants.apply(tmp_path, plan_id=plan_id, why="Counts moved.")


def test_apply_requires_plan_and_reason(tmp_path):
    _variant_vault(tmp_path)
    with pytest.raises(Exception, match="INVALID_ARGUMENTS"):
        _maintain(tmp_path, "bad", mode="tag-variants", apply=True, why="No plan.")


@pytest.mark.parametrize("uses", [1, 3])
def test_a_tied_group_is_listed_but_never_rewritten(tmp_path, uses):
    _log(tmp_path)
    for index in range(uses):
        _page(tmp_path, f"Notes/a{index}.md", "tags: [failure]")
        _page(tmp_path, f"Notes/b{index}.md", "tags: [failures]")
    _catalogue(tmp_path)
    result = tag_variants.preview(tmp_path)
    assert [group["tied"] for group in result["groups"]] == [True]
    assert result["pages_pending"] == 0 and result["batch"] == [] and "apply" not in result
    assert result["variant_uses"] == 0


def test_separator_spellings_are_rewritten_to_the_writers_normal_form(tmp_path):
    _log(tmp_path)
    for index in range(3):
        _page(tmp_path, f"Notes/raw-{index}.md", "tags: [Machine_Learning]")
    _page(tmp_path, "Notes/normal.md", "tags: [machine-learning]")
    _catalogue(tmp_path)
    result = tag_variants.preview(tmp_path)
    assert [group["canonical"] for group in result["groups"]] == ["machine-learning"]
    assert {tuple(entry["to"]) for entry in result["batch"]} == {("machine-learning",)}
    assert result["pages_pending"] == 3


def test_write_time_and_maintenance_read_one_count(tmp_path, monkeypatch):
    _variant_vault(tmp_path)
    reads = []
    original = tag_variants._catalogue_rows
    monkeypatch.setattr(
        tag_variants, "_catalogue_rows", lambda root: reads.append(1) or original(root)
    )
    preview = tag_variants.preview(tmp_path)
    advice = tag_variants.advise_authored(tmp_path, ["failure"], level="maximal")
    assert len(reads) == 2
    uses = {group["canonical"]: group["variants"][0]["uses"] for group in preview["groups"]}
    assert f"{uses['failures']} pages" in advice[0]


def test_preview_refuses_when_the_catalogue_cannot_answer(tmp_path):
    _variant_vault(tmp_path, catalogue=False)
    with pytest.raises(ValueError, match="TAG_USAGE_UNAVAILABLE"):
        tag_variants.preview(tmp_path)


_OWNED = [
    "Records/Incidents/Entries/incident.md",
    "records/lower/entry.md",
    "Planning/Roadmap/item.md",
    "_Adoption/runs/run.md",
    "Workflow-Contracts/contract.md",
    "workflow_contract/other.md",
    "_Schema/notes.md",
    "Evidence/Screens/shot.md",
]


def test_maintenance_skips_every_tree_another_subsystem_owns(tmp_path):
    _variant_vault(tmp_path, catalogue=False)
    for rel in _OWNED:
        _page(tmp_path, rel, "tags: [failure, failure_mode]")
    _page(tmp_path, "Notes/mode.md", "tags: [failure-modes]")
    _catalogue(tmp_path)
    owned = {rel: (tmp_path / "Knowledge Base" / rel).read_bytes() for rel in _OWNED}
    preview = _maintain(tmp_path, "preview", mode="tag-variants")
    groups = {group["canonical"]: group for group in preview["groups"]}
    assert groups["failures"]["variants"] == [
        {"tag": "failures", "uses": 4},
        {"tag": "failure", "uses": 2},
    ]
    assert "failure-mode" not in groups and "failure-modes" not in groups
    assert {entry["path"] for entry in preview["batch"]} == {
        "Knowledge Base/Notes/variant-0.md",
        "Knowledge Base/Notes/variant-1.md",
    }
    _maintain(
        tmp_path, "apply", mode="tag-variants", apply=True,
        plan_id=preview["plan_id"], why="Reconcile.",
    )
    assert {rel: (tmp_path / "Knowledge Base" / rel).read_bytes() for rel in _OWNED} == owned


def test_remote_surface_admits_the_plan_gated_mode(tmp_path):
    from exomem import capabilities
    from exomem.commands import product_commands_for

    _variant_vault(tmp_path)
    command = next(item for item in product_commands_for("mcp") if item.name == "maintain_memory")
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="test", tier2_enabled=True, product_commands=("maintain_memory",)
    )
    with capabilities.active_surface(descriptor):
        preview = writer_lease.invoke_command(
            command, tmp_path, idempotency_key="remote-preview", mode="tag-variants"
        )
        applied = writer_lease.invoke_command(
            command, tmp_path, idempotency_key="remote-apply", mode="tag-variants",
            apply=True, plan_id=preview["plan_id"], why="Reconcile from a remote client.",
        )
    assert applied["state"] == "committed"
    assert len(applied["paths"]) == 2


# ---------------- governed egress: withheld reads as absent ----------------


def _clear_governance_memos() -> None:
    from exomem.governance import egress, membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _govern(root: Path, paths: str) -> None:
    from test_governance_egress import write_rule, write_scope

    write_scope(root, paths=paths)
    write_rule(root, ceiling=0)
    _clear_governance_memos()


_LEAF_KEYS = (
    "mode", "plan_id", "group_count", "variant_uses", "groups", "groups_truncated",
    "pages_pending", "batch_pages", "unrewritable", "batch", "apply",
)


def test_withheld_pages_count_exactly_like_absent_ones(tmp_path):
    from test_governance_egress import _external

    from exomem.governance.principal import request_scope

    seen = {}
    for name in ("withheld", "absent"):
        root = tmp_path / name
        _variant_vault(root, catalogue=False)
        _page(root, "Notes/marks.md", "tags: [secret-marks]")
        if name == "withheld":
            for index in range(3):
                _page(root, f"Notes/Patterns/secret-{index}.md", "tags: [failure, secret-mark]")
        _govern(root, "Notes/Patterns/**")
        _catalogue(root)
        tag_variants._INDEX_CACHE.clear()
        with request_scope(_external()):
            preview = _maintain(root, f"preview-{name}", mode="tag-variants")
            advisory = tag_variants.advisory_for_page(root, "Knowledge Base/Notes/variant-0.md")
            advice = tag_variants.advise_authored(root, ["failure"], level="maximal")
        seen[name] = ({key: preview.get(key) for key in _LEAF_KEYS}, advisory, advice)
    assert seen["withheld"] == seen["absent"]
    preview, advisory, advice = seen["withheld"]
    assert preview["groups"][0]["canonical"] == "failures" and preview["pages_pending"] == 2
    assert advisory["canonical"] == "failures" and "4 pages" in advice[0]
    # The owner still counts the withheld pages, so the fixture really differs.
    _clear_governance_memos()
    tag_variants._INDEX_CACHE.clear()
    owner = tag_variants.preview(tmp_path / "withheld")
    assert {group["canonical"] for group in owner["groups"]} >= {"failure", "secret-mark"}


def test_apply_answers_a_withheld_batch_page_like_an_absent_one(tmp_path):
    from test_governance_egress import _external

    from exomem.governance.principal import request_scope

    outcomes = {}
    for name in ("withheld", "absent"):
        root = tmp_path / name
        _variant_vault(root)
        plan_id = tag_variants.preview(root)["plan_id"]
        if name == "absent":
            (root / "Knowledge Base/Notes/variant-1.md").unlink()
            _catalogue(root)
        _govern(root, "Notes/variant-1.md")
        tag_variants._INDEX_CACHE.clear()
        before = {p: p.read_bytes() for p in root.rglob("*.md")}
        with request_scope(_external()), pytest.raises(Exception) as raised:
            _maintain(
                root, f"apply-{name}", mode="tag-variants", apply=True,
                plan_id=plan_id, why="Reconcile.",
            )
        outcomes[name] = (type(raised.value).__name__, str(raised.value))
        assert {p: p.read_bytes() for p in root.rglob("*.md")} == before
    assert outcomes["withheld"] == outcomes["absent"]
    assert "STALE_TAG_VARIANT_PLAN" in outcomes["withheld"][1]


def test_apply_refuses_a_batch_page_withheld_inside_the_guard(tmp_path, monkeypatch):
    from exomem.governance import egress

    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    _leaf_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        egress, "write_target_withheld", lambda _root, rel, **_kw: rel.endswith("variant-1.md")
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    with pytest.raises(ValueError, match="STALE_TAG_VARIANT_PLAN"):
        tag_variants.apply(tmp_path, plan_id=plan_id, why="Withheld.")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before
