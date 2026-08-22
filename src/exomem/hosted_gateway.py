"""Pure private gateway contract and transfer authority for hosted cells."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

from fastmcp.tools import FunctionTool

from . import __version__, capabilities
from . import commands as commands_module
from .governance import authorization_sessions, authorization_transport
from .hosted_runtime import (
    HOSTED_PROTOCOL_VERSION,
    SUPPORTED_HOSTED_PROTOCOL_VERSIONS,
    HostedCellConfig,
)
from .kbdir import kb_dirname, kb_page_relative_form, kb_page_target, kb_relative_form

CONTRACT_SCHEMA_VERSION = 1
TRANSFER_GRANT_VERSION = 1
TRANSFER_AUDIENCE = "exomem-hosted-transfer"
TRANSFER_MAX_TTL_SECONDS = 15 * 60
TRANSFER_CLOCK_SKEW_SECONDS = 30

CELL_HEADER = "X-Exomem-Cell-Id"
PROTOCOL_HEADER = "X-Exomem-Protocol-Version"
REQUEST_HEADER = "X-Exomem-Request-Id"
PRINCIPAL_HEADER = "X-Exomem-Principal-Scope"
TRANSFER_GRANT_HEADER = "X-Exomem-Transfer-Grant"
ROUTING_STOPPED_HEADER = "X-Exomem-Routing-Stopped"

_OPAQUE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PRINCIPAL_SCOPE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OPERATIONS = frozenset({"upload", "download"})
_GRANT_FIELDS = frozenset(
    {"v", "aud", "op", "tenant", "cell", "principal", "iat", "exp", "jti", "limits"}
)


class HostedGatewayError(RuntimeError):
    """Stable private-contract error that never embeds a credential or tenant value."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class TrustedGatewayContext:
    cell_id: str
    protocol_version: str
    request_id: str
    principal_scope: str
    idempotency_key: str | None = None
    authenticated_credential_version: str | None = None
    security_revision: int | None = None


@dataclass(frozen=True, slots=True)
class TransferGrant:
    operation: str
    tenant_scope: str
    cell_id: str
    principal_scope: str
    issued_at: int
    expires_at: int
    jti: str
    max_bytes: int


def canonical_json(value: Any) -> bytes:
    """Render the one canonical JSON encoding used for digests and HMAC payloads."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_contract_json(contract: dict[str, Any]) -> bytes:
    return canonical_json(contract)


def _command_contract(command: commands_module.Command) -> dict[str, Any]:
    return {
        "name": command.name,
        "params": [
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "description": param.help,
            }
            for param in command.params
        ],
        "read_only": command.read_only,
        "mode": "read" if command.read_only else "write",
        "tier": command.tier,
        "capability": "core" if command.tier == 1 else "tier-2",
        "product_surface": command.product_surface,
        "actions": list(command.product_actions),
        "first_run_safe": command.first_run_safe,
        "routes": list(command.routes),
        "guarded_fields": list(command.guarded_fields),
    }


def _mcp_tool_contract(
    command: commands_module.Command,
    *,
    descriptor: capabilities.ActiveSurfaceDescriptor,
) -> dict[str, Any]:
    injected: tuple[object, ...] = (
        (Path("."), object()) if command.needs_schema else (Path("."),)
    )
    bound = commands_module.bind_vault(
        command.leaf,
        *injected,
        name=command.name,
        description=command.doc,
        command=command,
        surface_descriptor=descriptor,
    )
    tool = FunctionTool.from_function(
        bound,
        name=command.name,
        annotations=commands_module.mcp_tool_annotations(
            command.name,
            read_only=command.read_only,
            open_world=True,
            idempotent=command.read_only,
        ),
    )
    return {
        key: value
        for key, value in tool.to_mcp_tool().model_dump(mode="json", by_alias=True).items()
        if value is not None
    }


#: Vault subtrees a hosted tenant's own agent may read but never rewrite.
#:
#: `_Schema` is the vault's governing doctrine -- `schema.py` loads
#: `_Schema/references/frontmatter.md` and `page-types.md` as *the* frontmatter
#: and page-type contract -- and `_Governance` is the policy tree. Neither is
#: ordinary content.
#:
#: `hosted-alpha-agent-v1` protected both by accident of omission: it simply did
#: not expose a broad page-mutation command, and its own specification recorded
#: that absence as the control ("the command is absent from the profile and
#: rejected before invocation or lifecycle admission", including for a path
#: under `_Schema`). `hosted-alpha-agent-v3` exposes `edit_memory` and
#: `replace_memory`, so that control has to become a real one. A hosted tenant
#: is hookless and has the least out-of-band supervision of any tier; a
#: prompt-injected `capture_source` would otherwise be enough to rewrite the
#: doctrine that governs every later write.
PROTECTED_TREE_DIRNAMES: frozenset[str] = frozenset({"_Schema", "_Governance"})

#: Fixed locations inside a protected tree that ordinary hosted writes manage.
#:
#: `relation_review.review_artifact_path` hardcodes
#: `<KB>/_Schema/relation-reviews/<page-identity>.json`, and the semantic-write
#: machinery drops one sidecar there on every committed page create or
#: supersession. That happens on *every* profile including `v1`, on the plain
#: success path, with no attack involved -- so "a hosted profile never writes
#: inside `_Schema`" was never true and this change did not make it true.
#:
#: It is nonetheless not a hole, and the distinction is the one the requirement
#: now draws: the *location* is fixed by the system, never caller-supplied.
#: The open source taxonomy and project-key registries are the same kind of
#: fixed-placement side effect: a caller can introduce vocabulary through the
#: owning command, but cannot choose where the registry lives. They remain
#: user-owned per-vault configuration; "system-managed" describes only the
#: write path, not ownership of the bytes.
#:
#: None of these paths is exempt from the guard. A tenant-chosen target is still
#: refused, so an agent may cause a fixed-path update only through the command
#: that owns it and can never name the same file as an edit or replacement
#: target. The enumeration exists so success-path assertions can exclude these
#: paths by name while checking every other protected byte and directory entry.
SYSTEM_MANAGED_PROTECTED_PATHS: tuple[str, ...] = (
    "_Schema/project-keys.yaml",
    "_Schema/relation-reviews",
    "_Schema/source-taxonomy.yaml",
)


def is_system_managed_protected_path(kb_relative: str) -> bool:
    """Whether a KB-relative path is a fixed-placement managed write."""

    text = str(kb_relative).replace("\\", "/").strip("/")
    return any(
        text == owned or text.startswith(f"{owned}/")
        for owned in SYSTEM_MANAGED_PROTECTED_PATHS
    )

#: Caller-supplied write-target arguments, per command, that the guard inspects.
#:
#: The membership rule is deliberate and mechanical: this set is exactly the
#: commands the widening *newly* exposes (v3 minus v2). Already-published
#: surface is left classified as it was, so no shipped profile changes
#: behaviour. That rule, not the parameter name, is why `plan_memory`'s
#: `manifest_path` is guarded while `record_memory`'s identically-named,
#: identically-constrained `manifest_path` is not -- see
#: `test_guarded_set_is_exactly_what_the_widening_newly_exposes`.
#:
#: For `plan_memory` the guard is therefore defence in depth rather than the
#: only control: `structured_collections._require_profile_layer` already pins a
#: planning manifest under `Knowledge Base/Planning/`, so a protected tree is
#: unreachable through it either way. For `edit_memory` and `replace_memory`
#: the guard *is* the control.
#:
#: Within a guarded command *every* caller-supplied target argument is listed,
#: not just the obvious one -- partial coverage of a guarded command is how a
#: guard grows its next hole. `plan_memory` selects by `collection` as well as
#: `manifest_path`, so both are here. `replace_memory`'s `slug` names the
#: successor page's filename but cannot reach a tree: `vault.resolve_filename_slug`
#: admits lowercase ASCII kebab-case only, which has no `/`, `.` or `_` to spell
#: one with.
#:
#: Each entry carries the *kind* of target its arguments name, because the leaves
#: normalise them differently and the guard's whole premise is that it judges the
#: path the leaf will actually open. A `page` argument gets the Markdown suffix
#: the page leaves supply and names a file; a `collection` argument does not, and
#: names a directory or manifest. Reading a page argument as a directory is what
#: made `edit_memory path="Knowledge Base/Notes/_Schema"` refuse while its
#: identical `.md` spelling was allowed.
class ProtectedTargets(NamedTuple):
    """The guarded arguments of one command, and how its leaf reads them."""

    kind: str
    arguments: tuple[str, ...]


PROTECTED_TREE_PATH_ARGUMENTS: dict[str, ProtectedTargets] = {
    "edit_memory": ProtectedTargets("page", ("path",)),
    "replace_memory": ProtectedTargets("page", ("old_path",)),
    "plan_memory": ProtectedTargets("collection", ("collection", "manifest_path")),
}

#: Mutating commands that do not need the guard because their own leaf refuses a
#: protected-tree target. Every member takes at least one caller-supplied
#: argument that shapes where it writes -- an earlier version of this comment
#: claimed four of them were "policy-owned placement" with "no caller-chosen
#: path argument at all", which was simply false and is the kind of claim that
#: turns into a bypass. What is true, per member:
#:
#: - `observe_memory` rejects a `_Schema` document with
#:   `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`; the tree holds no writable
#:   compiled page.
#: - `record_memory` is pinned under `Knowledge Base/Records/` by
#:   `structured_collections._require_profile_layer`, for both `manifest_path`
#:   and `collection`.
#: - `connect_memory` reads its `path` to decide what to write *elsewhere*.
#: - `remember` and `capture_source` derive a filename from `slug`/`title`, and
#:   `vault.resolve_filename_slug` admits lowercase ASCII kebab-case only.
#: - `triage_memory` selects an existing review item by `ref`.
#: - `preserve_evidence` composes its path from `scope`, `category` and
#:   `filename` -- those three *are* the path. Its leaf strips separators and
#:   dots from each component, so a traversal collapses into one literal
#:   filename that cannot leave `Knowledge Base/Evidence/`. Note that this leaf
#:   writes without a `PathGuard`, which is a pre-existing gap shared with v1
#:   and v2 and is filed as a security follow-up rather than fixed here.
#:
#: Every one of these claims is exercised by
#: `test_target_constrained_mutations_are_actually_constrained`, which probes
#: every string argument in each command's published schema rather than a
#: hand-picked few.
#:
#: Membership is not self-certifying, so two separate checks back it.
#: `assert_profile_mutations_are_classified` runs at route registration and
#: refuses to serve a profile whose mutating commands are not all accounted for
#: in one list or the other, so a later widening cannot reopen this hole by
#: silence -- but that only proves every command is *named*, not that the claim
#: about it is true. The behavioural sweep proves the claim per member.
TARGET_CONSTRAINED_MUTATIONS: frozenset[str] = frozenset(
    {
        "remember",
        "observe_memory",
        "capture_source",
        "preserve_evidence",
        "triage_memory",
        "connect_memory",
        "record_memory",
    }
)


def _folded_segment(part: str) -> str:
    """One path component reduced to what the filesystem will actually match.

    Case is folded, and trailing dots and spaces are dropped because Windows
    strips them from a component before the call reaches the filesystem --
    `_Schema ` and `_Schema.` both open `_Schema`. A guard that compares the
    component as typed reports "not protected" for both.
    """

    return part.strip().rstrip(". ").casefold()


def _has_protected_segment(parts: Iterable[str]) -> bool:
    folded = {_folded_segment(name) for name in PROTECTED_TREE_DIRNAMES}
    return any(_folded_segment(part) in folded for part in parts)


def _spelling_names_a_protected_tree(text: str, *, drop_final: bool = False) -> bool:
    """Segment check under every flavour a target platform could apply.

    `PurePosixPath` alone is wrong for the platform this actually deploys to:
    it reads `C:_Schema/x.md` as one opaque component and reports no protected
    tree, while Windows reads it as drive-relative and opens `_Schema`. Both
    flavours are applied and either one is enough to refuse, which also covers
    UNC and `\\\\?\\` spellings without special-casing them.

    `drop_final` excludes the last component, which is what a *page* target
    needs: the leaf opens it as a file, so the component can never be a
    protected directory. Without it, `Knowledge Base/Notes/_Schema` -- an
    ordinary page the leaf opens as `_Schema.md` -- is refused while the
    identical `.md` spelling is allowed. Callers pass the suffixed rel-form, so
    a trailing separator still leaves a real final component (`.md`) to drop
    and the tree above it is still read.
    """

    for flavour in (PurePosixPath, PureWindowsPath):
        parts = flavour(text).parts
        if drop_final:
            parts = parts[:-1]
        if _has_protected_segment(parts):
            return True
    return False


def _deepest_existing(target: Path) -> tuple[Path, tuple[str, ...]]:
    """Split `target` into its deepest existing ancestor and the tail below it.

    `resolve()` cannot canonicalise a component that is not on disk, and the
    interesting targets usually are not: a hosted agent asking to create a new
    page inside `_Schema` names a file that does not exist yet. Resolving the
    deepest ancestor that *does* exist is what lets an alias, junction or link
    in the middle of the path be expanded, while the tail stays available for
    the literal check.

    A symlink counts as existing even when it dangles, so a broken link is
    resolved rather than walked through.
    """

    remainder: list[str] = []
    current = target
    while True:
        try:
            if current.exists() or current.is_symlink():
                return current, tuple(reversed(remainder))
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return current, tuple(reversed(remainder))
        remainder.append(current.name)
        current = parent


def _protected_roots(vault_root: Path) -> tuple[Path, ...]:
    """The resolved real directories this guard exists to protect."""

    kb = Path(vault_root) / kb_dirname()
    roots: list[Path] = []
    for name in sorted(PROTECTED_TREE_DIRNAMES):
        candidate = kb / name
        if candidate.exists() or candidate.is_symlink():
            roots.append(candidate.resolve())
    return tuple(roots)


def _resolution_names_a_protected_tree(target: Path, vault_root: Path) -> bool:
    """Resolve `target` as far as the filesystem allows and judge the result.

    This is the reading that sees what a *name* cannot: an NTFS 8.3 alias
    (`_GOVER~1` for `_Governance`), a junction, a symlink, or a `..` chain that
    lands inside a protected tree without ever spelling it. Containment is
    tested against the resolved protected roots rather than by comparing
    component names, because by the time the OS has resolved the path the
    alias is gone and only the real location remains.
    """

    existing, remainder = _deepest_existing(target)
    resolved = existing.resolve()

    for root in _protected_roots(vault_root):
        if resolved == root or root in resolved.parents:
            return True

    # Protected trees are not only the two at the KB root -- a nested one is
    # protected too -- so the resolved location is also read per segment.
    # Scoped to the vault where possible so a host directory above the vault
    # cannot decide the answer; outside the vault, over-refuse.
    try:
        scoped = resolved.relative_to(Path(vault_root).resolve()).parts
    except ValueError:
        scoped = resolved.parts
    if _has_protected_segment(scoped):
        return True

    # The tail below the deepest existing ancestor was never resolved, so it
    # gets the literal reading. This is what refuses a not-yet-existing page
    # inside `_Schema`.
    return _has_protected_segment(remainder)


def _evaluate_protected_tree(
    value: object, *, vault_root: Path | None, kind: str = "page"
) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False

    page = kind == "page"

    if page:
        # A page target is a *file*. The rel-form the leaf uses carries the
        # Markdown suffix, and only the directories above the file can be a
        # protected tree -- hence `drop_final`. The unprefixed raw text is kept
        # alongside it because a Windows drive or UNC root is only parseable
        # before the KB prefix is prepended, and it is suffixed the same way so
        # its final component is a filename too.
        suffixed = raw if raw.endswith(".md") else f"{raw}.md"
        spellings = ((kb_page_relative_form(raw), True), (suffixed, True))
    else:
        # A collection target names a directory or a manifest, and the
        # collection leaf supplies no suffix, so every component counts.
        spellings = (
            (kb_relative_form(raw), False),
            (raw, False),
            (raw.replace("\\", "/").lstrip("/"), False),
        )

    for text, drop_final in spellings:
        if _spelling_names_a_protected_tree(text, drop_final=drop_final):
            return True

    if vault_root is None:
        return False

    root = Path(vault_root)
    # Resolution readings, one per join the consuming leaf can perform. The
    # page write leaves join at the *KB* root via `kb_page_target`; evaluating
    # only the vault-root join is how the third bypass worked, because
    # `_GOVER~1/README.md` resolved to nothing there and its alias was never
    # expanded.
    if page:
        targets = [kb_page_target(root, raw)[0]]
    else:
        # `structured_collections.parse_manifest_bytes` does `root / Path(raw)`,
        # where a backslash is an ordinary filename character rather than a
        # separator. Folding `\` to `/` first and stopping there *under*
        # approximates that leaf: with `Planning/back\slash` a link into
        # `_Schema`, the folded reading resolves nowhere while the leaf's own
        # join lands inside the tree. All three joins are evaluated so the
        # reproduction over-approximates rather than under-approximates.
        targets = [
            root / kb_relative_form(raw),
            root / raw.replace("\\", "/").lstrip("/"),
            root / Path(raw),
        ]
    literal = Path(raw)
    if literal.is_absolute():
        targets.append(literal)
    return any(_resolution_names_a_protected_tree(target, root) for target in targets)


def _names_a_protected_tree(
    value: object, *, vault_root: Path | None, kind: str = "page"
) -> bool:
    """Return whether `value` names a path that touches a protected subtree.

    Three bypasses of this guard had one cause: the guard and the executor held
    independent notions of the same path. Round two matched segment names the
    executor never compared. Round three branched on `is_absolute()` while the
    executor stripped the separator and wrote the file. Round four joined at
    the vault root while the executor joined at the *KB* root, so an unprefixed
    `_GOVER~1/README.md` resolved to nothing and its NTFS 8.3 alias was never
    expanded. Enumerating shapes did not fix any of them.

    So the guard no longer has a normaliser of its own. It calls
    `kbdir.kb_relative_form` / `kb_page_relative_form` -- the same functions
    `edit._resolve` and `replace._resolve_kb_path` call -- and judges the path
    the executor will actually open. Two readings then run over every candidate
    target, and any hit refuses: a *spelling* reading, per component,
    case-folded, with trailing dots and spaces stripped and both Posix and
    Windows flavours applied; and a *resolution* reading, which resolves the
    deepest existing ancestor so aliases and links are expanded by the OS,
    tests containment against the resolved protected roots, then applies the
    spelling reading to the tail that could not be resolved.

    `kind` selects which leaf is being mirrored -- `"page"` for the page write
    leaves, `"collection"` for the structured-collection leaves -- because they
    normalise differently, and mirroring the wrong one is the whole failure
    mode this guard keeps being caught by.

    Over-refusing an exotic path costs a caller nothing; under-refusing one
    costs the tenant its doctrine. Every `Exception` raised anywhere in the
    evaluation therefore refuses: a guard's unknown case is never "allow". A
    `BaseException` -- `KeyboardInterrupt`, `SystemExit`, a cancellation -- is
    deliberately *not* caught, because those unwind the request rather than
    producing an answer, so nothing is admitted either way.
    """

    try:
        return _evaluate_protected_tree(value, vault_root=vault_root, kind=kind)
    except Exception:  # noqa: BLE001 - deliberate: an unreadable target is refused
        return True


def protected_tree_argument(
    command_name: str,
    kwargs: Mapping[str, Any],
    *,
    vault_root: Path | None = None,
) -> str | None:
    """Return the argument naming a protected subtree, or None when clean."""

    targets = PROTECTED_TREE_PATH_ARGUMENTS.get(command_name)
    if targets is None:
        return None
    for argument in targets.arguments:
        if argument in kwargs and _names_a_protected_tree(
            kwargs[argument], vault_root=vault_root, kind=targets.kind
        ):
            return argument
    return None


def assert_profile_mutations_are_classified(profile: str) -> None:
    """Fail closed when a hosted profile exposes an unclassified mutation.

    Called once while registering the hosted routes, so a cell configured for a
    profile whose write surface nobody has triaged refuses to start rather than
    serving an unguarded write primitive over the tenant's own doctrine.
    """

    unclassified = sorted(
        command.name
        for command in commands_module.product_commands_for_profile(profile, "rest")
        if not command.read_only
        and command.name not in PROTECTED_TREE_PATH_ARGUMENTS
        and command.name not in TARGET_CONSTRAINED_MUTATIONS
    )
    if unclassified:
        raise HostedGatewayError(
            "HOSTED_SURFACE_PROFILE_UNSUPPORTED",
            "hosted agent surface profile exposes an unclassified mutation",
        )


def hosted_agent_surface_descriptor(
    profile: str = commands_module.HOSTED_ALPHA_AGENT_PROFILE,
) -> capabilities.ActiveSurfaceDescriptor:
    """Return the immutable descriptor for one supported Hosted agent profile."""

    if profile not in commands_module.PRODUCT_SURFACE_PROFILES:
        raise HostedGatewayError(
            "HOSTED_SURFACE_PROFILE_UNSUPPORTED",
            "hosted agent surface profile is not supported",
        )
    try:
        registry = commands_module.product_commands_for_profile(profile, "rest")
    except ValueError as exc:
        raise HostedGatewayError(
            "HOSTED_SURFACE_PROFILE_UNSUPPORTED",
            "hosted agent surface profile is not supported",
        ) from exc
    return capabilities.ActiveSurfaceDescriptor(
        surface="hosted-agent",
        profile=profile,
        tier2_enabled=False,
        product_commands=tuple(command.name for command in registry),
    )


def build_gateway_contract(
    *,
    protocol_version: str = HOSTED_PROTOCOL_VERSION,
    expose_tier2: bool = True,
) -> dict[str, Any]:
    """Build the deterministic private contract directly from the REST registry."""

    if protocol_version not in SUPPORTED_HOSTED_PROTOCOL_VERSIONS:
        raise HostedGatewayError(
            "HOSTED_PROTOCOL_UNSUPPORTED",
            "hosted protocol version is not supported by this release",
        )

    registry = commands_module.product_commands_for("rest", expose_tier2=expose_tier2)
    base: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "protocol_version": protocol_version,
        "exomem_release": __version__,
        "compatibility": {
            "policy": "additive",
            "optional_response_fields_may_be_added": True,
            "breaking_changes_require_coordinated_rollout": True,
            "breaking_change_classes": [
                "command-removal",
                "parameter-removal-or-change",
                "envelope-change",
                "stable-error-removal-or-change",
            ],
        },
        "trusted_headers": {
            "cell": CELL_HEADER,
            "protocol": PROTOCOL_HEADER,
            "request": REQUEST_HEADER,
            "principal": PRINCIPAL_HEADER,
            "idempotency": "Idempotency-Key",
        },
        "authorization_session": {
            "carrier": "header",
            "name": authorization_transport.AUTHORIZATION_SESSION_HEADER_NAME,
            "service_authorization_header": "Authorization",
            "body_allowed": False,
            "query_allowed": False,
            "min_length": authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES,
            "max_length": authorization_sessions.AUTHORIZATION_SESSION_CREDENTIAL_BYTES,
        },
        "envelopes": {
            "success": {
                "required": ["success", "data"],
                "shape": {"success": True, "data": "command-result"},
            },
            "error": {
                "required": ["success", "error"],
                "error_required": ["code", "message", "remediation"],
                "shape": {
                    "success": False,
                    "error": {
                        "code": "stable-code",
                        "message": "content-free-message",
                        "remediation": None,
                    },
                },
            },
        },
        "transfer_grant": {
            "version": TRANSFER_GRANT_VERSION,
            "audience": TRANSFER_AUDIENCE,
            "operations": sorted(_OPERATIONS),
            "max_ttl_seconds": TRANSFER_MAX_TTL_SECONDS,
        },
        "commands": [_command_contract(command) for command in registry],
    }
    return {
        **base,
        "digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(canonical_json(base)).hexdigest(),
        },
    }


def build_agent_gateway_contract(
    *,
    profile: str = commands_module.HOSTED_ALPHA_AGENT_PROFILE,
    protocol_version: str = HOSTED_PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Build an MCP-ready, least-privilege contract for a Hosted agent surface."""

    descriptor = hosted_agent_surface_descriptor(profile)
    registry = commands_module.product_commands_for_profile(profile, "rest")
    legacy = build_gateway_contract(
        protocol_version=protocol_version,
        expose_tier2=False,
    )
    base = {
        key: value
        for key, value in legacy.items()
        if key not in {"commands", "digest", "transfer_grant"}
    }
    base["agent_profile"] = {
        **descriptor.as_metadata(),
        "immutable": True,
    }
    base["commands"] = [
        {
            **_command_contract(command),
            "mcp_tool": _mcp_tool_contract(command, descriptor=descriptor),
        }
        for command in registry
    ]
    return {
        **base,
        "digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(canonical_json(base)).hexdigest(),
        },
    }


def validate_opaque_scope(value: str, *, field: str) -> str:
    clean = str(value or "").strip()
    if not _OPAQUE_SCOPE.fullmatch(clean):
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID", f"trusted {field} must be an opaque identifier"
        )
    return clean


def validate_request_id(value: str) -> str:
    """Require the canonical UUIDv4 shape emitted by ``crypto.randomUUID``."""

    clean = str(value or "").strip()
    try:
        parsed = uuid.UUID(clean)
    except (AttributeError, ValueError) as exc:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted request identity must be a canonical UUIDv4",
        ) from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != clean:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted request identity must be a canonical UUIDv4",
        )
    return clean


def validate_principal_scope(value: str) -> str:
    """Require an unpadded base64url-encoded 256-bit principal scope."""

    clean = str(value or "").strip()
    if not _PRINCIPAL_SCOPE.fullmatch(clean):
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        )
    try:
        decoded = base64.b64decode(clean + "=", altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        ) from exc
    if len(decoded) != hashlib.sha256().digest_size or _b64encode(decoded) != clean:
        raise HostedGatewayError(
            "HOSTED_CONTEXT_INVALID",
            "trusted principal scope must be an opaque 256-bit digest",
        )
    return clean


def scoped_idempotency_key(context: TrustedGatewayContext) -> str | None:
    """Hash public retry identity with immutable cell and principal context."""

    if not context.idempotency_key:
        return None
    payload = "\0".join(
        (
            context.cell_id,
            context.principal_scope,
            context.idempotency_key,
        )
    )
    return f"hosted:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def implicit_retry_scope(context: TrustedGatewayContext) -> str:
    payload = "\0".join(
        (
            context.cell_id,
            context.principal_scope,
        )
    )
    return f"hosted-principal:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if _b64encode(decoded) != value:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    return decoded


def _resource_bound(config: HostedCellConfig, operation: str) -> int:
    if operation == "upload":
        return config.resource_limits.upload_bytes
    return config.resource_limits.storage_bytes


def _validate_grant_limits(config: HostedCellConfig, operation: str, max_bytes: int) -> int:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or max_bytes > _resource_bound(config, operation)
    ):
        raise HostedGatewayError(
            "HOSTED_TRANSFER_LIMIT_INVALID",
            "transfer grant exceeds the configured resource bound",
        )
    return max_bytes


def mint_transfer_grant(
    config: HostedCellConfig,
    *,
    tenant_scope: str,
    principal_scope: str,
    operation: str,
    jti: str,
    max_bytes: int,
    now: int | float | None = None,
    ttl_seconds: int = 5 * 60,
) -> str:
    """Mint an alpha HMAC grant using the existing unique cell credential."""

    service_credential = config.service_credential
    if service_credential is None:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE",
            "legacy private transfer is unavailable for dynamic-credential cells",
        )

    if operation not in _OPERATIONS:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_OPERATION_INVALID", "transfer operation is invalid"
        )
    tenant = validate_opaque_scope(tenant_scope, field="tenant scope")
    principal = validate_principal_scope(principal_scope)
    grant_id = validate_opaque_scope(jti, field="grant identity")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 0 < ttl_seconds <= TRANSFER_MAX_TTL_SECONDS
    ):
        raise HostedGatewayError(
            "HOSTED_TRANSFER_TTL_INVALID", "transfer grant lifetime is invalid"
        )
    limit = _validate_grant_limits(config, operation, max_bytes)
    issued_at = int(time.time() if now is None else now)
    claims = {
        "v": TRANSFER_GRANT_VERSION,
        "aud": TRANSFER_AUDIENCE,
        "op": operation,
        "tenant": tenant,
        "cell": config.cell_id,
        "principal": principal,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": grant_id,
        "limits": {"max_bytes": limit},
    }
    payload = _b64encode(canonical_json(claims))
    signature = hmac.new(
        service_credential.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload}.{_b64encode(signature)}"


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise HostedGatewayError(
                    "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
                )
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_hook)
    except HostedGatewayError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    return value


def verify_transfer_grant(
    token: str,
    config: HostedCellConfig,
    *,
    expected_operation: str,
    expected_tenant_scope: str | None,
    expected_principal_scope: str,
    now: int | float | None = None,
) -> TransferGrant:
    """Verify signature, strict claims, bindings, lifetime, and resource bounds."""

    service_credential = config.service_credential
    if service_credential is None:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_UNAVAILABLE",
            "legacy private transfer is unavailable for dynamic-credential cells",
        )

    try:
        payload, signature = str(token or "").split(".")
    except ValueError as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    presented_signature = _b64decode(signature)
    expected_signature = hmac.new(
        service_credential.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if len(presented_signature) != len(expected_signature) or not hmac.compare_digest(
        presented_signature, expected_signature
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    raw_claims = _b64decode(payload)
    claims = _strict_json_object(raw_claims)
    if canonical_json(claims) != raw_claims:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    if set(claims) != _GRANT_FIELDS or claims.get("v") != TRANSFER_GRANT_VERSION:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    limits = claims.get("limits")
    if not isinstance(limits, dict) or set(limits) != {"max_bytes"}:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in (issued_at, expires_at)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    assert isinstance(issued_at, int) and isinstance(expires_at, int)
    operation = claims.get("op")
    tenant = claims.get("tenant")
    cell = claims.get("cell")
    principal = claims.get("principal")
    audience = claims.get("aud")
    grant_id = claims.get("jti")
    if not all(
        isinstance(value, str) for value in (operation, tenant, cell, principal, audience, grant_id)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    assert isinstance(operation, str)
    assert isinstance(tenant, str)
    assert isinstance(cell, str)
    assert isinstance(principal, str)
    assert isinstance(audience, str)
    assert isinstance(grant_id, str)
    try:
        validate_opaque_scope(tenant, field="tenant scope")
        validate_principal_scope(principal)
        validate_opaque_scope(grant_id, field="grant identity")
    except HostedGatewayError as exc:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid"
        ) from exc
    if (
        audience != TRANSFER_AUDIENCE
        or operation not in _OPERATIONS
        or expected_operation not in _OPERATIONS
        or not hmac.compare_digest(operation, expected_operation)
        or not hmac.compare_digest(cell, config.cell_id)
        or (
            expected_tenant_scope is not None
            and not hmac.compare_digest(tenant, expected_tenant_scope)
        )
        or not hmac.compare_digest(principal, expected_principal_scope)
    ):
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is invalid")
    current = int(time.time() if now is None else now)
    if issued_at > current + TRANSFER_CLOCK_SKEW_SECONDS:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_INVALID", "transfer grant is not yet valid")
    if expires_at <= current:
        raise HostedGatewayError("HOSTED_TRANSFER_GRANT_EXPIRED", "transfer grant has expired")
    if expires_at <= issued_at or expires_at - issued_at > TRANSFER_MAX_TTL_SECONDS:
        raise HostedGatewayError(
            "HOSTED_TRANSFER_GRANT_INVALID", "transfer grant lifetime is invalid"
        )
    max_bytes = _validate_grant_limits(config, operation, limits.get("max_bytes"))
    return TransferGrant(
        operation=operation,
        tenant_scope=tenant,
        cell_id=cell,
        principal_scope=principal,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=grant_id,
        max_bytes=max_bytes,
    )


__all__ = [
    "CELL_HEADER",
    "CONTRACT_SCHEMA_VERSION",
    "PRINCIPAL_HEADER",
    "PROTOCOL_HEADER",
    "REQUEST_HEADER",
    "ROUTING_STOPPED_HEADER",
    "TRANSFER_AUDIENCE",
    "TRANSFER_GRANT_HEADER",
    "TRANSFER_GRANT_VERSION",
    "HostedGatewayError",
    "TransferGrant",
    "TrustedGatewayContext",
    "build_agent_gateway_contract",
    "build_gateway_contract",
    "canonical_contract_json",
    "canonical_json",
    "implicit_retry_scope",
    "hosted_agent_surface_descriptor",
    "mint_transfer_grant",
    "scoped_idempotency_key",
    "validate_opaque_scope",
    "validate_principal_scope",
    "validate_request_id",
    "verify_transfer_grant",
]
