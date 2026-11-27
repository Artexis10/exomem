"""Face grouping: each new face joins the nearest group of its embedder, or starts a group of its own.

A face joins the group whose centroid is most similar when that cosine similarity is at least
`join_similarity` and beats the next group by at least `join_margin`. Otherwise it starts a new group: a
doubtful face stays apart rather than join the wrong person. A face is stored only when its detection
score reaches `min_detection_score` and its box is at least `min_face_side` pixels on each side.

The four settings take their defaults from data: the pinned models' calibrations in `media_models.json`
and the assignment policy in `face_policy.json`. The owner's overrides in the people store win. A changed
setting applies to faces grouped afterwards; existing groups stay as they are.

Grouping is incremental and deterministic: the same faces in the same order from an empty store give the
same groups. Group ids stay stable, so names attached to groups survive later faces.
A face the owner moved out of a group (an exclusion) never joins that group again.

A re-scanned page's faces leave their groups before they are assigned again. A group that loses its last
face this way keeps its direction, so the same face, or its person in another picture, joins it again and
its name stays attached. The first face to join it replaces that direction.

A new group starts unnamed in the face index. Naming moves it into the people store with its id, centroid
and exclusions (`face_naming`), so grouping reads the groups of both stores as one set. A group id is
random, not the next number: a people store restored from a backup, or a rebuilt face index, then never
hands an id it holds to another person's group. A restored people store can also count faces that the
face index no longer holds, so loading the groups takes a named group's count and centroid from the faces
the index holds.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
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

from . import face_store, media_models, media_types

log = logging.getLogger(__name__)

#: Two detections of one face in one picture overlap at least this much (intersection over union). A rebuilt
#: face index detects the same face with a slightly different box when the execution provider differs, and an
#: exclusion must still find it. It identifies a detection, so it is geometry, not grouping policy.
_SAME_FACE_OVERLAP = 0.5
#: Keyframe times of one face agree to this many seconds; the CLIP index stores the time it sampled.
_SAME_FRAME_SECONDS = 1e-3
#: Group ids are drawn below this bound: the largest integer that a JSON client reading numbers as
#: doubles keeps exact, since an agent passes a group id back to name it.
_GROUP_ID_BOUND = 2**53


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


def ranges() -> dict[str, tuple[float, float]]:
    """Each setting's lowest and highest allowed value from the policy record; no upper bound is infinity.

    A value outside its range (a similarity above 1, a score above 1) would silently stop all grouping or
    storage. Refusing it rejects no meaningful value and costs the owner one error message naming the range.
    """
    return {
        name: (float(low), math.inf if high is None else float(high))
        for name, (low, high) in ((name, entry["range"]) for name, entry in policy().items())
    }


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

    Holds the groups of both stores; `named` says which live in the people store. Rows grow by doubling, so
    a store of many single-face groups costs no copy per new group.
    """

    def __init__(self, rows: list[tuple[int, bytes, int]], width: int, named: set[int]) -> None:
        capacity = max(16, len(rows))
        self.ids: list[int] = []
        self.row_of: dict[int, int] = {}
        self.named = named
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
        # The best two in one pass each, not a sort of every group; a tie goes to the lower id.
        best_row = int(np.argmax(similarity))
        best = float(similarity[best_row])
        if not np.isfinite(best) or best < limits.join_similarity:
            return None
        similarity[best_row] = -np.inf
        runner = float(similarity.max()) if count > 1 else -np.inf
        if best - runner < limits.join_margin:
            return None
        return self.ids[best_row]

    def changed_rows(self) -> tuple[list[tuple[bytes, int, int]], list[tuple[bytes, int, int]]]:
        """The changed groups' (centroid sum, count, id) rows: the people store's, then the face index's."""
        named: list[tuple[bytes, int, int]] = []
        unnamed: list[tuple[bytes, int, int]] = []
        for group_id in sorted(self.dirty):
            row = self.row_of[group_id]
            (named if group_id in self.named else unnamed).append(
                (face_store.pack_vector(self.sums[row]), self.counts[row], group_id)
            )
        self.dirty.clear()
        return named, unnamed


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
    query = "SELECT id, centroid_sum, face_count FROM groups WHERE model_id = ? ORDER BY id"
    named, stale = _named_groups_as_indexed(people, index, key[1])
    unnamed = index.execute(query, (key[1],)).fetchall()
    held_ids = {int(row[0]) for row in named}
    # A group in both stores (a people store restored over a live face index) is the named one.
    rows = sorted([*named, *(row for row in unnamed if int(row[0]) not in held_ids)], key=lambda row: int(row[0]))
    centroids = _Centroids(rows, int(media_models.model_spec("face-embed")["dimensions"]), held_ids)
    centroids.dirty.update(stale)  # Written back with the next commit.
    return centroids


def _named_groups_as_indexed(
    people: sqlite3.Connection, index: sqlite3.Connection, model_id: str
) -> tuple[list[tuple[int, bytes, int]], set[int]]:
    """The people store's groups of this embedder, counted over the faces that the face index holds.

    A named group's count and centroid match its faces in the face index after every write. A people store
    restored from a backup, or a face index rebuilt under it, breaks that: a re-scan would add the same faces
    again. A group whose faces the index lost keeps its direction with no face; a group whose faces differ
    takes its centroid from those faces. Returns the rows and the ids of the groups that changed.
    """
    named = people.execute(
        "SELECT id, centroid_sum, face_count FROM groups WHERE model_id = ? ORDER BY id", (model_id,)
    ).fetchall()
    wanted = json.dumps([int(row[0]) for row in named])
    indexed = dict(index.execute(
        "SELECT group_id, COUNT(*) FROM faces WHERE group_id IN (SELECT value FROM json_each(?)) GROUP BY group_id",
        (wanted,),
    ).fetchall())
    stale = {int(group_id) for group_id, _, count in named if int(count) != indexed.get(group_id, 0)}
    if not stale:
        return named, stale
    totals: dict[int, np.ndarray] = {}
    for group_id, blob in index.execute(
        "SELECT group_id, vector FROM faces WHERE group_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(stale)),),
    ):
        vector = face_store.unpack_vector(blob)
        totals[group_id] = totals[group_id] + vector if group_id in totals else vector
    rows = [
        (group_id, face_store.pack_vector(totals[group_id]) if group_id in totals else blob, indexed.get(group_id, 0))
        if group_id in stale else (group_id, blob, count)
        for group_id, blob, count in named
    ]
    return rows, stale


def new_group_id(people: sqlite3.Connection, index: sqlite3.Connection) -> int:
    """A random id that no group, and no face, of either store holds.

    One past the largest id is not enough: a restored people store or a rebuilt face index can lower the
    largest id while faces or names still hold the ids above it.
    """
    while True:
        candidate = secrets.randbelow(_GROUP_ID_BOUND - 1) + 1
        if not (
            people.execute("SELECT 1 FROM groups WHERE id = ?", (candidate,)).fetchone()
            or index.execute("SELECT 1 FROM groups WHERE id = ?", (candidate,)).fetchone()
            or index.execute("SELECT 1 FROM faces WHERE group_id = ? LIMIT 1", (candidate,)).fetchone()
        ):
            return candidate


class FacesStopped(RuntimeError):
    """The face store was deleted, or faces were turned off, before this write took the store lock."""


@dataclass(frozen=True)
class _Held:
    """This process's open connections to one vault's face stores, and the files (device, inode) they opened."""

    people: sqlite3.Connection
    index: sqlite3.Connection
    files: tuple[tuple[int, int] | None, tuple[int, int] | None]


#: Per face index path, the connections that writes reuse: opening and closing a store costs more than the
#: write. Guarded by `_WRITE_LOCK`. An open connection keeps its file's inode, so a store that another
#: process removed and created again always has another inode, and the next write reopens it.
_OPEN: dict[str, _Held] = {}
#: The vaults whose connections stay open at once. A service serves one vault; a process that writes to
#: more, such as a test run, closes the least recently opened instead of holding a descriptor per vault.
_OPEN_LIMIT = 4


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino


def _close_held(vault_root: Path) -> None:
    _close_key(str(face_store.index_path(vault_root)))


def _close_key(key: str) -> None:
    held = _OPEN.pop(key, None)
    if held is None:
        return
    for connection in (held.people, held.index):
        try:
            connection.close()
        except sqlite3.Error:
            log.warning("faces: a face store connection could not close", exc_info=True)


def _held(vault_root: Path) -> _Held:
    """The open connections, reopened when a store file was removed or replaced. Never creates the face index."""
    index_file, people_file = face_store.index_path(vault_root), face_store.people_path(vault_root)
    files = (_file_identity(index_file), _file_identity(people_file))
    held = _OPEN.get(str(index_file))
    if held is not None and held.files == files:
        return held
    _close_held(vault_root)
    if files[0] is None:
        raise FacesStopped("the face store does not exist")
    try:
        index = face_store.connect_index(vault_root, create=False, shared=True)
    except sqlite3.OperationalError as error:
        if index_file.is_file():
            raise
        raise FacesStopped("the face store does not exist") from error
    try:
        # The people store starts with the first name, inside the directory the face index holds.
        people = face_store.connect_people(vault_root, create=files[1] is None, shared=True)
    except BaseException:
        index.close()
        raise
    held = _Held(people, index, (_file_identity(index_file), _file_identity(people_file)))
    while len(_OPEN) >= _OPEN_LIMIT:
        _close_key(next(iter(_OPEN)))
    _OPEN[str(index_file)] = held
    return held


class Writing:
    """One write to both face stores: the people store first, then the face index, each held immediately.

    Every writer takes the two locks in this order, so two writers never wait on each other. A crash
    between the two commits leaves a centroid that counts faces the face index lost; loading the groups
    counts a named group over the faces the index holds again (`_named_groups_as_indexed`).

    A write never creates the face store: turning faces on creates it, and a delete removes it under the
    same lock. With `require_on`, the write also checks inside its transaction that faces are still on, so
    face work that was in flight when the owner turned faces off stores nothing. Raises `FacesStopped`.

    With `scrub`, the write removes the owner's data from the people store: after the commit it checkpoints
    the people store and empties its write-ahead log, so no old copy of a removed name stays in that file.
    A Cloud cell's backup copies the people store's files as they are, the write-ahead log included.
    """

    def __init__(self, vault_root: Path, *, require_on: bool = True, scrub: bool = False) -> None:
        self.vault_root = vault_root
        self.require_on = require_on
        self.scrub = scrub

    def _abandon(self, *, close: bool) -> None:
        for connection in (self.people, self.index):
            if connection.in_transaction:
                connection.rollback()
        if close:
            _close_held(self.vault_root)

    def _empty_people_log(self) -> None:
        """Checkpoint the people store and truncate its write-ahead log. The removal is committed either way."""
        try:
            busy, _, _ = self.people.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            busy = True
        if busy:
            # A reader held the log past the busy timeout; the next write that scrubs empties it.
            log.warning("faces: the people store's write-ahead log could not be emptied after a removal")

    def __enter__(self) -> Writing:
        _WRITE_LOCK.acquire()
        try:
            held = _held(self.vault_root)
            self.people, self.index = held.people, held.index
            try:
                self.people.execute("BEGIN IMMEDIATE")
                self.index.execute("BEGIN IMMEDIATE")
                if self.require_on and not self.index.execute(
                    "SELECT enabled FROM face_setting WHERE id = 1"
                ).fetchone()[0]:
                    raise FacesStopped("faces are off")
            except FacesStopped:
                self._abandon(close=False)
                raise
            except BaseException:
                self._abandon(close=True)  # A connection in an unknown state is not reused.
                raise
        except BaseException:
            _WRITE_LOCK.release()
            raise
        return self

    def __exit__(self, kind: Any, error: Any, trace: Any) -> None:
        try:
            if kind is None:
                self.people.commit()
                self.index.commit()
                if self.scrub:
                    self._empty_people_log()
            else:
                self._abandon(close=False)
        except BaseException:
            self._abandon(close=True)
            raise
        finally:
            _WRITE_LOCK.release()


def erase(vault_root: Path) -> tuple[Counts | None, list[str]]:
    """Count what the face store holds, then remove both databases, their side files and the directory.

    Holds this process's write lock throughout and first takes both stores' write locks, so face work in
    flight in any process finishes before the count; work that starts later finds the store gone or faces
    off (`Writing`). The caller turned faces off first. Returns the counts, None when a damaged store
    could not be counted, and what could not be removed. Raises `sqlite3.OperationalError` when another
    writer holds a store past the busy timeout; nothing is removed then.
    """
    with _WRITE_LOCK:
        _release(vault_root)
        held = []
        try:
            # Both write locks, in the order every writer takes them, then let go: nothing writes after this.
            for path in (face_store.people_path(vault_root), face_store.index_path(vault_root)):
                if path.is_file():
                    connection = sqlite3.connect(path, timeout=face_store.BUSY_TIMEOUT_MS / 1000)
                    held.append(connection)
                    connection.execute("BEGIN IMMEDIATE")
            found: Counts | None = counts(vault_root)
        except sqlite3.DatabaseError as error:
            if face_store.is_busy(error):
                raise
            # A damaged store is still erased; its counts are unknown, never zero.
            log.warning("faces: the face store could not be counted before its delete", exc_info=True)
            found = None
        finally:
            for connection in reversed(held):
                connection.rollback()
                connection.close()
        return found, face_store.remove_store(vault_root)


def release(vault_root: Path) -> None:
    """Close this process's face store connections and drop its centroids, which are face data too.

    Off and delete call it, so a process keeps no face data in memory and no store file open after them.
    """
    with _WRITE_LOCK:
        _release(vault_root)


def _release(vault_root: Path) -> None:
    people_key = str(face_store.people_path(vault_root))
    for key in [key for key in _CACHE if key[0] == people_key]:
        del _CACHE[key]
    _close_held(vault_root)


def _exclusions(writing: Writing, page: str) -> list[tuple[int, float | None, tuple[float, ...]]]:
    """The faces moved out of a group on one page, from the groups of both stores."""
    query = "SELECT group_id, frame_ts, x, y, width, height FROM exclusions WHERE page = ?"
    return [
        (group_id, frame_ts, (x, y, width, height))
        for store in (writing.people, writing.index)
        for group_id, frame_ts, x, y, width, height in store.execute(query, (page,))
    ]


def record_page(
    vault_root: Path, page: str, faces: Sequence[PageFace], *, model_id: str, mtime: float | None = None
) -> list[int]:
    """Replace a page's faces with `faces` and group each one that the settings keep.

    `page` is the picture's vault-relative path, as the CLIP index keys it, and `mtime` the modification
    time of the bytes scanned. The page's earlier faces leave their groups' centroids first, so a re-scan
    counts each face once. The page is recorded as scanned even when it holds no face. Returns the group
    id of each stored face, in storage order. Raises `FacesStopped` when faces were turned off or deleted.
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
        excluded = _exclusions(writing, page)
        rows, groups = [], []
        for face in kept:
            barred = {
                group_id for group_id, frame_ts, region in excluded
                if same_face(face.frame_ts, face.region, frame_ts, region)
            }
            group_id = centroids.nearest(face.vector, limits, barred)
            if group_id is None:
                # A new group is unnamed, so it lives in the face index until the owner names it.
                group_id = new_group_id(people, index)
                index.execute(
                    "INSERT INTO groups (id, model_id, centroid_sum, face_count) VALUES (?, ?, ?, 1)",
                    (group_id, model_id, face_store.pack_vector(face.vector)),
                )
                centroids.add_group(group_id, face.vector)
            else:
                centroids.join(group_id, face.vector)
            groups.append(group_id)
            rows.append((
                page, face.frame_ts, *face.region, face.score, model_id, face_store.pack_vector(face.vector), group_id,
            ))
        named, unnamed = centroids.changed_rows()
        people.executemany("UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?", named)
        index.executemany("UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?", unnamed)
        index.executemany(
            "INSERT INTO faces (page, frame_ts, x, y, width, height, score, model_id, vector, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        index.execute(
            "INSERT OR REPLACE INTO scanned_pages (page, model_id, face_count, mtime) VALUES (?, ?, ?, ?)",
            (page, model_id, len(rows), mtime),
        )
        generation = face_store.advance_generation(index)
        # Held only once both commits succeed; `Writing` commits on leaving this block.
    _CACHE[key] = (generation, centroids)
    return groups


def _drop_faces(writing: Writing, pages: Sequence[str]) -> int:
    """Remove these pages' faces from the face index and their groups' centroids. Returns the faces removed.

    A group the removal empties keeps its direction, as on a re-scan, so its name stays attached.
    """
    index, wanted = writing.index, json.dumps(sorted(set(pages)))
    rows = index.execute(
        "SELECT group_id, vector FROM faces WHERE page IN (SELECT value FROM json_each(?)) AND group_id IS NOT NULL",
        (wanted,),
    ).fetchall()
    leaving: dict[int, tuple[np.ndarray, int]] = {}
    for group_id, blob in rows:
        total, count = leaving.get(group_id, (0.0, 0))
        leaving[group_id] = (total + face_store.unpack_vector(blob), count + 1)
    group_ids = json.dumps(sorted(leaving))
    for store in (writing.people, index):
        changed = []
        for group_id, blob, count in store.execute(
            "SELECT id, centroid_sum, face_count FROM groups WHERE id IN (SELECT value FROM json_each(?))", (group_ids,)
        ).fetchall():
            total, gone = leaving[group_id]
            left = max(0, int(count) - gone)
            changed.append((face_store.pack_vector(face_store.unpack_vector(blob) - total) if left else blob, left, group_id))
        store.executemany("UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?", changed)
    removed = index.execute("DELETE FROM faces WHERE page IN (SELECT value FROM json_each(?))", (wanted,)).rowcount
    index.execute("DELETE FROM scanned_pages WHERE page IN (SELECT value FROM json_each(?))", (wanted,))
    return int(removed)


def forget_pages(vault_root: Path, pages: Sequence[str]) -> int:
    """Drop the faces of removed pictures, in the derived cleanup that drops their CLIP rows.

    Runs whether faces are on or off, and never creates the face store. `pages` are the removed vault paths
    the CLIP step receives, which name a removed picture by its own path. The pictures' exclusions go too,
    because they name a removed file. Returns the faces removed.
    """
    path = face_store.index_path(vault_root)
    # Only a picture or video holds faces. Any other removed file never opens the face store, so an unreadable index
    # holds back the cleanup of removed pictures alone, never every removal or a text Source's erasure.
    keys = sorted({page for page in pages if page and media_types.media_type_for(page) in media_types.VISUAL_KINDS})
    if not keys or not path.is_file():
        return 0
    wanted = json.dumps(keys)
    # A read first: most removals hold no picture with faces, and they should not wait for the write lock.
    with closing(face_store.connect_read_only(path)) as index:
        held = index.execute(
            "SELECT EXISTS (SELECT 1 FROM faces WHERE page IN (SELECT value FROM json_each(?1))) "
            "OR EXISTS (SELECT 1 FROM scanned_pages WHERE page IN (SELECT value FROM json_each(?1))) "
            "OR EXISTS (SELECT 1 FROM exclusions WHERE page IN (SELECT value FROM json_each(?1)))",
            (wanted,),
        ).fetchone()[0]
    people_file = face_store.people_path(vault_root)
    if not held and people_file.is_file():
        with closing(face_store.connect_read_only(people_file)) as people:
            held = people.execute(
                "SELECT EXISTS (SELECT 1 FROM exclusions WHERE page IN (SELECT value FROM json_each(?)))", (wanted,)
            ).fetchone()[0]
    if not held:
        return 0
    try:
        with Writing(vault_root, require_on=False, scrub=True) as writing:
            removed = _drop_faces(writing, keys)
            for store in (writing.people, writing.index):
                store.execute("DELETE FROM exclusions WHERE page IN (SELECT value FROM json_each(?))", (wanted,))
            face_store.advance_generation(writing.index)
    except FacesStopped:
        return 0  # Deleted meanwhile: nothing is left to drop.
    return removed


def forget_changed(vault_root: Path, page: str, mtime: float) -> bool:
    """Drop a picture's faces when they were found in other bytes than the current `mtime`'s.

    The CLIP step calls it when it stores new CLIP rows for a picture without storing faces: faces are off,
    or the face step failed. Faces of unchanged bytes stay. Returns whether faces were dropped.
    """
    path = face_store.index_path(vault_root)
    if not path.is_file():
        return False
    with closing(face_store.connect_read_only(path)) as index:
        row = index.execute("SELECT mtime FROM scanned_pages WHERE page = ?", (page,)).fetchone()
    if row is None or row[0] == mtime:
        return False
    try:
        with Writing(vault_root, require_on=False) as writing:
            _drop_faces(writing, [page])
            face_store.advance_generation(writing.index)
    except FacesStopped:
        return False
    return True


def scanned(vault_root: Path, pages: Sequence[str], *, model_id: str) -> set[str]:
    """Which of `pages` the face index records as scanned with this embedder. One query for the whole list."""
    path = face_store.index_path(vault_root)
    if not pages or not path.is_file():
        return set()
    with closing(face_store.connect_read_only(path)) as index:
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
    path = face_store.index_path(vault_root)
    if not path.is_file():
        return 0
    with closing(face_store.connect_read_only(path)) as index:
        return int(index.execute("SELECT COUNT(*) FROM scanned_pages WHERE model_id = ?", (model_id,)).fetchone()[0])


# --- Reads for search and status ----------------------------------------------------------------------
# Every read of names, groups and faces together lives here, so a change to where groups live stays local.


def person_pages(vault_root: Path, folded_names: Sequence[str]) -> dict[str, set[str]]:
    """The pages with a face of each named person, keyed by folded name; a name no person has is absent.

    Two queries whatever the store's size: one indexed name lookup in the people store and one indexed
    page lookup in the face index. A keyframe face's page is its video's page. Opens both read-only.
    """
    people_file, index_file = face_store.people_path(vault_root), face_store.index_path(vault_root)
    if not folded_names or not people_file.is_file() or not index_file.is_file():
        return {}
    with closing(face_store.connect_read_only(people_file)) as people:
        owners = people.execute(
            "SELECT l.group_id, p.folded_name FROM people p JOIN group_people l ON l.person_id = p.id "
            "WHERE p.folded_name IN (SELECT value FROM json_each(?))",
            (json.dumps(list(folded_names)),),
        ).fetchall()
    found: dict[str, set[str]] = {folded: set() for _, folded in owners}
    if not owners:
        return found
    person_of = dict(owners)
    with closing(face_store.connect_read_only(index_file)) as index:
        rows = index.execute(
            "SELECT DISTINCT group_id, page FROM faces WHERE group_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(person_of)),),
        ).fetchall()
    for group_id, page in rows:
        found[person_of[group_id]].add(page)
    return found


def named_people(vault_root: Path, *, limit: int) -> list[str]:
    """Up to `limit` person names, in name order, for a refusal that lists who can be searched."""
    path = face_store.people_path(vault_root)
    if not path.is_file():
        return []
    with closing(face_store.connect_read_only(path)) as people:
        return [name for (name,) in people.execute(
            "SELECT display_name FROM people ORDER BY folded_name LIMIT ?", (limit,)
        )]


@dataclass(frozen=True)
class Sample:
    """One face, as status shows it: `page` and `region` are what `read_media` takes to show it."""

    face: int
    page: str
    frame_ts: float | None
    region: tuple[float, float, float, float]


@dataclass(frozen=True)
class Group:
    group: int
    face_count: int
    person: str | None
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class Counts:
    faces: int
    #: Groups that hold a face, named or not.
    groups: int
    people: int
    #: Pictures and videos with at least one face.
    pages: int


def counts(vault_root: Path) -> Counts:
    """How many faces, non-empty groups, people and pictures with faces the stores hold.

    Zero for a store that does not exist. Four statements whatever the store's size.
    """
    people_file, index_file = face_store.people_path(vault_root), face_store.index_path(vault_root)
    faces = unnamed = named = people_count = pages = 0
    if index_file.is_file():
        with closing(face_store.connect_read_only(index_file)) as index:
            faces, pages, unnamed = index.execute(
                "SELECT (SELECT COUNT(*) FROM faces), (SELECT COUNT(DISTINCT page) FROM faces), "
                "(SELECT COUNT(*) FROM groups WHERE face_count > 0)"
            ).fetchone()
    if people_file.is_file():
        with closing(face_store.connect_read_only(people_file)) as people:
            named, people_count = people.execute(
                "SELECT (SELECT COUNT(*) FROM groups WHERE face_count > 0), (SELECT COUNT(*) FROM people)"
            ).fetchone()
    return Counts(int(faces), int(named) + int(unnamed), int(people_count), int(pages))


def _samples(index: sqlite3.Connection, group_ids: Sequence[int], per_group: int) -> dict[int, list[Sample]]:
    """Up to `per_group` faces of each group, in page order, in one statement over the group index."""
    found: dict[int, list[Sample]] = {group_id: [] for group_id in group_ids}
    rows = index.execute(
        "SELECT g.value, f.id, f.page, f.frame_ts, f.x, f.y, f.width, f.height FROM json_each(?) g "
        "JOIN faces f ON f.id IN (SELECT id FROM faces WHERE group_id = g.value ORDER BY page LIMIT ?) "
        "ORDER BY g.key, f.page, f.id",
        (json.dumps(list(group_ids)), per_group),
    )
    for group_id, face_id, page, frame_ts, x, y, width, height in rows:
        found[int(group_id)].append(Sample(int(face_id), page, frame_ts, (x, y, width, height)))
    return found


def groups_by_size(
    vault_root: Path, *, limit: int, after: tuple[int, int] | None, per_group: int
) -> list[Group]:
    """One page of non-empty groups, named and unnamed, largest first, each with its person and samples.

    `after` is the last listed group's (face_count, group id). Three statements whatever the store's size:
    one page from each store, merged, and the samples of the groups kept.
    """
    people_file, index_file = face_store.people_path(vault_root), face_store.index_path(vault_root)
    if not index_file.is_file():
        return []
    count, last = after if after is not None else (None, None)
    window = "WHERE g.face_count > 0 AND (? IS NULL OR g.face_count < ? OR (g.face_count = ? AND g.id > ?)) "
    bounds = (count, count, count, last)
    rows: list[tuple[int, int, str | None]] = []
    if people_file.is_file():
        with closing(face_store.connect_read_only(people_file)) as people:
            rows += people.execute(
                "SELECT g.id, g.face_count, p.display_name FROM groups g "
                "JOIN group_people l ON l.group_id = g.id JOIN people p ON p.id = l.person_id "
                + window + "ORDER BY g.face_count DESC, g.id LIMIT ?",
                (*bounds, limit),
            ).fetchall()
    with closing(face_store.connect_read_only(index_file)) as index:
        rows += index.execute(
            "SELECT g.id, g.face_count, NULL FROM groups g " + window + "ORDER BY g.face_count DESC, g.id LIMIT ?",
            (*bounds, limit),
        ).fetchall()
        rows = sorted(rows, key=lambda row: (-int(row[1]), int(row[0])))[:limit]
        if not rows:
            return []
        samples = _samples(index, [int(row[0]) for row in rows], per_group)
    return [Group(int(group_id), int(size), person, tuple(samples[int(group_id)])) for group_id, size, person in rows]


def page_faces(vault_root: Path, page: str) -> list[tuple[Sample, int | None, str | None]]:
    """The faces on one picture with each face's group and person, in frame and position order."""
    people_file, index_file = face_store.people_path(vault_root), face_store.index_path(vault_root)
    if not index_file.is_file():
        return []
    with closing(face_store.connect_read_only(index_file)) as index:
        rows = index.execute(
            "SELECT id, frame_ts, x, y, width, height, group_id FROM faces WHERE page = ? "
            "ORDER BY frame_ts, y, x, id",
            (page,),
        ).fetchall()
    persons: dict[int, str] = {}
    group_ids = sorted({int(row[6]) for row in rows if row[6] is not None})
    if group_ids and people_file.is_file():
        with closing(face_store.connect_read_only(people_file)) as people:
            persons = dict(people.execute(
                "SELECT l.group_id, p.display_name FROM group_people l JOIN people p ON p.id = l.person_id "
                "WHERE l.group_id IN (SELECT value FROM json_each(?))",
                (json.dumps(group_ids),),
            ).fetchall())
    return [
        (Sample(int(face_id), page, frame_ts, (x, y, width, height)), group_id, persons.get(group_id))
        for face_id, frame_ts, x, y, width, height, group_id in rows
    ]


def overrides(vault_root: Path) -> dict[str, float]:
    """The owner's threshold overrides; empty when the people store does not exist."""
    path = face_store.people_path(vault_root)
    if not path.is_file():
        return {}
    with closing(face_store.connect_read_only(path)) as people:
        return {name: float(value) for name, value in people.execute("SELECT name, value FROM threshold_overrides")}


def set_overrides(vault_root: Path, changes: dict[str, float | None]) -> None:
    """Write threshold overrides; None removes one. Faces grouped afterwards use them; groups stay."""
    with Writing(vault_root) as writing:
        for name, value in changes.items():
            if value is None:
                writing.people.execute("DELETE FROM threshold_overrides WHERE name = ?", (name,))
            else:
                writing.people.execute(
                    "INSERT INTO threshold_overrides (name, value) VALUES (?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                    (name, value),
                )
