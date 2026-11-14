from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from exomem import generated_fragments as fragments
from exomem import indexes


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reference(number: int) -> fragments.SourceRef:
    return fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=number)}")


@pytest.mark.parametrize("epoch", [None, 7])
def test_copied_legacy_uncertainty_survives_slicing_wrapping_and_fresh_entry_join(epoch):
    text = "Café 🧭 retained\r\n"
    legacy = fragments._Image.before(text, None, _hash(text), current_epoch=epoch)
    legacy.normalize_newlines()
    excerpt = legacy.slice(0, 6).wrap_excerpt("[", "]")
    current = fragments._Image("fresh entry").own(_reference(1))
    joined = fragments._Image.join((current, fragments._Image("\n"), excerpt))
    joined.splice(0, 5, fragments._Image("new"))
    plan = fragments.FragmentPlan.model_validate_json(joined.plan(_hash(text)).model_dump_json())
    assert plan.unknown_through_epoch == epoch
    assert {item.source_ref for item in plan.after.fragments} == {_reference(1).value}
    rebound = fragments._Image.before(joined.text, plan, _hash(joined.text))
    assert rebound.unknown_through_epoch == epoch


def test_lost_or_mismatched_before_map_raises_uncertainty_to_current_checkpoint():
    text = "retained bytes"
    valid = fragments._Image(text).plan(_hash(text))
    lost = fragments._Image.before(text, None, _hash(text), unknown_through_epoch=0, current_epoch=11)
    changed = fragments._Image.before(text, valid, _hash("different"), current_epoch=13)
    assert lost.plan(_hash(text)).unknown_through_epoch == 11
    assert changed.plan(_hash(text)).unknown_through_epoch == 13
    assert not lost.spans and not changed.spans


@pytest.mark.parametrize("kind", ["top", "notes", "entities"])
def test_count_refresh_keeps_source_owned_unicode_prose_and_crlf_byte_ranges(kind):
    reference = _reference(8)
    excerpt = fragments._Image("retained Café 🧭").own(reference)
    if kind == "top":
        image = fragments._Image.join((
            fragments._Image("# Index\r\n\r\n## Recent activity\r\n\r\n- "), excerpt,
            fragments._Image("\r\n\r\n## Counts\r\n\r\n- Notes (research): 1\r\n- Entities (person): 1\r\n"),
        ))

        def refresh(text):
            return indexes._rewrite_top_index_notes_and_entities_counts(
                text, notes_counts={"research": 2}, entities_counts={"person": 3}, _tracking=image,
            )
    elif kind == "notes":
        image = fragments._Image.join((
            fragments._Image("# Notes\r\n\r\n## By type\r\n\r\n### Research — "), excerpt,
            fragments._Image(" (1)\r\n- [[Notes/Research/Demo/|Demo]] (1) — "), excerpt,
            fragments._Image("\r\n"),
        ))

        def refresh(text):
            return indexes._refresh_notes_subindex_text(
                text, counts_by_type={"research": 2}, counts_by_subfolder={"Research": {"Demo": 2}}, _tracking=image,
            )
    else:
        image = fragments._Image.join((
            fragments._Image("# Entities\r\n\r\n## By type\r\n\r\n- [[Entities/People/|People]] (1) — "),
            excerpt, fragments._Image("\r\n"),
        ))

        def refresh(text):
            return indexes._refresh_entities_subindex_text(text, counts_by_type={"person": 3}, _tracking=image)

    before = _hash(image.text)
    image.unknown_through_epoch = 7
    rendered = indexes._rewritten_in_place(image.text, refresh, _tracking=image)
    assert "retained Café 🧭" in rendered
    assert "\n" not in rendered.replace("\r\n", "")
    plan = image.plan(before)
    assert plan.unknown_through_epoch == 7
    stripped, after = fragments.remove_source_fragments(rendered, plan, reference)
    assert "retained Café 🧭" not in stripped
    assert after.unknown_through_epoch == 7
    if kind == "top":
        assert "- Notes: 2" in stripped and "- Entities: 3" in stripped
    elif kind == "notes":
        assert "### Research —  (2)" in stripped and "[[Notes/Research/Demo/|Demo]] (2)" in stripped
    else:
        assert "[[Entities/People/|People]] (3)" in stripped


def _seed(root: Path) -> dict[str, fragments.FragmentMap | fragments.FragmentPlan]:
    contents = {
        "Knowledge Base/Sources/index.md": "# Sources\n\n## By type\n\n## Recent captures\n\n",
        "Knowledge Base/index.md": "# Index\n\n## Recent activity\n\n<!-- Newest first. -->\n\n## Counts\n\n- Sources: 0\n",
        "Knowledge Base/log.md": "# Activity\n\n---\n\n",
    }
    maps = {}
    for relative, text in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        maps[relative] = fragments.FragmentMap(image_sha256=_hash(text))
    (root / "Knowledge Base/.obsidian").mkdir()
    return maps


def _capture(
    root: Path,
    maps: dict,
    reference: fragments.SourceRef,
    number: int,
    *,
    summary: str | None = None,
) -> indexes.IndexUpdate:
    update = indexes.compute_updates(
        root,
        source_type="article",
        folder_title="Articles",
        folder_description="captured material",
        rel_source_path=f"Knowledge Base/Sources/Articles/capture-{number}",
        date_iso="2026-11-14",
        activity_summary=summary or f"Captured synthetic entry {number}",
        log_entry_body=f"Kept body {number}: [[Knowledge Base/Sources/Articles/capture-{number}]]",
        source_counts={"Articles": number},
        source_ref=reference,
        before_fragment_maps=maps,
    )
    for relative, content in zip(
        ("Knowledge Base/Sources/index.md", "Knowledge Base/index.md", "Knowledge Base/log.md"),
        (update.sources_index_content, update.top_index_content, update.log_content),
        strict=True,
    ):
        (root / relative).write_bytes(content.encode("utf-8"))
    maps.update(
        {
            key: fragments.FragmentPlan.model_validate_json(plan.model_dump_json())
            for key, plan in update.fragment_plans.items()
        }
    )
    return update


def test_real_capture_trim_keeps_old_source_ownership_inside_new_log_entry(tmp_path: Path) -> None:
    """Erasing a trimmed Source must preserve the capture that copied its excerpt."""
    maps = _seed(tmp_path)
    old, current = _reference(1), _reference(100)
    first = _capture(
        tmp_path, maps, old, 1, summary="Café 🧭 [[Knowledge Base/Sources/Articles/capture-1]]"
    )
    assert "[[Sources/Articles/capture-1]]" in first.top_index_content
    old_id = next(
        f.opaque_fragment_id
        for f in first.fragment_plans["Knowledge Base/index.md"].after.fragments
    )
    for relative, content in (
        ("Knowledge Base/Sources/index.md", first.sources_index_content),
        ("Knowledge Base/index.md", first.top_index_content),
        ("Knowledge Base/log.md", first.log_content),
    ):
        removed, removal = fragments.remove_source_fragments(
            content, first.fragment_plans[relative], old
        )
        assert "capture-1" not in removed
        assert removal.coverage_complete

    # A valid owner map must bind raw CRLF bytes before the renderer normalizes them.
    for relative, plan in list(maps.items()):
        assert isinstance(plan, fragments.FragmentPlan)
        original = (tmp_path / relative).read_bytes().decode("utf-8")
        crlf = original.replace("\n", "\r\n")
        translated = []
        for fragment in plan.after.fragments:
            start = len(
                original.encode("utf-8")[: fragment.start_byte]
                .decode("utf-8")
                .replace("\n", "\r\n")
                .encode("utf-8")
            )
            end = len(
                original.encode("utf-8")[: fragment.end_byte]
                .decode("utf-8")
                .replace("\n", "\r\n")
                .encode("utf-8")
            )
            translated.append(
                fragment.model_copy(
                    update={
                        "start_byte": start,
                        "end_byte": end,
                        "payload_sha256": hashlib.sha256(
                            crlf.encode("utf-8")[start:end]
                        ).hexdigest(),
                    }
                )
            )
        maps[relative] = fragments.FragmentMap(
            image_sha256=_hash(crlf), fragments=tuple(translated)
        )
        (tmp_path / relative).write_bytes(crlf.encode("utf-8"))

    for number in range(2, indexes.RECENT_ACTIVITY_CAP + 1):
        _capture(tmp_path, maps, _reference(number), number)
    before = (tmp_path / "Knowledge Base/log.md").read_bytes().decode("utf-8")
    update = _capture(tmp_path, maps, current, 51)
    plan = update.fragment_plans["Knowledge Base/log.md"]
    plan.after.validate_image(update.log_content.encode("utf-8"))
    assert plan.coverage_complete, plan.unknown
    old_fragments = [f for f in plan.after.fragments if f.source_ref == old.value]
    copied = next(f for f in old_fragments if f.opaque_fragment_id == old_id)
    copied_text = update.log_content.encode("utf-8")[copied.start_byte : copied.end_byte].decode(
        "utf-8"
    )
    assert copied_text == (
        "(bottom entry drops off at cap-50; trimmed 1 this write — bottom was: "
        "- 2026-11-14 — Café 🧭 `Sources/Articles/capture-1`…)"
    )
    outer = next(f for f in plan.after.fragments if f.source_ref == current.value)
    assert outer.start_byte < copied.start_byte < copied.end_byte < outer.end_byte

    without_old, after_old = fragments.remove_source_fragments(update.log_content, plan, old)
    assert "bottom entry drops off" not in without_old
    assert "Café" not in without_old
    assert "## [2026-11-14] add | Sources/Articles/capture-51" in without_old
    assert "Kept body 51: `Knowledge Base/Sources/Articles/capture-51`" in without_old
    assert after_old.coverage_complete
    after_old.after.validate_image(without_old.encode("utf-8"))

    without_current, after_current = fragments.remove_source_fragments(
        update.log_content, plan, current
    )
    assert (
        without_current
        == before[: before.index("\n---\n") + len("\n---\n")]
        + "\n\n"
        + before[before.index("\n---\n") + len("\n---\n") :]
    )
    assert not any(f.opaque_fragment_id == old_id for f in after_current.after.fragments)
    assert any(f.source_ref == old.value for f in after_current.after.fragments)


@pytest.mark.parametrize("corruption", ["image", "payload", "crossing"])
def test_invalid_before_map_cannot_authorize_removal(corruption: str) -> None:
    """A changed image or forged range cannot erase coincidentally matching prose."""
    reference = _reference(1)
    text = "prefix Café 🧭 suffix"
    data = text.encode("utf-8")
    fragment = fragments.Fragment(
        opaque_fragment_id="synthetic-owned-range",
        source_ref=reference.value,
        start_byte=0,
        end_byte=len(data) - 2,
        payload_sha256=hashlib.sha256(data[:-2]).hexdigest(),
    )
    mapping = fragments.FragmentMap(image_sha256=_hash(text), fragments=(fragment,))
    if corruption == "image":
        text += " externally changed"
    elif corruption == "payload":
        mapping = mapping.model_copy(
            update={"fragments": (fragment.model_copy(update={"payload_sha256": "0" * 64}),)}
        )
    else:
        crossing = fragment.model_copy(
            update={
                "opaque_fragment_id": "crossing-range",
                "start_byte": 2,
                "end_byte": len(data),
                "payload_sha256": hashlib.sha256(data[2:]).hexdigest(),
            }
        )
        mapping = mapping.model_copy(update={"fragments": (fragment, crossing)})
    result, plan = fragments.remove_source_fragments(text, mapping, reference)
    assert result == text
    assert not plan.coverage_complete
    assert plan.unknown


def test_missing_map_and_marker_looking_input_never_mint_source_ownership(tmp_path: Path) -> None:
    """Only the resolved primary reference can own caller text in a new capture."""
    _seed(tmp_path)
    claimed, actual = _reference(1), _reference(2)
    marker = f'<!-- fragment source_ref="{claimed.value}" -->'
    update = _capture(tmp_path, {}, actual, 2, summary=marker)
    plan = update.fragment_plans["Knowledge Base/index.md"]
    result, after = fragments.remove_source_fragments(update.top_index_content, plan, claimed)
    assert result == update.top_index_content
    assert marker in result
    assert {f.source_ref for f in plan.after.fragments} == {actual.value}
    assert not after.coverage_complete
    assert "fragment_map_missing" in after.unknown


def test_fragment_reference_requires_the_canonical_immutable_spelling() -> None:
    with pytest.raises(ValueError, match="fragment_source_ref_invalid"):
        fragments.SourceRef("exomem://memory/not-a-uuid")
