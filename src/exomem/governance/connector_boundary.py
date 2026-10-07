"""Host-owned connector ceilings over the existing canonical Scope membership."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import activation_manifest, find_corpus, reserved_paths, state_migration
from .. import structured_collections as collections
from ..kbdir import kb_dirname
from . import companions, membership, policy
from .decisions import Decision
from .principal import ClientBinding, RequestPrincipal

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

    def denied_scopes(self, who: RequestPrincipal) -> frozenset[str]:
        if who.administrative_ingress:
            return frozenset()
        if who.resolved and who.client_binding is not None and who.origin_session is not None:
            for binding, denied in self.clients:
                if binding == who.client_binding:
                    return denied
        return self.default_denied_scope_ids


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


def snapshot(root: Path, compiled: policy.Policy | None = None, *, maintenance: bool = False) -> Snapshot | None:
    """Read current host configuration; never infer authority from vault content.

    Invalid security state withholds content until the host repairs it. This
    costs connector availability, rather than exposing the protected corpus.
    """
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
                or set(data) - {"version", "default_denied_scope_ids", "clients", "capture_paths"}):
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
        if not default or required is not None and not set(required["protected_scopes"]) <= default:
            raise BoundaryUnavailable()
        if required is not None and not maintenance:
            active = policy.protective_scope_documents(compiled, frozenset(required["protected_scopes"]))
            if active != required["protected_scopes"]:
                raise BoundaryUnavailable()
            if set(required["protected_scopes"]) != default or list(capture_paths) != required["capture_paths"]:
                raise BoundaryUnavailable()
        clients = []
        seen = set()
        for item in data["clients"]:
            if not isinstance(item, dict) or set(item) != {"issuer", "client_id", "denied_scope_ids"}:
                raise BoundaryUnavailable()
            binding = ClientBinding(item["issuer"], item["client_id"])
            denied = _scope_ids(item["denied_scope_ids"], compiled)
            if binding in seen or not denied <= default:
                raise BoundaryUnavailable()
            seen.add(binding)
            clients.append((binding, denied))
        return Snapshot(hashlib.sha256(raw + compiled.fingerprint.encode()).hexdigest(), default, tuple(clients), capture_paths)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BoundaryUnavailable() from error


def cache_identity(root: Path, who: RequestPrincipal, compiled: policy.Policy | None = None) -> tuple:
    current = snapshot(root, compiled)
    return (who.client_binding, who.origin_session, who.administrative_ingress,
            current.revision if current is not None else None)


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
        if current is not None and mutation and path is not None and _capture_destination(current, path):
            denied = current.default_denied_scope_ids
        return Decision(policy.DISCLOSURE_MIN if denied.intersection(scope_ids) else policy.DISCLOSURE_MAX)
    except BoundaryUnavailable:
        return Decision(policy.DISCLOSURE_MIN)


def permits(root: Path, path: str, who: RequestPrincipal, *, content: bytes | None = None) -> bool:
    """Admit a named file through the shared membership kernel before disclosure."""
    try:
        root = Path(root)
        current = snapshot(root)
        denied = current.denied_scopes(who) if current is not None else frozenset()
        if not denied:
            return True
        if root / path == activation_manifest.manifest_path(root):
            return False
        compiled = policy.load(root)
        return _permits_membership(root, path, denied, compiled, content=content, who=who)
    except (BoundaryUnavailable, membership.MembershipUnresolved, OSError,
            reserved_paths.ReservedPathLeafError):
        return False


def _permits_membership(root: Path, path: str, denied: frozenset[str], compiled: policy.Policy,
                        *, content: bytes | None = None, who: RequestPrincipal | None = None,
                        proposed_companion: companions.BoundCompanion | None = None) -> bool:
    try:
        from ..collection_store.preview import bound_writer

        writer = bound_writer(root)
        if writer is not None and content is None and proposed_companion is None:
            with writer.read_snapshot():
                operation = writer._operation
                if who != operation.who:
                    return False
                targets = operation.projection_subjects(path)
                if targets is not None:
                    return bool(targets) and all(
                        not denied.intersection(membership.evaluate_metadata(target.basis.subject, compiled))
                        for target in targets
                    )
        if path.lower().endswith(".md"):
            raw = content if content is not None else reserved_paths.read_generic_bytes(root, path).data
            page = find_corpus.parse_page(root / path, 0, root, content=raw)
            if page is None:
                return False
            scopes = membership.evaluate_snapshot(page, compiled, content_hash=hashlib.sha256(raw).hexdigest())
        else:
            scopes = membership.evaluate_path_only(root, path, compiled, proposed_companion=proposed_companion).require_classified()
        return not denied.intersection(scopes)
    except (BoundaryUnavailable, membership.MembershipUnresolved, OSError,
            reserved_paths.ReservedPathLeafError):
        return False


def _capture_destination(current: Snapshot, path: str) -> str | None:
    portable = collections._portable_path_key(path)
    for folder in current.capture_paths:
        key = collections._portable_path_key(folder)
        if portable == key or portable.startswith(f"{key}/"):
            return folder
    return None


def require_create(root: Path, path: str, *, replace_existing: bool = False, directory: bool = False) -> None:
    """Decide destination eligibility before revealing existence or allocating names."""
    from .principal import effective_principal

    try:
        current = snapshot(root)
        who = effective_principal()
        if current is None or not current.denied_scopes(who):
            return
        if Path(root) / path == activation_manifest.manifest_path(root):
            raise BoundaryUnavailable()
        if replace_existing and permits(root, path, who):
            return
        folder = _capture_destination(current, path)
        if read_requirement(root) is not None and folder is not None and (
                path.startswith(f"{folder}/") or directory and path == folder):
            return
    except BoundaryUnavailable:
        pass
    # Mixed namespaces cannot support private-independent collisions. The caller
    # must select a configured capture destination; existing edits stay available.
    raise ValueError("WRITE_REFUSED: target is unavailable")


def verify_capture_namespaces(root: Path, current: Snapshot) -> None:
    """Establish the all-writer visibility invariant during stopped maintenance."""
    if not current.capture_paths:
        raise BoundaryUnavailable()
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
                        or not _permits_membership(root, item.relative_to(root).as_posix(),
                                                  current.default_denied_scope_ids, compiled)):
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


def require_write(root: Path, path: str, *, content: bytes | None, proposed: dict | None = None) -> None:
    """Preserve both current and proposed membership at the publication owner.

    A refused write cannot reveal or remove protective membership. The writer
    loses that target change; unrelated allowed edits keep their existing flow.
    """
    from .principal import effective_principal

    who = effective_principal()
    current = snapshot(root)
    if current is None:
        return
    limited = current.denied_scopes(who)
    # Even unrestricted writers must preserve capture namespace visibility.
    folder = _capture_destination(current, path)
    # Portable aliases could expose hidden collision state. All writers use
    # the configured folder spelling; choosing that spelling costs no content.
    if folder is not None and not path.startswith(f"{folder}/"):
        raise ValueError("WRITE_REFUSED: target is unavailable")
    denied = current.default_denied_scope_ids if folder is not None else limited
    if not denied:
        return
    if limited and (policy.is_governance_path(path) or root / path == activation_manifest.manifest_path(root)):
        raise ValueError("WRITE_REFUSED: target is unavailable")
    try:
        exists = (root / path).exists()
    except OSError:
        raise ValueError("WRITE_REFUSED: target is unavailable") from None
    if exists and limited and not permits(root, path, who):
        raise ValueError("WRITE_REFUSED: target is unavailable")
    if not exists and limited:
        require_create(root, path)
    compiled = policy.load(root)
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
                if not _permits_membership(root, artifact, denied, compiled, proposed_companion=bound):
                    raise companions.CompanionClassificationError("descriptor_invalid")
    except (companions.CompanionClassificationError, reserved_paths.ReservedPathLeafError, OSError):
        raise ValueError("WRITE_REFUSED: target is unavailable") from None
    if not _permits_membership(root, path, denied, compiled, content=content, proposed_companion=companion, who=who):
        raise ValueError("WRITE_REFUSED: target is unavailable")


def requirement_relative_path() -> str:
    return f"{kb_dirname()}/{reserved_paths.CONNECTOR_BOUNDARY_REQUIREMENT_NAME}"


def parse_requirement(raw: bytes) -> dict:
    try:
        data = json.loads(raw, object_pairs_hook=_object)
        if (not isinstance(data, dict) or set(data) != {"version", "protected_scopes", "selectors_sha256", "capture_paths"}
                or type(data["version"]) is not int or data["version"] != 1):
            raise BoundaryUnavailable()
        policy.compile_protective_scopes(data["protected_scopes"])
        if list(_capture_paths(data["capture_paths"])) != data["capture_paths"] or not data["capture_paths"]:
            raise BoundaryUnavailable()
        if data["selectors_sha256"] != _selectors_fingerprint(data["protected_scopes"]):
            raise BoundaryUnavailable()
        return data
    except (ValueError, TypeError, KeyError) as error:
        raise BoundaryUnavailable() from error


def read_requirement(root: Path) -> dict | None:
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
    data = json.dumps({"version": 1, "protected_scopes": documents,
                       "selectors_sha256": _selectors_fingerprint(documents), "capture_paths": list(current.capture_paths)},
                      sort_keys=True, separators=(",", ":")).encode()
    with reserved_paths._subsystem_authority_scope("governance.connector_boundary"):
        reserved_paths._publish_owner_bytes(root, Path(root) / requirement_relative_path(), DESCRIPTOR_ID, data)


def require_startup(root: Path, compatibility: frozenset[str]) -> None:
    """Serve only a complete armed state; a stopped host can repair any interruption."""
    try:
        required = read_requirement(root)
        configured = bool(os.environ.get(CONFIG_ENV, "").strip())
        enrolled = COMPATIBILITY_ID in compatibility
        if not (required is not None or configured or enrolled):
            return
        if required is None or not enrolled or snapshot(root) is None:
            raise BoundaryUnavailable()
    except BoundaryUnavailable as error:
        raise state_migration.StateMigrationOfflineRequired("connector boundary arming is incomplete") from error
