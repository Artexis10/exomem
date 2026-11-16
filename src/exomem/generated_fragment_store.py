"""Canonical generated-image custody and its disposable indexed lookup.

Only producer hooks call this owner under the actual canonical writer boundary.
The change window records opaque object identities, never Source references or bodies.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from . import held_fs, reserved_paths, source_custody, state_paths, vault
from .generated_fragments import Fragment, FragmentMap, FragmentPlan, SourceRef
from .source_capture import validate_sha256
from .source_custody import OwnerLocator, ProducerBirth

OWNER = "generated_fragment_store"
log = logging.getLogger(__name__)
DESCRIPTOR_ID = "generated-fragments"
INDEX_DESCRIPTOR_ID = "generated-fragments-index"
# These protocol bounds limit metadata I/O, journal retention and each caller-driven step.
MAX_RECORD_BYTES = 64 * 1024
CHANGE_WINDOW = 4096
MAX_STEP = 128
# Each commit removes at most this many retired change records, so the window keeps up at no extra head write.
TRIM_STEP = 2


def _opaque(value: str) -> str:
    if uuid.UUID(value).hex != value:
        raise ValueError("generated_identity_invalid")
    return value


_Id = Annotated[str, AfterValidator(_opaque)]
_Hash = Annotated[str, AfterValidator(validate_sha256)]
_Epoch = Annotated[int, Field(ge=0)]
_SourceReference = Annotated[str, AfterValidator(lambda value: SourceRef(value).value)]


def _id() -> str:
    return uuid.uuid4().hex


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1  # The canonical generated-custody protocol fixes this version.


class Pending(_Record):
    unit_id: _Id
    change_epoch: Annotated[int, Field(gt=0)]
    change_record_hash: _Hash


class Head(_Record):
    catalogue_id: _Id
    epoch: _Epoch = 0
    change_floor: Annotated[int, Field(ge=1)] = 1
    collected_through: _Epoch = 0
    unknown_through_epoch: _Epoch = 0
    summarized_through_epoch: _Epoch | None = None
    pending: Pending | None = None


class ObjectChange(_Record):
    # Kinds select the closed canonical layout, not a content vocabulary.
    kind: Literal["image", "page", "copy", "uncertainty", "slot"]
    object_id: _Id
    image_id: _Id | None = None
    page_number: Annotated[int, Field(ge=0)] | None = None
    expected_revision: _Id | None = None
    after_revision: _Id | None = None
    remove: bool = False


class Change(_Record):
    catalogue_id: _Id
    epoch: Annotated[int, Field(gt=0)]
    unit_id: _Id
    # Operations describe this owner's fixed metadata protocol.
    operation: Literal["image_begin", "image_page", "image_seal", "copy_reserve", "copy_ack",
                       "uncertainty", "birth", "collect", "no_effect", "slot_prepare", "slot_takeover", "slot_settle",
                       "slot_remove", "unit"]
    # A unit change carries one durability phase of staged objects, bounded by the caller step.
    objects: Annotated[tuple[ObjectChange, ...], Field(max_length=MAX_STEP)] = ()


class ImageHeader(_Record):
    image_id: _Id
    image_sha256: _Hash
    image_size: Annotated[int, Field(ge=0)]
    catalogue_id: _Id
    unknown_through_epoch: _Epoch
    whole_source_ref: _SourceReference | None = None
    # Explicit opaque membership shares the canonical record byte bound; no payload parser establishes it.
    opaque_source_refs: tuple[_SourceReference, ...] | None = None
    # These states distinguish incomplete construction from immutable usable pages and collection.
    state: Literal["building", "ready", "collecting"] = "building"
    page_count: Annotated[int, Field(ge=0)]
    written_pages: Annotated[int, Field(ge=0)] = 0
    last_page_hash: _Hash | None = None
    expected_last_page_hash: _Hash | None = None
    revision: _Id

    @model_validator(mode="after")
    def consistent_pages(self):
        if self.opaque_source_refs is not None and (self.whole_source_ref is not None
                or self.page_count != 0 or self.written_pages != 0
                or self.last_page_hash is not None or self.expected_last_page_hash is not None):
            raise ValueError("opaque_source_image_cannot_have_other_membership")
        if self.whole_source_ref is not None and (self.page_count != 0 or self.written_pages != 0
                or self.last_page_hash is not None or self.expected_last_page_hash is not None):
            raise ValueError("whole_source_image_cannot_have_fragment_pages")
        if (self.written_pages > self.page_count
                or (self.page_count == 0) != (self.expected_last_page_hash is None)
                or (self.written_pages == 0) != (self.last_page_hash is None)):
            raise ValueError("generated_image_page_counts_invalid")
        if self.state == "ready" and (self.written_pages != self.page_count
                                     or self.last_page_hash != self.expected_last_page_hash):
            raise ValueError("generated_ready_image_incomplete")
        return self


class ImagePage(_Record):
    image_id: _Id
    page_number: Annotated[int, Field(ge=0)]
    previous_page_hash: _Hash | None = None
    fragments: tuple[Fragment, ...]
    revision: _Id

    def digest(self) -> str:
        return hashlib.sha256(encode(self)).hexdigest()


class Copy(_Record):
    copy_id: _Id
    image_id: _Id
    owner: OwnerLocator
    origin_ledger: Annotated[str, Field(min_length=1, max_length=1024)]
    # A closed native witness permits collection; a published copy still remains an obligation.
    outcome: Literal["reserved", "present", "no_effect", "retired"] = "reserved"
    revision: _Id

    @property
    def closed(self) -> bool:
        return self.outcome in ("no_effect", "retired")


class Uncertainty(_Record):
    unit_id: _Id
    catalogue_id: _Id
    unknown_through_epoch: _Epoch
    image_id: _Id | None = None
    object_keys: Annotated[tuple[str, ...], Field(max_length=MAX_STEP)] = ()
    revision: _Id


class SlotPending(_Record):
    publication_id: _Id
    candidate_copy_id: _Id


class Slot(_Record):
    slot_id: _Id
    catalogue_id: _Id
    current_copy_id: _Id | None = None
    pending: SlotPending | None = None
    revision: _Id


# ObjectChange fixes these metadata kind tokens and the schema each token selects.
_OBJECT_MODELS = {"image": ImageHeader, "page": ImagePage, "copy": Copy, "uncertainty": Uncertainty, "slot": Slot}

_SLOT_SEAL = object()


@dataclass
class _Unit:
    """One root's staged objects in first-touch order, waiting for their durability phase."""

    root: Path
    staged: dict[Path, tuple[ObjectChange, BaseModel]] = field(default_factory=dict)
    before_bytes: bool = False


_UNITS: ContextVar[tuple[_Unit, ...]] = ContextVar("generated_catalogue_units", default=())


def _active_unit(root: Path) -> _Unit | None:
    return next((item for item in _UNITS.get() if item.root == root), None)


@contextmanager
def unit(root: Path) -> Iterator[None]:
    """Commit this context's catalogue operations for one root as phase changes, not one change each.

    Operations that native bytes depend on become durable at the next ``barrier``; acknowledgements,
    and anything else still staged, at exit. A nested unit shares the outer overlay and flushes at its exit.
    """
    root = Path(root).absolute()
    current = _active_unit(root)
    token = None
    if current is None:
        current = _Unit(root)
        token = _UNITS.set((*_UNITS.get(), current))
    try:
        yield
    finally:
        try:
            GeneratedFragmentStore(root)._flush(current)
        except Exception as error:  # noqa: BLE001 - like a failed acknowledgement, its obligations stay outstanding
            log.warning("generated catalogue unit left staged changes outstanding (%s)", type(error).__name__)
        finally:
            if token is not None:
                _UNITS.reset(token)


@dataclass(frozen=True)
class SlotPublication:
    """An internal publication right fenced by both slot identity and opaque revision."""

    root: Path
    owner: str
    locator: str
    physical_scope: str
    slot_id: str
    revision: str
    publication_id: str
    before_copy_id: str | None
    candidate_copy_id: str
    _seal: object


@dataclass(frozen=True)
class SlotBinding:
    state: Literal["observed", "missing", "pending", "unknown"]
    slot: Slot | None = None
    current: Copy | None = None
    candidate: Copy | None = None
    images: tuple[ImageHeader, ...] = ()
    publication: SlotPublication | None = None
    unknown_through_epoch: int | None = None


@dataclass(frozen=True)
class NativeSlotWitness:
    """Only a native-owner callback supplies retained installation/version evidence.

    Matching bytes or hashes cannot distinguish equal-byte images with different maps.
    """

    slot_id: str
    revision: str
    physical_scope: str
    publication_id: str | None
    # These choices form the native publication protocol, not a content vocabulary.
    selection: Literal["before", "candidate", "absence", "mismatch", "foreign"]


@dataclass(frozen=True)
class ImageBuild:
    header: ImageHeader
    pages: tuple[ImagePage, ...]


@dataclass(frozen=True)
class Observation:
    state: Literal["observed", "missing", "pending", "unknown"]
    value: ImageHeader | ImagePage | Copy | None = None
    unknown_through_epoch: int | None = None


@dataclass(frozen=True)
class Progress:
    state: Literal["ready", "pending", "unknown", "missing"]
    epoch: int
    processed: int = 0


@dataclass(frozen=True)
class Lookup:
    """One evidence page; consumers retain earlier unknowns and apply native owner revisions."""
    state: Literal["ready", "pending", "unknown"]
    catalogue_id: str | None
    epoch: int | None
    copies: tuple[Copy, ...] = ()
    image_ids: tuple[str, ...] = ()
    uncertain_images: tuple[str, ...] = ()
    cursor: tuple[str, str] | None = None
    uncertainty_cursor: tuple[int, str] | None = None


@dataclass(frozen=True)
class CopyPage:
    """Catalogue-qualified bindings; the caller checks native versions and image evidence."""

    state: Literal["ready", "pending", "unknown"]
    catalogue_id: str | None
    epoch: int | None
    copies: tuple[Copy, ...] = ()
    cursor: str | None = None


def canonical_root(root: Path) -> Path:
    return source_custody.manifest_root(root).parent / "generated"


def file_locator(catalogue_id: str, relative: str) -> str:
    """One opaque logical-file locator, shared by its retained native copy versions."""
    normalized = "/".join(vault._safe_guard_target(relative))
    if normalized != relative:
        raise ValueError("generated_file_locator_path_invalid")
    return uuid.uuid5(uuid.UUID(hex=_opaque(catalogue_id)), relative).hex


def cache_path(root: Path) -> Path:
    return state_paths.vault_state_dir(root) / ".generated-fragments.sqlite"


def encode(value: BaseModel) -> bytes:
    content = (json.dumps(value.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError("generated_metadata_bound_exceeded")
    return content


def _limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_STEP:
        raise ValueError("generated_step_bound_invalid")
    return limit


def _boundary(root: Path) -> None:
    from .writer_lease import active_manager

    boundary = active_manager()._mutation_coordinator_for(root)
    if not boundary.current_thread_holds_boundary() or boundary.current_thread_borrows_boundary():
        raise RuntimeError("generated custody requires the actual writer boundary")


def pack_pages(image_id: str, fragments: Iterable[Fragment]) -> Iterator[ImagePage]:
    """Pack unchanged fragment fields by exact serialized UTF-8 envelope size."""
    _opaque(image_id)
    items, previous, number, revision = [], None, 0, _id()
    size = len(encode(ImagePage(image_id=image_id, page_number=number, previous_page_hash=previous,
                                fragments=(), revision=revision)))
    for fragment in fragments:
        fragment_bytes = len(encode(fragment)) - 1
        addition = fragment_bytes + bool(items)
        if size + addition > MAX_RECORD_BYTES:
            if not items:
                raise ValueError("generated_metadata_bound_exceeded")
            page = ImagePage(image_id=image_id, page_number=number,
                             previous_page_hash=previous, fragments=tuple(items), revision=revision)
            yield page
            previous, revision, items, number = page.digest(), _id(), [], number + 1
            size = len(encode(ImagePage(image_id=image_id, page_number=number,
                                       previous_page_hash=previous, fragments=(), revision=revision)))
            addition = fragment_bytes
            if size + addition > MAX_RECORD_BYTES:
                raise ValueError("generated_metadata_bound_exceeded")
        items.append(fragment)
        size += addition
    if items:
        page = ImagePage(image_id=image_id, page_number=number, previous_page_hash=previous,
                         fragments=tuple(items), revision=revision)
        encode(page)
        yield page


class GeneratedFragmentStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.path = canonical_root(self.root)
        self._census: Iterator[tuple[str, str, int | None]] | None = None
        self._census_epoch: int | None = None
        self._census_token: str | None = None

    def close(self) -> None:
        if self._census is not None:
            self._census.close()
            self._census = None
        self._census_epoch = None
        self._census_token = None

    def _object_path(self, item: ObjectChange) -> Path:
        if item.kind == "image":
            if item.image_id != item.object_id or item.page_number is not None:
                raise ValueError("generated_image_key_invalid")
            return self.path / "images" / item.object_id[:2] / item.object_id / "head.json"
        if item.kind == "page":
            if item.image_id != item.object_id or item.page_number is None:
                raise ValueError("generated_page_key_invalid")
            return self.path / "images" / item.object_id[:2] / item.object_id / "pages" / f"{item.page_number}.json"
        if item.page_number is not None:
            raise ValueError("generated_object_key_invalid")
        if item.kind == "copy":
            if item.image_id is None:
                raise ValueError("generated_copy_key_invalid")
            return self._copy_path(item.object_id)
        if item.kind == "slot":
            if item.image_id is not None:
                raise ValueError("generated_slot_key_invalid")
            return self.path / "slots" / item.object_id[:2] / f"{item.object_id}.json"
        return self.path / "uncertain" / f"{item.object_id}.json"

    def _copy_path(self, copy_id: str) -> Path:
        _opaque(copy_id)
        return self.path / "copies" / copy_id[:2] / f"{copy_id}.json"

    def _read(self, path: Path, model):
        """Read one object as this context's unit sees it; the guard always witnesses the durable file."""
        value, guard = self._read_disk(path, model)
        staged = _active_unit(self.root)
        if staged is not None and path in staged.staged:
            return staged.staged[path][1], guard
        return value, guard

    def _read_disk(self, path: Path, model):
        with reserved_paths._subsystem_authority_scope(OWNER):
            relative = path.relative_to(self.root).as_posix()
            try:
                guard = vault.PathGuard.capture(self.root, relative, leaf_policy="absent")
            except vault.PathGuardError:
                raw, guard = vault.read_bounded_guarded_bytes(self.root, relative, limit=MAX_RECORD_BYTES)
                return model.model_validate_json(raw), guard
            return None, guard

    def head(self) -> Head:
        value, _ = self._read(self.path / "head.json", Head)
        if value is None:
            raise FileNotFoundError("generated catalogue is missing")
        if value.change_floor > value.epoch + 1 or value.collected_through >= value.change_floor:
            raise ValueError("generated_head_invalid")
        if value.pending is not None and value.pending.change_epoch != value.epoch:
            raise ValueError("generated_pending_epoch_invalid")
        if (value.unknown_through_epoch > value.epoch
                or (value.summarized_through_epoch is not None and value.summarized_through_epoch > value.epoch)):
            raise ValueError("generated_summary_epoch_invalid")
        return value

    @staticmethod
    def _global_horizon(head: Head) -> int:
        # Missing legacy coverage is unknown through the prior catalogue, never an empty summary.
        uncovered = head.summarized_through_epoch is None or head.summarized_through_epoch < head.epoch
        return max(head.unknown_through_epoch, head.epoch if uncovered else 0)

    def _write(self, path: Path, value: BaseModel, guard: vault.PathGuard | None = None):
        if guard is None:
            _, guard = self._read(path, type(value))
        return vault.PlannedWrite(path, encode(value).decode("utf-8"),
                                  create_only=guard.expects_absence, guard=guard)

    def _install(self, writes: Sequence[vault.PlannedWrite]) -> None:
        """Validate every exact owner target before entering service authority."""
        _boundary(self.root)
        for write in writes:
            path, guard = Path(write.path), write.guard
            if (not isinstance(guard, vault.PathGuard) or write.ensure_directories
                    or guard.target != path.relative_to(self.root).as_posix()
                    or reserved_paths.state_target_descriptor_id(self.root, path) != DESCRIPTOR_ID
                    or not isinstance(write.content, str)
                    or len(write.content.encode("utf-8")) > MAX_RECORD_BYTES):
                raise ValueError("generated_guarded_target_invalid")
            self._validate_payload(path, write.content)
        from . import curation_witness
        from .governance.principal import owner_local_producer

        with reserved_paths._subsystem_authority_scope(OWNER):
            with curation_witness.suspended(), owner_local_producer(self.root, OWNER):
                vault.batch_atomic_write(writes, vault_root=self.root,
                                         commit_point=False, post_commit_fanout=False)

    def _validate_payload(self, path: Path, content: str) -> None:
        relative = path.relative_to(self.path).parts
        if relative == ("head.json",):
            Head.model_validate_json(content)
            return
        if len(relative) == 2 and relative[0] == "changes":
            value = Change.model_validate_json(content)
            if relative[1] != f"{value.epoch}.json":
                raise ValueError("generated_change_path_invalid")
            for item in value.objects:
                self._object_path(item)
            return
        # The model supplies opaque identities; path equality rejects extra levels and aliases.
        if relative[0] == "images":
            if relative[-1] == "head.json":
                value = ImageHeader.model_validate_json(content)
                item = ObjectChange(kind="image", object_id=value.image_id, image_id=value.image_id)
            else:
                value = ImagePage.model_validate_json(content)
                item = ObjectChange(kind="page", object_id=value.image_id, image_id=value.image_id,
                                    page_number=value.page_number)
        elif relative[0] == "copies":
            value = Copy.model_validate_json(content)
            item = ObjectChange(kind="copy", object_id=value.copy_id, image_id=value.image_id)
        elif relative[0] == "uncertain":
            value = Uncertainty.model_validate_json(content)
            item = ObjectChange(kind="uncertainty", object_id=value.unit_id)
            for key in value.object_keys:
                self._validate_object_key(key)
        elif relative[0] == "slots":
            value = Slot.model_validate_json(content)
            item = ObjectChange(kind="slot", object_id=value.slot_id)
        else:
            raise ValueError("generated_record_path_invalid")
        if path != self._object_path(item):
            raise ValueError("generated_record_path_invalid")

    def _validate_object_key(self, key: str) -> None:
        # Keys are this protocol's canonical paths, never native owner locations.
        path = self.path / key
        parts = Path(key).parts
        if ".." in parts or path.relative_to(self.path).as_posix() != key:
            raise ValueError("generated_object_key_invalid")
        if not parts or parts[0] not in ("images", "copies", "uncertain", "slots"):
            raise ValueError("generated_object_key_invalid")
        if parts[0] == "images" and len(parts) in (4, 5):
            image_id = _opaque(parts[2])
            if parts[1] != image_id[:2]:
                raise ValueError("generated_object_key_invalid")
            if len(parts) == 4 and parts[3] == "head.json":
                return
            if len(parts) == 5 and parts[3] == "pages" and parts[4] == f"{int(parts[4][:-5])}.json" and int(parts[4][:-5]) >= 0:
                return
        elif parts[0] in ("copies", "slots") and len(parts) == 3:
            identity = _opaque(parts[2][:-5])
            if parts[1] == identity[:2] and parts[2] == f"{identity}.json":
                return
        elif parts[0] == "uncertain" and len(parts) == 2:
            identity = _opaque(parts[1][:-5])
            if parts[1] == f"{identity}.json":
                return
        raise ValueError("generated_object_key_invalid")

    def _remove(self, path: Path, guard: vault.PathGuard) -> None:
        _boundary(self.root)
        self._validate_delete_path(path)
        from . import curation_witness
        from .governance.principal import owner_local_producer
        from .vault_store import availability

        if availability.page_writer(self.root) is not None:
            raise RuntimeError("generated collection requires the store owner's physical revision integration")
        with reserved_paths._subsystem_authority_scope(OWNER):
            with curation_witness.suspended(), owner_local_producer(self.root, OWNER):
                guard.recheck(self.root)
                reserved_paths._remove_owner_file(self.root, path, DESCRIPTOR_ID, missing_ok=True)
        relative = path.relative_to(self.path).parts
        if len(relative) == 4 and relative[0] == "images" and relative[-1] == "head.json":
            self._remove_empty_image_dirs(relative[2])

    def _remove_empty_image_dirs(self, image_id: str) -> bool:
        """Finish the directory part of a declared image collection unit."""
        _boundary(self.root)
        _opaque(image_id)
        acquired = held_fs.acquire(self.root)
        if not acquired.ok:
            return False
        with acquired.require() as filesystem:
            base = self.path / "images" / image_id[:2] / image_id
            for path in (base / "pages", base):
                found = filesystem.parent(path.relative_to(self.root).as_posix(), access="mutate")
                if not found.ok:
                    if found.error is not None and found.error.code == "MISSING":
                        continue
                    return False
                with found.require() as parent:
                    if not filesystem.unlink_directory(parent).ok:
                        return False
        return True

    def _validate_delete_path(self, path: Path) -> None:
        relative = path.relative_to(self.path)
        if relative.parts[0] == "changes" and len(relative.parts) == 2:
            name = relative.name
            if name != f"{int(name[:-5])}.json" or int(name[:-5]) < 1:
                raise ValueError("generated_change_key_invalid")
        else:
            self._validate_object_key(relative.as_posix())

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Retain every SQLite leaf under the maintained private-owner boundary."""
        path = cache_path(self.root)
        conn = None
        with reserved_paths._subsystem_authority_scope(OWNER):
            with reserved_paths._identity_coordination_scope(
                self.root, descriptor_ids=(INDEX_DESCRIPTOR_ID,), identity_may_change=True,
            ):
                try:
                    with ExitStack() as held:
                        retained = held.enter_context(reserved_paths._sqlite_owner_target_scope(
                            self.root, path, INDEX_DESCRIPTOR_ID, create=True,
                        ))
                        # SQLite opens these closed protocol suffixes itself; retain existing leaves before opening it.
                        for suffix in ("-wal", "-shm", "-journal"):
                            try:
                                held.enter_context(reserved_paths._sqlite_owner_target_scope(
                                    self.root, path.with_name(path.name + suffix), INDEX_DESCRIPTOR_ID,
                                    create=False,
                                ))
                            except FileNotFoundError:
                                pass
                        conn = sqlite3.connect(retained, timeout=5.0)
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA busy_timeout=5000")
                        self._schema(conn)
                        reserved_paths._publish_sqlite_owner_family(self.root, path, INDEX_DESCRIPTOR_ID, conn)
                        yield conn
                except BaseException:
                    if conn is not None:
                        conn.rollback()
                    raise
                finally:
                    # WAL leaves may disappear at close, after their retained identity checks complete.
                    if conn is not None:
                        conn.close()

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoint (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), catalogue_id TEXT,
                applied_epoch INTEGER NOT NULL, ready INTEGER NOT NULL);
            INSERT OR IGNORE INTO checkpoint VALUES (1,NULL,0,0);
            CREATE TABLE IF NOT EXISTS images (
                image_id TEXT PRIMARY KEY, header TEXT NOT NULL, horizon INTEGER NOT NULL,
                next_page INTEGER NOT NULL DEFAULT 0, chain_hash TEXT, exact INTEGER NOT NULL DEFAULT 0,
                seen TEXT NOT NULL DEFAULT '', loss_recorded INTEGER NOT NULL DEFAULT 0,
                remaining INTEGER GENERATED ALWAYS AS (json_extract(header,'$.written_pages')-next_page) STORED);
            CREATE INDEX IF NOT EXISTS images_horizon ON images(horizon,image_id);
            CREATE INDEX IF NOT EXISTS images_unchecked ON images(seen,remaining,image_id);
            CREATE INDEX IF NOT EXISTS images_seen ON images(loss_recorded,seen,image_id);
            CREATE TABLE IF NOT EXISTS members (
                source_ref TEXT NOT NULL, image_id TEXT NOT NULL, PRIMARY KEY(source_ref,image_id));
            CREATE INDEX IF NOT EXISTS members_image ON members(image_id,source_ref);
            CREATE TABLE IF NOT EXISTS copies (
                copy_id TEXT PRIMARY KEY, image_id TEXT NOT NULL, closed INTEGER NOT NULL, data TEXT NOT NULL,
                seen TEXT NOT NULL DEFAULT '', loss_recorded INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS copies_image ON copies(image_id,copy_id);
            CREATE INDEX IF NOT EXISTS copies_outstanding ON copies(image_id,closed,copy_id);
            CREATE INDEX IF NOT EXISTS copies_seen ON copies(closed,seen,copy_id);
            CREATE INDEX IF NOT EXISTS copies_missing ON copies(loss_recorded,closed,seen,copy_id);
            CREATE INDEX IF NOT EXISTS copies_owner ON copies(
                json_extract(data,'$.owner.owner'),json_extract(data,'$.owner.locator'),copy_id);
            CREATE TABLE IF NOT EXISTS slot_refs (
                slot_id TEXT NOT NULL,copy_id TEXT NOT NULL,image_id TEXT NOT NULL,revision TEXT NOT NULL,
                seen TEXT NOT NULL DEFAULT '',loss_recorded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(slot_id,copy_id));
            CREATE INDEX IF NOT EXISTS slot_refs_copy ON slot_refs(copy_id,slot_id);
            CREATE INDEX IF NOT EXISTS slot_refs_image ON slot_refs(image_id,slot_id);
            CREATE INDEX IF NOT EXISTS slot_refs_seen ON slot_refs(loss_recorded,seen,slot_id,copy_id);
            CREATE TABLE IF NOT EXISTS uncertainties (
                unit_id TEXT PRIMARY KEY, image_id TEXT, horizon INTEGER NOT NULL,
                canonical INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS uncertainties_horizon ON uncertainties(horizon,image_id);
            CREATE INDEX IF NOT EXISTS uncertainties_image ON uncertainties(image_id,canonical);
            CREATE INDEX IF NOT EXISTS uncertainties_unpublished ON uncertainties(canonical,unit_id);
        """)

    def checkpoint(self) -> dict:
        with self._connect() as conn:
            return dict(conn.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone())

    def initialize(self) -> Head:
        _boundary(self.root)
        head, guard = self._read(self.path / "head.json", Head)
        if head is None:
            head = Head(catalogue_id=_id(), summarized_through_epoch=0)
            self._install((self._write(self.path / "head.json", head, guard),))
            try:
                with self._connect() as conn:
                    self._reset(conn, head, ready=True)
                    conn.commit()
            except (ValueError, OSError, RuntimeError, sqlite3.Error, vault.PathGuardError) as error:
                log.warning("generated derived checkpoint initialization failed (%s)", type(error).__name__)
        return self.head()

    @staticmethod
    def _reset(conn, head, *, ready=False) -> bool:
        remaining = False
        for table in ("members", "copies", "images", "uncertainties", "slot_refs"):
            # Names are the closed local schema; changing catalogue identity clears only a bounded slice.
            conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} LIMIT ?)", (MAX_STEP,))
            remaining |= conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        conn.execute("UPDATE checkpoint SET catalogue_id=?, applied_epoch=?, ready=? WHERE singleton=1",
                     (None if remaining else head.catalogue_id, head.epoch if ready and not remaining else 0,
                      int(ready and not remaining)))
        return not remaining

    def _set_head(self, expected: Head, value: Head) -> Head:
        current, guard = self._read(self.path / "head.json", Head)
        if current != expected:
            raise RuntimeError("generated canonical head changed")
        self._install((self._write(self.path / "head.json", value, guard),))
        observed = self.head()
        if observed != value:
            raise RuntimeError("generated canonical head did not survive exactly")
        return observed

    def _change(self, epoch: int) -> Change:
        value, _ = self._read(self.path / "changes" / f"{epoch}.json", Change)
        if value is None or value.epoch != epoch:
            raise ValueError("generated_change_missing")
        for item in value.objects:
            self._object_path(item)
        return value

    def _run(self, operation: str, values: Sequence[tuple[ObjectChange, BaseModel | None]], *,
             stage: Literal["before_bytes", "after_bytes"] | None = None) -> Head:
        """Commit one operation, or stage it in this context's unit.

        ``before_bytes`` work becomes durable at the unit's next barrier, before native bytes depend on it.
        ``after_bytes`` work records facts native custody already holds, so it may wait for the unit's end:
        a crash first leaves the earlier, honest outstanding state.
        """
        _boundary(self.root)
        staged = _active_unit(self.root)
        if staged is not None and stage is not None:
            self._recover()
            for item, value in values:
                path = self._object_path(item)
                if value is None:
                    raise ValueError("generated_staged_removal_invalid")
                current, _ = self._read(path, _OBJECT_MODELS[item.kind])
                self._check_object(path, item, value, current)
                first = staged.staged.get(path)
                # The merged object keeps its durable before-revision and first-touch position.
                merged = item if first is None else first[0].model_copy(update={"after_revision": item.after_revision})
                staged.staged[path] = (merged, value)
            staged.before_bytes |= stage == "before_bytes"
            if len(staged.staged) >= MAX_STEP:
                self._flush(staged)
            return self.head()
        # Immediate work keeps its place after everything staged before it.
        self._flush(staged)
        return self._commit(operation, values)

    def barrier(self) -> None:
        """Make staged work that native bytes depend on durable; acknowledgements may still wait."""
        staged = _active_unit(self.root)
        if staged is not None and staged.before_bytes:
            self._flush(staged)

    def flush(self) -> None:
        """Make all staged work durable, before a caller discards evidence that a staged fact replaces."""
        self._flush(_active_unit(self.root))

    def _flush(self, staged: _Unit | None) -> None:
        if staged is None or not staged.staged:
            return
        _boundary(self.root)
        # A failed phase leaves later reads on durable state: unflushed before-bytes work never reached native bytes,
        # and lost acknowledgements stay outstanding reservations and Slot rights that the next writer settles.
        values, staged.staged, staged.before_bytes = list(staged.staged.values()), {}, False
        for start in range(0, len(values), MAX_STEP):
            self._commit("unit", values[start:start + MAX_STEP])

    def _check_object(self, path: Path, item: ObjectChange, value: BaseModel | None, current) -> None:
        if getattr(current, "revision", None) != item.expected_revision:
            raise ValueError("generated_object_revision_changed")
        if value is None:
            if not item.remove:
                raise ValueError("generated_remove_not_declared")
            return
        self._validate_payload(path, encode(value).decode())
        if item.image_id != getattr(value, "image_id", None):
            raise ValueError("generated_record_image_binding_invalid")
        if item.after_revision != value.revision:
            raise ValueError("generated_after_revision_changed")

    def _commit(self, operation: str, values: Sequence[tuple[ObjectChange, BaseModel | None]]) -> Head:
        self._recover()
        head = self.head()
        epoch, unit_id = head.epoch + 1, _id()
        objects, writes, deletes = [], [], []
        for item, value in values:
            path = self._object_path(item)
            current, guard = self._read_disk(path, _OBJECT_MODELS[item.kind])
            self._check_object(path, item, value, current)
            if value is None:
                deletes.append((path, guard))
            else:
                writes.append(self._write(path, value, guard))
            objects.append(item)
        change = Change(catalogue_id=head.catalogue_id, epoch=epoch, unit_id=unit_id,
                        operation=operation, objects=tuple(objects))
        change_path = self.path / "changes" / f"{epoch}.json"
        previous, guard = self._read_disk(change_path, Change)
        if previous is not None:
            raise RuntimeError("generated orphan epochs require bounded reconciliation")
        self._install((self._write(change_path, change, guard),))
        pending = self._set_head(head, head.model_copy(update={
            "epoch": epoch, "change_floor": max(head.change_floor, epoch - CHANGE_WINDOW + 1),
            "pending": Pending(unit_id=unit_id, change_epoch=epoch,
                               change_record_hash=hashlib.sha256(encode(change)).hexdigest()),
        }))
        # This readback is a separate durability seam: no affected metadata is staged before it.
        if self.head() != pending:
            raise RuntimeError("generated pending head changed before staging")
        for path, guard in deletes:
            self._remove(path, guard)
        if writes:
            self._install(writes)
        self._observe(change)
        collected = self._collect_changes(pending)
        summary = self._summarize(pending, change)
        settled = self._set_head(pending, summary.model_copy(update={"pending": None,
                                                                     "collected_through": collected}))
        self._refresh(change, settled)
        return self.head()

    def _uncertain_record(self, change: Change | None, head: Head) -> Uncertainty:
        unit_id = change.unit_id if change is not None else head.pending.unit_id
        images = {item.image_id for item in change.objects if item.image_id is not None} if change else set()
        return Uncertainty(unit_id=unit_id, catalogue_id=head.catalogue_id,
                           unknown_through_epoch=head.epoch,
                           image_id=next(iter(images)) if len(images) == 1 else None,
                           object_keys=tuple(self._object_path(item).relative_to(self.path).as_posix()
                                             for item in change.objects) if change else (), revision=_id())

    def _observe(self, change: Change | None) -> None:
        head = self.head()
        unresolved = change is None
        if change is not None:
            for item in change.objects:
                model = _OBJECT_MODELS[item.kind]
                try:
                    current, _ = self._read(self._object_path(item), model)
                    if current is not None:
                        self._validate_payload(self._object_path(item), encode(current).decode())
                    if item.remove:
                        unresolved |= current is not None and current.revision != item.expected_revision
                        if current is None and item.kind == "image":
                            unresolved |= not self._remove_empty_image_dirs(item.object_id)
                    else:
                        # A surviving before-image is an honest incomplete unit, not unexplained ownership loss.
                        unresolved |= getattr(current, "revision", None) not in (item.expected_revision, item.after_revision)
                except (ValueError, OSError, vault.PathGuardError):
                    unresolved = True
        if unresolved:
            value = self._uncertain_record(change, head)
            path = self.path / "uncertain" / f"{value.unit_id}.json"
            # Every unit declares uncertain/<unit_id>.json as its reserved recovery object.
            with reserved_paths._subsystem_authority_scope(OWNER):
                try:
                    guard = vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="absent")
                except vault.PathGuardError:
                    guard = vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="stable")
            self._install((self._write(path, value, guard),))

    def _summarize(self, head: Head, change: Change | None) -> Head:
        """Fold this bounded metadata unit before its pending head can clear."""
        declared = {item.object_id: item for item in change.objects if item.kind == "uncertainty"} if change else {}
        identities = {head.pending.unit_id}
        identities.update(declared)
        horizon = head.unknown_through_epoch
        for identity in identities:
            try:
                path = self.path / "uncertain" / f"{identity}.json"
                value, guard = self._read(path, Uncertainty)
                if value is None:
                    item = declared.get(identity)
                    if item is None:
                        continue
                    # The pending descriptor already records this loss, even if its marker was never installed.
                    value = Uncertainty(unit_id=identity, catalogue_id=head.catalogue_id,
                                        unknown_through_epoch=head.epoch, image_id=item.image_id,
                                        revision=item.after_revision)
                    self._install((self._write(path, value, guard),))
                if (value.catalogue_id != head.catalogue_id or value.unit_id != identity
                        or value.unknown_through_epoch > head.epoch):
                    raise ValueError("generated_uncertainty_binding_invalid")
                if value.image_id is None:
                    horizon = max(horizon, value.unknown_through_epoch)
                    continue
                image = self._image(value.image_id)
                if image.unknown_through_epoch < value.unknown_through_epoch:
                    item = ObjectChange(kind="image", object_id=image.image_id, image_id=image.image_id)
                    # The construction revision stays stable: replay only raises U, never changes the image or map.
                    updated = image.model_copy(update={"unknown_through_epoch": value.unknown_through_epoch})
                    self._install((self._write(self._object_path(item), updated),))
            except (ValueError, OSError, vault.PathGuardError):
                # An unavailable scoped header cannot hide its uncertainty from direct readers.
                horizon = max(horizon, head.epoch)
        return head.model_copy(update={"unknown_through_epoch": horizon,
                                       "summarized_through_epoch": head.epoch})

    def recover(self) -> Head:
        """Settle bounded canonical metadata without requiring a qualified derived index."""
        _boundary(self.root)
        self._recover()
        return self.head()

    def _recover(self) -> None:
        head = self.head()
        covered_before = head.epoch - int(head.pending is not None)
        if head.summarized_through_epoch is None or head.summarized_through_epoch < covered_before:
            head = self._set_head(head, head.model_copy(update={
                "unknown_through_epoch": self._global_horizon(head), "summarized_through_epoch": head.epoch,
            }))
        if head.pending is not None:
            change = None
            try:
                candidate = self._change(head.epoch)
                if (candidate.catalogue_id == head.catalogue_id and candidate.unit_id == head.pending.unit_id
                        and hashlib.sha256(encode(candidate)).hexdigest() == head.pending.change_record_hash):
                    change = candidate
            except (ValueError, OSError, vault.PathGuardError):
                pass
            self._observe(change)
            summary = self._summarize(head, change)
            settled = self._set_head(head, summary.model_copy(update={"pending": None}))
            if change is not None:
                self._refresh(change, settled, allow_before=True)
            else:
                try:
                    with self._connect() as conn:
                        conn.execute("UPDATE checkpoint SET ready=0 WHERE singleton=1")
                        conn.commit()
                except (ValueError, OSError, RuntimeError, sqlite3.Error, vault.PathGuardError) as error:
                    # Canonical uncertainty already fences lookup; derived invalidation cannot block recovered publication.
                    log.warning("generated derived checkpoint invalidation failed (%s)", type(error).__name__)
            head = settled
        path = self.path / "changes" / f"{head.epoch + 1}.json"
        try:
            orphan, guard = self._read(path, Change)
        except (ValueError, OSError, vault.PathGuardError):
            # An unreadable orphan has authorized no object writes either.
            with reserved_paths._subsystem_authority_scope(OWNER):
                guard = vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="stable")
            orphan = True
        if orphan is not None:
            change = Change(catalogue_id=head.catalogue_id, epoch=head.epoch + 1,
                            unit_id=_id(), operation="no_effect")
            self._install((self._write(path, change, guard),))
            pending = self._set_head(head, head.model_copy(update={"epoch": change.epoch, "pending": Pending(
                unit_id=change.unit_id, change_epoch=change.epoch,
                change_record_hash=hashlib.sha256(encode(change)).hexdigest(),
            )}))
            settled = self._set_head(pending, pending.model_copy(update={
                "pending": None, "summarized_through_epoch": pending.epoch,
            }))
            self._refresh(change, settled)

    def allocate_birth(self) -> ProducerBirth:
        """Checkpoint an actual fresh allocation; the allocating hook must prove all hooks participate.

        Replay, adoption and configuration reads must never call this operation.
        """
        head = self._run("birth", ())
        return ProducerBirth(catalogue_id=head.catalogue_id, birth_epoch=head.epoch)

    def begin_image(self, image: bytes, mapping: FragmentMap | FragmentPlan, *,
                    unknown_through_epoch: int) -> ImageBuild:
        """Capture convenience; hashing caller bytes is proportional to their size."""
        _boundary(self.root)
        if isinstance(mapping, FragmentPlan):
            mapping = mapping.after
        mapping.validate_image(image)
        image_id = _id()
        pages = tuple(pack_pages(image_id, mapping.fragments))
        header = self.begin_verified_image(
            image_sha256=mapping.image_sha256, image_size=len(image), image_id=image_id,
            page_count=len(pages), expected_last_page_hash=pages[-1].digest() if pages else None,
            unknown_through_epoch=unknown_through_epoch,
        )
        return ImageBuild(header, pages)

    def begin_verified_image(self, *, image_sha256: str, image_size: int, page_count: int,
                             expected_last_page_hash: str | None,
                             unknown_through_epoch: int, image_id: str | None = None,
                             whole_source_ref: str | None = None,
                             opaque_source_refs: tuple[str, ...] | None = None) -> ImageHeader:
        """Begin an image after its native owner verified the byte hash and planned page chain.

        Large producers can prepare that evidence across invocations without loading the body here.
        The producer supplies a horizon from bound inputs, preserving it for slices and taking its maximum for joins.
        Historical plan completeness neither raises nor clears this target-specific horizon.
        """
        _boundary(self.root)
        head = self.head()
        if not 0 <= unknown_through_epoch <= head.epoch:
            raise ValueError("generated_uncertainty_horizon_invalid")
        image_id = image_id or _id()
        header = ImageHeader(image_id=image_id, image_sha256=image_sha256,
                             image_size=image_size, catalogue_id=head.catalogue_id,
                             unknown_through_epoch=unknown_through_epoch, page_count=page_count,
                             expected_last_page_hash=expected_last_page_hash,
                             whole_source_ref=whole_source_ref, opaque_source_refs=opaque_source_refs, revision=_id())
        item = ObjectChange(kind="image", object_id=image_id, image_id=image_id, after_revision=header.revision)
        self._run("image_begin", ((item, header),), stage="before_bytes")
        return header

    def begin_whole_image(self, *, whole_source_ref: str, image_sha256: str,
                          image_size: int) -> ImageHeader:
        """Bind whole bytes after the producer proves allocation, Manifest and sealed-original ownership.

        This trusted seam accepts binary metadata; neither text parsing nor byte equality establishes ownership.
        """
        header = self.begin_verified_image(whole_source_ref=whole_source_ref,
                                           image_sha256=image_sha256, image_size=image_size,
                                           page_count=0, expected_last_page_hash=None,
                                           unknown_through_epoch=0)
        return self.seal_image(header.image_id)

    def begin_opaque_image(self, *, source_refs: Sequence[str], image_sha256: str,
                           image_size: int, unknown_through_epoch: int) -> ImageHeader:
        """Bind producer-declared members of exact opaque bytes without textual spans."""
        members = tuple(sorted({SourceRef(reference).value for reference in source_refs}))
        header = self.begin_verified_image(opaque_source_refs=members,
            image_sha256=image_sha256, image_size=image_size, page_count=0,
            expected_last_page_hash=None, unknown_through_epoch=unknown_through_epoch)
        return self.seal_image(header.image_id)

    def _image(self, image_id: str, *, guards: list[vault.PathGuard] | None = None) -> ImageHeader:
        _opaque(image_id)
        item = ObjectChange(kind="image", object_id=image_id, image_id=image_id)
        value, guard = self._read(self._object_path(item), ImageHeader)
        if value is None:
            raise FileNotFoundError("generated image is missing")
        head = self.head()
        if (value.image_id != image_id or value.catalogue_id != head.catalogue_id
                or value.unknown_through_epoch > head.epoch):
            raise ValueError("generated_image_binding_invalid")
        if guards is not None:
            guards.append(guard)
        return value

    def append_image(self, page: ImagePage) -> ImageHeader:
        _boundary(self.root)
        self._recover()
        current = self._image(page.image_id)
        item = ObjectChange(kind="page", object_id=page.image_id, image_id=page.image_id,
                            page_number=page.page_number, after_revision=page.revision)
        existing, _ = self._read(self._object_path(item), ImagePage)
        if existing is not None and existing != page:
            raise ValueError("generated immutable page differs")
        if page.page_number < current.written_pages and existing == page:
            return current
        if current.state != "building" or current.written_pages >= current.page_count:
            raise ValueError("generated image is not building")
        if page.page_number != current.written_pages or page.previous_page_hash != current.last_page_hash:
            raise ValueError("generated_page_chain_invalid")
        if any(not 0 <= fragment.start_byte < fragment.end_byte <= current.image_size for fragment in page.fragments):
            raise ValueError("generated_fragment_range_invalid")
        encode(page)
        updated = current.model_copy(update={"written_pages": current.written_pages + 1,
                                             "last_page_hash": page.digest(), "revision": _id()})
        values = [] if existing is not None else [(item, page)]
        values.append((ObjectChange(kind="image", object_id=page.image_id, image_id=page.image_id,
                          expected_revision=current.revision, after_revision=updated.revision), updated),
        )
        self._run("image_page", values, stage="before_bytes")
        return updated

    def seal_image(self, image_id: str) -> ImageHeader:
        _boundary(self.root)
        self._recover()
        current = self._image(image_id)
        if current.state == "ready":
            return current
        if (current.state != "building" or current.written_pages != current.page_count
                or current.last_page_hash != current.expected_last_page_hash):
            raise ValueError("generated image page chain is incomplete")
        updated = current.model_copy(update={"state": "ready", "revision": _id()})
        self._run("image_seal", ((ObjectChange(kind="image", object_id=image_id, image_id=image_id,
                   expected_revision=current.revision, after_revision=updated.revision), updated),),
                  stage="before_bytes")
        return updated

    def reserve_copy(self, image_id: str, *, owner: OwnerLocator, origin_ledger: str,
                     expected_header: ImageHeader, expected_root: Path) -> Copy:
        """Reserve after the native producer verified this exact image and map binding."""
        _boundary(self.root)
        self._recover()
        current = self._image(image_id)
        if current.state != "ready":
            raise ValueError("generated copy requires a ready image")
        if Path(expected_root).absolute() != self.root or current != expected_header:
            raise ValueError("generated verified image binding changed")
        value = Copy(copy_id=_id(), image_id=image_id, owner=owner, origin_ledger=origin_ledger, revision=_id())
        self._run("copy_reserve", ((ObjectChange(kind="copy", object_id=value.copy_id, image_id=image_id,
                   after_revision=value.revision), value),), stage="before_bytes")
        return value

    def _slot_id(self, owner: str, locator: str, physical_scope: str, catalogue_id: str) -> str:
        # Native owner fields share the existing opaque bounds; no process identity manufactures a durable scope.
        OwnerLocator(owner=owner, locator=locator, version=physical_scope)
        # UUIDv5 and canonical JSON fix the key protocol and make the tuple unambiguous.
        name = json.dumps((owner, locator, physical_scope), ensure_ascii=False, separators=(",", ":"))
        return uuid.uuid5(uuid.UUID(hex=catalogue_id), name).hex

    def _slot_records(self, slot: Slot) -> tuple[tuple[Copy, ...], tuple[ImageHeader, ...]]:
        if slot.catalogue_id != self.head().catalogue_id:
            raise ValueError("generated slot catalogue differs")
        identities = (slot.current_copy_id, slot.pending.candidate_copy_id if slot.pending else None)
        copies, images, guards = [], {}, []
        for identity in dict.fromkeys(identity for identity in identities if identity is not None):
            copy, guard = self._read(self._copy_path(identity), Copy)
            if copy is None or copy.copy_id != identity:
                raise ValueError("generated slot copy binding is unavailable")
            guards.append(guard)
            image = self._image(copy.image_id, guards=guards)
            if image.state != "ready":
                raise ValueError("generated slot image is not ready")
            copies.append(copy)
            images[image.image_id] = image
        if len({(copy.owner.owner, copy.owner.locator) for copy in copies}) > 1:
            raise ValueError("generated slot copy owners differ")
        for guard in guards:
            guard.recheck(self.root)
        return tuple(copies), tuple(images.values())

    def read_slot(self, *, owner: str, locator: str, physical_scope: str) -> SlotBinding:
        """Read one canonical native binding; missing says nothing about native file presence."""
        _boundary(self.root)
        slot = None
        try:
            head = self.head()
            identity = self._slot_id(owner, locator, physical_scope, head.catalogue_id)
            item = ObjectChange(kind="slot", object_id=identity)
            slot, guard = self._read(self._object_path(item), Slot)
            if slot is None:
                guard.recheck(self.root)
                if self.head() != head or head.pending is not None:
                    return SlotBinding("pending", unknown_through_epoch=self._global_horizon(head))
                return SlotBinding("missing", unknown_through_epoch=self._global_horizon(head))
            if slot.slot_id != identity:
                raise ValueError("generated slot key differs")
            copies, images = self._slot_records(slot)
            if any(copy.owner.owner != owner or copy.owner.locator != locator for copy in copies):
                raise ValueError("generated slot owner differs")
            by_id = {copy.copy_id: copy for copy in copies}
            publication = None
            if slot.pending is not None:
                publication = SlotPublication(self.root, owner, locator, physical_scope, identity,
                                              slot.revision, slot.pending.publication_id,
                                              slot.current_copy_id, slot.pending.candidate_copy_id, _SLOT_SEAL)
            guard.recheck(self.root)
            if self.head() != head:
                return SlotBinding("pending", slot)
            return SlotBinding("pending" if head.pending is not None else "observed", slot,
                               by_id.get(slot.current_copy_id),
                               by_id.get(slot.pending.candidate_copy_id) if slot.pending else None,
                               images, publication,
                               max((self._global_horizon(head),
                                    *(image.unknown_through_epoch for image in images))))
        except (ValueError, OSError, vault.PathGuardError):
            return SlotBinding("unknown", slot)

    def prepare_slot(self, *, owner: str, locator: str, physical_scope: str,
                     expected_revision: str | None, expected_current_copy_id: str | None,
                     candidate: Copy, publication_id: str) -> SlotPublication:
        """Reserve one candidate without changing the current native binding or retiring a copy."""
        _boundary(self.root)
        self._recover()
        _opaque(publication_id)
        binding = self.read_slot(owner=owner, locator=locator, physical_scope=physical_scope)
        if binding.state not in ("missing", "observed"):
            raise ValueError("generated slot binding is unavailable")
        current = binding.slot
        if (getattr(current, "revision", None) != expected_revision
                or getattr(current, "current_copy_id", None) != expected_current_copy_id):
            raise ValueError("generated slot binding changed")
        if current is not None and current.pending is not None:
            raise ValueError("generated slot publication is pending")
        item = ObjectChange(kind="copy", object_id=candidate.copy_id, image_id=candidate.image_id)
        reserved, _ = self._read(self._object_path(item), Copy)
        if (reserved != candidate or candidate.closed or candidate.owner.owner != owner
                or candidate.owner.locator != locator or self._image(candidate.image_id).state != "ready"):
            raise ValueError("generated slot candidate differs from its reservation")
        head = self.head()
        identity = self._slot_id(owner, locator, physical_scope, head.catalogue_id)
        value = Slot(slot_id=identity, catalogue_id=head.catalogue_id,
                     current_copy_id=expected_current_copy_id,
                     pending=SlotPending(publication_id=publication_id, candidate_copy_id=candidate.copy_id), revision=_id())
        self._run("slot_prepare", ((ObjectChange(kind="slot", object_id=identity,
                   expected_revision=expected_revision, after_revision=value.revision), value),),
                  stage="before_bytes")
        return SlotPublication(self.root, owner, locator, physical_scope, identity, value.revision,
                               publication_id, expected_current_copy_id, candidate.copy_id, _SLOT_SEAL)

    def takeover_slot(self, previous: SlotPublication, *, candidate: Copy,
                      publication_id: str) -> SlotPublication:
        """Revoke an exact pending right without claiming what its native publication did."""
        _boundary(self.root)
        self._recover()
        _opaque(publication_id)
        binding = self.check_slot(previous)
        item = ObjectChange(kind="copy", object_id=candidate.copy_id, image_id=candidate.image_id)
        reserved, copy_guard = self._read(self._object_path(item), Copy)
        guards = [copy_guard]
        image = self._image(candidate.image_id, guards=guards)
        if (reserved != candidate or candidate.outcome != "reserved"
                or candidate.copy_id in (previous.before_copy_id, previous.candidate_copy_id)
                or candidate.owner.owner != previous.owner or candidate.owner.locator != previous.locator
                or image.state != "ready" or publication_id == previous.publication_id):
            raise ValueError("generated takeover requires a fresh reserved candidate and publication")
        for guard in guards:
            guard.recheck(self.root)
        self._uncertain_slot(binding)
        binding = self.check_slot(previous)
        value = binding.slot.model_copy(update={
            "pending": SlotPending(publication_id=publication_id, candidate_copy_id=candidate.copy_id),
            "revision": _id(),
        })
        self._run("slot_takeover", ((ObjectChange(kind="slot", object_id=value.slot_id,
                   expected_revision=previous.revision, after_revision=value.revision), value),))
        return SlotPublication(self.root, previous.owner, previous.locator, previous.physical_scope,
                               value.slot_id, value.revision, publication_id, value.current_copy_id,
                               candidate.copy_id, _SLOT_SEAL)

    def check_slot(self, publication: SlotPublication) -> SlotBinding:
        """Fence both forward installation and inverse publication against a superseded slot."""
        _boundary(self.root)
        if (not isinstance(publication, SlotPublication) or publication._seal is not _SLOT_SEAL
                or publication.root != self.root):
            raise ValueError("generated slot publication is not owner-bound")
        binding = self.read_slot(owner=publication.owner, locator=publication.locator,
                                 physical_scope=publication.physical_scope)
        slot = binding.slot
        if (binding.state != "observed" or slot is None or slot.pending is None
                or slot.slot_id != publication.slot_id or slot.revision != publication.revision
                or slot.current_copy_id != publication.before_copy_id
                or slot.pending.publication_id != publication.publication_id
                or slot.pending.candidate_copy_id != publication.candidate_copy_id):
            raise ValueError("generated slot publication was superseded or is unavailable")
        return binding

    def _native_slot_witness(self, binding: SlotBinding,
                             witness: Callable[[SlotBinding], NativeSlotWitness]) -> NativeSlotWitness:
        if not callable(witness):
            raise ValueError("generated slot requires a native-owner witness callback")
        proof = witness(binding)
        slot = binding.slot
        if (not isinstance(proof, NativeSlotWitness) or proof.slot_id != slot.slot_id
                or proof.revision != slot.revision
                or proof.publication_id != (slot.pending.publication_id if slot.pending else None)):
            raise ValueError("generated native witness is not slot-bound")
        return proof

    def _uncertain_slot(self, binding: SlotBinding) -> SlotBinding:
        images = tuple(image.image_id for image in binding.images)
        for image_id in images or (None,):
            # One slot revision describes one observed loss, even across retries and restarts.
            identity = uuid.uuid5(uuid.UUID(hex=binding.slot.revision), image_id or "global").hex
            self.mark_uncertain(image_id, unit_id=identity)
        head = self.head()
        headers = tuple(self._image(image_id) for image_id in images)
        return SlotBinding("unknown", binding.slot, binding.current, binding.candidate, headers,
                           unknown_through_epoch=max((self._global_horizon(head),
                                                      *(image.unknown_through_epoch for image in headers))))

    def settle_slot(self, publication: SlotPublication, *,
                    witness: Callable[[SlotBinding], NativeSlotWitness]) -> SlotBinding:
        """Select a witnessed native binding and revoke the old publication right, without copy acknowledgements."""
        _boundary(self.root)
        self._recover()
        binding = self.check_slot(publication)
        proof = self._native_slot_witness(binding, witness)
        binding = self.check_slot(publication)
        if proof.physical_scope != publication.physical_scope or proof.selection == "foreign":
            return SlotBinding("pending", binding.slot, binding.current, binding.candidate,
                               binding.images, binding.publication, binding.unknown_through_epoch)
        if proof.selection == "mismatch":
            return self._uncertain_slot(binding)
        if proof.selection == "before":
            selected = binding.current
        elif proof.selection == "candidate":
            selected = binding.candidate
        elif proof.selection == "absence":
            selected = None
        else:
            raise ValueError("generated native witness selection is invalid")
        if selected is not None and selected.closed:
            return self._uncertain_slot(binding)
        value = binding.slot.model_copy(update={"current_copy_id": selected.copy_id if selected else None,
                                                "pending": None, "revision": _id()})
        self._run("slot_settle", ((ObjectChange(kind="slot", object_id=value.slot_id,
                   expected_revision=publication.revision, after_revision=value.revision), value),),
                  stage="after_bytes")
        return self.read_slot(owner=publication.owner, locator=publication.locator,
                              physical_scope=publication.physical_scope)

    def remove_slot(self, *, owner: str, locator: str, physical_scope: str, expected_revision: str,
                    witness: Callable[[SlotBinding], NativeSlotWitness]) -> SlotBinding:
        """Remove one exact, settled slot only after native-owner absence evidence."""
        _boundary(self.root)
        self._recover()
        binding = self.read_slot(owner=owner, locator=locator, physical_scope=physical_scope)
        if (binding.state != "observed" or binding.slot.revision != expected_revision
                or binding.slot.pending is not None):
            raise ValueError("generated slot removal requires an exact settled revision")
        proof = self._native_slot_witness(binding, witness)
        checked = self.read_slot(owner=owner, locator=locator, physical_scope=physical_scope)
        if checked.state != "observed" or checked.slot != binding.slot:
            raise ValueError("generated slot binding changed during its native witness")
        if proof.physical_scope != physical_scope or proof.selection == "foreign":
            return SlotBinding("pending", binding.slot, binding.current, images=binding.images,
                               unknown_through_epoch=binding.unknown_through_epoch)
        if proof.selection != "absence":
            return self._uncertain_slot(binding)
        self._run("slot_remove", ((ObjectChange(kind="slot", object_id=binding.slot.slot_id,
                   expected_revision=expected_revision, remove=True), None),))
        return self.read_slot(owner=owner, locator=locator, physical_scope=physical_scope)

    def ack_copy(self, copy_id: str, *, owner: OwnerLocator,
                 outcome: Literal["present", "no_effect", "retired"]) -> Copy:
        _boundary(self.root)
        self._recover()
        _opaque(copy_id)
        path = self._copy_path(copy_id)
        current, _ = self._read(path, Copy)
        if current is None or current.copy_id != copy_id or current.owner != owner:
            raise ValueError("generated copy owner witness differs")
        if current.closed and current.outcome != outcome:
            raise ValueError("generated copy witness is already closed")
        if current.outcome == "present" and outcome == "no_effect":
            raise ValueError("generated published copy requires a retirement witness")
        if current.outcome == outcome:
            return current
        value = Copy.model_validate({**current.model_dump(), "outcome": outcome, "revision": _id()})
        self._run("copy_ack", ((ObjectChange(kind="copy", object_id=copy_id, image_id=value.image_id,
                   expected_revision=current.revision, after_revision=value.revision), value),),
                  stage="after_bytes")
        return value

    def mark_uncertain(self, image_id: str | None = None, *, unit_id: str | None = None) -> Uncertainty:
        _boundary(self.root)
        # Its horizon names the next durable epoch, so earlier staged work commits first.
        self._flush(_active_unit(self.root))
        self._recover()
        head = self.head()
        if image_id is not None:
            _opaque(image_id)
        if unit_id is not None:
            _opaque(unit_id)
            existing, _ = self._read(self.path / "uncertain" / f"{unit_id}.json", Uncertainty)
            if existing is not None:
                if existing.catalogue_id != head.catalogue_id or existing.image_id != image_id:
                    raise ValueError("generated uncertainty identity differs")
                return existing
        value = Uncertainty(unit_id=unit_id or _id(), catalogue_id=head.catalogue_id,
                            unknown_through_epoch=head.epoch + 1, image_id=image_id, revision=_id())
        self._run("uncertainty", ((ObjectChange(kind="uncertainty", object_id=value.unit_id,
                   image_id=image_id, after_revision=value.revision), value),))
        return value

    def copy_record(self, copy_id: str) -> Observation:
        """Read one expected copy directly, independently of the derived membership index."""
        _boundary(self.root)
        try:
            _opaque(copy_id)
            head = self.head()
            value, guard = self._read(self._copy_path(copy_id), Copy)
            if value is not None and value.copy_id != copy_id:
                raise ValueError("generated copy key differs")
            guard.recheck(self.root)
            if head.pending is not None or self.head() != head:
                return Observation("pending", value)
            return Observation("observed" if value is not None else "missing", value)
        except (ValueError, OSError, vault.PathGuardError):
            return Observation("unknown")

    def image_header(self, image_id: str) -> Observation:
        """Read bounded image binding evidence without loading its map or native bytes."""
        try:
            value = self._image(image_id)
            head = self.head()
            return Observation("observed" if value.state == "ready" and head.pending is None else "pending", value,
                               max(value.unknown_through_epoch, self._global_horizon(head)))
        except FileNotFoundError:
            return Observation("missing")
        except (ValueError, OSError, vault.PathGuardError):
            return Observation("unknown")

    def image_page(self, image_id: str, page_number: int) -> Observation:
        try:
            header = self._image(image_id)
            item = ObjectChange(kind="page", object_id=image_id, image_id=image_id, page_number=page_number)
            value, _ = self._read(self._object_path(item), ImagePage)
            if value is None:
                return Observation("unknown" if page_number < header.page_count else "missing")
            if value.image_id != image_id or value.page_number != page_number:
                return Observation("unknown")
            return Observation("observed" if header.state == "ready" else "pending", value)
        except FileNotFoundError:
            return Observation("missing")
        except (ValueError, OSError, vault.PathGuardError):
            return Observation("unknown")

    def _cache_uncertainty(self, conn, value: Uncertainty, *, canonical: bool = True) -> None:
        scope = value.image_id or ""
        if not canonical and conn.execute("SELECT 1 FROM uncertainties WHERE image_id IS ? AND canonical=1 LIMIT 1",
                                          (scope,)).fetchone():
            if value.image_id is not None:
                conn.execute("UPDATE images SET exact=0,loss_recorded=1 WHERE image_id=?", (value.image_id,))
            return
        if canonical:
            conn.execute("DELETE FROM uncertainties WHERE image_id IS ? AND canonical=0 AND horizon<=?",
                         (scope, value.unknown_through_epoch))
        conn.execute("INSERT INTO uncertainties(unit_id,image_id,horizon,canonical) VALUES (?,?,?,?) "
                     "ON CONFLICT(unit_id) DO UPDATE SET image_id=excluded.image_id,"
                     "horizon=max(horizon,excluded.horizon),canonical=excluded.canonical",
                     (value.unit_id, scope, value.unknown_through_epoch, int(canonical)))
        if value.image_id is not None:
            conn.execute("UPDATE images SET horizon=max(horizon,?),exact=0,loss_recorded=1 WHERE image_id=?",
                         (value.unknown_through_epoch, value.image_id))
            if canonical:
                try:
                    header = self._image(value.image_id)
                except (ValueError, OSError, vault.PathGuardError):
                    pass
                else:
                    conn.execute("UPDATE images SET header=? WHERE image_id=?",
                                 (encode(header).decode(), value.image_id))

    def _cache_object(self, conn, item: ObjectChange, epoch: int, *, allow_before: bool = False) -> None:
        path = self._object_path(item)
        model = _OBJECT_MODELS[item.kind]
        try:
            value, _ = self._read(path, model)
            if value is not None:
                self._validate_payload(path, encode(value).decode())
                if getattr(value, "catalogue_id", self.head().catalogue_id) != self.head().catalogue_id:
                    raise ValueError("generated_record_catalogue_invalid")
        except (ValueError, OSError, vault.PathGuardError):
            value = None
        if value is None:
            if item.remove:
                if item.kind == "copy":
                    conn.execute("DELETE FROM copies WHERE copy_id=?", (item.object_id,))
                elif item.kind == "image":
                    conn.execute("DELETE FROM members WHERE image_id=?", (item.object_id,))
                    conn.execute("DELETE FROM copies WHERE image_id=?", (item.object_id,))
                    conn.execute("DELETE FROM images WHERE image_id=?", (item.object_id,))
                    conn.execute("DELETE FROM uncertainties WHERE image_id=?", (item.object_id,))
                elif item.kind == "slot":
                    conn.execute("DELETE FROM slot_refs WHERE slot_id=?", (item.object_id,))
                return
            if allow_before and item.expected_revision is None:
                return
            self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=self.head().catalogue_id,
                                    unknown_through_epoch=epoch, image_id=item.image_id, revision=_id()), canonical=False)
            return
        if item.kind == "image":
            seen = self._census_token if self._census_epoch == epoch else ""
            old = conn.execute("SELECT header,seen FROM images WHERE image_id=?", (value.image_id,)).fetchone()
            if old is not None:
                previous = ImageHeader.model_validate_json(old[0])
                if (previous.image_sha256, previous.image_size, previous.expected_last_page_hash,
                    previous.whole_source_ref, previous.opaque_source_refs) != (
                        value.image_sha256, value.image_size, value.expected_last_page_hash,
                        value.whole_source_ref, value.opaque_source_refs):
                    self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=value.catalogue_id,
                                            unknown_through_epoch=epoch, image_id=value.image_id, revision=_id()), canonical=False)
            conn.execute("INSERT INTO images(image_id,header,horizon,seen) VALUES (?,?,?,?) ON CONFLICT(image_id) "
                         "DO UPDATE SET header=excluded.header,horizon=max(horizon,excluded.horizon),seen=excluded.seen,loss_recorded=0",
                         (value.image_id, encode(value).decode(), value.unknown_through_epoch, seen))
            if self._census_epoch == epoch and (old is None or old[1] != seen):
                conn.execute("UPDATE images SET next_page=0,chain_hash=NULL,exact=0 WHERE image_id=?", (value.image_id,))
            if value.whole_source_ref is not None:
                # Explicit whole ownership uses the same positive-membership index as fragment ownership.
                conn.execute("INSERT OR IGNORE INTO members VALUES (?,?)", (value.whole_source_ref, value.image_id))
            if value.opaque_source_refs is not None:
                conn.executemany("INSERT OR IGNORE INTO members VALUES (?,?)",
                    ((reference, value.image_id) for reference in value.opaque_source_refs))
        elif item.kind == "page":
            row = conn.execute("SELECT next_page,chain_hash FROM images WHERE image_id=?", (value.image_id,)).fetchone()
            if row is None or row[0] != value.page_number or row[1] != value.previous_page_hash:
                self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=self.head().catalogue_id,
                                        unknown_through_epoch=epoch, image_id=value.image_id, revision=_id()), canonical=False)
            else:
                conn.execute("UPDATE images SET next_page=next_page+1,chain_hash=? WHERE image_id=?",
                             (value.digest(), value.image_id))
            # Positive evidence survives loss or replacement of its enclosing map.
            conn.executemany("INSERT OR IGNORE INTO members VALUES (?,?)",
                             ((f.source_ref, value.image_id) for f in value.fragments))
        elif item.kind == "copy":
            conn.execute("INSERT INTO copies(copy_id,image_id,closed,data,seen) VALUES (?,?,?,?,?) ON CONFLICT(copy_id) DO UPDATE SET "
                         "image_id=excluded.image_id,closed=excluded.closed,data=excluded.data,seen=excluded.seen,loss_recorded=0",
                         (value.copy_id, value.image_id, int(value.closed), encode(value).decode(),
                          self._census_token if self._census_epoch == epoch else ""))
            if conn.execute("SELECT 1 FROM images WHERE image_id=?", (value.image_id,)).fetchone() is None:
                self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=self.head().catalogue_id,
                                        unknown_through_epoch=epoch, image_id=value.image_id, revision=_id()), canonical=False)
        elif item.kind == "slot":
            try:
                copies, _ = self._slot_records(value)
            except (ValueError, OSError, vault.PathGuardError):
                self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=value.catalogue_id,
                                        unknown_through_epoch=epoch, revision=_id()), canonical=False)
                return
            conn.execute("DELETE FROM slot_refs WHERE slot_id=?", (value.slot_id,))
            conn.executemany("INSERT INTO slot_refs(slot_id,copy_id,image_id,revision,seen) VALUES (?,?,?,?,?)",
                             ((value.slot_id, copy.copy_id, copy.image_id, value.revision,
                               self._census_token if self._census_epoch == epoch else "") for copy in copies))
        else:
            self._cache_uncertainty(conn, value)

    def _qualify_images(self, conn, image_ids) -> None:
        for image_id in set(image_ids):
            row = conn.execute("SELECT header,next_page,chain_hash,horizon FROM images WHERE image_id=?", (image_id,)).fetchone()
            if row is None:
                continue
            header = ImageHeader.model_validate_json(row[0])
            exact = (header.state == "ready" and row[1] == header.page_count
                     and row[2] == header.expected_last_page_hash and row[3] <= header.unknown_through_epoch)
            conn.execute("UPDATE images SET exact=? WHERE image_id=?", (int(exact), image_id))

    def _refresh(self, change: Change, head: Head, *, allow_before: bool = False) -> bool:
        try:
            with self._connect() as conn:
                checkpoint = conn.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone()
                if checkpoint["catalogue_id"] != head.catalogue_id or checkpoint["applied_epoch"] != change.epoch - 1:
                    return False
                conn.execute("BEGIN IMMEDIATE")
                for item in change.objects:
                    self._cache_object(conn, item, change.epoch, allow_before=allow_before)
                uncertainty, _ = self._read(self.path / "uncertain" / f"{change.unit_id}.json", Uncertainty)
                if uncertainty is not None:
                    self._cache_uncertainty(conn, uncertainty)
                self._qualify_images(conn, (item.image_id for item in change.objects if item.image_id is not None))
                conn.execute("UPDATE checkpoint SET applied_epoch=? WHERE singleton=1", (change.epoch,))
                conn.commit()
                return True
        except (ValueError, OSError, RuntimeError, sqlite3.Error, vault.PathGuardError) as error:
            # Canonical publication remains authoritative; a failed derived transaction keeps lookup unqualified.
            log.warning("generated derived checkpoint refresh failed (%s)", type(error).__name__)
            return False

    def reconcile(self, *, limit: int = 16) -> Progress:
        """Observe bounded changed objects; a cold cache uses explicit census instead."""
        _boundary(self.root)
        _limit(limit)
        self._recover()
        head = self.head()
        checkpoint = self.checkpoint()
        if checkpoint["catalogue_id"] != head.catalogue_id or not checkpoint["ready"]:
            return Progress("pending", head.epoch)
        if checkpoint["applied_epoch"] < head.change_floor - 1:
            return Progress("pending", head.epoch)
        processed = 0
        for epoch in range(checkpoint["applied_epoch"] + 1, min(head.epoch + 1, checkpoint["applied_epoch"] + limit + 1)):
            try:
                change = self._change(epoch)
                if change.catalogue_id != head.catalogue_id:
                    return Progress("unknown", head.epoch, processed)
                self._refresh(change, head)
            except (ValueError, OSError, vault.PathGuardError):
                return Progress("unknown", head.epoch, processed)
            processed += 1
        return Progress("ready" if self.checkpoint()["applied_epoch"] == head.epoch else "pending", head.epoch, processed)

    def _census_keys(self) -> Iterator[tuple[str, str, int | None]]:
        """Hold streaming directory handles only during explicit cold census, never lookup."""
        acquired = held_fs.acquire(self.root)
        if not acquired.ok:
            raise RuntimeError("generated census cannot acquire vault")
        with acquired.require() as filesystem:
            for family in ("images", "copies", "slots", "uncertain"):
                path = (self.path / family).relative_to(self.root).as_posix()
                result = filesystem.parent(path)
                if not result.ok:
                    if result.error is not None and result.error.code == "MISSING":
                        continue
                    raise RuntimeError("generated census directory is unsafe")
                with result.require() as parent:
                    names = filesystem.iter_names(parent)
                    try:
                        for name in names:
                            if family == "uncertain":
                                identity = _opaque(name[:-5])
                                if name != f"{identity}.json":
                                    raise ValueError("generated census key is unknown")
                                yield family, identity, None
                                continue
                            # UUID hex keys fix this prefix alphabet; the check concerns spelling only.
                            if len(name) != 2 or any(c not in "0123456789abcdef" for c in name):
                                raise ValueError("generated census prefix is unknown")
                            prefix_result = filesystem.parent(f"{path}/{name}")
                            if not prefix_result.ok:
                                raise RuntimeError("generated census prefix is unsafe")
                            with prefix_result.require() as prefix:
                                children = filesystem.iter_names(prefix)
                                try:
                                    for child in children:
                                        identity = _opaque(child if family == "images" else child[:-5])
                                        if identity[:2] != name or (family in ("copies", "slots") and child != f"{identity}.json"):
                                            raise ValueError("generated census identity is unknown")
                                        yield family, identity, None
                                finally:
                                    children.close()
                    finally:
                        names.close()

    def census(self, *, limit: int = 16) -> Progress:
        _boundary(self.root)
        _limit(limit)
        self._recover()
        head = self.head()
        if self._census is None or self._census_epoch != head.epoch:
            self.close()
            self._census, self._census_epoch = self._census_keys(), head.epoch
            self._census_token = _id()
            with self._connect() as conn:
                checkpoint = conn.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone()
                if checkpoint["catalogue_id"] != head.catalogue_id:
                    cleared = self._reset(conn, head)
                    if not cleared:
                        conn.commit()
                        self.close()
                        return Progress("pending", head.epoch)
                else:
                    # A census never discards earlier positive ownership just because its map disappeared.
                    conn.execute("UPDATE checkpoint SET ready=0 WHERE singleton=1")
                conn.commit()
        processed = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                while processed < limit:
                    # A partially checked image persists its page cursor without qualifying the checkpoint.
                    partial = conn.execute("SELECT image_id,header,next_page FROM images "
                                           "WHERE seen=? AND remaining>0 ORDER BY remaining,image_id LIMIT 1",
                                           (self._census_token,)).fetchone()
                    if partial is not None:
                        item = ObjectChange(kind="page", object_id=partial[0], image_id=partial[0], page_number=partial[2])
                        self._cache_object(conn, item, head.epoch)
                        now = conn.execute("SELECT next_page FROM images WHERE image_id=?", (partial[0],)).fetchone()[0]
                        if now == partial[2]:
                            # A lost page cannot freeze census or erase earlier positive evidence.
                            conn.execute("UPDATE images SET next_page=json_extract(header,'$.written_pages') WHERE image_id=?", (partial[0],))
                        self._qualify_images(conn, (partial[0],))
                    else:
                        family, identity, _ = next(self._census)
                        if family == "images":
                            item = ObjectChange(kind="image", object_id=identity, image_id=identity)
                        elif family == "copies":
                            value, _ = self._read(self._copy_path(identity), Copy)
                            if value is None:
                                raise ValueError("generated copy disappeared during census")
                            item = ObjectChange(kind="copy", object_id=identity, image_id=value.image_id)
                        elif family == "slots":
                            item = ObjectChange(kind="slot", object_id=identity)
                        else:
                            item = ObjectChange(kind="uncertainty", object_id=identity)
                        self._cache_object(conn, item, head.epoch)
                        if item.image_id is not None:
                            self._qualify_images(conn, (item.image_id,))
                    processed += 1
            except StopIteration:
                if self.head() != head:
                    conn.rollback()
                    self.close()
                    return Progress("pending", head.epoch, processed)
                missing = []
                for comparison in ("<", ">"):
                    missing.extend(conn.execute(
                        f"SELECT image_id,NULL AS copy_id,NULL AS slot_id FROM images WHERE loss_recorded=0 AND seen{comparison}? LIMIT ?",
                        (self._census_token, limit - len(missing))).fetchall())
                    missing.extend(conn.execute(
                        f"SELECT image_id,copy_id,NULL AS slot_id FROM copies WHERE loss_recorded=0 AND closed=0 AND seen{comparison}? LIMIT ?",
                        (self._census_token, limit - len(missing))).fetchall())
                    missing.extend(conn.execute(
                        f"SELECT image_id,copy_id,slot_id FROM slot_refs WHERE loss_recorded=0 AND seen{comparison}? LIMIT ?",
                        (self._census_token, limit - len(missing))).fetchall())
                for row in missing:
                    self._cache_uncertainty(conn, Uncertainty(unit_id=_id(), catalogue_id=head.catalogue_id,
                                            unknown_through_epoch=head.epoch, image_id=row[0], revision=_id()), canonical=False)
                    if row[2] is not None:
                        conn.execute("UPDATE slot_refs SET loss_recorded=1 WHERE slot_id=? AND copy_id=?", (row[2], row[1]))
                    elif row[1] is not None:
                        conn.execute("UPDATE copies SET loss_recorded=1 WHERE copy_id=?", (row[1],))
                if len(missing) == limit:
                    conn.commit()
                    return Progress("pending", head.epoch, processed + len(missing))
                unpublished = conn.execute("SELECT image_id FROM uncertainties WHERE canonical=0 LIMIT 1").fetchone()
                if unpublished is not None:
                    conn.commit()
                    self.close()
                    self.mark_uncertain(unpublished[0] or None)
                    return Progress("pending", self.head().epoch, processed)
                old_closed = conn.execute("SELECT copy_id FROM copies WHERE closed=1 AND seen<? LIMIT ?",
                                          (self._census_token, limit)).fetchall()
                conn.executemany("DELETE FROM copies WHERE copy_id=?", old_closed)
                if len(old_closed) == limit:
                    conn.commit()
                    return Progress("pending", head.epoch, processed)
                conn.execute("UPDATE checkpoint SET applied_epoch=?,ready=1 WHERE singleton=1", (head.epoch,))
                conn.commit()
                self.close()
                return Progress("ready", head.epoch, processed)
            except (ValueError, OSError, RuntimeError, vault.PathGuardError):
                conn.commit()
                self.close()
                return Progress("unknown", head.epoch, processed)
            conn.commit()
        return Progress("pending", head.epoch, processed)

    def owner_copies(self, *, owner: str, locator: str, limit: int = 32,
                     cursor: str | None = None) -> CopyPage:
        """Select all versions for one opaque owner locator without scanning canonical records."""
        _boundary(self.root)
        _limit(limit)
        # OwnerLocator defines both opaque-field bounds; this read intentionally does not select a version.
        OwnerLocator(owner=owner, locator=locator, version=locator)
        if cursor is not None:
            _opaque(cursor)
        try:
            head = self.head()
            if head.pending is not None:
                return CopyPage("pending", head.catalogue_id, head.epoch)
            with self._connect() as conn:
                checkpoint = conn.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone()
                if (checkpoint["catalogue_id"] != head.catalogue_id
                        or checkpoint["applied_epoch"] != head.epoch or not checkpoint["ready"]):
                    return CopyPage("pending", head.catalogue_id, head.epoch)
                rows = conn.execute("SELECT data FROM copies WHERE json_extract(data,'$.owner.owner')=? "
                                    "AND json_extract(data,'$.owner.locator')=? AND copy_id>? "
                                    "ORDER BY copy_id LIMIT ?", (owner, locator, cursor or "", limit + 1)).fetchall()
                copies = tuple(Copy.model_validate_json(row[0]) for row in rows[:limit])
                if self.head() != head:
                    return CopyPage("pending", head.catalogue_id, head.epoch)
                return CopyPage("pending" if len(rows) > limit else "ready", head.catalogue_id,
                                head.epoch, copies, copies[-1].copy_id if len(rows) > limit else None)
        except (ValueError, OSError, RuntimeError, sqlite3.Error, vault.PathGuardError):
            return CopyPage("unknown", None, None)

    def lookup(self, source_ref: str, birth: ProducerBirth | None, *, limit: int = 32,
               cursor: tuple[str, str] | None = None,
               uncertainty_cursor: tuple[int, str] | None = None) -> Lookup:
        """One target join and indexed horizon reads; never reconcile or enumerate at lookup."""
        _boundary(self.root)
        SourceRef(source_ref)
        _limit(limit)
        try:
            head = self.head()
        except (ValueError, OSError, vault.PathGuardError):
            return Lookup("unknown", None, None)
        if head.pending is not None:
            return Lookup("pending", head.catalogue_id, head.epoch)
        with self._connect() as conn:
            checkpoint = conn.execute("SELECT * FROM checkpoint WHERE singleton=1").fetchone()
            if (checkpoint["catalogue_id"] != head.catalogue_id or checkpoint["applied_epoch"] != head.epoch
                    or not checkpoint["ready"]):
                return Lookup("pending", head.catalogue_id, head.epoch)
            birth_qualified = (birth is not None and birth.catalogue_id == head.catalogue_id
                               and 0 < birth.birth_epoch <= head.epoch)
            image_cursor, copy_cursor = cursor or ("", "")
            # Materialize one indexed image and a bounded copy slice before sorting the join.
            rows = conn.execute("WITH member AS MATERIALIZED (SELECT image_id FROM members "
                                "WHERE source_ref=? AND image_id>=? AND (image_id<>? OR ?<>'') "
                                "ORDER BY image_id LIMIT 1), bounded_copies AS MATERIALIZED ("
                                "SELECT copy_id,data FROM copies WHERE image_id=(SELECT image_id FROM member) "
                                "AND closed=0 AND copy_id>CASE WHEN (SELECT image_id FROM member)=? "
                                "THEN ? ELSE '' END ORDER BY copy_id LIMIT ?) "
                                "SELECT i.image_id,i.exact,i.horizon,c.copy_id,c.data,EXISTS("
                                "SELECT 1 FROM members WHERE source_ref=? AND image_id>i.image_id) AS more_images "
                                "FROM member m JOIN images i ON i.image_id=m.image_id LEFT JOIN bounded_copies c "
                                "ON 1 ORDER BY c.copy_id",
                                (source_ref, image_cursor, image_cursor, copy_cursor, image_cursor,
                                 copy_cursor, limit + 1, source_ref)).fetchall()
            uncertain, has_unknown = [], birth_qualified and self._global_horizon(head) >= birth.birth_epoch
            if birth_qualified:
                uncertainty_after = uncertainty_cursor or (-1, "")
                uncertainty_rows = conn.execute(
                    "WITH image_uncertainty AS MATERIALIZED (SELECT horizon,image_id FROM images "
                    "WHERE horizon>=? AND (horizon,image_id)>(?,?) ORDER BY horizon,image_id LIMIT ?),"
                    "scoped_uncertainty AS MATERIALIZED (SELECT horizon,image_id FROM uncertainties "
                    "WHERE horizon>=? AND (horizon,image_id)>(?,?) ORDER BY horizon,image_id LIMIT ?),"
                    "bounded AS MATERIALIZED (SELECT * FROM image_uncertainty UNION SELECT * FROM scoped_uncertainty "
                    "ORDER BY horizon,image_id LIMIT ?) SELECT b.horizon,b.image_id,"
                    "EXISTS(SELECT 1 FROM images WHERE horizon>=? LIMIT 1) OR "
                    "EXISTS(SELECT 1 FROM uncertainties WHERE horizon>=? LIMIT 1) AS has_unknown "
                    "FROM (SELECT 1) LEFT JOIN bounded b ON 1 ORDER BY b.horizon,b.image_id",
                    (birth.birth_epoch, *uncertainty_after, limit + 1, birth.birth_epoch,
                     *uncertainty_after, limit + 1, limit + 1, birth.birth_epoch, birth.birth_epoch),
                ).fetchall()
                uncertain = [(row[0], row[1]) for row in uncertainty_rows if row[0] is not None]
                has_unknown |= any(row[2] for row in uncertainty_rows)
            page, unknown_page = rows[:limit], uncertain[:limit]
            copies = tuple(Copy.model_validate_json(row[4]) for row in page if row[4] is not None)
            state = "ready"
            if not birth_qualified or has_unknown or any(not row[1] or row[2] >= birth.birth_epoch for row in page):
                state = "unknown"
            elif (len(rows) > limit or len(uncertain) > limit or any(row[5] for row in page)
                  or any(copy.outcome == "reserved" for copy in copies)):
                state = "pending"
            if self.head() != head:
                return Lookup("pending", head.catalogue_id, head.epoch)
            return Lookup(state, head.catalogue_id, head.epoch, copies,
                          tuple(dict.fromkeys(row[0] for row in page)), tuple(row[1] for row in unknown_page if row[1]),
                          (page[-1][0], page[-1][3]) if len(rows) > limit else
                          (page[-1][0], "") if page and page[-1][5] else None,
                          (unknown_page[-1][0], unknown_page[-1][1]) if len(uncertain) > limit else None)

    def collect(self, image_id: str, *, limit: int = 16) -> Progress:
        """Retire only closed native-copy witnesses, then bounded image pages."""
        _boundary(self.root)
        _limit(limit)
        self.reconcile(limit=limit)
        head = self.head()
        checkpoint = self.checkpoint()
        if not checkpoint["ready"] or checkpoint["catalogue_id"] != head.catalogue_id or checkpoint["applied_epoch"] != head.epoch:
            return Progress("pending", head.epoch)
        try:
            header = self._image(image_id)
        except FileNotFoundError:
            return Progress("missing", head.epoch)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM slot_refs WHERE image_id=? LIMIT 1", (image_id,)).fetchone():
                return Progress("pending", head.epoch)
            if conn.execute("SELECT 1 FROM uncertainties WHERE image_id='' OR image_id=? LIMIT 1", (image_id,)).fetchone():
                return Progress("unknown", head.epoch)
            if conn.execute("SELECT 1 FROM copies WHERE image_id=? AND closed=0 LIMIT 1", (image_id,)).fetchone():
                raise ValueError("generated image has outstanding copy obligations")
            closed = conn.execute("SELECT data FROM copies WHERE image_id=? LIMIT ?", (image_id, limit)).fetchall()
        processed = 0
        for row in closed:
            copy = Copy.model_validate_json(row[0])
            self._run("collect", ((ObjectChange(kind="copy", object_id=copy.copy_id, image_id=image_id,
                       expected_revision=copy.revision, remove=True), None),))
            processed += 1
        if processed:
            return Progress("pending", self.head().epoch, processed)
        if header.state != "collecting":
            value = header.model_copy(update={"state": "collecting", "revision": _id()})
            self._run("collect", ((ObjectChange(kind="image", object_id=image_id, image_id=image_id,
                       expected_revision=header.revision, after_revision=value.revision), value),))
            header = value
        for _ in range(min(limit, header.written_pages)):
            number = header.written_pages - 1
            item = ObjectChange(kind="page", object_id=image_id, image_id=image_id, page_number=number, remove=True)
            page, _ = self._read(self._object_path(item), ImagePage)
            item = item.model_copy(update={"expected_revision": page.revision if page is not None else None})
            value = header.model_copy(update={"written_pages": number,
                                             "last_page_hash": None if number == 0 else header.last_page_hash,
                                             "revision": _id()})
            self._run("collect", ((item, None), (ObjectChange(kind="image", object_id=image_id, image_id=image_id,
                       expected_revision=header.revision, after_revision=value.revision), value)))
            header = value
            processed += 1
        if header.written_pages == 0:
            self._run("collect", ((ObjectChange(kind="image", object_id=image_id, image_id=image_id,
                       expected_revision=header.revision, remove=True), None),))
            with self._connect() as conn:
                if conn.execute("SELECT 1 FROM uncertainties WHERE image_id=? LIMIT 1", (image_id,)).fetchone():
                    return Progress("unknown", self.head().epoch, processed + 1)
            return Progress("missing", self.head().epoch, processed + 1)
        return Progress("pending", self.head().epoch, processed)

    def _collect_changes(self, head: Head) -> int:
        """Remove a bounded prefix of retired change records; the settled head records how far it got."""
        collected = head.collected_through
        for epoch in range(head.collected_through + 1, min(head.change_floor, head.collected_through + TRIM_STEP + 1)):
            path = self.path / "changes" / f"{epoch}.json"
            with reserved_paths._subsystem_authority_scope(OWNER):
                try:
                    guard = vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="absent")
                except vault.PathGuardError:
                    guard = vault.PathGuard.capture(self.root, path.relative_to(self.root).as_posix(), leaf_policy="stable")
            self._remove(path, guard)
            collected = epoch
        return collected
