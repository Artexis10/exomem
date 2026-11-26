"""Face grouping: each new face joins the nearest group of its embedder, or starts a group of its own.

A face joins the group whose centroid is most similar when that cosine similarity is at least
`join_similarity` and beats the next group by at least `join_margin`. Otherwise it starts a new group: a
doubtful face stays apart rather than join the wrong person. A face is stored only when its detection
score reaches `min_detection_score` and its box is at least `min_face_side` pixels on each side.

The four settings take their defaults from data: the pinned models' calibrations in `media_models.json`
and the assignment policy in `face_policy.json`. The owner's overrides in the people store win. A changed
setting applies to faces grouped afterwards; existing groups stay as they are.

Grouping is incremental and deterministic: the same faces in the same order from an empty store give the
same groups with the same ids. Group ids stay stable, so names attached to groups survive later faces.
A face the owner moved out of a group (an exclusion) never joins that group again.

A re-scanned page's faces leave their groups before they are assigned again. A group that loses its last
face this way keeps its direction, so the same face, or its person in another picture, joins it again and
its name stays attached. The first face to join it replaces that direction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np

from . import face_store, media_models

#: Two detections of one face in one picture overlap at least this much (intersection over union). A rebuilt
#: face index detects the same face with a slightly different box when the execution provider differs, and an
#: exclusion must still find it. It identifies a detection, so it is geometry, not grouping policy.
_SAME_FACE_OVERLAP = 0.5
#: Keyframe times of one face agree to this many seconds; the CLIP index stores the time it sampled.
_SAME_FRAME_SECONDS = 1e-3


@dataclass(frozen=True)
class Thresholds:
    join_similarity: float
    join_margin: float
    min_face_side: float
    min_detection_score: float


@dataclass(frozen=True)
class PageFace:
    """One detected face of a picture, as the face step hands it to grouping."""

    #: None for an image; the keyframe time in seconds for a video.
    frame_ts: float | None
    #: [x, y, width, height] fractions of the upright picture: the `read_media` region form.
    region: tuple[float, float, float, float]
    #: The box's shorter side in pixels of the upright picture.
    side: float
    score: float
    #: The embedder's unit vector.
    vector: np.ndarray


@cache
def policy() -> dict[str, Any]:
    """The shipped record of the grouping settings and where each default comes from."""
    return json.loads(files("exomem").joinpath("face_policy.json").read_text(encoding="utf-8"))["settings"]


def defaults() -> dict[str, float]:
    """Each setting's default: a model calibration from the manifest, or the policy record's value."""
    values = {}
    for name, entry in policy().items():
        calibration = entry.get("calibration")
        if calibration is None:
            values[name] = float(entry["default"])
        else:
            spec = media_models.model_spec(calibration["model"])
            values[name] = float(spec["calibration"][calibration["name"]]["value"])
    return values


def thresholds(people: sqlite3.Connection) -> Thresholds:
    """The settings in force: the defaults, with the owner's overrides from the people store."""
    values = defaults()
    for name, value in people.execute("SELECT name, value FROM threshold_overrides"):
        if name in values:
            values[name] = float(value)
    return Thresholds(**values)


def _overlap(first: Sequence[float], second: Sequence[float]) -> float:
    """Intersection over union of two [x, y, width, height] boxes."""
    x1, y1, w1, h1 = first
    x2, y2, w2, h2 = second
    width = min(x1 + w1, x2 + w2) - max(x1, x2)
    height = min(y1 + h1, y2 + h2) - max(y1, y2)
    inter = width * height if width > 0 and height > 0 else 0.0
    union = w1 * h1 + w2 * h2 - inter
    return inter / union if union > 0 else 0.0


def same_face(frame_ts: float | None, region: Sequence[float], other_ts: float | None, other: Sequence[float]) -> bool:
    """Whether two detections on one page are the same face: the same frame and overlapping boxes."""
    if (frame_ts is None) != (other_ts is None):
        return False
    if frame_ts is not None and abs(frame_ts - other_ts) > _SAME_FRAME_SECONDS:  # type: ignore[operator]
        return False
    return _overlap(region, other) >= _SAME_FACE_OVERLAP


class _Centroids:
    """The centroid sums of one embedder's groups, in id order, with their unit directions.

    Rows grow by doubling, so a store of many single-face groups costs no copy per new group.
    """

    def __init__(self, rows: list[tuple[int, bytes, int]], width: int) -> None:
        capacity = max(16, len(rows))
        self.ids: list[int] = []
        self.row_of: dict[int, int] = {}
        self.sums = np.zeros((capacity, width), dtype=np.float32)
        self.unit = np.zeros((capacity, width), dtype=np.float32)
        self.counts: list[int] = []
        self.dirty: set[int] = set()
        for group_id, blob, count in rows:
            self._append(group_id, face_store.unpack_vector(blob), count)
        self.dirty.clear()

    def _append(self, group_id: int, total: np.ndarray, count: int) -> None:
        row = len(self.ids)
        if row == len(self.sums):
            self.sums = np.vstack([self.sums, np.zeros_like(self.sums)])
            self.unit = np.vstack([self.unit, np.zeros_like(self.unit)])
        self.ids.append(group_id)
        self.row_of[group_id] = row
        self.counts.append(count)
        self.sums[row] = total
        self._refresh(row)
        self.dirty.add(group_id)

    def _refresh(self, row: int) -> None:
        norm = float(np.linalg.norm(self.sums[row]))
        self.unit[row] = self.sums[row] / norm if norm > 0 else 0.0

    def add_group(self, group_id: int, vector: np.ndarray) -> None:
        self._append(group_id, np.asarray(vector, dtype=np.float32), 1)

    def join(self, group_id: int, vector: np.ndarray) -> None:
        row = self.row_of[group_id]
        if self.counts[row]:
            self.sums[row] += np.asarray(vector, dtype=np.float32)
        else:
            self.sums[row] = vector  # A group with no face holds only the direction it last had.
        self.counts[row] += 1
        self._refresh(row)
        self.dirty.add(group_id)

    def leave(self, group_id: int, vector: np.ndarray) -> None:
        """Take a re-scanned face out of its group's centroid; a group it empties keeps its direction.

        A group of another embedder is not held here.
        """
        row = self.row_of.get(group_id)
        if row is None:
            return
        if self.counts[row] > 1:
            self.sums[row] -= np.asarray(vector, dtype=np.float32)
            self._refresh(row)
        self.counts[row] = max(0, self.counts[row] - 1)
        self.dirty.add(group_id)

    def nearest(self, vector: np.ndarray, limits: Thresholds, barred: set[int]) -> int | None:
        """The group this face joins, or None when it starts a new group."""
        count = len(self.ids)
        if count == 0:
            return None
        similarity = self.unit[:count] @ np.asarray(vector, dtype=np.float32)
        # A group the owner emptied has a zero direction; it and a barred group take no face.
        empty = ~self.unit[:count].any(axis=1)
        similarity = np.where(empty, -np.inf, similarity)
        for group_id in barred:
            row = self.row_of.get(group_id)
            if row is not None:
                similarity[row] = -np.inf
        order = np.argsort(-similarity, kind="stable")  # ties go to the older group
        best = float(similarity[order[0]])
        if not np.isfinite(best) or best < limits.join_similarity:
            return None
        runner = float(similarity[order[1]]) if count > 1 else -np.inf
        if best - runner < limits.join_margin:
            return None
        return self.ids[int(order[0])]

    def changed_rows(self) -> list[tuple[bytes, int, int]]:
        rows = [
            (face_store.pack_vector(self.sums[self.row_of[group_id]]), self.counts[self.row_of[group_id]], group_id)
            for group_id in sorted(self.dirty)
        ]
        self.dirty.clear()
        return rows


#: Per people store and embedder, the centroids as of a face index generation. A record that finds the
#: generation unchanged skips reloading every group, so a backfill reads the groups once, not once per picture.
_CACHE: dict[tuple[str, str], tuple[str, _Centroids]] = {}
#: Writers in this process take turns; writers in other processes wait on the people store's write lock.
_WRITE_LOCK = threading.Lock()


def _centroids(people: sqlite3.Connection, index: sqlite3.Connection, key: tuple[str, str]) -> _Centroids:
    generation = face_store.current_generation(index)
    held = _CACHE.pop(key, None)
    if held is not None and held[0] == generation:
        return held[1]
    rows = people.execute(
        "SELECT id, centroid_sum, face_count FROM groups WHERE model_id = ? ORDER BY id", (key[1],)
    ).fetchall()
    return _Centroids(rows, int(media_models.model_spec("face-embed")["dimensions"]))


class Writing:
    """One write to both face stores: the people store first, then the face index, each held immediately.

    Every writer takes the two locks in this order, so two writers never wait on each other. A crash
    between the two commits leaves a centroid that counts faces the face index lost; the page stays
    unscanned and its next scan adds them again, which moves no centroid's direction.
    """

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root

    def __enter__(self) -> Writing:
        _WRITE_LOCK.acquire()
        try:
            self.people = face_store.connect_people(self.vault_root)
            try:
                self.index = face_store.connect_index(self.vault_root)
            except BaseException:
                self.people.close()
                raise
            self.people.execute("BEGIN IMMEDIATE")
            self.index.execute("BEGIN IMMEDIATE")
        except BaseException:
            for name in ("index", "people"):
                connection = getattr(self, name, None)
                if connection is not None:
                    connection.close()
            _WRITE_LOCK.release()
            raise
        return self

    def __exit__(self, kind: Any, error: Any, trace: Any) -> None:
        try:
            if kind is None:
                self.people.commit()
                self.index.commit()
            else:
                self.people.rollback()
                self.index.rollback()
        finally:
            self.people.close()
            self.index.close()
            _WRITE_LOCK.release()


def _exclusions(people: sqlite3.Connection, page: str) -> list[tuple[int, float | None, tuple[float, ...]]]:
    return [
        (group_id, frame_ts, (x, y, width, height))
        for group_id, frame_ts, x, y, width, height in people.execute(
            "SELECT group_id, frame_ts, x, y, width, height FROM exclusions WHERE page = ?", (page,)
        )
    ]


def record_page(vault_root: Path, page: str, faces: Sequence[PageFace], *, model_id: str) -> list[int]:
    """Replace a page's faces with `faces` and group each one that the settings keep.

    `page` is the picture's vault-relative path, as the CLIP index keys it. The page's earlier faces leave
    their groups' centroids first, so a re-scan counts each face once. The page is recorded as scanned
    even when it holds no face. Returns the group id of each stored face, in storage order.
    """
    key = (str(face_store.people_path(vault_root)), model_id)
    with Writing(vault_root) as writing:
        people, index = writing.people, writing.index
        limits = thresholds(people)
        centroids = _centroids(people, index, key)
        for group_id, blob in index.execute(
            "SELECT group_id, vector FROM faces WHERE page = ? AND group_id IS NOT NULL ORDER BY id", (page,)
        ).fetchall():
            centroids.leave(group_id, face_store.unpack_vector(blob))
        index.execute("DELETE FROM faces WHERE page = ?", (page,))
        kept = sorted(
            (face for face in faces if face.score >= limits.min_detection_score and face.side >= limits.min_face_side),
            key=lambda face: (-1.0 if face.frame_ts is None else face.frame_ts, face.region),
        )
        excluded = _exclusions(people, page)
        rows, groups = [], []
        for face in kept:
            barred = {
                group_id for group_id, frame_ts, region in excluded
                if same_face(face.frame_ts, face.region, frame_ts, region)
            }
            group_id = centroids.nearest(face.vector, limits, barred)
            if group_id is None:
                group_id = people.execute(
                    "INSERT INTO groups (model_id, centroid_sum, face_count) VALUES (?, ?, 1)",
                    (model_id, face_store.pack_vector(face.vector)),
                ).lastrowid
                centroids.add_group(group_id, face.vector)
            else:
                centroids.join(group_id, face.vector)
            groups.append(group_id)
            rows.append((
                page, face.frame_ts, *face.region, face.score, model_id, face_store.pack_vector(face.vector), group_id,
            ))
        people.executemany("UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?", centroids.changed_rows())
        index.executemany(
            "INSERT INTO faces (page, frame_ts, x, y, width, height, score, model_id, vector, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        index.execute(
            "INSERT OR REPLACE INTO scanned_pages (page, model_id, face_count) VALUES (?, ?, ?)",
            (page, model_id, len(rows)),
        )
        generation = face_store.advance_generation(index)
        # Held only once both commits succeed; `Writing` commits on leaving this block.
    _CACHE[key] = (generation, centroids)
    return groups


def scanned(vault_root: Path, pages: Sequence[str], *, model_id: str) -> set[str]:
    """Which of `pages` the face index records as scanned with this embedder. One query for the whole list."""
    if not pages or not face_store.index_path(vault_root).is_file():
        return set()
    with closing(face_store.connect_index(vault_root)) as index:
        index.execute("CREATE TEMP TABLE IF NOT EXISTS wanted (page TEXT PRIMARY KEY)")
        index.executemany("INSERT OR IGNORE INTO temp.wanted (page) VALUES (?)", ((page,) for page in pages))
        return {
            page for (page,) in index.execute(
                "SELECT s.page FROM scanned_pages s JOIN temp.wanted w ON w.page = s.page WHERE s.model_id = ?",
                (model_id,),
            )
        }


def scanned_count(vault_root: Path, *, model_id: str) -> int:
    """How many pages the face index records as scanned with this embedder."""
    if not face_store.index_path(vault_root).is_file():
        return 0
    with closing(face_store.connect_index(vault_root)) as index:
        return int(index.execute("SELECT COUNT(*) FROM scanned_pages WHERE model_id = ?", (model_id,)).fetchone()[0])
