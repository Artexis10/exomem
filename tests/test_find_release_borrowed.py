"""A hit on a page never carries text or annotations taken from a page the release policy withholds.

A scene frame is its own page, grouped under its video's page. A hit on the video can borrow the
frame's excerpt, its path and its time. The release policy decides the frame by its own path, so a
ceiling rule can withhold the frame while the video stays visible. The video's hit must then carry
nothing the frame supplied.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import build_indexes, migrate_apply, vault  # noqa: F401

from exomem import commands
from exomem import find as find_module
from exomem.find_types import Hit
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal, request_scope

NOTES = "Knowledge Base/Notes/Browse"
CLIP = f"{NOTES}/Clip-Two.mp4"
VIDEO_PAGE = f"{CLIP}.md"
FRAME_MEDIA = f"{CLIP}.frames/scene-000-001.000.jpg"
FRAME_PAGE = f"{FRAME_MEDIA}.md"
FRAME_TEXT = "confidential frame excerpt"
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"

EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")
OWNER = RequestPrincipal(audience_id="owner", surface="cli")
# No rule names this audience: nothing is withheld from it.
UNRULED = RequestPrincipal(audience_id="partner", surface="mcp")

STORE_MODES = pytest.mark.parametrize("store", [False, True], ids=["file-mode", "store-mode"])


def _page(title, *, kind="note", updated=None, body=None, **frontmatter) -> bytes:
    lines = ["---", f"title: {title}", f"type: {kind}"]
    if updated:
        lines.append(f"updated: {updated}")
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    lines += ["---", f"# {title}", "", body or f"A note about {title}.", ""]
    return "\n".join(lines).encode()


def _write_video_with_withheld_frame(root) -> None:
    """A video with one scene frame, and a rule that withholds every frame from `external`."""
    pages = {
        VIDEO_PAGE: _page("Clip Two", kind="source", updated="2026-03-03", evidence_file=CLIP),
        FRAME_PAGE: _page(
            "Frame one", kind="source", updated="2026-03-04", body=FRAME_TEXT,
            parent_media=CLIP, evidence_file=FRAME_MEDIA, frame_ts=1.0,
        ),
        f"{NOTES}/Other.md": _page("Other", updated="2026-03-01"),
    }
    for rel, content in pages.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content)
    governance = root / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "frames.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Frames\npaths: ["Notes/Browse/Clip-Two.mp4.frames/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "rules" / "frames-external.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


def _drop_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def _world(vault, store: bool) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    build_indexes(vault)
    if store:
        assert migrate_apply(vault) == 0


def _hits(vault, who: RequestPrincipal, **find_args) -> list[dict]:  # noqa: F811
    with request_scope(who):
        answer = commands.op_find(vault, limit=100, graph=False, **find_args)
    return answer["hits"] if isinstance(answer, dict) else answer


def _video_hit(hits: list[dict]) -> dict:
    return next(hit for hit in hits if hit["path"] == VIDEO_PAGE)


@STORE_MODES
def test_a_withheld_frames_text_and_place_are_not_on_its_videos_browse_hit(vault, store) -> None:  # noqa: F811
    _world(vault, store)
    _drop_caches()

    hits = _hits(vault, EXTERNAL, query="")

    assert FRAME_PAGE not in [hit["path"] for hit in hits]
    video = _video_hit(hits)
    assert FRAME_TEXT not in json.dumps(hits)
    assert FRAME_MEDIA not in json.dumps(hits)
    assert "scene_frame" not in video and "scene_match_at" not in video


@STORE_MODES
@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_a_search_hit_on_the_video_carries_nothing_from_its_withheld_frame(vault, store, mode) -> None:  # noqa: F811
    _world(vault, store)
    _drop_caches()

    hits = _hits(vault, EXTERNAL, query="confidential", mode=mode)

    assert FRAME_TEXT not in json.dumps(hits)
    assert FRAME_MEDIA not in json.dumps(hits)
    # The video stays where it ranked; only what came from the frame is gone.
    assert VIDEO_PAGE in [hit["path"] for hit in hits]


@STORE_MODES
def test_the_owner_and_an_unruled_audience_still_read_the_frame_on_the_videos_hit(vault, store) -> None:  # noqa: F811
    _world(vault, store)
    _drop_caches()

    for who in (OWNER, UNRULED):
        video = _video_hit(_hits(vault, who, query=""))
        assert video["scene_frame"] == FRAME_MEDIA, who.audience_id
        assert "scene_match_at" in video, who.audience_id


@STORE_MODES
def test_an_external_answer_does_not_change_what_the_owner_reads_next_from_the_cache(vault, store) -> None:  # noqa: F811
    _world(vault, store)
    _drop_caches()

    _hits(vault, EXTERNAL, query="")
    # The caches stay warm from the external answer: the owner's answer comes from the same entries.
    owner_hits = _hits(vault, OWNER, query="")

    assert _video_hit(owner_hits)["scene_frame"] == FRAME_MEDIA


def _annotate(vault, who: RequestPrincipal, hit: Hit) -> Hit:  # noqa: F811
    _drop_caches()
    with request_scope(who):
        released = egress.annotate_hits(vault, [hit], limit=5)
    assert [h.path for h in released.hits] == [VIDEO_PAGE]
    return released.hits[0]


def test_a_chunk_taken_from_a_withheld_page_is_cleared_from_the_hit_that_declared_it(vault) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    frame_hash = hashlib.sha256((vault / FRAME_PAGE).read_bytes()).hexdigest()

    def video_hit() -> Hit:
        # A semantic hit declares each page its excerpt and annotations came from.
        return Hit(
            path=VIDEO_PAGE, type="source", scope=None, title="Clip Two", updated="2026-03-03",
            excerpt=FRAME_TEXT, passage={"text": FRAME_TEXT}, matched_part=2, transcript_ts=1.0,
            evidence_sources={FRAME_PAGE: frame_hash},
        )

    owner_hit = _annotate(vault, OWNER, video_hit())
    external_hit = _annotate(vault, EXTERNAL, video_hit())

    assert (owner_hit.excerpt, owner_hit.matched_part) == (FRAME_TEXT, 2)
    assert (external_hit.excerpt, external_hit.passage) == ("", None)
    assert (external_hit.matched_part, external_hit.transcript_ts) == (None, None)


def test_a_tombstoned_frame_is_cleared_from_its_videos_hit_when_no_policy_is_set(vault, monkeypatch) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    shutil.rmtree(vault / "Knowledge Base" / "_Governance")
    monkeypatch.setattr(egress.lifecycle, "tombstoned_paths", lambda _vault: frozenset({FRAME_PAGE}))
    hit = Hit(
        path=VIDEO_PAGE, type="source", scope=None, title="Clip Two", updated="2026-03-03",
        excerpt=FRAME_TEXT, scene_frame=FRAME_MEDIA, scene_frame_ts=1.0,
    )

    released = _annotate(vault, EXTERNAL, hit)

    assert (released.excerpt, released.scene_frame, released.scene_frame_ts) == ("", None, None)
