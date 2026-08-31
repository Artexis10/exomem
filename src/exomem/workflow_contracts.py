"""Code-owned parsing, storage, and resolution for workflow contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import yaml
from yaml.constructor import ConstructorError

from .kbdir import kb_dirname
from .vault import (
    PathGuard,
    PathGuardError,
    PlannedWrite,
    batch_atomic_write,
    plan_log_writes,
    read_bounded_guarded_bytes,
    read_guarded_text,
    sanitize_title_filename,
    shipped_schema_target,
)

FAMILY = "workflow"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 512
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_SUMMARIES = 128
_KEY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_OWNERSHIP = re.compile(r"^[a-z][a-z0-9_-]{0,31}(?:\.[a-z][a-z0-9_-]{0,31})+$")
_FIELDS = (
    "type",
    "contract_id",
    "schema_version",
    "key",
    "title",
    "lifecycle",
    "scope",
    "planning",
    "companions",
    "capture",
    "planning_transition",
)
_RENDERER_TEMPLATE = MappingProxyType({
    "algorithm_version": 1,
    "open": "<!-- exomem:workflow-contract-presentation:start -->",
    "close": "<!-- exomem:workflow-contract-presentation:end -->",
    "derived_notice": "<!-- Derived from workflow-contract frontmatter; refresh restores this block. -->",
    "heading": "## Workflow contract: {title}",
    "scope": "This active policy applies to {scope}.",
    "scope_dimensions": ("projects", "domains", "activities"),
    "scope_labels": MappingProxyType(
        {"projects": "projects", "domains": "domains", "activities": "activities"}
    ),
    "scope_dimension": "{dimension}: {values}",
    "all_scope": "all work",
    "standalone_ownership": "Planning holds the complete durable work hierarchy.",
    "companion_ownership": "Planning retains durable intent; declared companion ownership is {companions}.",
    "companion_separator": "; ",
    "companion_overflow": "; … (+{remaining})",
    "list_separator": ", ",
    "list_overflow": ", … (+{remaining})",
    "display_value": "{name} ({owns})",
    "item_cap": 4,
    "json_ensure_ascii": False,
    "line_layout": (
        "open",
        "derived_notice",
        "heading",
        "blank",
        "scope",
        "ownership",
        "records",
        "close",
    ),
    "records": "Records holds observed outcomes; it never completes Planning automatically.",
    "max_bytes": 4096,
})
_AGENT_PROTOCOL_TEMPLATE = MappingProxyType(
    {
        "version": 1,
        "intent": MappingProxyType(
            {
                "explicit": "route",
                "proactive": MappingProxyType(
                    {
                        "requires": ("active-prominence", "durable-intent"),
                        "excludes": ("tentative",),
                    }
                ),
                "planning": (
                    "inspect",
                    "update-one-unambiguous",
                    "create-if-none",
                    "ask-if-ambiguous",
                ),
                "context": MappingProxyType({"missing": "unknown", "null": "known-absent"}),
                "standalone": "complete-durable-hierarchy",
                "companion": "opaque-execution-references-only",
            }
        ),
        "outcomes": MappingProxyType(
            {
                "explicit": "route",
                "proactive": MappingProxyType(
                    {"requires": ("active-prominence", "identified-outcome")}
                ),
                "records": (
                    "inspect",
                    "append-one-compatible",
                    "propose-if-none",
                    "ask-if-ambiguous",
                ),
                "references": "opaque-bounded",
                "transition": MappingProxyType(
                    {
                        "explicit-only": "explicit-user-transition-only",
                        "propose-after-outcome": "propose-review-only",
                        "automatic": "forbidden",
                    }
                ),
            }
        ),
        "review": MappingProxyType(
            {
                "surfaces": ("plan-progress", "unreflected-outcomes"),
                "mode": "deterministic-read-only",
                "completion-inference": "forbidden",
            }
        ),
        "service": MappingProxyType(
            {
                "conversation-classification": "agent-supplied-facts-only",
                "companion-calls": "forbidden",
                "external-state-inference": "forbidden",
            }
        ),
    }
)

_BUILTIN_DECISION = MappingProxyType(
    {
        "planning": MappingProxyType({"mode": "standalone"}),
        "companions": (),
        "capture": MappingProxyType(
            {"durable_intent": "proactive", "observed_outcomes": "proactive"}
        ),
        "planning_transition": "propose-after-outcome",
    }
)


class WorkflowContractError(ValueError):
    """A stable workflow-contract refusal."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Keep SafeLoader's type boundary while refusing ambiguous mappings."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _safe_load_mapping(source: str) -> Any:
    """Parse workflow YAML through SafeLoader without last-key-wins semantics."""
    return yaml.load(source, Loader=_UniqueKeySafeLoader)


@dataclass(frozen=True)
class WorkflowContract:
    data: dict[str, Any]

    @property
    def contract_id(self) -> str:
        return self.data["contract_id"]

    @property
    def key(self) -> str:
        return self.data["key"]

    @property
    def title(self) -> str:
        return self.data["title"]

    @property
    def lifecycle(self) -> str:
        return self.data["lifecycle"]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_semantic_bytes(self.data)).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data))


@dataclass(frozen=True)
class ContractFamily:
    """Code-owned family metadata; vault data never registers behavior."""

    key: str
    schema_versions: tuple[int, ...]
    parser: Any
    validator: Any
    resolver: Any
    renderer: Any
    projector: Any


_FAMILIES: dict[str, ContractFamily] = {}


def registered_families() -> dict[str, ContractFamily]:
    """Return the fixed product registry without exposing a mutation path."""
    return dict(_FAMILIES)


def portable_projection() -> dict[str, Any]:
    """The stable, vault-independent workflow protocol shared by public surfaces."""
    invariants = {
        "planning": "intended future state",
        "records": "observed outcomes and event history",
        "governance": "governance and egress remain authoritative",
        "external_references": "opaque; never proof of external state or capability",
    }
    semantic = {
        "family": FAMILY,
        "schema_version": SCHEMA_VERSION,
        "operations": ("inventory", "inspect", "validate", "resolve", "preview", "save", "refresh"),
        "invariants": invariants,
        "builtin_fallback": _builtin_decision_projection(),
        "context_semantics": {"missing": "unknown", "null": "known-absent"},
        "precedence": ("explicit", "scoped", "default", "builtin"),
        "argument_semantics": "exact-v1",
        "agent_protocol": _agent_protocol_projection(),
        "renderer_template": _renderer_template_projection(),
    }
    return {
        **semantic,
        "digest": hashlib.sha256(_semantic_bytes(semantic)).hexdigest(),
    }


def _renderer_template_projection() -> dict[str, Any]:
    """Return a JSON-shaped portable copy without sharing renderer state."""
    template = dict(_RENDERER_TEMPLATE)
    template["scope_dimensions"] = list(_RENDERER_TEMPLATE["scope_dimensions"])
    template["scope_labels"] = dict(_RENDERER_TEMPLATE["scope_labels"])
    template["line_layout"] = list(_RENDERER_TEMPLATE["line_layout"])
    return template


def _agent_protocol_projection() -> dict[str, Any]:
    """Return a detached JSON-shaped copy of the bounded agent protocol."""
    return {
        "version": _AGENT_PROTOCOL_TEMPLATE["version"],
        "intent": {
            "explicit": _AGENT_PROTOCOL_TEMPLATE["intent"]["explicit"],
            "proactive": {
                "requires": list(_AGENT_PROTOCOL_TEMPLATE["intent"]["proactive"]["requires"]),
                "excludes": list(_AGENT_PROTOCOL_TEMPLATE["intent"]["proactive"]["excludes"]),
            },
            "planning": list(_AGENT_PROTOCOL_TEMPLATE["intent"]["planning"]),
            "context": dict(_AGENT_PROTOCOL_TEMPLATE["intent"]["context"]),
            "standalone": _AGENT_PROTOCOL_TEMPLATE["intent"]["standalone"],
            "companion": _AGENT_PROTOCOL_TEMPLATE["intent"]["companion"],
        },
        "outcomes": {
            "explicit": _AGENT_PROTOCOL_TEMPLATE["outcomes"]["explicit"],
            "proactive": {
                "requires": list(_AGENT_PROTOCOL_TEMPLATE["outcomes"]["proactive"]["requires"])
            },
            "records": list(_AGENT_PROTOCOL_TEMPLATE["outcomes"]["records"]),
            "references": _AGENT_PROTOCOL_TEMPLATE["outcomes"]["references"],
            "planning_reference": {
                "unambiguous": "link-opaque",
                "absent": "record-without-plan",
                "ambiguous": "no-link-surface-review",
            },
            "transition": dict(_AGENT_PROTOCOL_TEMPLATE["outcomes"]["transition"]),
        },
        "review": {
            "surfaces": list(_AGENT_PROTOCOL_TEMPLATE["review"]["surfaces"]),
            "mode": _AGENT_PROTOCOL_TEMPLATE["review"]["mode"],
            "completion-inference": _AGENT_PROTOCOL_TEMPLATE["review"]["completion-inference"],
        },
        "service": dict(_AGENT_PROTOCOL_TEMPLATE["service"]),
    }


def _builtin_decision_projection() -> dict[str, Any]:
    """Return the code-owned standalone posture without sharing mutable state."""
    return {
        "planning": dict(_BUILTIN_DECISION["planning"]),
        "companions": list(_BUILTIN_DECISION["companions"]),
        "capture": dict(_BUILTIN_DECISION["capture"]),
        "planning_transition": _BUILTIN_DECISION["planning_transition"],
    }


def contract_directory(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "contracts" / FAMILY


def migration_marker_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "workflow-contract-migration.yaml"


def ensure_migration_marker(vault_root: Path, *, review_required: bool = False) -> dict[str, Any]:
    """Create the semantic migration marker before changing a shipped scaffold."""
    path = migration_marker_path(vault_root)
    try:
        source, _guard = _guarded_source(vault_root, path)
    except FileNotFoundError:
        source = None
    except (OSError, UnicodeDecodeError, PathGuardError) as error:
        raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE") from error
    if source is not None:
        try:
            marker = _safe_load_mapping(source)
        except yaml.YAMLError as error:
            raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE") from error
        if (
            not isinstance(marker, dict)
            or set(marker) != {"schema_version", "review_required"}
            or type(marker["schema_version"]) is not int
            or marker["schema_version"] != 1
            or type(marker["review_required"]) is not bool
        ):
            raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE")
        return marker
    review_required = review_required or _managed_scaffold_sentinel_exists(vault_root)
    marker = {"schema_version": 1, "review_required": review_required}
    try:
        batch_atomic_write(
            [
                PlannedWrite(
                    path=path,
                    content=f"schema_version: 1\nreview_required: {'true' if review_required else 'false'}\n",
                    create_only=True,
                    guard=_absent_guard(vault_root, path),
                )
            ],
            vault_root=Path(vault_root),
        )
    except PathGuardError as error:
        raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE") from error
    return marker


def _managed_scaffold_sentinel_exists(vault_root: Path) -> bool:
    root = Path(vault_root)
    sentinels = (
        root / kb_dirname() / "_Schema" / "SKILL.md",
        shipped_schema_target(root) / "SKILL.md",
    )
    for sentinel in sentinels:
        try:
            _guarded_source(root, sentinel)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
            raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE") from error
        return True
    return False


def migration_required(vault_root: Path) -> bool | None:
    """Return review state, or ``None`` when a marker cannot be trusted."""
    path = migration_marker_path(vault_root)
    try:
        source, _guard = _guarded_source(vault_root, path)
    except FileNotFoundError:
        try:
            return _managed_scaffold_sentinel_exists(vault_root)
        except WorkflowContractError:
            return None
    except (OSError, UnicodeDecodeError, PathGuardError):
        return None
    try:
        marker = _safe_load_mapping(source)
    except yaml.YAMLError:
        return None
    if (
        not isinstance(marker, dict)
        or set(marker) != {"schema_version", "review_required"}
        or type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 1
        or type(marker.get("review_required")) is not bool
    ):
        return None
    return marker["review_required"]


def parse_proposal(proposal: Mapping[str, Any]) -> WorkflowContract:
    """Validate an exact v1 proposal; reads never repair user-authored data."""
    if not isinstance(proposal, Mapping) or tuple(proposal) != _FIELDS:
        raise WorkflowContractError(
            "WORKFLOW_CONTRACT_INVALID", "proposal must have exact v1 fields"
        )
    data = {key: proposal[key] for key in _FIELDS}
    if (
        data["type"] != "workflow-contract"
        or type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise WorkflowContractError(
            "WORKFLOW_CONTRACT_INVALID", "unsupported type or schema version"
        )
    _uuid(data["contract_id"])
    _token(data["key"], "key")
    _text(data["title"], "title")
    if data["lifecycle"] not in {"active", "archived"}:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "lifecycle")
    _scope(data["scope"])
    if not isinstance(data["planning"], Mapping) or set(data["planning"]) != {"mode"}:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "planning")
    mode = data["planning"].get("mode")
    if mode not in {"standalone", "companion"}:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "planning mode")
    _companions(data["companions"], mode)
    _capture(data["capture"])
    if data["planning_transition"] not in {"explicit-only", "propose-after-outcome"}:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "planning transition")
    return WorkflowContract(data)


def canonical_content(contract: WorkflowContract, authored_body: str = "") -> str:
    frontmatter = yaml.safe_dump(
        contract.as_dict(),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    ).rstrip("\n")
    presentation = render_presentation(contract)
    managed = _presentation_span(authored_body)
    if managed is not None:
        start, end = managed
        return f"---\n{frontmatter}\n---\n" + authored_body[:start] + presentation + authored_body[end:]
    authored = authored_body
    if authored and not authored.endswith("\n"):
        authored += "\n"
    return f"---\n{frontmatter}\n---\n\n{presentation}\n{authored}"


def render_presentation(contract: WorkflowContract) -> str:
    scope = contract.data["scope"]
    selected = (
        _RENDERER_TEMPLATE["list_separator"].join(
            _RENDERER_TEMPLATE["scope_dimension"].format(
                dimension=_RENDERER_TEMPLATE["scope_labels"][dimension],
                values=_quoted_values(scope[dimension]),
            )
            for dimension in _RENDERER_TEMPLATE["scope_dimensions"]
            if scope[dimension]
        )
        or _RENDERER_TEMPLATE["all_scope"]
    )
    if contract.data["planning"]["mode"] == "standalone":
        ownership = _RENDERER_TEMPLATE["standalone_ownership"]
    else:
        cap = _RENDERER_TEMPLATE["item_cap"]
        companions = _RENDERER_TEMPLATE["companion_separator"].join(
            _RENDERER_TEMPLATE["display_value"].format(
                name=_quoted(item["name"]), owns=_quoted_values(item["owns"])
            )
            for item in contract.data["companions"][:cap]
        )
        if len(contract.data["companions"]) > cap:
            companions += _RENDERER_TEMPLATE["companion_overflow"].format(
                remaining=len(contract.data["companions"]) - cap
            )
        ownership = _RENDERER_TEMPLATE["companion_ownership"].format(
            companions=companions
        )
    lines = {
        "open": _RENDERER_TEMPLATE["open"],
        "derived_notice": _RENDERER_TEMPLATE["derived_notice"],
        "heading": _RENDERER_TEMPLATE["heading"].format(title=_quoted(contract.title)),
        "blank": "",
        "scope": _RENDERER_TEMPLATE["scope"].format(scope=selected),
        "ownership": ownership,
        "records": _RENDERER_TEMPLATE["records"],
        "close": _RENDERER_TEMPLATE["close"],
    }
    rendered = "\n".join(
        lines[name] for name in _RENDERER_TEMPLATE["line_layout"]
    )
    if len(rendered.encode("utf-8")) > _RENDERER_TEMPLATE["max_bytes"]:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "presentation exceeds bound")
    return rendered


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=_RENDERER_TEMPLATE["json_ensure_ascii"])


def _quoted_values(values: list[str]) -> str:
    cap = _RENDERER_TEMPLATE["item_cap"]
    rendered = _RENDERER_TEMPLATE["list_separator"].join(_quoted(value) for value in values[:cap])
    return (
        rendered
        if len(values) <= cap
        else rendered
        + _RENDERER_TEMPLATE["list_overflow"].format(remaining=len(values) - cap)
    )


def save_contract(
    vault_root: Path,
    contract: WorkflowContract,
    *,
    why: str,
    name: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    if not isinstance(why, str) or not why.strip():
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID_ARGUMENTS", "why is required")
    path, content, guard, _current_hash = prepare_contract_save(
        vault_root,
        contract,
        name=name,
        expected_hash=expected_hash,
    )
    try:
        log_plan = plan_log_writes(
            Path(vault_root),
            date_iso=date.today().isoformat(),
            op="schema_memory",
            rel_path_no_ext=path.relative_to(vault_root).with_suffix("").as_posix(),
            body="Workflow contract mutation "
            + json.dumps(
                {"key": contract.key, "contract_id": contract.contract_id, "rationale": why},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            operation_token="workflow-contract:"
            + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        if log_plan.warning is not None:
            raise WorkflowContractError("WORKFLOW_CONTRACT_AUDIT_UNAVAILABLE")
        batch_atomic_write(
            [
                PlannedWrite(path=path, content=content, create_only=name is None, guard=guard),
                *log_plan.writes,
            ],
            vault_root=Path(vault_root),
        )
    except PathGuardError as error:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "contract write was refused") from error
    return {
        "key": contract.key,
        "contract_id": contract.contract_id,
        "path": path.relative_to(vault_root).as_posix(),
        "content": content,
        "content_hash": source_hash(content),
        "fingerprint": contract.fingerprint,
        "why": why,
    }


def prepare_contract_save(
    vault_root: Path,
    contract: WorkflowContract,
    *,
    name: str | None = None,
    expected_hash: str | None = None,
    require_expected_hash: bool = True,
) -> tuple[Path, str, PathGuard, str | None]:
    """Run the complete read-only precondition shared by preview and save."""
    if migration_required(vault_root) is None:
        raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE")
    root = contract_directory(vault_root)
    contracts, findings, limited = _scan(
        vault_root, released_only=False, physical_identity=True
    )
    if limited:
        raise WorkflowContractError("WORKFLOW_CONTRACT_SCAN_LIMIT")
    if findings:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID_INVENTORY")
    if name is not None:
        existing = [item for item in contracts if item[0].key == name]
        if not existing:
            raise WorkflowContractError("WORKFLOW_CONTRACT_NOT_FOUND")
        if len(existing) != 1:
            raise WorkflowContractError("WORKFLOW_CONTRACT_DUPLICATE_IDENTITY")
        old_contract, path, _source = existing[0]
        try:
            guarded_source, guard = _guarded_source(vault_root, path)
        except PathGuardError as error:
            raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "unsafe contract path") from error
        if require_expected_hash and (
            expected_hash is None or guard.expected_content_hash != expected_hash
        ):
            raise WorkflowContractError("WORKFLOW_CONTRACT_STALE")
        if old_contract.contract_id != contract.contract_id:
            raise WorkflowContractError(
                "WORKFLOW_CONTRACT_INVALID", "contract identity is immutable"
            )
        if old_contract.key != contract.key:
            raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "contract key is immutable")
        content = canonical_content(contract, _body(guarded_source))
        current_hash = source_hash(guarded_source)
    else:
        if any(
            item.contract_id == contract.contract_id or item.key == contract.key
            for item, _path, _source in contracts
        ):
            raise WorkflowContractError("WORKFLOW_CONTRACT_PATH_CONFLICT", "identity already exists")
        filename = sanitize_title_filename(contract.title, max_length=100) or contract.key
        path = root / f"{filename}.md"
        if any(
            candidate.name.casefold() == path.name.casefold() for candidate in root.glob("*.md")
        ):
            raise WorkflowContractError("WORKFLOW_CONTRACT_PATH_CONFLICT", "title path exists")
        try:
            guard = _absent_guard(vault_root, path)
        except PathGuardError as error:
            if error.code == "PATH_GUARD_CHANGED":
                raise WorkflowContractError(
                    "WORKFLOW_CONTRACT_PATH_CONFLICT", "title path already exists"
                ) from error
            raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "unsafe contract path") from error
        content = canonical_content(contract)
        current_hash = None
    return path, content, guard, current_hash


def is_saved_contract_key(value: object) -> bool:
    """Return whether ``value`` is a legal saved workflow-contract key."""
    return (
        isinstance(value, str)
        and len(value.encode("ascii", "ignore")) == len(value)
        and 1 <= len(value) <= 64
        and _KEY.fullmatch(value) is not None
    )


def _guarded_source(vault_root: Path, path: Path) -> tuple[str, PathGuard]:
    root = Path(vault_root)
    try:
        return read_guarded_text(root, path)
    except FileNotFoundError:
        # `read_guarded_text` cannot retain an ancestor that does not lead to a
        # leaf. Capture the absent leaf before classifying it as missing so a
        # symlinked/reparse parent never becomes a benign no-file result.
        PathGuard.capture(root, path.relative_to(root).as_posix(), leaf_policy="absent")
        raise


def _absent_guard(vault_root: Path, path: Path) -> PathGuard:
    return PathGuard.capture(
        Path(vault_root), path.relative_to(vault_root).as_posix(), leaf_policy="absent"
    )


def inspect_contract(vault_root: Path, name: str) -> dict[str, Any]:
    if migration_required(vault_root) is None:
        raise WorkflowContractError("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE")
    contracts, findings, limited = _scan(vault_root)
    if limited:
        raise WorkflowContractError("WORKFLOW_CONTRACT_SCAN_LIMIT")
    found = [item for item in contracts if item[0].key == name]
    if not found and findings:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID")
    if not found:
        raise WorkflowContractError("WORKFLOW_CONTRACT_NOT_FOUND")
    if len(found) != 1:
        raise WorkflowContractError("WORKFLOW_CONTRACT_DUPLICATE_IDENTITY")
    contract, path, source = found[0]
    return {
        "contract": contract.as_dict(),
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": source_hash(source),
        "fingerprint": contract.fingerprint,
        "presentation_drift": _presentation_drift(contract, source),
    }


def validate_saved_contract(vault_root: Path, name: str) -> dict[str, Any]:
    """Validate one released saved contract without routing through inspection."""
    if migration_required(vault_root) is None:
        return {
            "valid": False,
            "findings": [{"code": "WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE"}],
        }
    contracts, findings, limited = _scan(vault_root)
    if limited:
        return {"valid": False, "findings": [{"code": "WORKFLOW_CONTRACT_SCAN_LIMIT"}]}
    matching_findings = [item for item in findings if item.get("key") == name]
    if matching_findings:
        return {"valid": False, "findings": matching_findings[:32]}
    matches = [item for item in contracts if item[0].key == name]
    if len(matches) != 1:
        code = "WORKFLOW_CONTRACT_DUPLICATE_IDENTITY" if len(matches) > 1 else "WORKFLOW_CONTRACT_NOT_FOUND"
        return {"valid": False, "findings": [{"code": code}]}
    if findings:
        return {"valid": False, "findings": findings[:32]}
    contract, path, source = matches[0]
    return {
        "valid": True,
        "findings": [],
        "proposal": contract.as_dict(),
        "fingerprint": contract.fingerprint,
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": source_hash(source),
        "presentation_drift": _presentation_drift(contract, source),
    }


def inventory_contracts(vault_root: Path) -> dict[str, Any]:
    migration = migration_required(vault_root)
    contracts, findings, limited = _scan(vault_root)
    if not any(item.get("detail") == "unsafe contract directory" for item in findings):
        findings = _unsupported_family_findings(vault_root) + findings
    findings = findings[:32]
    if limited:
        return {"valid": False, "code": "WORKFLOW_CONTRACT_SCAN_LIMIT", "findings": findings}
    summaries = [
        {
            "key": contract.key,
            "contract_id": contract.contract_id,
            "title": contract.title,
            "lifecycle": contract.lifecycle,
            "scope": contract.data["scope"],
            "fingerprint": contract.fingerprint,
        }
        for contract, _path, _source in contracts
        if contract.lifecycle == "active"
    ]
    summaries.sort(key=lambda item: item["key"])
    result = {
        "valid": not findings,
        "summaries": summaries[:MAX_SUMMARIES],
        "total": len(summaries),
        "truncated": len(summaries) > MAX_SUMMARIES,
        "findings": findings[:32],
    }
    if not summaries and migration is True:
        result["status"] = "workflow_contract_migration_required"
    elif migration is None:
        result["status"] = "WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE"
    return result


def _unsupported_family_findings(vault_root: Path) -> list[dict[str, str]]:
    root = contract_directory(vault_root).parent
    probe = root / ".workflow-family-scan-guard"
    try:
        try:
            _absent_guard(vault_root, probe)
        except PathGuardError as error:
            if error.code != "PATH_GUARD_CHANGED":
                raise
            PathGuard.capture(
                Path(vault_root), probe.relative_to(vault_root).as_posix(), leaf_policy="stable"
            )
    except PathGuardError:
        return [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe family directory"}]
    if not root.is_dir():
        return []
    findings: list[dict[str, str]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe family directory"}]
    for path in entries:
        if path.name == FAMILY:
            continue
        try:
            path.lstat()
        except OSError:
            return [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe family directory"}]
        if path.is_symlink():
            return [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe family directory"}]
        if path.is_dir():
            findings.append(
                {
                    "code": "WORKFLOW_CONTRACT_UNSUPPORTED_FAMILY",
                    "path": path.relative_to(vault_root).as_posix(),
                }
            )
    return findings[:32]


def resolve_contracts(
    vault_root: Path,
    context: Mapping[str, str | None] | None,
    *,
    name: str | None = None,
    proposal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _context(context)
    migration = migration_required(vault_root)
    if migration is None:
        return _refusal("WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE")
    if name and proposal is not None:
        return _refusal("WORKFLOW_CONTRACT_INVALID_ARGUMENTS")
    if name == "@standalone":
        return _builtin(normalized, source="explicit")
    if proposal is not None:
        try:
            return _resolved(
                vault_root, parse_proposal(proposal), normalized, "ephemeral", None, None
            )
        except WorkflowContractError as error:
            return _refusal(error.code)
    contracts, findings, limited = _scan(vault_root)
    if limited:
        return _refusal("WORKFLOW_CONTRACT_SCAN_LIMIT")
    if name:
        invalid = next((item for item in findings if item.get("key") == name), None)
        if invalid is not None:
            return _refusal("WORKFLOW_CONTRACT_INVALID", finding=invalid)
        matches = [
            (contract, path, source) for contract, path, source in contracts if contract.key == name
        ]
        if not matches:
            return _refusal("WORKFLOW_CONTRACT_NOT_FOUND")
        if len(matches) != 1:
            return _refusal("WORKFLOW_CONTRACT_DUPLICATE_IDENTITY")
        contract, path, source = matches[0]
        if sum(item[0].contract_id == contract.contract_id for item in contracts) != 1:
            return _refusal("WORKFLOW_CONTRACT_DUPLICATE_IDENTITY")
        if contract.lifecycle != "active":
            return _refusal("WORKFLOW_CONTRACT_INACTIVE")
        return _resolved(vault_root, contract, normalized, "explicit", path, source)
    if findings:
        return _refusal("WORKFLOW_CONTRACT_INVALID_INVENTORY")
    active = [
        (contract, path, source)
        for contract, path, source in contracts
        if contract.lifecycle == "active"
    ]
    defaults = [item for item in active if not any(item[0].data["scope"].values())]
    if len(defaults) > 1:
        return _refusal("WORKFLOW_CONTRACT_AMBIGUOUS", candidates=_candidates(defaults))
    viable: list[tuple[WorkflowContract, Path, str, int]] = []
    for contract, path, source in active:
        matching = 0
        unknown = False
        ruled_out = False
        for context_key, scope_key in (
            ("project", "projects"),
            ("domain", "domains"),
            ("activity", "activities"),
        ):
            selectors = contract.data["scope"][scope_key]
            state, value = normalized[context_key]
            if not selectors:
                continue
            if state == "unknown":
                unknown = True
            elif value is None or value not in selectors:
                ruled_out = True
                break
            else:
                matching += 1
        if not ruled_out and unknown and any(contract.data["scope"].values()):
            return _refusal("WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE")
        if not ruled_out and matching:
            viable.append((contract, path, source, matching))
    if viable:
        maximum = max(item[3] for item in viable)
        winners = [item for item in viable if item[3] == maximum]
        if len(winners) != 1:
            return _refusal(
                "WORKFLOW_CONTRACT_AMBIGUOUS", candidates=_candidates(winners)
            )
        contract, path, source, specificity = winners[0]
        return _resolved(
            vault_root, contract, normalized, "scoped", path, source, specificity=specificity
        )
    if defaults:
        contract, path, source = defaults[0]
        return _resolved(vault_root, contract, normalized, "default", path, source)
    if migration is True and not active:
        return _refusal("WORKFLOW_CONTRACT_MIGRATION_REQUIRED")
    return _builtin(normalized)


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _scan(
    vault_root: Path,
    *,
    released_only: bool = True,
    physical_identity: bool = False,
) -> tuple[list[tuple[WorkflowContract, Path, str]], list[dict[str, str]], bool]:
    root = contract_directory(vault_root)
    try:
        _contract_storage_guard(vault_root)
    except PathGuardError:
        return (
            [],
            [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe contract directory"}],
            False,
        )
    if not root.exists():
        return [], [], False
    if not root.is_dir():
        return (
            [],
            [{"code": "WORKFLOW_CONTRACT_INVALID", "detail": "unsafe contract directory"}],
            False,
        )
    candidates = [
        path
        for path in sorted(root.glob("*.md"), key=lambda value: value.name.casefold())
        if path.is_file() or path.is_symlink()
    ]
    visible_paths = _released_paths(vault_root, candidates) if released_only else set(candidates)
    contracts: list[tuple[WorkflowContract, Path, str]] = []
    findings: list[dict[str, str]] = []
    scanned = 0
    scanned_bytes = 0
    for path in candidates:
        if path not in visible_paths:
            continue
        scanned += 1
        try:
            relative = path.relative_to(vault_root).as_posix()
            if path.lstat().st_size + scanned_bytes > MAX_SCAN_BYTES:
                return (
                    [],
                    [{"code": "WORKFLOW_CONTRACT_SCAN_LIMIT", "detail": "scan bound exceeded"}],
                    True,
                )
            raw, _guard = read_bounded_guarded_bytes(
                Path(vault_root), relative, limit=MAX_SCAN_BYTES - scanned_bytes
            )
        except (OSError, PathGuardError):
            findings.append(
                {
                    "code": "WORKFLOW_CONTRACT_INVALID",
                    "detail": "unreadable contract",
                    "path": path.relative_to(vault_root).as_posix(),
                }
            )
            continue
        scanned_bytes += len(raw)
        if scanned > MAX_FILES or scanned_bytes > MAX_SCAN_BYTES:
            return (
                [],
                [{"code": "WORKFLOW_CONTRACT_SCAN_LIMIT", "detail": "scan bound exceeded"}],
                True,
            )
        if len(raw) > MAX_FILE_BYTES:
            findings.append(
                {"code": "WORKFLOW_CONTRACT_INVALID", "detail": "contract exceeds file bound"}
            )
            continue
        try:
            source = raw.decode("utf-8")
            frontmatter = _frontmatter(source)
            contracts.append((parse_proposal(frontmatter), path, source))
        except (UnicodeDecodeError, yaml.YAMLError, WorkflowContractError):
            try:
                key = _recovered_top_level_key(raw.decode("utf-8"))
            except UnicodeDecodeError:
                key = None
            findings.append(
                {
                    "code": "WORKFLOW_CONTRACT_INVALID",
                    "detail": "invalid contract",
                    "path": path.relative_to(vault_root).as_posix(),
                    **({"key": key} if key is not None else {}),
                }
            )
    identities = (
        contracts
        if physical_identity
        else [item for item in contracts if item[0].lifecycle == "active"]
    )
    keys = [contract.key for contract, _path, _source in identities]
    ids = [contract.contract_id for contract, _path, _source in identities]
    if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
        findings.append(
            {"code": "WORKFLOW_CONTRACT_DUPLICATE_IDENTITY", "detail": "duplicate active identity"}
        )
    return contracts, findings, False


def _contract_storage_guard(vault_root: Path) -> None:
    """Check every configured contract-directory ancestor before enumeration."""
    root = contract_directory(vault_root)
    probe = root / ".workflow-contracts-scan-guard"
    try:
        _absent_guard(vault_root, probe)
    except PathGuardError as error:
        if error.code != "PATH_GUARD_CHANGED":
            raise
        PathGuard.capture(
            Path(vault_root),
            probe.relative_to(vault_root).as_posix(),
            leaf_policy="stable",
        )


def _released_paths(vault_root: Path, candidates: list[Path]) -> set[Path]:
    """Apply the ordinary egress decision before any inventory-derived result."""
    if not candidates:
        return set()
    from .governance import egress

    entries = [{"path": candidate.relative_to(vault_root).as_posix()} for candidate in candidates]
    projected = egress.filter_withheld_entries(vault_root, {"items": entries})
    allowed = projected.get("items", []) if isinstance(projected, Mapping) else []
    return {
        Path(vault_root) / item["path"]
        for item in allowed
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }


def _find_by_key(
    vault_root: Path, key: str, *, released_only: bool = True
) -> tuple[WorkflowContract, Path, str] | None:
    contracts, _findings, _limited = _scan(vault_root, released_only=released_only)
    matches = [item for item in contracts if item[0].key == key]
    return matches[0] if len(matches) == 1 else None


def _frontmatter(source: str) -> dict[str, Any]:
    if not source.startswith("---\n"):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "frontmatter missing")
    closing = source.find("\n---\n", 4)
    if closing < 0:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "frontmatter not closed")
    loaded = _safe_load_mapping(source[4:closing])
    if not isinstance(loaded, dict):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "frontmatter is not mapping")
    return loaded


def _recovered_top_level_key(source: str) -> str | None:
    """Recover one literal top-level key without accepting an invalid contract."""
    if not source.startswith("---\n"):
        return None
    closing = source.find("\n---\n", 4)
    if closing < 0:
        return None
    try:
        document = yaml.compose(source[4:closing], Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(document, yaml.MappingNode):
        return None
    keys = [
        value_node.value
        for key_node, value_node in document.value
        if isinstance(key_node, yaml.ScalarNode)
        and key_node.value == "key"
        and isinstance(value_node, yaml.ScalarNode)
        and value_node.tag == "tag:yaml.org,2002:str"
    ]
    return keys[0] if len(keys) == 1 else None


def _body(source: str) -> str:
    closing = source.find("\n---\n", 4)
    return source[closing + 5 :] if closing >= 0 else ""


def _presentation_span(body: str) -> tuple[int, int] | None:
    open_marker = _RENDERER_TEMPLATE["open"]
    close_marker = _RENDERER_TEMPLATE["close"]
    open_count = body.count(open_marker)
    close_count = body.count(close_marker)
    if open_count == close_count == 0:
        return None
    if open_count != 1 or close_count != 1:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "presentation topology")
    start = body.index(open_marker)
    close = body.index(close_marker)
    if close < start:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "presentation topology")
    return start, close + len(close_marker)


def _presentation_drift(contract: WorkflowContract, source: str) -> bool:
    body = _body(source)
    try:
        span = _presentation_span(body)
    except WorkflowContractError:
        return True
    return span is None or body[slice(*span)] != render_presentation(contract)


def _semantic_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _uuid(value: Any) -> None:
    if not isinstance(value, str):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "contract id")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "contract id") from None
    if str(parsed) != value:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "contract id")


def _text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)
    if value != unicodedata.normalize("NFKC", value).strip() or any(
        unicodedata.category(char).startswith("C") for char in value
    ):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)


def _token(value: Any, field: str, *, project: bool = False) -> None:
    pattern = _PROJECT if project else _KEY
    if (
        not isinstance(value, str)
        or len(value.encode("ascii", "ignore")) != len(value)
        or not pattern.fullmatch(value)
    ):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)
    if not project and len(value) > 64:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)


def _sorted_tokens(value: Any, field: str, *, project: bool = False) -> None:
    if not isinstance(value, list) or len(value) > 16 or value != sorted(value):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)
    if len(value) != len(set(value)):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", field)
    for token in value:
        _token(token, field, project=project)


def _scope(value: Any) -> None:
    if not isinstance(value, Mapping) or tuple(value) != ("projects", "domains", "activities"):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "scope")
    _sorted_tokens(value["projects"], "scope.projects", project=True)
    _sorted_tokens(value["domains"], "scope.domains")
    _sorted_tokens(value["activities"], "scope.activities")


def _companions(value: Any, mode: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > 8
        or (mode == "standalone" and value)
        or (mode == "companion" and not value)
    ):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "companions")
    keys: list[str] = []
    owned: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or tuple(item) != ("key", "name", "owns"):
            raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "companion")
        _token(item["key"], "companion key")
        _text(item["name"], "companion name")
        owns = item["owns"]
        if (
            not isinstance(owns, list)
            or not owns
            or len(owns) > 16
            or owns != sorted(owns)
            or len(owns) != len(set(owns))
        ):
            raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "companion ownership")
        for token in owns:
            if (
                not isinstance(token, str)
                or not 3 <= len(token.encode("ascii", "ignore")) == len(token) <= 128
                or not _OWNERSHIP.fullmatch(token)
                or token in owned
            ):
                raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "companion ownership")
            owned.add(token)
        keys.append(item["key"])
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "companion order")


def _capture(value: Any) -> None:
    if not isinstance(value, Mapping) or tuple(value) != ("durable_intent", "observed_outcomes"):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "capture")
    if any(value[key] not in {"explicit", "proactive"} for key in value):
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID", "capture")


def _context(context: Mapping[str, str | None] | None) -> dict[str, tuple[str, str | None]]:
    if context is None:
        context = {}
    if not isinstance(context, Mapping) or set(context) - {"project", "domain", "activity"}:
        raise WorkflowContractError("WORKFLOW_CONTRACT_INVALID_ARGUMENTS", "context")
    normalized: dict[str, tuple[str, str | None]] = {}
    for key, project in (("project", True), ("domain", False), ("activity", False)):
        if key not in context:
            normalized[key] = ("unknown", None)
        elif context[key] is None:
            normalized[key] = ("absent", None)
        else:
            _token(context[key], key, project=project)
            normalized[key] = ("known", context[key])
    return normalized


def _refusal(code: str, **detail: Any) -> dict[str, Any]:
    return {"resolved": False, "code": code, **detail}


def _candidates(items: list[tuple[Any, ...]]) -> list[dict[str, str]]:
    return [
        {"key": item[0].key, "contract_id": item[0].contract_id}
        for item in sorted(items, key=lambda item: (item[0].contract_id, item[0].key))[:16]
    ]


def _builtin(
    context: dict[str, tuple[str, str | None]], *, source: str = "builtin"
) -> dict[str, Any]:
    return {
        "resolved": True,
        "source": source,
        "schema_version": SCHEMA_VERSION,
        "context": context,
        "decision": _builtin_decision_projection(),
        "capabilities": {"declared_companion_keys": [], "available": []},
        "agent_protocol": _agent_protocol_projection(),
        "explanation": "Planning owns intended future state and Records holds observed outcomes.",
        "warnings": [],
    }


def _resolved(
    vault_root: Path,
    contract: WorkflowContract,
    context: dict[str, tuple[str, str | None]],
    source: str,
    path: Path | None,
    raw: str | None,
    *,
    specificity: int | None = None,
) -> dict[str, Any]:
    result = {
        "resolved": True,
        "source": source,
        "schema_version": SCHEMA_VERSION,
        "context": context,
        "contract_id": contract.contract_id,
        "key": contract.key,
        "title": contract.title,
        "fingerprint": contract.fingerprint,
        "decision": {
            "planning": contract.data["planning"],
            "companions": contract.data["companions"],
            "capture": contract.data["capture"],
            "planning_transition": contract.data["planning_transition"],
        },
        "capabilities": {
            "declared_companion_keys": [item["key"] for item in contract.data["companions"]],
            "available": [],
        },
        "agent_protocol": _agent_protocol_projection(),
        "explanation": render_presentation(contract),
        "warnings": [],
    }
    if path is not None and raw is not None:
        result["path"] = path.relative_to(vault_root).as_posix()
        result["source_hash"] = source_hash(raw)
    if specificity is not None:
        result["specificity"] = specificity
    return result


_FAMILIES[FAMILY] = ContractFamily(
    key=FAMILY,
    schema_versions=(SCHEMA_VERSION,),
    parser=parse_proposal,
    validator=parse_proposal,
    resolver=resolve_contracts,
    renderer=render_presentation,
    projector=portable_projection,
)
