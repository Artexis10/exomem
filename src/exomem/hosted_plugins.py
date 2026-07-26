"""Deterministic, tenant-neutral Hosted client plugin candidates."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import commands, hosted_gateway


PLUGIN_ROOT = Path("plugins/hosted")
PLATFORMS = ("claude", "openai")
SKILL_NAMES = (
    "exomem",
    "exomem-capture",
    "exomem-continue",
    "exomem-reflect",
    "exomem-research",
    "exomem-review",
)
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_LEGACY_OR_EXCLUDED_TOOLS = frozenset({
    "edit_memory", "replace_memory", "transfer_artifact", "process_media", "adopt_vault",
    "adoption_studio", "maintain_memory", "schema_memory", "manage_memory_file",
    "query_dataset", "read_media", "coordination_status",
    "create_file", "move_file", "delete", "edit", "replace", "note", "find", "get", "add", "query_data",
})
TOOL_REFERENCE = re.compile(
    r"(?:`|\b)(" + "|".join(re.escape(name) for name in sorted(
        set(commands.PRODUCT_PUBLIC_NAMES) | _LEGACY_OR_EXCLUDED_TOOLS
    )) + r")(?:`|\s*\()"
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
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _repo_root(repo_root: Path | None = None) -> Path:
    return (repo_root or Path(__file__).resolve().parents[2]).resolve()


def _validate_definition(raw: dict[str, Any]) -> HostedDefinition:
    allowed = {
        "plugin_id", "version", "endpoint", "profile", "channel", "distribution_scope", "source_release",
        "support_url", "privacy_url", "terms_url", "claude_schema_version", "openai_schema_version",
        "website_url", "repository_url", "license", "author_name", "author_url",
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
    allowed = set(commands.product_commands_for_profile(commands.HOSTED_ALPHA_AGENT_PROFILE, "rest"))
    allowed_names = {command.name for command in allowed}
    declared = _frontmatter(text, skill)["required_tools"]
    observed = tuple(sorted(set(TOOL_REFERENCE.findall(text))))
    unavailable = (set(declared) | set(observed)) - allowed_names
    if unavailable:
        raise ValueError(f"{skill}: unavailable Hosted tools: {', '.join(sorted(unavailable))}")
    missing_declaration = set(observed) - set(declared)
    if missing_declaration:
        raise ValueError(f"{skill}: callable tools must be declared: {', '.join(sorted(missing_declaration))}")
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


def validate_hosted_public_inputs(repo_root: Path | None = None) -> None:
    """Fail closed over Hosted sources and committed candidate files before release."""

    root = _repo_root(repo_root)
    paths = [root / PLUGIN_ROOT / "definition.json", root / PLUGIN_ROOT / "behavior-fixtures-v1.json"]
    paths.extend(_skill_paths(root))
    generated = root / PLUGIN_ROOT / "generated"
    if generated.is_dir():
        paths.extend(path for path in generated.rglob("*") if path.is_file())
    forbidden = re.compile(
        r"(?i)(\[todo:|\$\{[^}]+\}|exomem_vault_path|localhost|127\.0\.0\.1|"
        r"file://|[A-Z0-9_]*(?:token|secret|password)\s*[:=]|tenant[_-]?id|vault[_-]?path|"
        r"\buvx\b|\bhooks?\b)"
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if forbidden.search(text):
            raise ValueError(f"Hosted public artifact is unsafe: {path}")


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
                "scopes": {"exomem.read": "Read governed memory", "exomem.write": "Write governed memory"},
            }
        },
        "tools": {
            entry["name"]: {
                "securitySchemes": [{
                    "type": "oauth2",
                    "scopes": ["exomem.read" if entry["read_only"] else "exomem.write"],
                }]
            }
            for entry in contract["commands"]
        },
        "runtime_requirement": "MCP responses include _meta['mcp/www_authenticate'] from the gateway.",
    }


def compatibility_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = _repo_root(repo_root)
    definition = load_definition(root)
    dependencies = skill_dependencies(root)
    contract = hosted_gateway.build_agent_gateway_contract(profile=definition.profile)
    oauth_overlay = oauth_discovery_overlay(contract)
    raw_definition = json.loads((root / PLUGIN_ROOT / "definition.json").read_text(encoding="utf-8"))
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
    if committed != compatibility_manifest(root):
        raise ValueError("Hosted compatibility descriptor is stale")


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


def _copy_skills(root: Path, destination: Path) -> None:
    for source in _skill_paths(root):
        target = destination / source.relative_to(root / PLUGIN_ROOT / "skills")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def _copy_assets(root: Path, destination: Path) -> None:
    for source in sorted((root / PLUGIN_ROOT / "assets").rglob("*")):
        if source.is_file():
            target = destination / source.relative_to(root / PLUGIN_ROOT / "assets")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def _files_digest(directory: Path, *, exclude: Iterable[str] = ()) -> str:
    excluded = set(exclude)
    entries: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            if relative not in excluded:
                entries.append((relative, _sha256(path.read_bytes())))
    return _sha256(_canonical_json(entries))


def _map_digest(files: dict[str, bytes], prefix: str) -> str:
    entries = [
        (path.removeprefix(prefix), _sha256(contents))
        for path, contents in sorted(files.items())
        if path.startswith(prefix)
    ]
    return _sha256(_canonical_json(entries))


def _package_lock(root: Path, platform: str, artifact_root: Path) -> dict[str, Any]:
    definition = load_definition(root)
    compatibility = compatibility_manifest(root)
    return {
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


def _validate_openai_app_id(value: str | None) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9]+", clean):
        raise ValueError("OpenAI candidate requires a registered OpenAI app release input")
    return clean


def validate_openai_candidate(package: Path) -> None:
    """Repository-owned equivalent of the current universal-plugin ingestion gate."""

    try:
        plugin = json.loads((package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        app = json.loads((package / ".app.json").read_text(encoding="utf-8"))
        marketplace = json.loads((package / "marketplace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("OpenAI candidate manifests must be valid JSON") from exc
    if plugin.get("skills") != "./skills/" or plugin.get("mcpServers") != "./.mcp.json" or plugin.get("apps") != "./.app.json":
        raise ValueError("OpenAI plugin manifest has invalid companion paths")
    if set(app) != {"apps"} or not isinstance(app["apps"].get("exomem"), dict) or set(app["apps"]["exomem"]) - {"id", "category"}:
        raise ValueError("OpenAI app manifest must contain only the registered app mapping")
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9]+", str(app["apps"]["exomem"].get("id", ""))):
        raise ValueError("OpenAI app manifest must contain a registered app ID")
    plugins = marketplace.get("plugins")
    expected_marketplace = {
        "name": plugin.get("name"),
        "source": {"source": "local", "path": "./plugins/exomem-hosted"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "productivity",
    }
    if not isinstance(plugins, list) or len(plugins) != 1 or plugins[0] != expected_marketplace:
        raise ValueError("OpenAI marketplace metadata must own ON_INSTALL authentication")


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
    validate_hosted_public_inputs(root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    app_id = _validate_openai_app_id(openai_app_id) if "openai" in selected else None
    files: dict[str, bytes] = {}
    for item in selected:
        prefix = f"{item}/"
        for source in _skill_paths(root):
            files[prefix + "skills/" + source.relative_to(root / PLUGIN_ROOT / "skills").as_posix()] = (
                source.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            )
        for source in (root / PLUGIN_ROOT / "assets").rglob("*"):
            if source.is_file():
                files[prefix + "assets/" + source.relative_to(root / PLUGIN_ROOT / "assets").as_posix()] = source.read_bytes()
        files[prefix + ".mcp.json"] = _canonical_json({"mcpServers": {"exomem": {"type": "http", "url": definition.endpoint}}}) + b"\n"
        if item == "claude":
            files[prefix + ".claude-plugin/plugin.json"] = _canonical_json({
                "name": definition.plugin_id, "version": definition.version,
                "description": "Governed long-term memory for relevant project work.",
                "author": {"name": definition.author_name, "url": definition.author_url},
                "homepage": definition.website_url, "repository": definition.repository_url,
                "license": definition.license, "keywords": ["memory", "knowledge", "governance"],
            }) + b"\n"
        else:
            files[prefix + ".codex-plugin/plugin.json"] = _canonical_json({
                "id": definition.plugin_id, "name": definition.plugin_id, "version": definition.version,
                "description": "Governed long-term memory for relevant project work.", "skills": "./skills/",
                "mcpServers": "./.mcp.json", "apps": "./.app.json",
                "author": {"name": definition.author_name, "url": definition.author_url},
                "homepage": definition.website_url, "repository": definition.repository_url, "license": definition.license,
                "keywords": ["memory", "knowledge", "governance"], "interface": _interface_metadata(definition),
            }) + b"\n"
            files[prefix + ".app.json"] = _canonical_json({"apps": {"exomem": {"id": app_id, "category": "productivity"}}}) + b"\n"
            files[prefix + "marketplace.json"] = _canonical_json({
                "name": definition.plugin_id, "interface": _interface_metadata(definition), "plugins": [{
                    "name": definition.plugin_id, "source": {"source": "local", "path": "./plugins/exomem-hosted"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "productivity",
                }],
            }) + b"\n"
        lock = _package_lock(root, item, root / PLUGIN_ROOT / "generated" / item)
        lock["artifact_sha256"] = _map_digest(files, prefix)
        files[f"{item}.lock.json"] = _canonical_json(lock) + b"\n"
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
    definition = load_definition(root)
    skill_dependencies(root)
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
    registered_openai_app = _validate_openai_app_id(openai_app_id) if "openai" in selected else None
    temporary_root = Path(tempfile.mkdtemp(prefix="exomem-hosted-render-", dir=allowed_root))
    temporary = temporary_root
    if destination.exists():
        shutil.copytree(destination, temporary)
    for selected_platform in selected:
        package = temporary / selected_platform
        if package.exists():
            shutil.rmtree(package)
        (temporary / f"{selected_platform}.lock.json").unlink(missing_ok=True)
        package.mkdir(parents=True)
        _copy_skills(root, package / "skills")
        _copy_assets(root, package / "assets")
        mcp = {"mcpServers": {"exomem": {"type": "http", "url": definition.endpoint}}}
        _write_json(package / ".mcp.json", mcp)
        if selected_platform == "claude":
            _write_json(package / ".claude-plugin" / "plugin.json", {
                "name": definition.plugin_id,
                "version": definition.version,
                "description": "Governed long-term memory for relevant project work.",
                "author": {"name": definition.author_name, "url": definition.author_url},
                "homepage": definition.website_url,
                "repository": definition.repository_url,
                "license": definition.license,
                "keywords": ["memory", "knowledge", "governance"],
            })
        else:
            _write_json(package / ".codex-plugin" / "plugin.json", {
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
            })
            _write_json(package / ".app.json", {
                "apps": {"exomem": {"id": registered_openai_app, "category": "productivity"}},
            })
            _write_json(package / "marketplace.json", {
                "name": definition.plugin_id,
                "interface": _interface_metadata(definition),
                "plugins": [{
                    "name": definition.plugin_id,
                    "source": {"source": "local", "path": "./plugins/exomem-hosted"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "productivity",
                }],
            })
        _write_json(temporary / f"{selected_platform}.lock.json", _package_lock(root, selected_platform, package))
    _write_json(temporary / "compatibility.json", compatibility_manifest(root))
    if destination.exists():
        backup = destination.with_name(f".{destination.name}.previous")
        if backup.exists():
            raise ValueError("render backup directory already exists")
        destination.replace(backup)
        try:
            temporary.replace(destination)
        except OSError:
            backup.replace(destination)
            raise
        shutil.rmtree(backup)
    else:
        temporary.replace(destination)
    return destination


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
    expected_files = {
        path.relative_to(expected).as_posix(): path.read_bytes()
        for path in expected.rglob("*")
        if path.is_file() and (path.parts[len(expected.parts)] in selected or path.name in {
            "compatibility.json", *(f"{item}.lock.json" for item in selected)
        })
    }
    actual_files = candidate_files(root, platform=platform, openai_app_id=openai_app_id)
    if expected_files != actual_files:
        raise ValueError("Hosted generated artifacts are stale; run hosted-plugin.py render")


def regenerate_claude(repo_root: Path | None = None) -> Path:
    """Refresh only the known committed Claude candidate, without staging a new tree."""

    root = _repo_root(repo_root)
    generated = root / PLUGIN_ROOT / "generated"
    package = generated / "claude"
    required = (package / ".claude-plugin" / "plugin.json", package / ".mcp.json")
    if not all(path.is_file() for path in required):
        raise ValueError("committed Claude candidate is absent")
    definition = load_definition(root)
    skill_dependencies(root)
    _copy_skills(root, package / "skills")
    _copy_assets(root, package / "assets")
    _write_json(package / ".mcp.json", {"mcpServers": {"exomem": {"type": "http", "url": definition.endpoint}}})
    _write_json(package / ".claude-plugin" / "plugin.json", {
        "name": definition.plugin_id,
        "version": definition.version,
        "description": "Governed long-term memory for relevant project work.",
        "author": {"name": definition.author_name, "url": definition.author_url},
        "homepage": definition.website_url,
        "repository": definition.repository_url,
        "license": definition.license,
        "keywords": ["memory", "knowledge", "governance"],
    })
    for relative, contents in candidate_files(root, platform="claude").items():
        target = generated / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    return generated


def archive(
    repo_root: Path | None = None,
    output: Path | None = None,
    *,
    openai_app_id: str | None = None,
    platform: str = "claude",
) -> Path:
    root = _repo_root(repo_root)
    validate_hosted_public_inputs(root)
    check(root, openai_app_id=openai_app_id, platform=platform)
    output_root = output or root / "dist" / "hosted"
    output_root.mkdir(parents=True, exist_ok=True)
    selected = PLATFORMS if platform == "all" else (platform,)
    for selected_platform in selected:
        package = root / PLUGIN_ROOT / "generated" / selected_platform
        archive_path = output_root / f"{selected_platform}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive_file:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    entry = zipfile.ZipInfo(path.relative_to(package).as_posix(), (1980, 1, 1, 0, 0, 0))
                    entry.external_attr = (stat.S_IFREG | 0o644) << 16
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive_file.writestr(entry, path.read_bytes())
        _write_json(output_root / f"{selected_platform}.zip.lock.json", {
            "platform": selected_platform, "archive_sha256": _sha256(archive_path.read_bytes())
        })
    return output_root


def promotion_record(repo_root: Path | None, platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    return _repo_root(repo_root) / PLUGIN_ROOT / "promotion" / f"{platform}.json"


def promote(
    repo_root: Path | None,
    platform: str,
    evidence: dict[str, Any],
    *,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
) -> None:
    """Promote only evidence from a real, content-bearing clean-client journey."""
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    if "operator_key_id" in evidence and (
        not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id
    ):
        raise ValueError("promotion requires an operator-trusted signing key")
    required = {
        "schema_version", "platform", "client_version", "clean_client_identity", "timestamp",
        "paired_run_id", "exomem_identity", "tenant", "entitlement", "provisioning_operation", "cell",
        "cell_count", "volume_count", "result_sha256", "package_artifact_sha256", "archive_sha256",
        "compatibility_sha256", "schema_contract_sha256", "endpoint", "operator_key_id",
        "operator_signature", "native_install",
        "authorization", "tool_discovery", "content_recall", "citation", "durable_capture",
        "fresh_chat_recall",
    }
    if evidence.get("mocked") or not required.issubset(evidence) or not all(evidence[key] for key in required - {
        "schema_version", "platform", "client_version", "clean_client_identity", "timestamp", "paired_run_id",
        "exomem_identity", "tenant", "entitlement", "provisioning_operation", "cell", "cell_count",
        "volume_count", "result_sha256", "package_artifact_sha256", "archive_sha256", "compatibility_sha256",
        "schema_contract_sha256", "endpoint", "operator_key_id", "operator_signature",
    }):
        raise ValueError("live promotion requires real content-bearing client evidence")
    root = _repo_root(repo_root)
    validate_hosted_public_inputs(root)
    compatibility = compatibility_manifest(root)
    try:
        lock = json.loads((root / PLUGIN_ROOT / "generated" / f"{platform}.lock.json").read_text(encoding="utf-8"))
        archive_path = root / "dist" / "hosted" / f"{platform}.zip"
        archive_lock = json.loads((root / "dist" / "hosted" / f"{platform}.zip.lock.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("promotion requires a committed generated package and archive lock") from exc
    if evidence["schema_version"] != 1 or evidence["platform"] != platform:
        raise ValueError("promotion evidence has an invalid version or platform")
    if not all(isinstance(evidence[key], str) and evidence[key].strip() for key in (
        "client_version", "clean_client_identity", "timestamp", "paired_run_id", "exomem_identity",
        "tenant", "entitlement", "provisioning_operation", "cell",
    )) or evidence["cell_count"] != 1 or evidence["volume_count"] != 1:
        raise ValueError("promotion evidence has invalid identity or expected resource counts")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", evidence["timestamp"]):
        raise ValueError("promotion evidence timestamp must be canonical UTC")
    check(root, platform=platform)
    if _files_digest(root / PLUGIN_ROOT / "generated" / platform) != lock["artifact_sha256"] or _sha256(archive_path.read_bytes()) != archive_lock["archive_sha256"]:
        raise ValueError("promotion candidate bytes are stale")
    if evidence["endpoint"] != compatibility["endpoint"] or any(
        evidence[key] != compatibility[key]
        for key in ("compatibility_sha256", "schema_contract_sha256")
    ):
        raise ValueError("promotion evidence has a different compatibility identity")
    if evidence["package_artifact_sha256"] != lock["artifact_sha256"] or evidence["archive_sha256"] != archive_lock["archive_sha256"]:
        raise ValueError("promotion evidence has a different package binding")
    if not all(re.fullmatch(r"[0-9a-f]{64}", str(evidence[key])) for key in (
        "result_sha256", "package_artifact_sha256", "archive_sha256", "compatibility_sha256", "schema_contract_sha256"
    )):
        raise ValueError("promotion evidence digests must be SHA-256 values")
    unsigned = {key: value for key, value in evidence.items() if key != "operator_signature"}
    if not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id:
        raise ValueError("promotion requires an operator-trusted signing key")
    expected_signature = hmac.new(
        trusted_secret.encode("utf-8"), _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(str(evidence["operator_signature"]), expected_signature):
        raise ValueError("promotion operator signature is invalid")
    _write_json_atomic(promotion_record(root, platform), {
        "schema_version": 1, "platform": platform, "state": "live", "package_lock": lock,
        "compatibility_sha256": compatibility["compatibility_sha256"], "evidence": evidence,
    })


def demote(repo_root: Path | None, platform: str, reason: str) -> None:
    root = _repo_root(repo_root)
    if not reason.strip():
        raise ValueError("demotion requires a reason")
    _write_json_atomic(promotion_record(root, platform), {
        "schema_version": 1, "platform": platform, "state": "failed", "reason": reason.strip()
    })


def distribution_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = _repo_root(repo_root)
    records = {
        platform: json.loads(promotion_record(root, platform).read_text(encoding="utf-8"))
        for platform in PLATFORMS
    }
    live = [platform for platform, record in records.items() if record["state"] == "live"]
    identities = {records[platform].get("compatibility_sha256") for platform in live}
    paired = {
        tuple(records[platform].get("evidence", {}).get(key) for key in (
            "paired_run_id", "exomem_identity", "tenant", "entitlement", "provisioning_operation", "cell",
            "cell_count", "volume_count",
        ))
        for platform in live
    }
    return {
        "live_platforms": live,
        "cross_client_ready": len(live) == len(PLATFORMS) and len(identities) == 1 and len(paired) == 1,
    }
