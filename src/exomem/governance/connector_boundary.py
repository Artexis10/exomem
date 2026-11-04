"""Host-owned connector ceilings over the existing canonical Scope membership."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import activation_manifest, find_corpus, reserved_paths, state_migration, state_paths
from .. import structured_collections as collections
from ..kbdir import kb_dirname
from . import companions, membership, policy
from .decisions import Decision, decide
from .principal import OWNER_AUDIENCE, ClientBinding, RequestPrincipal

CONFIG_ENV = "EXOMEM_CONNECTOR_BOUNDARY_CONFIG"
COMPATIBILITY_ID = state_migration.CONNECTOR_BOUNDARY_COMPATIBILITY_ID
DESCRIPTOR_ID = reserved_paths.CONNECTOR_BOUNDARY_DESCRIPTOR_ID
# This is a parser resource bound, not a limit on customer vocabulary.
_MAX_CONFIG_BYTES = 1_048_576


class BoundaryUnavailable(ValueError):
    """Invalid security configuration; the host must repair its configuration."""

    def __init__(self) -> None:
        super().__init__("connector content boundary is unavailable")


@dataclass(frozen=True, slots=True)
class Snapshot:
    revision: str
    default_denied_scope_ids: frozenset[str]
    clients: tuple[tuple[ClientBinding, frozenset[str]], ...]
    capture_paths: tuple[str, ...]
    vocabulary: dict | None = None
    writable: tuple[tuple[ClientBinding, frozenset[str]], ...] = ()

    def denied_scopes(self, who: RequestPrincipal) -> frozenset[str]:
        if who.administrative_ingress:
            return frozenset()
        if who.resolved and who.client_binding is not None and who.origin_session is not None:
            for binding, denied in self.clients:
                if binding == who.client_binding:
                    return denied
        return self.default_denied_scope_ids

    def writable_scopes(self, who: RequestPrincipal) -> frozenset[str]:
        """The scopes the matched client entry lets `who` write; none without an entry."""
        if who.resolved and who.client_binding is not None and who.origin_session is not None:
            for binding, writable in self.writable:
                if binding == who.client_binding:
                    return writable
        return frozenset()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BoundaryUnavailable()
        result[key] = value
    return result


def _scope_ids(value: object, compiled: policy.Policy) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BoundaryUnavailable()
    ids = frozenset(value)
    if len(ids) != len(value) or not ids <= compiled.scopes.keys():
        raise BoundaryUnavailable()
    return ids


def _capture_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
        raise BoundaryUnavailable()
    for path in value:
        logical = PurePosixPath(path)
        if (not logical.parts or logical.is_absolute() or logical.as_posix() != path
                or ".." in logical.parts or "\\" in path
                or reserved_paths.classify_logical(path).disposition is not reserved_paths.PathDisposition.ORDINARY):
            raise BoundaryUnavailable()
    if len({collections._portable_path_key(path) for path in value}) != len(value):
        raise BoundaryUnavailable()
    return tuple(sorted(value))


_FileSignature = tuple[int, int, int, int, int] | None
# Keyed by vault and maintenance mode. An entry is reused only while every input
# keeps its stat signature: the configuration file, the requirement, the state
# manifest and the compiled policy. A refusal is never cached; it is re-decided
# each call. Least recently used entries go first, as in the governance store's
# sidecar cache: a service needs two entries for its one vault, and 64 keeps a
# multi-vault process warm without one entry for every vault root it ever saw.
_SNAPSHOTS_MAX = 64
_SNAPSHOTS: OrderedDict[tuple[str, bool], tuple[tuple, Snapshot | None]] = OrderedDict()
_SNAPSHOTS_LOCK = threading.Lock()
# Where the state manifest lives is placement: a function of the vault path and
# the placement environment, both in the key. Only its contents are evidence,
# and those are re-checked by stat signature on every call.
_MANIFEST_PATHS: dict[tuple, Path] = {}


def _file_signature(path: Path, *, follow: bool = True) -> _FileSignature:
    try:
        found = path.stat() if follow else path.lstat()
    except FileNotFoundError:
        return None
    # Size, both times, device and inode: an atomic replace changes the inode,
    # an in-place rewrite changes ctime, which no caller can set back.
    return (found.st_mtime_ns, found.st_ctime_ns, found.st_size, found.st_dev, found.st_ino)


def _manifest_location(root: Path) -> Path:
    key = (str(root), state_paths._placement_environment())
    found = _MANIFEST_PATHS.get(key)
    if found is None:
        found = state_migration._manifest_path(state_paths.vault_state_dir(root))
        _MANIFEST_PATHS[key] = found
    return found


def _input_signature(root: Path, configured: str) -> tuple:
    """Every input `_read_snapshot` decides from, without reading any of them."""
    config = _file_signature(Path(configured)) if configured else None
    requirement = _file_signature(Path(root) / requirement_relative_path(), follow=False)
    manifest = _file_signature(_manifest_location(root))
    fingerprint = policy.load(root).fingerprint if configured else None
    return (configured, config, requirement, manifest, fingerprint)


def snapshot(root: Path, compiled: policy.Policy | None = None, *, maintenance: bool = False) -> Snapshot | None:
    """Read current host configuration; never infer authority from vault content.

    Invalid security state withholds content until the host repairs it. This
    costs connector availability, rather than exposing the protected corpus.
    Content admission asks once per path, so an unchanged answer is reused.
    """
    configured = os.environ.get(CONFIG_ENV, "").strip()
    try:
        fingerprint, current = state_paths.request_memo(
            "connector_boundary.snapshot", (str(root), maintenance, configured),
            lambda: _current_snapshot(root, configured, maintenance),
        )
    except (OSError, ValueError) as error:
        if isinstance(error, BoundaryUnavailable):
            raise
        return _read_snapshot(root, compiled, maintenance=maintenance)
    if compiled is not None and configured and compiled.fingerprint != fingerprint:
        raise BoundaryUnavailable()
    return current


def _current_snapshot(
    root: Path, configured: str, maintenance: bool,
) -> tuple[str | None, Snapshot | None]:
    signature = _input_signature(root, configured)
    key = (str(root), maintenance)
    with _SNAPSHOTS_LOCK:
        cached = _SNAPSHOTS.get(key)
    if cached is None or cached[0] != signature:
        cached = (signature, _read_snapshot(root, None, maintenance=maintenance))
    with _SNAPSHOTS_LOCK:
        _SNAPSHOTS[key] = cached
        _SNAPSHOTS.move_to_end(key)
        while len(_SNAPSHOTS) > _SNAPSHOTS_MAX:
            _SNAPSHOTS.popitem(last=False)
    return signature[-1], cached[1]


def _read_snapshot(
    root: Path, compiled: policy.Policy | None, *, maintenance: bool,
) -> Snapshot | None:
    configured = os.environ.get(CONFIG_ENV, "").strip()
    required = read_requirement(root)
    enrolled = COMPATIBILITY_ID in (state_migration.recorded_descriptor_ids(root) or ())
    if not configured:
        if required is not None or enrolled:
            raise BoundaryUnavailable()
        return None
    if enrolled and required is None and not maintenance:
        raise BoundaryUnavailable()
    try:
        path = Path(configured)
        if not path.is_absolute() or path.resolve().is_relative_to(Path(root).resolve()):
            raise BoundaryUnavailable()
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise BoundaryUnavailable()
        data = json.loads(raw, object_pairs_hook=_object)
        # Version and field names are the closed configuration protocol.
        if (not isinstance(data, dict) or not {"version", "default_denied_scope_ids", "clients"} <= set(data)
                or set(data) - {"version", "default_denied_scope_ids", "clients", "capture_paths", "vocabulary"}):
            raise BoundaryUnavailable()
        if type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["clients"], list):
            raise BoundaryUnavailable()
        active = policy.load(root)
        if compiled is not None and compiled.fingerprint != active.fingerprint:
            raise BoundaryUnavailable()
        compiled = active
        if compiled.blocked:
            raise BoundaryUnavailable()
        default = _scope_ids(data["default_denied_scope_ids"], compiled)
        capture_paths = _capture_paths(data.get("capture_paths", []))
        vocabulary = data.get("vocabulary", (required or {}).get("vocabulary"))
        if vocabulary is not None:
            from ..vocabulary import instances

            instances.validate(vocabulary, compiled)
            if not set(vocabulary["private"]) <= default:
                raise BoundaryUnavailable()
        if not default or required is not None and not set(required["protected_scopes"]) <= default:
            raise BoundaryUnavailable()
        residual = compiled.residual_scope_id
        if residual is not None and residual not in default:
            raise BoundaryUnavailable()
        if required is not None and not maintenance:
            active = policy.protective_scope_documents(compiled, frozenset(required["protected_scopes"]))
            if active != required["protected_scopes"]:
                raise BoundaryUnavailable()
            if set(required["protected_scopes"]) != default or list(capture_paths) != required["capture_paths"]:
                raise BoundaryUnavailable()
        clients = []
        writable_by_client = []
        seen = set()
        for item in data["clients"]:
            # nosemgrep: ep-word-set -- the closed field names of the host configuration schema; `writable_scope_ids` is optional.
            if (not isinstance(item, dict) or not {"issuer", "client_id", "denied_scope_ids"} <= set(item)
                    or set(item) - {"issuer", "client_id", "denied_scope_ids", "writable_scope_ids"}):
                raise BoundaryUnavailable()
            binding = ClientBinding(item["issuer"], item["client_id"])
            denied = _scope_ids(item["denied_scope_ids"], compiled)
            writable = _scope_ids(item.get("writable_scope_ids", []), compiled)
            if binding in seen or not denied <= default:
                raise BoundaryUnavailable()
            # A writable scope is exclusive to its client: it is outside the client's own denied set,
            # on a fenced client, never the residual scope, and denied to the default context. A writer
            # needs a default-closed vault: with no residual scope an unclassified page is open, and a
            # workflow record that names the writer's inbox pages would land in it.
            if writable and (residual is None or not denied or writable & denied or residual in writable
                             or not writable <= default):
                raise BoundaryUnavailable()
            seen.add(binding)
            clients.append((binding, denied))
            writable_by_client.append((binding, writable))
        for binding, writable in writable_by_client:
            for other, other_denied in clients:
                if other != binding and other_denied and not writable <= other_denied:
                    raise BoundaryUnavailable()
        return Snapshot(hashlib.sha256(raw + compiled.fingerprint.encode()).hexdigest(), default, tuple(clients),
                        capture_paths, vocabulary, tuple(writable_by_client))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BoundaryUnavailable() from error


def cache_identity(root: Path, who: RequestPrincipal, compiled: policy.Policy | None = None) -> tuple:
    current = snapshot(root, compiled)
    return (who.client_binding, who.origin_session, who.administrative_ingress,
            current.revision if current is not None else None)


def admission_identity(root: Path, who: RequestPrincipal, compiled: policy.Policy | None = None) -> tuple:
    """Name the denied scopes, configuration revision and registry assignment.

    `cache_identity` closes over the whole principal, one value per login. The
    floors read the principal only through `Snapshot.denied_scopes`, so callers
    denied the same scopes share verdicts under the same assignment.
    """
    current = snapshot(root, compiled)
    requirement = read_requirement(root)
    # Registry assignments can move an unchanged page into a different scope.
    assignment = json.dumps((requirement or {}).get("vocabulary"), sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
    if current is None:
        return (frozenset(), None, assignment)
    return (current.denied_scopes(who), current.revision, assignment)


def unrestricted(root: Path, who: RequestPrincipal) -> bool:
    try:
        current = snapshot(root)
        return current is None or not current.denied_scopes(who)
    except BoundaryUnavailable:
        return False


def require_global_observation(root: Path) -> None:
    """Global usage checks cannot expose hidden contributors through save outcomes.

    A limited owner loses this operation until an admitted projection exists;
    ordinary owner identity and point-write authority remain separate.
    """
    from .principal import effective_principal

    if not unrestricted(root, effective_principal()):
        raise ValueError("GOVERNANCE_OPERATION_UNAVAILABLE: global observation is unavailable")


def decide_scopes(root: Path, who: RequestPrincipal, scope_ids, *, compiled: policy.Policy,
                   mutation: bool = False, path: str | None = None) -> Decision:
    try:
        current = snapshot(root, compiled)
        denied = current.denied_scopes(who) if current is not None else frozenset()
        if (current is not None and mutation and path is not None and _capture_destination(current, path)
                and frozenset(scope_ids) != membership.evaluate_path_selectors(Path(root), path, compiled)):
            # A capture page keeps exactly the membership its path gives.
            return Decision(policy.DISCLOSURE_MIN)
        return Decision(policy.DISCLOSURE_MIN if denied.intersection(scope_ids) else policy.DISCLOSURE_MAX)
    except (BoundaryUnavailable, membership.MembershipUnresolved, OSError):
        return Decision(policy.DISCLOSURE_MIN)


#: An admission that cannot be decided withholds the path.
_ADMISSION_REFUSALS = (BoundaryUnavailable, membership.MembershipUnresolved, OSError,
                       reserved_paths.ReservedPathLeafError)


def permits(root: Path, path: str, who: RequestPrincipal, *, content: bytes | None = None, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> bool:
    """Admit a named file through the shared membership kernel before disclosure."""
    try:
        root = Path(root)
        current = snapshot(root)
        denied = current.denied_scopes(who) if current is not None else frozenset()
        return _permits_denied(root, path, denied, who, content=content, artifact_copy=artifact_copy)
    except _ADMISSION_REFUSALS:
        return False


def permits_filter(root: Path, who: RequestPrincipal) -> Callable[[str], bool]:
    """`permits` for each path of one walk.

    Inside a request scope every snapshot read returns the request's pinned
    answer (`state_paths.request_memo`), so the filter reads it once instead of
    once per admitted path, a cost that grew with the corpus. Outside that scope,
    including after it ends, each path reads the live host configuration, so an
    existing filter follows a change.
    """
    root = Path(root)
    scope = state_paths.request_memo_scope()
    if scope is None:
        return lambda path: permits(root, path, who)
    try:
        current = snapshot(root)
        denied: frozenset[str] | None = current.denied_scopes(who) if current is not None else frozenset()
    except _ADMISSION_REFUSALS:
        denied = None

    def admit(path: str) -> bool:
        if state_paths.request_memo_scope() is not scope:
            return permits(root, path, who)
        if denied is None:
            return False
        try:
            return _permits_denied(root, path, denied, who)
        except _ADMISSION_REFUSALS:
            return False

    return admit


def _permits_denied(root: Path, path: str, denied: frozenset[str], who: RequestPrincipal,
                    *, content: bytes | None = None, artifact_copy: reserved_paths.GenericFileCopy | None = None) -> bool:
    if not denied:
        return True
    if root / path == activation_manifest.manifest_path(root):
        return False
    compiled = policy.load(root)
    return _permits_membership(root, path, denied, compiled, content=content, who=who, artifact_copy=artifact_copy)


def _membership_sets(root: Path, path: str, compiled: policy.Policy,
                     *, content: bytes | None = None, who: RequestPrincipal | None = None,
                     proposed_companion: companions.BoundCompanion | None = None,
                     artifact_copy: reserved_paths.GenericFileCopy | None = None) -> tuple[frozenset[str], ...]:
    """The scope set of each subject that `path` names; raises when membership cannot be decided."""
    from ..collection_store.preview import bound_writer

    writer = bound_writer(root)
    if writer is not None and content is None and proposed_companion is None and artifact_copy is None:
        with writer.read_snapshot():
            operation = writer._operation
            if who != operation.who:
                raise BoundaryUnavailable()
            targets = operation.projection_subjects(path)
            if targets is not None:
                return tuple(membership.evaluate_metadata(target.basis.subject, compiled) for target in targets)
    if path.lower().endswith(".md") and artifact_copy is None:
        if content is None and _unwritten_capture_slot(root, path, who):
            # The capture invariant gives every page there the membership of its path, so a name no page holds yet has it too.
            return (membership.evaluate_path_selectors(root, path, compiled),)
        raw = content if content is not None else reserved_paths.read_generic_bytes(root, path).data
        page = find_corpus.parse_page(root / path, 0, root, content=raw)
        if page is None:
            raise BoundaryUnavailable()
        return (membership.evaluate_snapshot(page, compiled, content_hash=hashlib.sha256(raw).hexdigest()),)
    return (membership.evaluate_path_only(root, path, compiled, proposed_companion=proposed_companion,
                                          artifact_copy=artifact_copy).require_classified(),)


def _permits_membership(root: Path, path: str, denied: frozenset[str], compiled: policy.Policy,
                        *, content: bytes | None = None, who: RequestPrincipal | None = None,
                        proposed_companion: companions.BoundCompanion | None = None,
                        artifact_copy: reserved_paths.GenericFileCopy | None = None) -> bool:
    try:
        sets = _membership_sets(root, path, compiled, content=content, who=who,
                                proposed_companion=proposed_companion, artifact_copy=artifact_copy)
    except _ADMISSION_REFUSALS:
        return False
    return bool(sets) and all(not denied.intersection(scopes) for scopes in sets)


def _unwritten_capture_slot(root: Path, path: str, who: RequestPrincipal | None) -> bool:
    """Whether `path` is a free slot in this restricted writer's own inbox."""
    if who is None:
        return False
    ceiling = write_ceiling(root, who)
    return (ceiling is not None and _in_inbox(root, path, ceiling)
            and not os.path.lexists(Path(root) / path))


def _capture_destination(current: Snapshot, path: str) -> str | None:
    portable = collections._portable_path_key(path)
    for folder in current.capture_paths:
        key = collections._portable_path_key(folder)
        if portable == key or portable.startswith(f"{key}/"):
            return folder
    return None


@dataclass(frozen=True, slots=True)
class WriteCeiling:
    """What one restricted writer may write, with the host state that decides it."""

    who: RequestPrincipal
    writable: frozenset[str]
    current: Snapshot | None
    compiled: policy.Policy


def write_ceiling(root: Path, who: RequestPrincipal) -> WriteCeiling | None:
    """The one definition of a restricted writer: `None` when the boundary does not confine `who`.

    A restricted writer is a non-owner under a non-empty policy, or any caller,
    the owner included, whose connector entry denies a scope. Administrative ingress, an
    unfenced client and every caller on a vault with an empty policy and no connector
    configuration are not. Writable scopes come only from the host configuration.
    """
    if who.administrative_ingress:
        return None
    current = snapshot(root)
    non_owner = who.audience_id != OWNER_AUDIENCE
    if current is None and not non_owner:
        return None
    compiled = policy.load(root)
    fenced = current is not None and bool(current.denied_scopes(who))
    if not fenced and (compiled.empty or not non_owner):
        return None
    return WriteCeiling(who, current.writable_scopes(who) if current is not None else frozenset(), current, compiled)


def _writable(ceiling: WriteCeiling, scopes: frozenset[str]) -> bool:
    """A scope set that is non-empty, inside the writable scopes, and read in full by the writer."""
    who = ceiling.who
    return (bool(scopes) and scopes <= ceiling.writable and who.resolved and not ceiling.compiled.blocked
            and decide(scopes, audience=who.audience_id, policy=ceiling.compiled).level >= policy.DISCLOSURE_MAX)


def _in_inbox(root: Path, path: str, ceiling: WriteCeiling, *, directory: bool = False) -> bool:
    """Whether `path` lies in a capture path whose own membership the writer may write."""
    current = ceiling.current
    if current is None or read_requirement(root) is None:
        return False
    folder = _capture_destination(current, path)
    if folder is None or not (path.startswith(f"{folder}/") or directory and path == folder):
        return False
    probe = f"{path}/x" if directory else path  # a directory is judged by a leaf it would hold
    try:
        return _writable(ceiling, membership.evaluate_path_selectors(Path(root), probe, ceiling.compiled))
    except _ADMISSION_REFUSALS:
        return False


def _is_writable_page(root: Path, path: str, ceiling: WriteCeiling) -> bool:
    """Whether `path` is an existing page that the writer reads in full inside its writable scopes."""
    try:
        if not (Path(root) / path).exists():
            return False
        sets = _membership_sets(Path(root), path, ceiling.compiled, who=ceiling.who)
    except _ADMISSION_REFUSALS:
        return False
    return bool(sets) and all(_writable(ceiling, scopes) for scopes in sets)


def _target_admitted(root: Path, path: str, ceiling: WriteCeiling, *, existing: bool, directory: bool = False) -> bool:
    if Path(root) / path == activation_manifest.manifest_path(root) or policy.is_governance_path(path):
        return False
    return _in_inbox(root, path, ceiling, directory=directory) or (existing and _is_writable_page(root, path, ceiling))


def _require_target(root: Path, path: str, *, existing: bool, directory: bool = False) -> None:
    from .principal import effective_principal

    try:
        ceiling = write_ceiling(root, effective_principal())
        if ceiling is None or _target_admitted(root, path, ceiling, existing=existing, directory=directory):
            return
    except BoundaryUnavailable:
        pass
    # One answer for a withheld, an absent and a read-only target, decided from scope sets alone.
    raise ValueError("WRITE_REFUSED: target is unavailable")


def require_create(root: Path, path: str, *, replace_existing: bool = False, directory: bool = False) -> None:
    """Decide destination eligibility before revealing existence or allocating names."""
    _require_target(root, path, existing=replace_existing, directory=directory)


def effect_admission(root: Path) -> Callable[[str | Path], bool]:
    """Decide where a door's side effects may land, once for one plan.

    The returned test takes a page, vault-relative or absolute. A restricted writer's effect
    lands only inside its write ceiling and is skipped, neither written nor refused,
    anywhere else; an unavailable boundary skips every effect. Every other caller's effects
    all land. A door that plans an effect on another page (an activity log or index entry, a
    back-reference, a link rewrite) asks this one test and plans nothing when it says no.
    """
    from .principal import effective_principal

    root = Path(root)

    def relative(page: str | Path) -> str | None:
        try:
            return Path(page).relative_to(root).as_posix() if Path(page).is_absolute() else str(page)
        except ValueError:
            return None

    try:
        ceiling = write_ceiling(root, effective_principal())
    except BoundaryUnavailable:
        return lambda page: False
    if ceiling is None:
        return lambda page: True
    return lambda page: (rel := relative(page)) is not None and _target_admitted(root, rel, ceiling, existing=True)


def capture_folders(root: Path, *, tree: str | None = None) -> tuple[str, ...]:
    """The configured capture paths, none when the boundary is absent or unavailable.

    `tree` keeps the capture paths that lie under `<knowledge base>/<tree>/`.
    """
    try:
        current = snapshot(root)
    except BoundaryUnavailable:
        return ()
    if current is None:
        return ()
    if tree is None:
        return current.capture_paths
    return tuple(folder for folder in current.capture_paths if _in_tree(folder, tree))


def _in_tree(path: str, tree: str) -> bool:
    """Whether `path` lies under `<knowledge base>/<tree>/`, under any portable spelling."""
    return collections._portable_path_key(path).startswith(collections._portable_path_key(f"{kb_dirname()}/{tree}/"))


def inbox_destination(root: Path, folder: str, leaf: str) -> str | None:
    """Where a restricted writer's page that a door places in `folder/leaf` goes: its inbox, as a vault folder.

    `folder` is the shared destination `<knowledge base>/<tree>/<rest>`. It redirects to
    `<capture path>/<rest>`: the first capture path, in sorted order, that lies under
    `<knowledge base>/<tree>/` and where the writer may create. A destination already
    inside a capture path stays. `None` when the boundary does not confine the caller;
    with no capture path to use, the write is refused. Only a door that generates its
    own destination calls this: an explicit destination is never redirected.
    """
    from .principal import effective_principal

    try:
        ceiling = write_ceiling(root, effective_principal())
        if ceiling is None:
            return None
        if _in_inbox(root, f"{folder}/{leaf}", ceiling):
            return folder
        parts = folder.split("/")
        if len(parts) > 1:
            rest = "/".join(parts[2:])
            for capture in ceiling.current.capture_paths if ceiling.current is not None else ():
                target = f"{capture}/{rest}" if rest else capture
                if _in_tree(capture, parts[1]) and _in_inbox(root, f"{target}/{leaf}", ceiling):
                    return target
    except BoundaryUnavailable:
        pass
    raise ValueError("WRITE_REFUSED: target is unavailable")


def require_target(root: Path, path: str) -> None:
    """Decide an existing target before a write door resolves or reads it."""
    _require_target(root, path, existing=True)


def capture_namespace(root: Path, path: str) -> bool:
    """Whether `path` lies in a configured capture namespace, under any spelling of it."""
    current = snapshot(root)
    return current is not None and _capture_destination(current, path) is not None


def _keeps_path_membership(root: Path, path: str, compiled: policy.Policy, **evidence) -> bool:
    """Whether the item at `path` has exactly the membership its path alone gives."""
    try:
        sets = _membership_sets(Path(root), path, compiled, **evidence)
        return bool(sets) and all(
            scopes == membership.evaluate_path_selectors(Path(root), path, compiled) for scopes in sets)
    except _ADMISSION_REFUSALS:
        return False


def verify_capture_namespaces(root: Path, current: Snapshot) -> None:
    """Establish the all-writer membership invariant of capture namespaces during stopped maintenance."""
    if not current.capture_paths:
        raise BoundaryUnavailable()
    if current.vocabulary is not None:
        from ..vocabulary import instances

        instances.verify_legacy_assignment(root, current.vocabulary)
    compiled = policy.load(root)
    for folder in current.capture_paths:
        parent = Path(root)
        for part in PurePosixPath(folder).parts:
            if not parent.exists():
                break
            if parent.is_symlink() or not parent.is_dir():
                raise BoundaryUnavailable()
            key = collections._portable_path_key(part)
            if any(child.name != part and collections._portable_path_key(child.name) == key for child in parent.iterdir()):
                raise BoundaryUnavailable()
            parent /= part
        target = Path(root) / folder
        if not os.path.lexists(target):
            continue
        if target.is_symlink() or not target.is_dir():
            raise BoundaryUnavailable()
        for directory, directories, files in os.walk(target, followlinks=False):
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise BoundaryUnavailable()
            for name in files:
                item = Path(directory) / name
                if (not stat.S_ISREG(item.lstat().st_mode)
                        or not _keeps_path_membership(root, item.relative_to(root).as_posix(), compiled)):
                    raise BoundaryUnavailable()


def _proposed_companion(root: Path, artifact: str, proposed: dict) -> companions.BoundCompanion:
    sibling = f"{artifact}.md"
    page_write = proposed.get(sibling)
    page = (page_write.content.encode("utf-8") if page_write is not None and isinstance(page_write.content, str)
            else reserved_paths.read_generic_bytes(root, sibling).data)
    binary_write = proposed.get(artifact)
    if binary_write is not None and not isinstance(binary_write.content, str):
        digest, size = binary_write.content.sha256, binary_write.content.size
    else:
        raw = (binary_write.content.encode("utf-8") if binary_write is not None
               else reserved_paths.read_generic_bytes(root, artifact).data)
        digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
    return companions.classify_proposed_sibling(artifact, artifact_sha256=digest,
                                               artifact_size=size, companion=page)


def require_write(
    root: Path, path: str, *, content: bytes | None, proposed: dict | None = None,
    granted: bool = False,
) -> None:
    """Preserve both current and proposed membership at the publication owner.

    A restricted writer keeps its target inside its writable scopes before and after the
    write. Every writer keeps a capture page's membership equal to its path's. A refused
    write cannot reveal or remove protective membership. A restricted writer's record of
    workflow state is admitted here without a membership check: no audience is served it, a
    door writes it, and a JSON record has no content or companion for a scope to select.
    Registry storage that the vocabulary authority granted to this caller for this change
    (`granted`) skips the writable-scope rule only; the caller passes it for that one path,
    never for a whole write.
    """
    from .principal import effective_principal

    who = effective_principal()
    ceiling = write_ceiling(root, who)
    current = ceiling.current if ceiling is not None else snapshot(root)
    folder = _capture_destination(current, path) if current is not None else None
    if ceiling is None and folder is None:
        return
    refused = ValueError("WRITE_REFUSED: target is unavailable")
    # Portable aliases could expose hidden collision state. All writers use
    # the configured folder spelling; choosing that spelling costs no content.
    if folder is not None and not path.startswith(f"{folder}/"):
        raise refused
    if ceiling is not None and policy.is_workflow_state_path(path):
        return
    if ceiling is not None and (policy.is_governance_path(path)
                                or Path(root) / path == activation_manifest.manifest_path(root)):
        raise refused
    scope_exempt = ceiling is not None and granted
    if ceiling is not None and not scope_exempt and not _target_admitted(root, path, ceiling, existing=True):
        raise refused
    compiled = ceiling.compiled if ceiling is not None else policy.load(root)

    def admitted(target: str, **evidence) -> bool:
        try:
            sets = _membership_sets(Path(root), target, compiled, who=who, **evidence)
        except _ADMISSION_REFUSALS:
            return False
        if folder is not None and not all(
                scopes == membership.evaluate_path_selectors(Path(root), target, compiled) for scopes in sets):
            return False
        return bool(sets) and (ceiling is None or scope_exempt or all(_writable(ceiling, scopes) for scopes in sets))

    proposed = proposed or {}
    companion = None
    try:
        if not path.lower().endswith(".md"):
            outcome = membership.evaluate_path_only(root, path, compiled)
            if outcome.state != "classified" or f"{path}.md" in proposed:
                companion = _proposed_companion(root, path, proposed)
        elif content is not None:
            try:
                descriptor = companions._descriptor(content)
            except companions.CompanionClassificationError as error:
                if error.reason != "descriptor_missing":
                    raise
            else:
                artifact = descriptor.get("artifact_path")
                if not isinstance(artifact, str) or path != f"{artifact}.md":
                    raise companions.CompanionClassificationError("descriptor_invalid")
                bound = _proposed_companion(root, artifact, proposed)
                if not admitted(artifact, proposed_companion=bound):
                    raise companions.CompanionClassificationError("descriptor_invalid")
    except (companions.CompanionClassificationError, reserved_paths.ReservedPathLeafError, OSError):
        raise refused from None
    if not admitted(path, content=content, proposed_companion=companion):
        raise refused


def requirement_relative_path() -> str:
    return f"{kb_dirname()}/{reserved_paths.CONNECTOR_BOUNDARY_REQUIREMENT_NAME}"


def parse_requirement(raw: bytes) -> dict:
    try:
        data = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(data, dict) or type(data.get("version")) is not int:
            raise BoundaryUnavailable()
        fields = {"version", "protected_scopes", "selectors_sha256", "capture_paths"}
        if data["version"] == 2:
            fields.add("vocabulary")
        elif data["version"] != 1:
            raise BoundaryUnavailable()
        if set(data) != fields:
            raise BoundaryUnavailable()
        compiled = policy.compile_protective_scopes(data["protected_scopes"])
        if data["version"] == 2:
            from ..vocabulary import instances

            instances.validate(data["vocabulary"], compiled)
        if list(_capture_paths(data["capture_paths"])) != data["capture_paths"] or not data["capture_paths"]:
            raise BoundaryUnavailable()
        if data["selectors_sha256"] != _selectors_fingerprint(data["protected_scopes"]):
            raise BoundaryUnavailable()
        return data
    except (ValueError, TypeError, KeyError) as error:
        raise BoundaryUnavailable() from error


def read_requirement(root: Path) -> dict | None:
    """The arming record; only a stopped host changes it, so a request reads it once."""
    return state_paths.request_memo(
        "connector_boundary.requirement", (str(root), requirement_relative_path()),
        lambda: _read_requirement(root),
    )


def _read_requirement(root: Path) -> dict | None:
    target = Path(root) / requirement_relative_path()
    try:
        target.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise BoundaryUnavailable() from error
    try:
        with reserved_paths._subsystem_authority_scope("governance.connector_boundary"):
            raw = reserved_paths._read_owner_bytes(root, target, DESCRIPTOR_ID, limit=_MAX_CONFIG_BYTES)
        return parse_requirement(raw)
    except (OSError, RuntimeError, ValueError) as error:
        raise BoundaryUnavailable() from error


def _selectors_fingerprint(documents: dict) -> str:
    return hashlib.sha256(json.dumps(documents, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def publish_requirement(root: Path, current: Snapshot) -> None:
    documents = policy.protective_scope_documents(policy.load(root), current.default_denied_scope_ids)
    requirement = {"version": 1, "protected_scopes": documents,
                   "selectors_sha256": _selectors_fingerprint(documents), "capture_paths": list(current.capture_paths)}
    if current.vocabulary is not None:
        requirement.update(version=2, vocabulary=current.vocabulary)
    data = json.dumps(requirement, sort_keys=True, separators=(",", ":")).encode()
    with reserved_paths._subsystem_authority_scope("governance.connector_boundary"):
        reserved_paths._publish_owner_bytes(root, Path(root) / requirement_relative_path(), DESCRIPTOR_ID, data)


def require_startup(root: Path, compatibility: frozenset[str]) -> None:
    """Serve only a complete armed state; a stopped host can repair any interruption."""
    try:
        required = read_requirement(root)
        configured = bool(os.environ.get(CONFIG_ENV, "").strip())
        enrolled = COMPATIBILITY_ID in compatibility
        if not (required is not None or configured or enrolled):
            # Readiness has just verified the state manifest. Caching the
            # answer here spares each request a read of machine-local state.
            snapshot(root)
            return
        if required is None or not enrolled or snapshot(root) is None:
            raise BoundaryUnavailable()
    except BoundaryUnavailable as error:
        raise state_migration.StateMigrationOfflineRequired("connector boundary arming is incomplete") from error
