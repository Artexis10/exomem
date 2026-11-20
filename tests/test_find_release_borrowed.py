"""A hit on a page never carries text or annotations taken from a page the release policy withholds.

A scene frame is its own page, grouped under its video's page. A hit on the video can borrow the
frame's excerpt, its path and its time. The release policy decides the frame by its own path, so a
ceiling rule can withhold the frame while the video stays visible. The video's hit must then carry
nothing the frame supplied, however the frame page names its media file or where it is filed.
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
from exomem.find_types import LENT_FRAME, Hit
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


# A child page is grouped under its video by `parent_media` alone. What it names as its own media
# file, and where it is filed, decide nothing about whether it lends its text to the video's hit.
CHILD_SHAPES = {
    "frames-folder-no-media-file": (f"{CLIP}.frames/scene-000-t1000ms.jpg.md", {}, "Notes/Browse/Clip-Two.mp4.frames/**"),
    "private-folder-no-media-file": ("Knowledge Base/Private/moment.md", {}, "Private/**"),
    "private-folder-media-file-is-the-video": ("Knowledge Base/Private/moment.jpg.md", {"evidence_file": CLIP}, "Private/**"),
}
BROWSE_OR_KEYWORD = pytest.mark.parametrize(
    "find_args", [{"query": ""}, {"query": "confidential", "mode": "keyword"}], ids=["browse", "keyword"],
)


def _write_child_world(root, shape: str, *, body: str, **extra) -> str:
    child, media, glob = CHILD_SHAPES[shape]
    pages = {
        VIDEO_PAGE: _page("Clip Two", kind="source", updated="2026-03-03", evidence_file=CLIP),
        child: _page("Frame one", kind="source", updated="2026-03-04", body=body,
                     parent_media=CLIP, frame_ts=1.0, **media, **extra),
        f"{NOTES}/Other.md": _page("Other", updated="2026-03-01"),
    }
    for rel, content in pages.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content)
    governance = root / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "child.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Child\npaths: ["{glob}"]\n', encoding="utf-8",
    )
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "rules" / "child-external.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )
    return child


@STORE_MODES
@BROWSE_OR_KEYWORD
@pytest.mark.parametrize("shape", list(CHILD_SHAPES))
def test_a_withheld_child_page_lends_nothing_whatever_media_file_it_names(vault, store, find_args, shape) -> None:  # noqa: F811
    child = _write_child_world(vault, shape, body=FRAME_TEXT)
    build_indexes(vault)
    if store:
        assert migrate_apply(vault) == 0
    _drop_caches()

    hits = _hits(vault, EXTERNAL, **find_args)

    assert child not in [hit["path"] for hit in hits]
    assert FRAME_TEXT not in json.dumps(hits)
    assert VIDEO_PAGE in [hit["path"] for hit in hits]


@STORE_MODES
@BROWSE_OR_KEYWORD
def test_the_owner_reads_a_frame_whose_media_file_is_not_its_own_sidecars_name(vault, store, find_args) -> None:  # noqa: F811
    frames = f"{CLIP}.frames"
    offbeat = f"{frames}/renamed.jpg"
    pages = {
        VIDEO_PAGE: _page("Clip Two", kind="source", updated="2026-03-03", evidence_file=CLIP),
        f"{frames}/scene-000-t1000ms.jpg.md": _page(
            "Frame one", kind="source", updated="2026-03-04", body=FRAME_TEXT,
            parent_media=CLIP, evidence_file=offbeat, frame_ts=1.0,
        ),
        f"{NOTES}/Other.md": _page("Other", updated="2026-03-01"),
    }
    for rel, content in pages.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(content)
    # A governed vault whose one rule touches nothing here: every page stays released.
    (vault / "Knowledge Base" / "_Governance" / "scopes").mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base" / "_Governance" / "scopes" / "other.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Other\npaths: ["Unrelated/**"]\n', encoding="utf-8",
    )
    (vault / "Knowledge Base" / "_Governance" / "rules").mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base" / "_Governance" / "rules" / "other-external.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )
    build_indexes(vault)
    if store:
        assert migrate_apply(vault) == 0
    _drop_caches()

    video = _video_hit(_hits(vault, OWNER, **find_args))

    assert video["scene_frame"] == offbeat
    if find_args.get("query"):
        # Only the frame matches the query, so its text is the excerpt.
        assert FRAME_TEXT in video["excerpt"]
    else:
        # A browse takes the excerpt of whichever family member it meets first, which depends on
        # walk order; the owner must still get an excerpt, never the cleared "".
        assert video["excerpt"]


def _write_video_whose_own_text_matches(root) -> None:
    """A video whose own timed transcript matches `keynote`, with one frame that a rule withholds from `external`."""
    _write_video_with_withheld_frame(root)
    (root / CLIP).write_bytes(b"\x00video")
    (root / f"{CLIP}.frames" / "scene-000-t1000ms.jpg").write_bytes(b"\xff\xd8x")
    (root / f"{CLIP}.frames" / f"{FRAME_MEDIA.rsplit('/', 1)[-1]}.md").unlink()
    (root / f"{CLIP}.frames" / "scene-000-t1000ms.jpg.md").write_bytes(
        _page("Frame one", kind="source", updated="2026-03-04", body=FRAME_TEXT,
              parent_media=CLIP, evidence_file=f"{CLIP}.frames/scene-000-t1000ms.jpg", frame_ts=1.0)
    )
    (root / VIDEO_PAGE).write_bytes(
        _page("Clip Two", kind="source", updated="2026-03-03", evidence_file=CLIP, media_type="video",
              body="## Extracted text\n\n[0:01] keynote about turbines")
    )


@STORE_MODES
def test_withholding_only_a_videos_nearest_frame_keeps_the_videos_own_excerpt(vault, store) -> None:  # noqa: F811
    _write_video_whose_own_text_matches(vault)
    build_indexes(vault)
    if store:
        assert migrate_apply(vault) == 0
    _drop_caches()

    owner = _video_hit(_hits(vault, OWNER, query="keynote", mode="keyword"))
    external = _video_hit(_hits(vault, EXTERNAL, query="keynote", mode="keyword"))

    assert owner["scene_frame"] == f"{CLIP}.frames/scene-000-t1000ms.jpg"
    assert "keynote" in external["excerpt"]
    assert "scene_frame" not in external and "scene_match_at" not in external


def _released(vault, who: RequestPrincipal, hit: Hit):  # noqa: F811
    _drop_caches()
    with request_scope(who):
        return egress.annotate_hits(vault, [hit], limit=5)


def test_a_chunk_taken_from_a_withheld_page_is_cleared_from_the_hit_that_declared_it(vault) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    frame_hash = hashlib.sha256((vault / FRAME_PAGE).read_bytes()).hexdigest()

    def video_hit() -> Hit:
        # A semantic hit declares each page its excerpt and annotations came from, and not what each lent.
        return Hit(
            path=VIDEO_PAGE, type="source", scope=None, title="Clip Two", updated="2026-03-03",
            excerpt=FRAME_TEXT, passage={"text": FRAME_TEXT}, matched_part=2, transcript_ts=1.0,
            evidence_sources={FRAME_PAGE: frame_hash},
        )

    owner = _released(vault, OWNER, video_hit())
    external = _released(vault, EXTERNAL, video_hit())

    assert [h.path for h in owner.hits] == [VIDEO_PAGE]
    assert [h.path for h in external.hits] == [VIDEO_PAGE]
    assert (owner.hits[0].excerpt, owner.hits[0].matched_part) == (FRAME_TEXT, 2)
    assert (external.hits[0].excerpt, external.hits[0].passage) == ("", None)
    assert (external.hits[0].matched_part, external.hits[0].transcript_ts) == (None, None)


def test_a_page_that_lent_only_its_frame_leaves_the_hits_own_text(vault) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    frame_hash = hashlib.sha256((vault / FRAME_PAGE).read_bytes()).hexdigest()
    hit = Hit(
        path=VIDEO_PAGE, type="source", scope=None, title="Clip Two", updated="2026-03-03",
        excerpt="the video's own words", scene_frame=FRAME_MEDIA, scene_frame_ts=1.0, transcript_ts=1.0,
        evidence_sources={FRAME_PAGE: frame_hash}, lent_roles={FRAME_PAGE: (LENT_FRAME,)},
    )

    released = _released(vault, EXTERNAL, hit)

    assert [h.path for h in released.hits] == [VIDEO_PAGE]
    assert released.hits[0].excerpt == "the video's own words"
    assert released.hits[0].transcript_ts == 1.0
    assert (released.hits[0].scene_frame, released.hits[0].scene_frame_ts) == (None, None)


def test_a_tombstoned_frame_is_cleared_from_its_videos_hit_when_no_policy_is_set(vault, monkeypatch) -> None:  # noqa: F811
    _write_video_with_withheld_frame(vault)
    shutil.rmtree(vault / "Knowledge Base" / "_Governance")
    monkeypatch.setattr(egress.lifecycle, "tombstoned_paths", lambda _vault: frozenset({FRAME_PAGE}))
    frame_hash = hashlib.sha256((vault / FRAME_PAGE).read_bytes()).hexdigest()
    hit = Hit(
        path=VIDEO_PAGE, type="source", scope=None, title="Clip Two", updated="2026-03-03",
        excerpt=FRAME_TEXT, scene_frame=FRAME_MEDIA, scene_frame_ts=1.0,
        evidence_sources={FRAME_PAGE: frame_hash},
    )

    released = _released(vault, EXTERNAL, hit)

    assert [h.path for h in released.hits] == [VIDEO_PAGE]
    assert (released.hits[0].excerpt, released.hits[0].scene_frame, released.hits[0].scene_frame_ts) == ("", None, None)
