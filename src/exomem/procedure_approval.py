"""Approved procedures: the authenticated approvals ledger and its one verdict.

The owner endorses one exact version of a page. The endorsement is one entry file
under the reserved `_Approvals/<ref>/` tree. Only the owner-only `approve_procedure`
operation writes that tree, and each entry carries an HMAC made with a key kept in
the vault's state directory, outside the vault. A copied vault therefore carries entries that
fail their MAC and approves nothing on another host.

This module owns the entry format, the key, the crash-safe write, the eligibility
test, the verdict and the quarantine step. The operation and every serving surface
are thin adapters over it. The caller of a write function holds the vault mutation
boundary and the dispatcher's `approve_procedure` owner authority; the module reads
the latest entry of a ref and appends after it, so two writers of one ref must not
overlap.

The key lives in the vault's state directory, which follows the vault's path. A vault
that moves to another path gets a new key, so its entries read foreign, with one owner
finding per ref, until the owner approves each page again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import held_fs, memory_refs, reserved_paths, state_paths
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
LEDGER_DIRNAME = "_Approvals"
QUARANTINE_DIRNAME = "_quarantine"
KEY_FILENAME = "procedure-approval.key"
LEDGER_DESCRIPTOR = "procedure-approvals"
KEY_DESCRIPTOR = "procedure-approval-key"
KEY_OWNER = "procedure_approval.key"

#: The two entry actions are the closed protocol tokens of the entry format.
APPROVE = "approve"
REVOKE = "revoke"
ACTIONS = (APPROVE, REVOKE)

APPROVED = "approved"
STALE = "stale"
UNAPPROVED = "unapproved"

#: Finding kinds an owner can see for a ref.
FOREIGN = "foreign_or_tampered"
UNREADABLE = "unreadable"
UNAVAILABLE = "unavailable"

#: Why a page cannot be approved. Each names one test that the page failed.
NOT_COMPILED = "not_compiled"
NOT_LIVE = "not_live"
NO_METHOD_UNIT = "no_method_unit"
NO_STABLE_ID = "no_stable_id"
ID_UNRESOLVED = "id_unresolved"
DUPLICATE_ID = "duplicate_id"

_KEY_BYTES = 32
_ENTRY_LIMIT_BYTES = 8192
_LABEL_LIMIT = 256
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# These patterns check the shape of fields this module writes itself (hex digests and
# ids), never the meaning of a person's or a model's words.
_ENTRY_ID_SHAPE = re.compile(r"[0-9a-f]{32}")
_DIGEST_SHAPE = re.compile(r"[0-9a-f]{64}")
_FIELDS = frozenset(
    {"schema_version", "entry_id", "ref", "version", "action", "principal", "client", "recorded_at"}
)

#: Why a page fails each eligibility test and what to change, so a refusal names its own fix.
_INELIGIBLE = {
    NOT_COMPILED: (
        "This page is not a compiled note. Captured material stays a source and is never approved.",
        "Approve a compiled procedure page.",
    ),
    NOT_LIVE: (
        "The page has no live status.",
        "Set a live status with the ordinary editor, read the page again, then approve its new "
        "content_hash.",
    ),
    NO_METHOD_UNIT: (
        "The page has no method unit.",
        "Add a procedure or method block with the ordinary editor, read the page again, then "
        "approve its new content_hash.",
    ),
    NO_STABLE_ID: (
        "The page has no stable exomem_id.",
        "Run maintain_memory with mode backfill-ids and apply its plan, then approve the page by "
        "the ref it now holds.",
    ),
    ID_UNRESOLVED: (
        "No page holds this ref, or the page is not readable here.",
        "Check the ref in the page's frontmatter, then approve again.",
    ),
    DUPLICATE_ID: (
        "More than one page repeats this exomem_id.",
        "Give the copy a new exomem_id, then approve the page again.",
    ),
}

_UNAVAILABLE_FIX = (
    "Check the Approvals folder in the vault and the vault's state directory. If the key file is "
    "damaged, restore it from your backup. Without a backup, remove it and approve each page again."
)

# What the owner reads for each of the three finding kinds. They are closed and carry no customer vocabulary.
_FINDING_TEXT = {
    FOREIGN: (
        "Its approval entries do not verify with this host's approval key. They may come "
        "from another host, or someone changed them. Approve the page again to approve it "
        "on this host."
    ),
    UNREADABLE: (
        "An approval entry cannot be read, so the page reads unapproved. Approve the page "
        "again, or run the quarantine action for this ref, to move the file aside."
    ),
    UNAVAILABLE: (
        "The approvals ledger or the approval key cannot be read now, so the page reads "
        f"unapproved. {_UNAVAILABLE_FIX}"
    ),
}


class ApprovalRefused(Exception):
    """A write that refused before it changed anything. `code` names the cause, `remediation` the fix."""

    def __init__(
        self, code: str, message: str, remediation: str, *, condition: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.condition = condition


def _unavailable() -> ApprovalRefused:
    return ApprovalRefused(
        "ledger_unavailable",
        "The approvals ledger or the approval key cannot be read now, so nothing changed.",
        _UNAVAILABLE_FIX,
    )


@dataclass(frozen=True, slots=True)
class Entry:
    """One verified ledger entry."""

    entry_id: str
    ref: str
    version: str
    action: str
    principal: str
    client: str
    recorded_at: str

    def fields(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "entry_id": self.entry_id,
            "ref": self.ref,
            "version": self.version,
            "action": self.action,
            "principal": self.principal,
            "client": self.client,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """One owner-only observation about a ref's entries. It never carries the key."""

    ref: str
    kind: str
    entries: tuple[str, ...]
    message: str

    def row(self) -> dict[str, object]:
        """The one wire shape of a finding, for the listing and the owner's page read."""
        return {"ref": self.ref, "kind": self.kind, "message": self.message, "entries": list(self.entries)}


@dataclass(frozen=True, slots=True)
class RefLedger:
    """Everything the ledger holds for one ref, sorted into the three entry classes."""

    ref: str
    verified: tuple[Entry, ...] = ()
    foreign: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    unavailable: bool = False

    @property
    def latest(self) -> Entry | None:
        return self.verified[-1] if self.verified else None

    @property
    def held(self) -> bool:
        """Whether the ledger holds anything for this ref, or could not be read to say."""
        return bool(self.verified or self.foreign or self.unreadable or self.unavailable)

    @property
    def latest_approve(self) -> Entry | None:
        return next((entry for entry in reversed(self.verified) if entry.action == APPROVE), None)

    def findings(self) -> tuple[Finding, ...]:
        found: list[Finding] = []
        if self.unavailable:
            found.append(Finding(self.ref, UNAVAILABLE, (), _FINDING_TEXT[UNAVAILABLE]))
        if self.unreadable:
            found.append(Finding(self.ref, UNREADABLE, self.unreadable, _FINDING_TEXT[UNREADABLE]))
        if self.foreign:
            found.append(Finding(self.ref, FOREIGN, self.foreign, _FINDING_TEXT[FOREIGN]))
        return tuple(found)


@dataclass(frozen=True, slots=True)
class PageFacts:
    """What the eligibility test read from one page, and the version that it binds."""

    ref: str | None
    version: str
    eligible: bool
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """The one answer every surface reports. `entry` and `findings` reach the owner only."""

    value: str
    entry: Entry | None = None
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class Moved:
    """One entry file that the quarantine step moved aside."""

    ref: str
    name: str
    kind: str
    quarantined_as: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """What an `approve` or `revoke` did. `outcome` is a sentence to repeat as it stands."""

    action: str
    outcome: str
    appended: Entry | None
    existing: Entry | None
    quarantined: tuple[Moved, ...]
    #: The version of the latest verified `approve` entry before this call, or None.
    previous_approved_version: str | None


# --- paths and key -------------------------------------------------------------------


def key_path(vault_root: Path) -> Path:
    """The owner's HMAC key, in the vault's external state directory. No surface serves it."""
    return state_paths.vault_state_dir(Path(vault_root)) / KEY_FILENAME


def _ref_id(ref: str) -> str:
    identifier = memory_refs.parse_memory_ref(ref)
    if identifier is None:
        raise ValueError("approvals are kept per stable memory ref")
    return identifier


def ledger_root(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / LEDGER_DIRNAME


def entry_path(vault_root: Path, ref: str, entry_id: str) -> Path:
    return ledger_root(vault_root) / _ref_id(ref) / entry_id


def _relative(*parts: str) -> str:
    return "/".join((kb_dirname(), LEDGER_DIRNAME, *parts))


def _read_key(vault_root: Path) -> bytes | None:
    """The key, or None when this host holds none. A key that cannot be read raises OSError."""
    root = Path(vault_root)
    with reserved_paths._subsystem_authority_scope(KEY_OWNER):  # noqa: SLF001 - owner-bound read
        try:
            raw = reserved_paths._read_owner_bytes(  # noqa: SLF001
                root, key_path(root), KEY_DESCRIPTOR, limit=_KEY_BYTES
            )
        except FileNotFoundError:
            return None
    if len(raw) != _KEY_BYTES:
        raise OSError("approval key is damaged")
    return raw


def _ensure_key(vault_root: Path) -> bytes:
    """The key, generated on the first write with mode 0600 outside the vault."""
    root = Path(vault_root)
    try:
        key = _read_key(root)
    except OSError as error:
        raise ApprovalRefused(
            "key_unavailable",
            "The approval key is damaged or unreadable.",
            "Restore the key file from your backup. Without a backup, remove it from the vault's "
            "state directory and approve each page again; earlier entries read as foreign until then.",
        ) from error
    if key is not None:
        return key
    key = secrets.token_bytes(_KEY_BYTES)
    with reserved_paths._subsystem_authority_scope(KEY_OWNER):  # noqa: SLF001
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            root, key_path(root), KEY_DESCRIPTOR, key
        )
    return key


# --- entry format ----------------------------------------------------------------------


def is_version(value: object) -> bool:
    """Whether `value` has the shape of a page version: a SHA-256 digest in lowercase hex.

    An entry whose version lacks this shape is unreadable, so a writer checks it before it appends.
    """
    return isinstance(value, str) and _DIGEST_SHAPE.fullmatch(value) is not None


def _canonical(fields: Mapping[str, object]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _mac(key: bytes, fields: Mapping[str, object]) -> str:
    """The HMAC covers every field except the MAC, including the schema version and client."""
    return hmac.new(key, _canonical(fields), hashlib.sha256).hexdigest()


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed if _timestamp(parsed) == value else None


def _parse_entry(data: bytes) -> tuple[Entry, dict[str, object], str] | None:
    """The entry, its signed fields and its MAC, or None when the file is unreadable."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or set(document) != _FIELDS | {"mac"}:
        return None
    version = document["schema_version"]
    # A newer schema is unreadable here, never guessed at: it might be a revoke.
    if type(version) is not int or version != SCHEMA_VERSION:
        return None
    text = {name: document[name] for name in _FIELDS - {"schema_version"}}
    mac = document["mac"]
    if not all(isinstance(value, str) for value in (*text.values(), mac)):
        return None
    identifier = memory_refs.parse_memory_ref(text["ref"])
    if (
        identifier is None
        or memory_refs.memory_ref(identifier) != text["ref"]
        or not _ENTRY_ID_SHAPE.fullmatch(text["entry_id"])
        or not is_version(text["version"])
        or not _DIGEST_SHAPE.fullmatch(mac)
        # nosemgrep: ep-lexical-intent -- the closed entry actions this module writes; not a reading of meaning.
        or text["action"] not in ACTIONS
        or _parse_timestamp(text["recorded_at"]) is None
    ):
        return None
    entry = Entry(
        entry_id=text["entry_id"],
        ref=text["ref"],
        version=text["version"],
        action=text["action"],
        principal=text["principal"],
        client=text["client"],
        recorded_at=text["recorded_at"],
    )
    return entry, {name: document[name] for name in _FIELDS}, mac


# --- reading the ledger ------------------------------------------------------------------


class _Unavailable(Exception):
    """The ledger directory could not be read safely."""


def _list_entries(
    filesystem: held_fs.HeldFilesystem, directory_relative: str
) -> list[tuple[str, bytes | None]] | None:
    """Every candidate entry of one directory as (name, bytes), or None when it is missing.

    A leftover publish temporary is never an entry. An object that is not a plain
    single-link file reads as None bytes, which makes it unreadable instead of absent.
    """
    held = filesystem.parent(directory_relative)
    if not held.ok:
        if held.error is not None and held.error.code == "MISSING":
            return None
        raise _Unavailable
    items: list[tuple[str, bytes | None]] = []
    with held.require() as directory:
        try:
            names = sorted(
                name
                for name in filesystem.iter_names(directory)
                if not held_fs.is_publish_temporary(name)
            )
        except held_fs.HeldFsError as error:
            raise _Unavailable from error
        children = filesystem.children(directory)
        if not children.ok:
            raise _Unavailable
        records = {record.relative_path: record.identity for record in children.require()}
        for name in names:
            identity = records.get(name)
            if identity is not None and identity.kind == "directory":
                continue
            if identity is None or identity.link_count != 1:
                items.append((name, None))
                continue
            opened = filesystem.file(directory, name)
            if not opened.ok:
                items.append((name, None))
                continue
            with opened.require() as file:
                read = filesystem.read(file, max_bytes=_ENTRY_LIMIT_BYTES)
            items.append((name, read.require() if read.ok else None))
    return items


def _classify(ref: str, items: Iterable[tuple[str, bytes | None]], key: bytes | None) -> RefLedger:
    verified: list[Entry] = []
    foreign: list[str] = []
    unreadable: list[str] = []
    for name, data in items:
        parsed = _parse_entry(data) if data is not None else None
        # The entry's path names its ref and itself; content that disagrees is unreadable.
        if parsed is None or parsed[0].ref != ref or parsed[0].entry_id != name:
            unreadable.append(name)
            continue
        entry, fields, mac = parsed
        if key is not None and hmac.compare_digest(mac, _mac(key, fields)):
            verified.append(entry)
        else:
            foreign.append(name)
    verified.sort(key=lambda entry: (entry.recorded_at, entry.entry_id))
    return RefLedger(ref, tuple(verified), tuple(foreign), tuple(unreadable))


def ledgers(vault_root: Path, refs: Iterable[str]) -> dict[str, RefLedger]:
    """The ledger of each named ref, reading the key once and only when an entry exists.

    A missing tree gives empty ledgers. A ledger or key that cannot be read comes back
    `unavailable`, which every verdict reads as unapproved and never as the whole truth.
    """
    root = Path(vault_root)
    wanted = tuple(dict.fromkeys(refs))
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        return {ref: RefLedger(ref, unavailable=True) for ref in wanted}
    listed: dict[str, list[tuple[str, bytes | None]]] = {}
    unreadable_dirs: set[str] = set()
    with acquired.require() as filesystem:
        for ref in wanted:
            try:
                listed[ref] = _list_entries(filesystem, _relative(_ref_id(ref))) or []
            except _Unavailable:
                unreadable_dirs.add(ref)
    key: bytes | None = None
    key_failed = False
    if any(listed.values()):
        try:
            key = _read_key(root)
        except OSError:
            key_failed = True
    result: dict[str, RefLedger] = {}
    for ref in wanted:
        if ref in unreadable_dirs or (key_failed and listed[ref]):
            result[ref] = RefLedger(ref, unavailable=True)
        else:
            result[ref] = _classify(ref, listed[ref], key)
    return result


def ledger(vault_root: Path, ref: str) -> RefLedger:
    return ledgers(vault_root, (ref,))[ref]


def listed_refs(vault_root: Path) -> tuple[str, ...]:
    """Every ref that has a directory in the ledger. A missing tree lists nothing."""
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        raise _unavailable()
    with acquired.require() as filesystem:
        held = filesystem.parent(_relative())
        if not held.ok:
            if held.error is not None and held.error.code == "MISSING":
                return ()
            raise _unavailable()
        with held.require() as directory:
            children = filesystem.children(directory)
            if not children.ok:
                raise _unavailable()
            return tuple(
                memory_refs.memory_ref(record.relative_path)
                for record in children.require()
                if record.identity.kind == "directory"
                and memory_refs.normalize_id(record.relative_path) == record.relative_path
            )


# --- eligibility and verdict ---------------------------------------------------------------


def page_ref(frontmatter: Mapping[str, object]) -> str | None:
    """The stable ref that a page's frontmatter names, or None when it names none."""
    identifier = memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD))
    return memory_refs.memory_ref(identifier) if identifier else None


def page_facts(
    vault_root: Path,
    rel_path: str,
    data: bytes,
    *,
    holders: Sequence[str] | None = None,
) -> PageFacts:
    """Whether the page at `rel_path` may be approved, and the version that an entry binds.

    Eligible means the compiled note-type role, status class live, at least one method
    unit, and a stable `exomem_id` that resolves to this one page. The test reads those
    facts and never the note type's key. `holders` is every page that holds the id; the
    default resolves it from the reference index.
    """
    from . import context_roles, semantic_contract

    root = Path(vault_root)
    version = hashlib.sha256(data).hexdigest()
    state = semantic_contract.build_page_state(root, rel_path, data.decode("utf-8", "replace"))
    ref = memory_refs.memory_ref(state.identity) if state.identity_kind == "exomem_id" else None
    if not state.structurally_compiled:
        return PageFacts(ref, version, False, NOT_COMPILED)
    if not state.eligible_compiled:
        return PageFacts(ref, version, False, NOT_LIVE)
    categories = context_roles.projection_categories(context_roles.load_roles(root), "method")
    if not any(unit.category in categories for unit in state.document.units):
        return PageFacts(ref, version, False, NO_METHOD_UNIT)
    if ref is None:
        return PageFacts(None, version, False, NO_STABLE_ID)
    if holders is None:
        holders = memory_refs.paths_for_ids_read_only(root, [state.identity]).get(
            state.identity, ()
        )
    if len(holders) > 1:
        return PageFacts(ref, version, False, DUPLICATE_ID)
    if tuple(holders) != (rel_path,):
        return PageFacts(ref, version, False, ID_UNRESOLVED)
    return PageFacts(ref, version, True)


def owner_view(vault_root: Path, who: object) -> bool:
    """Whether `who` gets the owner's view of the ledger: `stale`, entries, findings and every action.

    The owner audience alone is not enough. An owner connection that a connector boundary fences
    from some scopes reads the vault as a restricted reader, so it must not approve its own edited
    text, revoke or move entries of pages it cannot see, or list their refs.
    """
    from .governance import egress, raw_protection

    return raw_protection.is_owner(who) and egress.unrestricted_content_access(Path(vault_root), who)


def verdict(
    facts: PageFacts | None,
    ref_ledger: RefLedger | None,
    *,
    owner: bool,
    full_release: bool,
) -> Verdict:
    """The one verdict function. Every surface calls it and reports its value as it stands.

    `stale`, the entry and the findings reach the owner only; anyone else sees `approved`
    or `unapproved`. A reader whose release of the page is below FULL sees `unapproved`.
    """
    see_ledger = owner and full_release
    findings = ref_ledger.findings() if ref_ledger is not None and owner else ()
    if facts is None or not facts.eligible or not full_release:
        return Verdict(UNAPPROVED, findings=findings)
    if ref_ledger is None:
        return Verdict(UNAPPROVED)
    # An unreadable or unavailable ledger could hide a revoke, so it fails its own ref.
    if ref_ledger.unreadable or ref_ledger.unavailable:
        return Verdict(UNAPPROVED, findings=findings)
    latest = ref_ledger.latest
    entry = latest if see_ledger else None
    if latest is None or latest.action != APPROVE:
        return Verdict(UNAPPROVED, entry, findings)
    if latest.version == facts.version:
        return Verdict(APPROVED, entry, findings)
    return Verdict(STALE if owner else UNAPPROVED, entry, findings)


def served_verdict(
    vault_root: Path,
    rel_path: str,
    ref_ledger: RefLedger | None,
    *,
    owner: bool,
    full_release: bool,
    read: Callable[[], bytes],
    holders: Callable[[], Sequence[str]],
) -> Verdict:
    """The verdict a read surface reports, reading the page and its holders only when they decide it.

    The page's bytes and holders matter only when the latest verified entry is an `approve` entry
    that a reader may see. Every other case reads `unapproved` whatever the page is, so it gives the
    value and findings of `verdict` without the page's facts. A serving surface passes `read` and
    `holders` as callables, and a page with no approval entry costs one ledger read and no more.
    """
    if ref_ledger is None or not full_release or ref_ledger.unreadable or ref_ledger.unavailable:
        return verdict(None, ref_ledger, owner=owner, full_release=full_release)
    latest = ref_ledger.latest
    if latest is None or latest.action != APPROVE:
        # As in `verdict`, the owner still sees the latest entry, here a revoke.
        return Verdict(UNAPPROVED, latest if owner else None, ref_ledger.findings() if owner else ())
    facts = page_facts(vault_root, rel_path, read(), holders=holders())
    return verdict(facts, ref_ledger, owner=owner, full_release=full_release)


# --- writing -----------------------------------------------------------------------------------


def _require_owner_authority() -> None:
    if not reserved_paths.owner_authorized(LEDGER_DESCRIPTOR):
        raise RuntimeError("approvals are written only under the approve_procedure authority")


def _committed() -> None:
    """Tell the dispatcher that a durable ledger change happened, so the result is a committed one."""
    from .writer_lease import mark_active_mutation_committed

    mark_active_mutation_committed()


def _quarantine_ref(vault_root: Path, ref: str, *, include_foreign: bool) -> tuple[Moved, ...]:
    """Move one ref's unreadable entry files, and its foreign ones when asked, aside."""
    root = Path(vault_root)
    current = ledger(root, ref)
    if current.unavailable:
        raise _unavailable()
    wanted = [(name, UNREADABLE) for name in current.unreadable]
    if include_foreign:
        wanted += [(name, FOREIGN) for name in current.foreign]
    moved: list[Moved] = []
    identifier = _ref_id(ref)
    for name, kind in wanted:
        destination_name = name
        destination = ledger_root(root) / QUARANTINE_DIRNAME / identifier / destination_name
        while destination.exists():
            destination_name = f"{name}-{secrets.token_hex(4)}"
            destination = destination.with_name(destination_name)
        try:
            reserved_paths._move_owner_file(  # noqa: SLF001 - owner-bound move
                root,
                entry_path(root, ref, name),
                LEDGER_DESCRIPTOR,
                destination,
                LEDGER_DESCRIPTOR,
                replace=False,
            )
        except (OSError, RuntimeError) as error:
            raise ApprovalRefused(
                "quarantine_failed",
                f"An approval entry could not be moved aside ({name}).",
                "Remove that file from the ledger by hand, then try again.",
            ) from error
        _committed()
        moved.append(Moved(ref, name, kind, destination_name))
    return tuple(moved)


def quarantine(vault_root: Path, ref: str | None = None) -> tuple[Moved, ...]:
    """Move unreadable entry files aside without appending anything.

    With no ref it covers every ref and moves unreadable files only. With a ref it also
    moves that ref's foreign or tampered entries. A quarantined file is never read again.
    """
    _require_owner_authority()
    if ref is not None:
        return _quarantine_ref(vault_root, ref, include_foreign=True)
    refs = listed_refs(vault_root)
    moved: list[Moved] = []
    for each in refs:
        moved.extend(_quarantine_ref(vault_root, each, include_foreign=False))
    return tuple(moved)


def _append(
    vault_root: Path,
    ref: str,
    action: str,
    version: str,
    principal: str,
    client: str,
    *,
    previous: RefLedger,
    now: datetime | None,
) -> Entry:
    root = Path(vault_root)
    key = _ensure_key(root)
    moment = now or datetime.now(UTC)
    # A writer time later than every verified entry of the ref, so a slow clock
    # cannot order a new entry before an old one.
    latest = previous.latest
    if latest is not None:
        floor = _parse_timestamp(latest.recorded_at)
        if floor is not None and moment <= floor:
            moment = floor + timedelta(microseconds=1)
    entry = Entry(
        entry_id=uuid.uuid4().hex,
        ref=ref,
        version=version,
        action=action,
        principal=principal[:_LABEL_LIMIT],
        client=client[:_LABEL_LIMIT],
        recorded_at=_timestamp(moment),
    )
    fields = entry.fields()
    data = _canonical({**fields, "mac": _mac(key, fields)}) + b"\n"
    # The staged temporary is renamed into place in one step, so a crash leaves the
    # whole entry or none; the rename is the receipt.
    reserved_paths._publish_owner_bytes(  # noqa: SLF001 - owner-bound create
        root, entry_path(root, ref, entry.entry_id), LEDGER_DESCRIPTOR, data
    )
    _committed()
    return entry


def approve(
    vault_root: Path,
    ref: str,
    facts: PageFacts | None,
    *,
    expected_version: str,
    principal: str,
    client: str,
    now: datetime | None = None,
) -> Outcome:
    """Approve one exact page version: guards, then the quarantine step, then the repeat test.

    A refused guard changes nothing. A call that repeats the latest verified entry's
    action and version appends nothing and reports that entry.
    """
    _require_owner_authority()
    _ref_id(ref)
    if facts is None or not facts.eligible or facts.ref != ref:
        # No facts means that no page holds the ref.
        condition = facts.condition if facts is not None and facts.ref == ref else ID_UNRESOLVED
        problem, fix = _INELIGIBLE[condition]
        raise ApprovalRefused("ineligible", problem, fix, condition=condition)
    if expected_version != facts.version:
        raise ApprovalRefused(
            "stale_version",
            "The page changed since it was read.",
            "Read the page again, then approve its current content_hash.",
        )
    moved = _quarantine_ref(vault_root, ref, include_foreign=False)
    current = ledger(vault_root, ref)
    if current.unavailable:
        raise _unavailable()
    previous = current.latest_approve
    latest = current.latest
    if latest is not None and latest.action == APPROVE and latest.version == facts.version:
        return Outcome(
            action=APPROVE,
            outcome="This version is already approved. Nothing was added.",
            appended=None,
            existing=latest,
            quarantined=moved,
            previous_approved_version=latest.version,
        )
    entry = _append(
        vault_root, ref, APPROVE, facts.version, principal, client, previous=current, now=now
    )
    return Outcome(
        action=APPROVE,
        outcome="Approved this version of the procedure.",
        appended=entry,
        existing=None,
        quarantined=moved,
        previous_approved_version=previous.version if previous else None,
    )


def revoke(
    vault_root: Path,
    ref: str,
    *,
    current_version: str | None,
    expected_version: str | None,
    principal: str,
    client: str,
    now: datetime | None = None,
) -> Outcome:
    """Withdraw an approval. It works for a retired or deleted page.

    `current_version` is the page's version now, or None when no page holds the ref.
    When `expected_version` is given and a page exists, a mismatch refuses first.
    """
    _require_owner_authority()
    _ref_id(ref)
    if (
        expected_version is not None
        and current_version is not None
        and expected_version != current_version
    ):
        raise ApprovalRefused(
            "stale_version",
            "The page changed since it was read.",
            "Read the page again, then revoke with its current content_hash.",
        )
    moved = _quarantine_ref(vault_root, ref, include_foreign=False)
    current = ledger(vault_root, ref)
    if current.unavailable:
        raise _unavailable()
    approved = current.latest_approve
    if approved is None:
        return Outcome(
            action=REVOKE,
            outcome="No approval exists for this procedure. Nothing was added.",
            appended=None,
            existing=None,
            quarantined=moved,
            previous_approved_version=None,
        )
    withdrawn = expected_version or approved.version
    latest = current.latest
    if latest is not None and latest.action == REVOKE and latest.version == withdrawn:
        return Outcome(
            action=REVOKE,
            outcome="This approval is already revoked. Nothing was added.",
            appended=None,
            existing=latest,
            quarantined=moved,
            previous_approved_version=approved.version,
        )
    entry = _append(
        vault_root, ref, REVOKE, withdrawn, principal, client, previous=current, now=now
    )
    return Outcome(
        action=REVOKE,
        outcome="Revoked the approval. The page stays readable as a reference.",
        appended=entry,
        existing=None,
        quarantined=moved,
        previous_approved_version=approved.version,
    )
