"""Deterministic, tenant-neutral Hosted client plugin candidates."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import commands, hosted_gateway

PLUGIN_ROOT = Path("plugins/hosted")
PLATFORMS = ("claude", "openai")
DEMOTION_REASONS = frozenset(
    {"artifact-withdrawn", "client-regression", "contract-drift", "operator-withdrawal"}
)
SKILL_NAMES = (
    "exomem",
    "exomem-capture",
    "exomem-continue",
    "exomem-reflect",
    "exomem-research",
    "exomem-review",
)
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_LEGACY_OR_EXCLUDED_TOOLS = frozenset(
    {
        "edit_memory",
        "replace_memory",
        "transfer_artifact",
        "process_media",
        "adopt_vault",
        "adoption_studio",
        "maintain_memory",
        "schema_memory",
        "manage_memory_file",
        "query_dataset",
        "read_media",
        "coordination_status",
        "create_file",
        "move_file",
        "delete",
        "edit",
        "replace",
        "note",
        "find",
        "get",
        "add",
        "query_data",
    }
)
_CANONICAL_CALLABLES = frozenset(
    {command.name for command in commands.COMMANDS}
    | set(commands.PRODUCT_PUBLIC_NAMES)
    | {route for command in commands.COMMANDS for route in command.routes}
    | {action for command in commands.COMMANDS for action in command.product_actions}
    | _LEGACY_OR_EXCLUDED_TOOLS
)
_RAW_PRIVATE_IDENTITY_FIELDS = frozenset(
    {
        "clean_client_identity",
        "paired_run_id",
        "exomem_identity",
        "tenant",
        "entitlement",
        "provisioning_operation",
        "cell",
    }
)
TOOL_REFERENCE = re.compile(
    r"(?:`|\b)("
    + "|".join(re.escape(name) for name in sorted(_CANONICAL_CALLABLES))
    + r")(?:`|\s*\()"
)
PROSE_CALL_REFERENCE = re.compile(
    r"\b(?:use|call|run|invoke|execute|via)\s+([a-z_][a-z0-9_]*)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class HostedDefinition:
    plugin_id: str
    version: str
    endpoint: str
    profile: str
    channel: str
    distribution_scope: str
    source_release: str
    support_url: str
    privacy_url: str
    terms_url: str
    website_url: str
    repository_url: str
    license: str
    author_name: str
    author_url: str
    claude_schema_version: str
    openai_schema_version: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_difference_paths(left: Any, right: Any, *, limit: int = 12) -> tuple[str, ...]:
    """Return bounded, value-free paths that explain a JSON contract mismatch."""

    differences: list[str] = []

    def visit(expected: Any, actual: Any, path: str) -> None:
        if len(differences) >= limit or expected == actual:
            return
        if type(expected) is not type(actual):
            differences.append(path)
            return
        if isinstance(expected, dict):
            for key in sorted(set(expected) | set(actual)):
                nested_path = f"{path}.{key}" if path else str(key)
                if key not in expected or key not in actual:
                    differences.append(nested_path)
                else:
                    visit(expected[key], actual[key], nested_path)
                if len(differences) >= limit:
                    return
            return
        if isinstance(expected, list):
            for index in range(max(len(expected), len(actual))):
                nested_path = f"{path}[{index}]"
                if index >= len(expected) or index >= len(actual):
                    differences.append(nested_path)
                else:
                    visit(expected[index], actual[index], nested_path)
                if len(differences) >= limit:
                    return
            return
        differences.append(path or "root")

    visit(left, right, "")
    return tuple(differences)


def _repo_root(repo_root: Path | None = None) -> Path:
    return (repo_root or Path(__file__).resolve().parents[2]).resolve()


def _validate_definition(raw: dict[str, Any]) -> HostedDefinition:
    allowed = {
        "plugin_id",
        "version",
        "endpoint",
        "profile",
        "channel",
        "distribution_scope",
        "source_release",
        "support_url",
        "privacy_url",
        "terms_url",
        "claude_schema_version",
        "openai_schema_version",
        "website_url",
        "repository_url",
        "license",
        "author_name",
        "author_url",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(f"unsupported Hosted definition fields: {', '.join(sorted(unexpected))}")
    missing = allowed - set(raw)
    if missing:
        raise ValueError(f"missing Hosted definition fields: {', '.join(sorted(missing))}")
    if not SEMVER.fullmatch(str(raw["version"])):
        raise ValueError("plugin version must be a strict semantic version")
    if not SEMVER.fullmatch(str(raw["source_release"])):
        raise ValueError("source release must be a strict semantic version")
    endpoint = str(raw["endpoint"])
    if not endpoint.startswith("https://") or not endpoint.endswith("/mcp/v1"):
        raise ValueError("Hosted endpoint must be a fixed HTTPS versioned /mcp/v1 resource")
    if raw["channel"] != "production":
        raise ValueError("only the production channel is distributable")
    if endpoint != "https://substratesystems.io/api/exomem/mcp/v1":
        raise ValueError("production endpoint must be the canonical Hosted resource")
    if raw["profile"] != commands.HOSTED_ALPHA_AGENT_PROFILE:
        raise ValueError("Hosted definition must use the exact alpha profile")
    if raw["distribution_scope"] != "pending":
        raise ValueError("Hosted distribution scope remains pending real-client acceptance")
    for key in ("support_url", "privacy_url", "terms_url"):
        if not str(raw[key]).startswith("https://"):
            raise ValueError(f"{key} must be an HTTPS URL")
    return HostedDefinition(**{key: str(value) for key, value in raw.items()})


def load_definition_file(path: Path) -> HostedDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Hosted definition must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Hosted definition must be an object")
    return _validate_definition(raw)


def load_definition(repo_root: Path | None = None) -> HostedDefinition:
    root = _repo_root(repo_root)
    return load_definition_file(root / PLUGIN_ROOT / "definition.json")


def _skill_paths(root: Path) -> tuple[Path, ...]:
    return tuple(root / PLUGIN_ROOT / "skills" / name / "SKILL.md" for name in SKILL_NAMES)


def _frontmatter(text: str, skill: Path) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError(f"{skill}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{skill}: unterminated frontmatter")
    fields: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"{skill}: invalid frontmatter")
        fields[key.strip()] = value.strip()
    required = fields.get("required_tools", "")
    if not required.startswith("[") or not required.endswith("]"):
        raise ValueError(f"{skill}: required_tools must be a list")
    fields["required_tools"] = tuple(
        item.strip() for item in required[1:-1].split(",") if item.strip()
    )
    return fields


def validate_skill_text(text: str, skill: Path) -> tuple[str, ...]:
    allowed = set(
        commands.product_commands_for_profile(commands.HOSTED_ALPHA_AGENT_PROFILE, "rest")
    )
    allowed_names = {command.name for command in allowed}
    declared = _frontmatter(text, skill)["required_tools"]
    observed = set(TOOL_REFERENCE.findall(text))
    observed.update(
        name for name in PROSE_CALL_REFERENCE.findall(text) if name in _CANONICAL_CALLABLES
    )
    observed = tuple(sorted(observed))
    unavailable = (set(declared) | set(observed)) - allowed_names
    if unavailable:
        raise ValueError(f"{skill}: unavailable Hosted tools: {', '.join(sorted(unavailable))}")
    missing_declaration = set(observed) - set(declared)
    if missing_declaration:
        raise ValueError(
            f"{skill}: callable tools must be declared: {', '.join(sorted(missing_declaration))}"
        )
    unused = set(declared) - set(observed)
    if unused:
        raise ValueError(f"{skill}: declared tools are not used: {', '.join(sorted(unused))}")
    return tuple(declared)


def skill_dependencies(repo_root: Path | None = None) -> dict[str, tuple[str, ...]]:
    root = _repo_root(repo_root)
    result: dict[str, tuple[str, ...]] = {}
    for skill in _skill_paths(root):
        text = skill.read_text(encoding="utf-8")
        result[skill.parent.name] = validate_skill_text(text, skill)
    return result


def validate_behavior_observation(scenario: dict[str, Any], observation: dict[str, Any]) -> None:
    """Evaluate one real-client trace against a behavior fixture scenario."""

    if not isinstance(scenario, dict) or not isinstance(observation, dict):
        raise ValueError("behavior scenario and observation must be objects")
    tools = observation.get("tools")
    if tools != scenario.get("expected_tools"):
        raise ValueError("behavior observation has a different tool sequence")
    if not isinstance(tools, list) or any(tool not in _CANONICAL_CALLABLES for tool in tools):
        raise ValueError("behavior observation contains an unknown tool")
    forbidden = scenario.get("forbidden_tools")
    if not isinstance(forbidden, list) or set(tools) & set(forbidden):
        raise ValueError("behavior observation used a forbidden tool")
    if observation.get("fresh_chat") is not scenario.get("fresh_chat"):
        raise ValueError("behavior observation has the wrong conversation boundary")
    citation = observation.get("citation")
    if scenario.get("citation") is True:
        if not isinstance(citation, str) or not citation.strip():
            raise ValueError("behavior observation is missing a useful citation")
    elif citation is not None:
        raise ValueError("behavior observation contains an unexpected citation")
    write_count = observation.get("write_count")
    if type(write_count) is not int or write_count < 0:
        raise ValueError("behavior observation has an invalid write count")
    if scenario.get("no_write") is True and write_count != 0:
        raise ValueError("behavior observation made a forbidden write")
    capture_contract = scenario.get("capture")
    capture = observation.get("capture")
    if capture_contract is None:
        if capture is not None:
            raise ValueError("behavior observation contains an unexpected capture")
        return
    if not isinstance(capture_contract, dict) or not isinstance(capture, dict):
        raise ValueError("behavior observation is missing its capture")
    if write_count < 1:
        raise ValueError("behavior observation capture requires a durable write")
    text = capture.get("text")
    if (
        capture.get("kind") != capture_contract.get("kind")
        or capture.get("distilled") is not True
        or capture.get("transcript_dump") is not False
        or not isinstance(text, str)
        or not text.strip()
        or len(text.split()) > capture_contract.get("max_words", 0)
    ):
        raise ValueError("behavior observation capture violates the distilled payload contract")


def validate_hosted_public_inputs(
    repo_root: Path | None = None, *, include_generated: bool = True
) -> None:
    """Fail closed over Hosted sources and committed candidate files before release."""

    root = _repo_root(repo_root)
    hosted_root = root / PLUGIN_ROOT
    paths = [
        hosted_root / "definition.json",
        hosted_root / "behavior-fixtures-v1.json",
        hosted_root / "acceptance-fixture-v1.json",
    ]
    paths.extend(_skill_paths(root))
    directories = ["assets", "acceptance", "promotion"]
    if include_generated:
        directories.append("generated")
    for directory in directories:
        candidate = hosted_root / directory
        if candidate.exists():
            paths.extend(
                path for path in candidate.rglob("*") if path.is_file() or path.is_symlink()
            )
    forbidden = re.compile(
        r"(?i)(\[todo:|\$\{[^}]+\}|exomem_vault_path|localhost|127\.0\.0\.1|"
        r"file://|[A-Z0-9_]*(?:token|secret|password)\s*[:=]|"
        r"\btenant[_-]?id\b|\bvault[_-]?path\b|"
        r"\buvx\b|\bhooks?\b)"
    )

    def inspect(path: Path, content: bytes | None = None) -> None:
        if path.is_symlink():
            raise ValueError(f"Hosted public artifact may not be a symlink: {path}")
        if path.suffix.lower() not in {".json", ".md", ".svg", ".zip"}:
            raise ValueError(f"Hosted public artifact has an unknown binary format: {path}")
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(
                io.BytesIO(content if content is not None else path.read_bytes())
            ) as archive_file:
                for info in archive_file.infolist():
                    member = Path(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        member.is_absolute()
                        or ".." in member.parts
                        or (mode and stat.S_IFMT(mode) != stat.S_IFREG)
                    ):
                        raise ValueError(
                            f"Hosted archive has an unsafe member: {path}!{info.filename}"
                        )
                    if info.is_dir() or member.suffix.lower() not in {".json", ".md", ".svg"}:
                        raise ValueError(
                            f"Hosted archive has an unknown member: {path}!{info.filename}"
                        )
                    inspect(Path(info.filename), archive_file.read(info))
            return
        try:
            text = (content if content is not None else path.read_bytes()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Hosted public artifact is not UTF-8: {path}") from exc
        if forbidden.search(text):
            raise ValueError(f"Hosted public artifact is unsafe: {path}")
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Hosted public JSON is invalid: {path}") from exc

            def inspect_json(value: Any) -> None:
                if isinstance(value, dict):
                    for key, nested in value.items():
                        evidence_path = (
                            "promotion" in path.parts
                            or "acceptance" in path.parts
                            or path.name.startswith("acceptance-")
                        )
                        if (
                            evidence_path
                            and key in _RAW_PRIVATE_IDENTITY_FIELDS
                            and isinstance(nested, str)
                        ):
                            raise ValueError(
                                f"Hosted public artifact contains a raw private identifier: {path}"
                            )
                        if re.search(r"(?i)(?:token|secret|password)$", str(key)) and isinstance(
                            nested, str
                        ):
                            raise ValueError(
                                f"Hosted public artifact contains a credential value: {path}"
                            )
                        inspect_json(nested)
                elif isinstance(value, list):
                    for nested in value:
                        inspect_json(nested)

            inspect_json(payload)

    for path in paths:
        inspect(path)


def _skills_digest(root: Path) -> str:
    payload = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8").replace("\r\n", "\n")
        for path in _skill_paths(root)
    }
    return _sha256(_canonical_json(payload))


def oauth_discovery_overlay(contract: dict[str, Any]) -> dict[str, Any]:
    """Gateway-owned OAuth discovery metadata, separate from raw cell schemas."""

    return {
        "schema_version": 1,
        "resource": "https://substratesystems.io/api/exomem/mcp/v1",
        "protected_resource_metadata": (
            "https://substratesystems.io/.well-known/oauth-protected-resource/api/exomem/mcp/v1"
        ),
        "issuer": "https://substratesystems.io/api/exomem/oauth",
        "authorization_server_metadata": (
            "https://substratesystems.io/.well-known/oauth-authorization-server/api/exomem/oauth"
        ),
        "authorize_url": "https://substratesystems.io/api/exomem/oauth/authorize",
        "token_url": "https://substratesystems.io/api/exomem/oauth/token",
        "revoke_url": "https://substratesystems.io/api/exomem/oauth/revoke",
        "securitySchemes": {
            "oauth2": {
                "authorization_url": "https://substratesystems.io/api/exomem/oauth/authorize",
                "token_url": "https://substratesystems.io/api/exomem/oauth/token",
                "scopes": {
                    "exomem.read": "Read governed memory",
                    "exomem.write": "Write governed memory",
                },
            }
        },
        "tools": {
            entry["name"]: {
                "securitySchemes": [
                    {
                        "type": "oauth2",
                        "scopes": ["exomem.read" if entry["read_only"] else "exomem.write"],
                    }
                ]
            }
            for entry in contract["commands"]
        },
        "runtime_requirement": (
            "MCP responses include _meta['mcp/www_authenticate'] from the gateway."
        ),
    }


def compatibility_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = _repo_root(repo_root)
    definition = load_definition(root)
    dependencies = skill_dependencies(root)
    contract = hosted_gateway.build_agent_gateway_contract(profile=definition.profile)
    if definition.source_release != contract["exomem_release"]:
        raise ValueError(
            "Hosted definition source release must match the agent contract release"
        )
    oauth_overlay = oauth_discovery_overlay(contract)
    raw_definition = json.loads(
        (root / PLUGIN_ROOT / "definition.json").read_text(encoding="utf-8")
    )
    commands_in_order = tuple(item["name"] for item in contract["commands"])
    base = {
        "schema_version": 1,
        "plugin_id": definition.plugin_id,
        "plugin_version": definition.version,
        "endpoint": definition.endpoint,
        "profile": definition.profile,
        "source_release": definition.source_release,
        "commands": list(commands_in_order),
        "command_surface_sha256": contract["agent_profile"]["active_capability_sha256"],
        "schema_contract_sha256": contract["digest"]["value"],
        "definition_sha256": _sha256(_canonical_json(raw_definition)),
        "skills_sha256": _skills_digest(root),
        "skills": {name: list(required_tools) for name, required_tools in dependencies.items()},
        "agent_contract": contract,
        "oauth_discovery": oauth_overlay,
        "oauth_discovery_sha256": _sha256(_canonical_json(oauth_overlay)),
    }
    return {**base, "compatibility_sha256": _sha256(_canonical_json(base))}


def check_compatibility_descriptor(repo_root: Path | None = None) -> None:
    root = _repo_root(repo_root)
    path = root / PLUGIN_ROOT / "generated" / "compatibility.json"
    try:
        committed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Hosted compatibility descriptor must be valid JSON") from exc
    actual = compatibility_manifest(root)
    if committed != actual:
        paths = ", ".join(_json_difference_paths(committed, actual))
        raise ValueError(f"Hosted compatibility descriptor is stale: {paths}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value) + b"\n")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    try:
        temporary.write_bytes(_canonical_json(value) + b"\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    try:
        temporary.write_bytes(value)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_skills(root: Path, destination: Path) -> None:
    for source in _skill_paths(root):
        target = destination / source.relative_to(root / PLUGIN_ROOT / "skills")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            source.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n"
        )


def _copy_assets(root: Path, destination: Path) -> None:
    for source in sorted((root / PLUGIN_ROOT / "assets").rglob("*")):
        if source.is_file():
            target = destination / source.relative_to(root / PLUGIN_ROOT / "assets")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def _files_digest(directory: Path, *, exclude: Iterable[str] = ()) -> str:
    excluded = set(exclude)
    entries: list[tuple[str, str]] = []
    for path in directory.rglob("*"):
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if relative not in excluded:
                entries.append((relative, _sha256(path.read_bytes())))
    return _sha256(_canonical_json(sorted(entries)))


def _map_digest(files: dict[str, bytes], prefix: str) -> str:
    entries = [
        (path.removeprefix(prefix), _sha256(contents))
        for path, contents in sorted(files.items())
        if path.startswith(prefix)
    ]
    return _sha256(_canonical_json(entries))


def _archive_bytes_from_entries(entries: Iterable[tuple[str, bytes]]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive_file:
        for name, contents in sorted(entries):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            # ZipInfo otherwise writes the host OS into the central directory
            # (Windows=0, Unix=3), making identical package inputs drift in CI.
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_STORED
            archive_file.writestr(entry, contents)
    return payload.getvalue()


def _archive_bytes_from_map(files: dict[str, bytes], prefix: str) -> bytes:
    return _archive_bytes_from_entries(
        (path.removeprefix(prefix), contents)
        for path, contents in files.items()
        if path.startswith(prefix)
    )


def _package_lock(
    root: Path,
    platform: str,
    artifact_root: Path,
    *,
    registered_app_id: str | None = None,
) -> dict[str, Any]:
    definition = load_definition(root)
    compatibility = compatibility_manifest(root)
    lock = {
        "schema_version": 1,
        "platform": platform,
        "platform_schema_version": getattr(definition, f"{platform}_schema_version"),
        "plugin_id": definition.plugin_id,
        "plugin_version": definition.version,
        "endpoint": definition.endpoint,
        "profile": definition.profile,
        "command_surface_sha256": compatibility["command_surface_sha256"],
        "schema_contract_sha256": compatibility["schema_contract_sha256"],
        "definition_sha256": compatibility["definition_sha256"],
        "skills_sha256": compatibility["skills_sha256"],
        "compatibility_sha256": compatibility["compatibility_sha256"],
        "oauth_discovery_sha256": compatibility["oauth_discovery_sha256"],
        "artifact_sha256": _files_digest(artifact_root),
    }
    if platform == "openai":
        if registered_app_id is None:
            raise ValueError("OpenAI package lock requires a registered app identity")
        lock["registered_app_id_sha256"] = _registered_app_id_sha256(registered_app_id)
    return lock


def _validate_openai_app_id(value: str | None) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9]+", clean):
        raise ValueError("OpenAI candidate requires a registered OpenAI app release input")
    return clean


def _registered_app_id_sha256(value: str) -> str:
    return _sha256(_validate_openai_app_id(value).encode("utf-8"))


def _generated_openai_app_id(generated: Path) -> str:
    try:
        app_id = json.loads(
            (generated / "openai" / ".app.json").read_text(encoding="utf-8")
        )["apps"]["exomem"]["id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenAI candidate is registration-pending or invalid") from exc
    return _validate_openai_app_id(app_id)


def _validate_openai_lock_identity(generated: Path, app_id: str) -> None:
    expected = _registered_app_id_sha256(app_id)
    for name in ("openai.lock.json", "openai.zip.lock.json"):
        try:
            lock = json.loads((generated / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("OpenAI candidate is registration-pending or invalid") from exc
        if lock.get("registered_app_id_sha256") != expected:
            raise ValueError("OpenAI lock does not bind the registered app identity")


def validate_openai_candidate(package: Path) -> None:
    """Repository-owned equivalent of the current universal-plugin ingestion gate."""

    try:
        plugin = json.loads((package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        app = json.loads((package / ".app.json").read_text(encoding="utf-8"))
        marketplace = json.loads((package / "marketplace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("OpenAI candidate manifests must be valid JSON") from exc
    if (
        plugin.get("skills") != "./skills/"
        or plugin.get("mcpServers") != "./.mcp.json"
        or plugin.get("apps") != "./.app.json"
    ):
        raise ValueError("OpenAI plugin manifest has invalid companion paths")
    if (
        set(app) != {"apps"}
        or not isinstance(app["apps"].get("exomem"), dict)
        or set(app["apps"]["exomem"]) - {"id", "category"}
    ):
        raise ValueError("OpenAI app manifest must contain only the registered app mapping")
    try:
        app_id = _validate_openai_app_id(app["apps"]["exomem"].get("id"))
    except ValueError as exc:
        raise ValueError("OpenAI app manifest must contain a registered app ID") from exc
    plugins = marketplace.get("plugins")
    if (
        set(marketplace) != {"name", "interface", "plugins"}
        or marketplace.get("name") != plugin.get("name")
        or marketplace.get("interface") != {"displayName": "Exomem Hosted"}
    ):
        raise ValueError("OpenAI marketplace interface contains unsupported fields")
    expected_marketplace = {
        "name": plugin.get("name"),
        "source": {"source": "local", "path": "./plugins/exomem-hosted"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "productivity",
    }
    if not isinstance(plugins, list) or len(plugins) != 1 or plugins[0] != expected_marketplace:
        raise ValueError("OpenAI marketplace metadata must own ON_INSTALL authentication")
    _validate_openai_lock_identity(package.parent, app_id)


def _interface_metadata(definition: HostedDefinition) -> dict[str, Any]:
    return {
        "displayName": "Exomem Hosted",
        "shortDescription": "Governed long-term memory.",
        "longDescription": "Governed long-term memory for relevant project work.",
        "developerName": definition.author_name,
        "category": "productivity",
        "capabilities": ["Interactive", "Write"],
        "websiteURL": definition.website_url,
        "privacyPolicyURL": definition.privacy_url,
        "termsOfServiceURL": definition.terms_url,
        "brandColor": "#172033",
        "composerIcon": "./assets/icon.svg",
        "logo": "./assets/icon.svg",
        "logoDark": "./assets/icon.svg",
        "screenshots": [],
        "defaultPrompt": ["Use governed long-term memory."],
    }


def candidate_files(
    repo_root: Path | None = None, *, platform: str = "claude", openai_app_id: str | None = None
) -> dict[str, bytes]:
    """Return deterministic candidate bytes without creating a staging directory."""

    root = _repo_root(repo_root)
    definition = load_definition(root)
    skill_dependencies(root)
    validate_hosted_public_inputs(root, include_generated=False)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    app_id = _validate_openai_app_id(openai_app_id) if "openai" in selected else None
    files: dict[str, bytes] = {}
    for item in selected:
        prefix = f"{item}/"
        for source in _skill_paths(root):
            files[
                prefix + "skills/" + source.relative_to(root / PLUGIN_ROOT / "skills").as_posix()
            ] = source.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        for source in (root / PLUGIN_ROOT / "assets").rglob("*"):
            if source.is_file():
                files[
                    prefix
                    + "assets/"
                    + source.relative_to(root / PLUGIN_ROOT / "assets").as_posix()
                ] = source.read_bytes()
        files[prefix + ".mcp.json"] = (
            _canonical_json(
                {"mcpServers": {"exomem": {"type": "http", "url": definition.endpoint}}}
            )
            + b"\n"
        )
        if item == "claude":
            files[prefix + ".claude-plugin/plugin.json"] = (
                _canonical_json(
                    {
                        "name": definition.plugin_id,
                        "version": definition.version,
                        "description": "Governed long-term memory for relevant project work.",
                        "author": {"name": definition.author_name, "url": definition.author_url},
                        "homepage": definition.website_url,
                        "repository": definition.repository_url,
                        "license": definition.license,
                        "keywords": ["memory", "knowledge", "governance"],
                    }
                )
                + b"\n"
            )
        else:
            files[prefix + ".codex-plugin/plugin.json"] = (
                _canonical_json(
                    {
                        "id": definition.plugin_id,
                        "name": definition.plugin_id,
                        "version": definition.version,
                        "description": "Governed long-term memory for relevant project work.",
                        "skills": "./skills/",
                        "mcpServers": "./.mcp.json",
                        "apps": "./.app.json",
                        "author": {"name": definition.author_name, "url": definition.author_url},
                        "homepage": definition.website_url,
                        "repository": definition.repository_url,
                        "license": definition.license,
                        "keywords": ["memory", "knowledge", "governance"],
                        "interface": _interface_metadata(definition),
                    }
                )
                + b"\n"
            )
            files[prefix + ".app.json"] = (
                _canonical_json({"apps": {"exomem": {"id": app_id, "category": "productivity"}}})
                + b"\n"
            )
            files[prefix + "marketplace.json"] = (
                _canonical_json(
                    {
                        "name": definition.plugin_id,
                        "interface": {"displayName": "Exomem Hosted"},
                        "plugins": [
                            {
                                "name": definition.plugin_id,
                                "source": {"source": "local", "path": "./plugins/exomem-hosted"},
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_INSTALL",
                                },
                                "category": "productivity",
                            }
                        ],
                    }
                )
                + b"\n"
            )
        lock = _package_lock(
            root,
            item,
            root / PLUGIN_ROOT / "generated" / item,
            registered_app_id=app_id,
        )
        lock["artifact_sha256"] = _map_digest(files, prefix)
        files[f"{item}.lock.json"] = _canonical_json(lock) + b"\n"
        archive_bytes = _archive_bytes_from_map(files, prefix)
        files[f"{item}.zip"] = archive_bytes
        files[f"{item}.zip.lock.json"] = (
            _canonical_json(
                {
                    "platform": item,
                    "archive_sha256": _sha256(archive_bytes),
                    **(
                        {"registered_app_id_sha256": _registered_app_id_sha256(app_id)}
                        if item == "openai"
                        else {}
                    ),
                }
            )
            + b"\n"
        )
    files["compatibility.json"] = _canonical_json(compatibility_manifest(root)) + b"\n"
    return files


def render(
    repo_root: Path | None = None,
    output: Path | None = None,
    *,
    openai_app_id: str | None = None,
    platform: str = "claude",
    staging_root: Path | None = None,
) -> Path:
    root = _repo_root(repo_root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    destination = (output or root / PLUGIN_ROOT / "generated").resolve()
    allowed_root = (staging_root or (root / PLUGIN_ROOT)).resolve()
    if allowed_root not in destination.parents or destination == allowed_root:
        raise ValueError("render output must be below the explicit staging root")
    if destination == root or destination in root.parents:
        raise ValueError("render output must not be at or above the repository")
    managed_destination = (root / PLUGIN_ROOT / "generated").resolve()
    if destination.exists() and destination != managed_destination:
        raise ValueError("render output already exists; refuse to replace an unchecked directory")
    selected = PLATFORMS if platform == "all" else (platform,)
    with ExitStack() as release_locks:
        if destination == managed_destination:
            for selected_platform in sorted(PLATFORMS):
                release_locks.enter_context(_promotion_mutex(root, selected_platform))
        candidate = candidate_files(root, platform=platform, openai_app_id=openai_app_id)
        nonce = uuid4().hex
        temporary_root = allowed_root / f".exomem-hosted-render-{nonce}"
        temporary = temporary_root / "generated"
        backup: Path | None = None
        try:
            temporary_root.mkdir(parents=False, exist_ok=False)
            if destination.exists():
                shutil.copytree(destination, temporary)
            else:
                temporary.mkdir()
            for selected_platform in selected:
                package = temporary / selected_platform
                if package.exists():
                    shutil.rmtree(package)
                for suffix in (".lock.json", ".zip", ".zip.lock.json"):
                    (temporary / f"{selected_platform}{suffix}").unlink(missing_ok=True)
            for relative, contents in candidate.items():
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
            if "openai" in selected:
                validate_openai_candidate(temporary / "openai")

            if destination.exists():
                backup = destination.with_name(f".{destination.name}.previous-{nonce}")
                destination.replace(backup)
            try:
                temporary.replace(destination)
            except OSError:
                if backup is not None and backup.exists() and not destination.exists():
                    backup.replace(destination)
                raise
            if backup is not None:
                shutil.rmtree(backup)
            return destination
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)


def check(
    repo_root: Path | None = None,
    *,
    openai_app_id: str | None = None,
    platform: str = "claude",
) -> None:
    root = _repo_root(repo_root)
    validate_hosted_public_inputs(root)
    check_compatibility_descriptor(root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    expected = root / PLUGIN_ROOT / "generated"
    if "openai" in selected:
        generated_app_id = _generated_openai_app_id(expected)
        if openai_app_id is None:
            openai_app_id = generated_app_id
        elif _validate_openai_app_id(openai_app_id) != generated_app_id:
            raise ValueError("OpenAI candidate app identity does not match the requested release")
        _validate_openai_lock_identity(expected, generated_app_id)
    expected_files = {
        path.relative_to(expected).as_posix(): path.read_bytes()
        for path in expected.rglob("*")
        if path.is_file()
        and (
            path.parts[len(expected.parts)] in selected
            or path.name
            in {
                "compatibility.json",
                *(f"{item}.lock.json" for item in selected),
                *(f"{item}.zip" for item in selected),
                *(f"{item}.zip.lock.json" for item in selected),
            }
        )
    }
    actual_files = candidate_files(root, platform=platform, openai_app_id=openai_app_id)
    if expected_files != actual_files:
        raise ValueError("Hosted generated artifacts are stale; run hosted-plugin.py render")


def regenerate_claude(repo_root: Path | None = None) -> Path:
    """Atomically replace the committed Claude candidate from canonical bytes."""

    root = _repo_root(repo_root)
    return render(root, platform="claude")


def _archive_bytes(package: Path) -> bytes:
    return _archive_bytes_from_entries(
        (path.relative_to(package).as_posix(), path.read_bytes())
        for path in package.rglob("*")
        if path.is_file()
    )


def archive(
    repo_root: Path | None = None,
    output: Path | None = None,
    *,
    openai_app_id: str | None = None,
    platform: str = "claude",
) -> Path:
    root = _repo_root(repo_root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    with ExitStack() as release_locks:
        for selected_platform in sorted(selected):
            release_locks.enter_context(_promotion_mutex(root, selected_platform))
        validate_hosted_public_inputs(root)
        check(root, openai_app_id=openai_app_id, platform=platform)
        output_root = output or root / "dist" / "hosted"
        output_root.mkdir(parents=True, exist_ok=True)
        for selected_platform in selected:
            package = root / PLUGIN_ROOT / "generated" / selected_platform
            archive_path = output_root / f"{selected_platform}.zip"
            archive_bytes = _archive_bytes(package)
            _write_bytes_atomic(archive_path, archive_bytes)
            lock = {
                "platform": selected_platform,
                "archive_sha256": _sha256(archive_bytes),
            }
            if selected_platform == "openai":
                lock["registered_app_id_sha256"] = _registered_app_id_sha256(
                    _generated_openai_app_id(root / PLUGIN_ROOT / "generated")
                )
            _write_json_atomic(
                output_root / f"{selected_platform}.zip.lock.json",
                lock,
            )
    return output_root


def promotion_record(repo_root: Path | None, platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    return _repo_root(repo_root) / PLUGIN_ROOT / "promotion" / f"{platform}.json"


def promotion_record_sha256(repo_root: Path | None, platform: str) -> str:
    path = promotion_record(repo_root, platform)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("promotion record must be valid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError("promotion record must be an object")
    return _sha256(_canonical_json(record))


@contextmanager
def _promotion_mutex(root: Path, platform: str) -> Iterator[None]:
    path = root / PLUGIN_ROOT / f".{platform}.promotion.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        yield
    except FileExistsError as exc:
        raise ValueError("promotion state is being changed by another process") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
            path.unlink(missing_ok=True)


def _load_acceptance_fixture(root: Path) -> dict[str, Any]:
    try:
        fixture = json.loads(
            (root / PLUGIN_ROOT / "acceptance-fixture-v1.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Hosted acceptance fixture must be valid JSON") from exc
    if not isinstance(fixture, dict) or fixture.get("schema_version") != 1:
        raise ValueError("Hosted acceptance fixture has an unsupported schema")
    return fixture


def _current_release_binding(root: Path, platform: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        lock = json.loads(
            (root / PLUGIN_ROOT / "generated" / f"{platform}.lock.json").read_text(encoding="utf-8")
        )
        archive_path = root / PLUGIN_ROOT / "generated" / f"{platform}.zip"
        archive_lock = json.loads(
            (root / PLUGIN_ROOT / "generated" / f"{platform}.zip.lock.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "promotion requires a committed generated package and archive lock"
        ) from exc
    check(root, platform=platform)
    package = root / PLUGIN_ROOT / "generated" / platform
    archive_bytes = archive_path.read_bytes()
    if (
        _files_digest(package) != lock.get("artifact_sha256")
        or archive_bytes != _archive_bytes(package)
        or _sha256(archive_bytes) != archive_lock.get("archive_sha256")
    ):
        raise ValueError("promotion candidate bytes are stale")
    return lock, archive_lock


def _validate_promotion_evidence(
    root: Path,
    platform: str,
    evidence: dict[str, Any],
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    require_fresh: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    required_strings = {
        "client_version",
        "clean_client_identity_hmac_sha256",
        "oauth_client_config_sha256",
        "timestamp",
        "paired_run_hmac_sha256",
        "test_identity",
        "exomem_identity_hmac_sha256",
        "tenant_hmac_sha256",
        "entitlement_hmac_sha256",
        "provisioning_operation_hmac_sha256",
        "cell_hmac_sha256",
        "result_sha256",
        "package_artifact_sha256",
        "archive_sha256",
        "compatibility_sha256",
        "schema_contract_sha256",
        "command_surface_sha256",
        "endpoint",
        "plugin_version",
        "profile",
        "operator_key_id",
        "operator_signature",
    }
    required_counts = {
        "identity_count",
        "tenant_count",
        "entitlement_count",
        "operation_count",
        "cell_count",
        "volume_count",
    }
    required_operations = {
        "native_install",
        "authorization",
        "tool_discovery",
        "content_recall",
        "citation",
        "durable_capture",
        "fresh_chat_recall",
    }
    required = {
        "schema_version",
        "platform",
        *required_strings,
        *required_counts,
        *required_operations,
    }
    if evidence.get("mocked") or set(evidence) != required:
        raise ValueError("live promotion requires exact real content-bearing client evidence")
    if evidence["schema_version"] != 1 or evidence["platform"] != platform:
        raise ValueError("promotion evidence has an invalid version or platform")
    if not all(
        isinstance(evidence[key], str) and evidence[key].strip() for key in required_strings
    ):
        raise ValueError("promotion evidence has invalid identity fields")
    if not all(evidence[key] is True for key in required_operations):
        raise ValueError("live promotion requires successful content-bearing client operations")

    fixture = _load_acceptance_fixture(root)
    expected_counts = fixture.get("required_counts")
    count_keys = {
        "identity_count": "identity",
        "tenant_count": "tenant",
        "entitlement_count": "entitlement",
        "operation_count": "operation",
        "cell_count": "cell",
        "volume_count": "volume",
    }
    if not isinstance(expected_counts, dict) or any(
        type(evidence[field]) is not int or evidence[field] != expected_counts.get(fixture_key)
        for field, fixture_key in count_keys.items()
    ):
        raise ValueError("promotion evidence has invalid expected resource counts")
    if evidence["test_identity"] != fixture.get("run_id"):
        raise ValueError("promotion evidence has a different test identity")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", evidence["timestamp"]):
        raise ValueError("promotion evidence timestamp must be canonical UTC")
    timestamp = datetime.fromisoformat(evidence["timestamp"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    if timestamp > now or (require_fresh and (now - timestamp).total_seconds() > 24 * 60 * 60):
        raise ValueError("promotion evidence timestamp is stale")

    compatibility = compatibility_manifest(root)
    definition = load_definition(root)
    lock, archive_lock = _current_release_binding(root, platform)
    expected_identity = {
        "endpoint": compatibility["endpoint"],
        "compatibility_sha256": compatibility["compatibility_sha256"],
        "schema_contract_sha256": compatibility["schema_contract_sha256"],
        "command_surface_sha256": compatibility["command_surface_sha256"],
        "plugin_version": definition.version,
        "profile": definition.profile,
        "package_artifact_sha256": lock["artifact_sha256"],
        "archive_sha256": archive_lock["archive_sha256"],
    }
    if any(evidence[key] != value for key, value in expected_identity.items()):
        raise ValueError("promotion evidence has a different compatibility or package identity")
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", evidence[key])
        for key in (
            "result_sha256",
            "package_artifact_sha256",
            "archive_sha256",
            "compatibility_sha256",
            "schema_contract_sha256",
            "command_surface_sha256",
            "clean_client_identity_hmac_sha256",
            "oauth_client_config_sha256",
            "paired_run_hmac_sha256",
            "exomem_identity_hmac_sha256",
            "tenant_hmac_sha256",
            "entitlement_hmac_sha256",
            "provisioning_operation_hmac_sha256",
            "cell_hmac_sha256",
        )
    ):
        raise ValueError("promotion evidence digests must be SHA-256 values")
    if not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id:
        raise ValueError("promotion requires an operator-trusted signing key")
    unsigned = {key: value for key, value in evidence.items() if key != "operator_signature"}
    expected_signature = hmac.new(
        trusted_secret.encode("utf-8"), _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(evidence["operator_signature"], expected_signature):
        raise ValueError("promotion operator signature is invalid")
    return compatibility, lock, archive_lock


def promote(
    repo_root: Path | None,
    platform: str,
    evidence: dict[str, Any],
    *,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
    expected_state: str | None = None,
    expected_record_sha256: str | None = None,
) -> None:
    """Promote only evidence from a real, content-bearing clean-client journey."""
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    if "operator_key_id" in evidence and (
        not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id
    ):
        raise ValueError("promotion requires an operator-trusted signing key")
    root = _repo_root(repo_root)
    if expected_state not in {"pending", "failed"} or not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_record_sha256 or "")
    ):
        raise ValueError("promotion requires expected state and record digest")
    record_path = promotion_record(root, platform)
    with _promotion_mutex(root, platform):
        try:
            prior = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("promotion requires a valid current record") from exc
        if (
            prior.get("state") != expected_state
            or _sha256(_canonical_json(prior)) != expected_record_sha256
        ):
            raise ValueError("promotion record changed; refresh before retrying")
        validate_hosted_public_inputs(root)
        compatibility, lock, _archive_lock = _validate_promotion_evidence(
            root,
            platform,
            evidence,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
        )
        _write_json_atomic(
            record_path,
            {
                "schema_version": 1,
                "platform": platform,
                "state": "live",
                "package_lock": lock,
                "compatibility_sha256": compatibility["compatibility_sha256"],
                "evidence": evidence,
            },
        )


def demote(
    repo_root: Path | None,
    platform: str,
    reason: str,
    *,
    expected_state: str | None = None,
    expected_record_sha256: str | None = None,
) -> None:
    root = _repo_root(repo_root)
    if reason not in DEMOTION_REASONS:
        raise ValueError("demotion requires a stable reason code")
    if expected_state != "live" or not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_record_sha256 or "")
    ):
        raise ValueError("demotion requires expected live state and record digest")
    record_path = promotion_record(root, platform)
    with _promotion_mutex(root, platform):
        try:
            prior = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("demotion requires a valid current record") from exc
        if (
            prior.get("state") != expected_state
            or _sha256(_canonical_json(prior)) != expected_record_sha256
        ):
            raise ValueError("promotion record changed; refresh before retrying")
        _write_json_atomic(
            record_path,
            {
                "schema_version": 1,
                "platform": platform,
                "state": "failed",
                "reason": reason,
            },
        )


def distribution_manifest(
    repo_root: Path | None = None,
    *,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo_root)
    records = {
        platform: json.loads(promotion_record(root, platform).read_text(encoding="utf-8"))
        for platform in PLATFORMS
    }
    live: list[str] = []
    for platform, record in records.items():
        if record.get("state") != "live":
            continue
        evidence = record.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError("live promotion record has no evidence")
        compatibility, lock, _archive_lock = _validate_promotion_evidence(
            root,
            platform,
            evidence,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            require_fresh=False,
        )
        if (
            record.get("package_lock") != lock
            or record.get("compatibility_sha256") != compatibility["compatibility_sha256"]
        ):
            raise ValueError("live promotion record has stale package bindings")
        live.append(platform)
    identities = {records[platform].get("compatibility_sha256") for platform in live}
    paired = {
        tuple(
            records[platform].get("evidence", {}).get(key)
            for key in (
                "paired_run_hmac_sha256",
                "exomem_identity_hmac_sha256",
                "tenant_hmac_sha256",
                "entitlement_hmac_sha256",
                "provisioning_operation_hmac_sha256",
                "cell_hmac_sha256",
                "cell_count",
                "volume_count",
            )
        )
        for platform in live
    }
    return {
        "live_platforms": live,
        "cross_client_ready": len(live) == len(PLATFORMS)
        and len(identities) == 1
        and len(paired) == 1,
    }
