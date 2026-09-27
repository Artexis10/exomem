import uuid
from pathlib import Path

import pytest

from exomem import commands, lexstore, tag_variants, vault, writer_lease


def _page(root: Path, rel: str, tags_line: str, body: str = "Plain body text.\n") -> Path:
    path = root / "Knowledge Base" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\ntitle: {path.stem}\nexomem_id: {uuid.uuid4()}\n{tags_line}\n---\n{body}",
        encoding="utf-8",
    )
    return path


def _log(root: Path) -> None:
    log = root / "Knowledge Base" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text("# Log\n", encoding="utf-8")


def test_groups_pick_the_most_used_spelling():
    counts = {"dogfood": 9, "dogfooding": 3, "failure": 2, "failures": 5, "x_y": 1, "x-y": 1}
    grouped = tag_variants.groups(counts)
    assert set(grouped) == {"dogfood", "failure", "x-y"}
    assert tag_variants.canonical(grouped["dogfood"]) == "dogfood"
    assert tag_variants.canonical(grouped["failure"]) == "failures"
    # a tie prefers the write-time normal form
    assert tag_variants.canonical(grouped["x-y"]) == "x-y"


def test_minority_requires_strictly_higher_usage():
    assert tag_variants.minority("dogfooding", {"dogfood": 4}) == ("dogfood", 4)
    assert tag_variants.minority("dogfooding", {"dogfood": 4, "dogfooding": 4}) is None
    assert tag_variants.minority("dogfood", {"dogfood": 4, "dogfooding": 1}) is None
    assert tag_variants.minority("news", {"new": 40}) is None


def test_maximal_records_the_canonical_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tag_variants, "usage_counts", lambda _root: {"dogfood": 7, "failures": 3, "failure": 1}
    )
    tags, notes = tag_variants.reconcile_authored(
        tmp_path, ["dogfooding", "failure", "fresh"], level="maximal"
    )
    assert tags == ["dogfood", "failures", "fresh"]
    assert len(notes) == 2 and "'dogfood'" in notes[0]


@pytest.mark.parametrize("level", ["off", "light", "balanced"])
def test_other_levels_keep_authored_tags(tmp_path, monkeypatch, level):
    monkeypatch.setattr(tag_variants, "usage_counts", lambda _root: {"dogfood": 7})
    assert tag_variants.reconcile_authored(tmp_path, ["dogfooding"], level=level) == (
        ["dogfooding"],
        [],
    )


def test_unavailable_catalogue_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "usage_counts", lambda _root: None)
    assert tag_variants.reconcile_authored(tmp_path, ["dogfooding"], level="maximal") == (
        ["dogfooding"],
        [],
    )


def test_advisory_names_the_canonical_tag_on_one_line(tmp_path, monkeypatch):
    path = _page(tmp_path, "Notes/a.md", "tags: [dogfooding, other]")
    monkeypatch.setattr(tag_variants, "usage_counts", lambda _root: {"dogfood": 5, "dogfooding": 1})
    notice = tag_variants.advisory_for_page(tmp_path, path.relative_to(tmp_path).as_posix())
    assert notice["canonical"] == "dogfood" and notice["tag"] == "dogfooding"
    assert "\n" not in notice["message"]
    assert tag_variants.valid_advisory(notice)


def test_catalogue_counts_tags_per_page(tmp_path):
    _log(tmp_path)
    for index in range(3):
        _page(tmp_path, f"Notes/d{index}.md", "tags: [dogfood]")
    _page(tmp_path, "Notes/e.md", "tags: [dogfooding, dogfooding]")
    assert lexstore.get_store(tmp_path).rebuild_atomic()
    counts = tag_variants.usage_counts(tmp_path)
    assert counts["dogfood"] == 3
    assert counts["dogfooding"] == 1


@pytest.mark.parametrize(
    "tags_line",
    [
        "tags: [dogfooding, keep]",
        "tags:\n  - dogfooding\n  - keep",
        "tags: dogfooding, keep",
    ],
)
def test_rewrite_changes_only_the_tags_key(tags_line):
    body = "Body mentions dogfooding and tags: [dogfooding] verbatim.\r\n\n  - dogfooding\n"
    text = f"---\ntitle: T\n{tags_line}\nstatus: active\n---\n{body}"
    updated = tag_variants._rewrite(text, ["dogfood", "keep"])
    assert updated is not None
    assert updated.endswith(body)
    fm, after_body, _ = vault.parse_frontmatter(updated)
    assert fm["tags"] == ["dogfood", "keep"]
    assert fm["status"] == "active" and fm["title"] == "T"


def test_rewrite_quotes_tags_that_yaml_would_misread():
    updated = tag_variants._rewrite("---\ntags: [a]\n---\nx\n", ["#hash", "a: b"])
    assert vault.parse_frontmatter(updated)[0]["tags"] == ["#hash", "a: b"]


def _maintain(root: Path, key: str, **kwargs):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")
    return writer_lease.invoke_command(command, root, idempotency_key=key, **kwargs)


def _variant_vault(root: Path) -> dict[str, str]:
    _log(root)
    bodies = {}
    for index in range(4):
        path = _page(root, f"Notes/canon-{index}.md", "tags: [dogfood, failures]")
        bodies[path.name] = path.read_text(encoding="utf-8").split("---\n", 2)[2]
    for index in range(2):
        body = f"This body says dogfooding and failure {index} and must not change.\n"
        path = _page(root, f"Notes/variant-{index}.md", "tags: [dogfooding, failure, Kept]", body)
        bodies[path.name] = body
    _page(root, "Sources/Articles/raw.md", "tags: [dogfooding]")
    return bodies


def test_preview_lists_groups_with_counts_and_writes_nothing(tmp_path):
    _variant_vault(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    result = _maintain(tmp_path, "preview", mode="tag-variants")
    groups = {group["canonical"]: group for group in result["groups"]}
    assert groups["dogfood"]["variants"] == [
        {"tag": "dogfood", "uses": 4},
        {"tag": "dogfooding", "uses": 2},
    ]
    assert groups["failures"]["uses"] == 6
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
        why="Reconcile inflection variants.",
    )
    assert applied["state"] == "committed"
    for index in range(2):
        path = tmp_path / f"Knowledge Base/Notes/variant-{index}.md"
        fm, body, _ = vault.parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["tags"] == ["dogfood", "failures", "Kept"]
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
            why="Reconcile inflection variants.",
        )


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


def test_apply_leaf_reports_what_remains(tmp_path, monkeypatch):
    monkeypatch.setattr(tag_variants, "BATCH_PAGES", 1)
    _variant_vault(tmp_path)
    plan_id = tag_variants.preview(tmp_path)["plan_id"]
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with manager.mutation_guard(tmp_path, operation="test"):
        pass
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    result = tag_variants.apply(tmp_path, plan_id=plan_id, why="Batch one.")
    assert result["pages_remaining"] == 1
    assert result["next"] == {"tool": "maintain_memory", "args": {"mode": "tag-variants"}}


def test_apply_requires_plan_and_reason(tmp_path):
    _variant_vault(tmp_path)
    with pytest.raises(Exception, match="INVALID_ARGUMENTS"):
        _maintain(tmp_path, "bad", mode="tag-variants", apply=True, why="No plan.")
