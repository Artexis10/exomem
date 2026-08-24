"""Deterministic, tenant-neutral Hosted client plugin candidates."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from . import commands, hosted_gateway

PLUGIN_ROOT = Path("plugins/hosted")
DEFAULT_CANDIDATE = "hosted-alpha-agent-v1"
LIFECYCLE_CANDIDATE = "hosted-alpha-agent-v2"
EPISTEMIC_CANDIDATE = "hosted-alpha-agent-v3"
#: The first candidate whose membership is derived from the product surface
#: rather than hand-listed. See `commands.HOSTED_SURFACE_EXCLUSIONS`.
PARITY_CANDIDATE = "hosted-alpha-agent-v4"
#: Every distributable candidate and the surface profile it pins. A third
#: candidate is what retired the old pairwise `== LIFECYCLE_CANDIDATE`
#: branching: membership questions now ask the registry, not a constant.
CANDIDATE_PROFILES: Mapping[str, str] = MappingProxyType(
    {
        DEFAULT_CANDIDATE: commands.HOSTED_ALPHA_AGENT_PROFILE,
        LIFECYCLE_CANDIDATE: commands.HOSTED_ALPHA_AGENT_V2_PROFILE,
        EPISTEMIC_CANDIDATE: commands.HOSTED_ALPHA_AGENT_V3_PROFILE,
        PARITY_CANDIDATE: commands.HOSTED_ALPHA_AGENT_V4_PROFILE,
    }
)
#: Candidates whose profile exposes `record_memory`. These pin the Records
#: reader floor, bind their own selection cases, and must clear live Records
#: acceptance for their own profile identifier before promotion.
RECORDS_CANDIDATES: frozenset[str] = frozenset(
    {LIFECYCLE_CANDIDATE, EPISTEMIC_CANDIDATE, PARITY_CANDIDATE}
)
#: Candidate-scoped skills, rendered on top of the shared `SKILL_NAMES`. Each
#: candidate carries its own copies: a candidate package is immutable once its
#: lock pins `skills_sha256`, so sharing a file across candidates would let one
#: candidate's edit silently invalidate another's recorded release identity.
CANDIDATE_SKILL_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        DEFAULT_CANDIDATE: (),
        LIFECYCLE_CANDIDATE: ("exomem-records",),
        EPISTEMIC_CANDIDATE: ("exomem-records", "exomem-supersede"),
        PARITY_CANDIDATE: ("exomem-records", "exomem-supersede"),
    }
)
PLATFORMS = ("claude", "openai")
DIRECTORY_CHANNELS = ("claude-connector", "claude-plugin", "openai-plugin")
REGISTERED_OPENAI_APP_ID = "plugin_asdk_app_6a5e3d26f2b08191a04424d1c1b33fc0"
DIRECTORY_STATES = frozenset(
    {"draft", "submitted", "in_review", "approved", "published", "rejected", "withdrawn"}
)
DIRECTORY_MINIMUM_REVIEW_WINDOW = timedelta(days=1)
DIRECTORY_REVIEWER_EVIDENCE_MAX_AGE = timedelta(hours=1)
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
# Callable names the prose scanners must recognize but that no product command
# registry entry contributes: legacy primitives and hand-registered aliases. It
# is a recognition vocabulary so an undeclared mention in a skill is caught --
# never an availability policy. Per-profile availability is decided by
# `validate_skill_text` against the resolved profile.
_ADDITIONAL_CALLABLE_NAMES = frozenset(
    {
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
    | _ADDITIONAL_CALLABLE_NAMES
)
_RAW_PRIVATE_IDENTITY_FIELDS = frozenset(
    {
        "clean_client_identity",
        "paired_run_id",
        "exomem_identity",
        "tenant",
        "tenant_id",
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


class MarketplaceFixtureSeedError(ValueError):
    """The checked reviewer fixture could not be seeded and verified exactly."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _full_tool_contract_sha256(compatibility: dict[str, Any]) -> str:
    tools = [
        {
            "name": command["name"],
            "description": command["mcp_tool"]["description"],
            "inputSchema": command["mcp_tool"]["inputSchema"],
            "outputSchema": command["mcp_tool"].get("outputSchema"),
            "annotations": command["mcp_tool"]["annotations"],
        }
        for command in compatibility["agent_contract"]["commands"]
    ]
    return _sha256(_canonical_json(sorted(tools, key=lambda tool: tool["name"])))


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


def _records_live_acceptance_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "records_live_acceptance.py"
    spec = importlib.util.spec_from_file_location("_exomem_records_live_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Records live acceptance verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_profile(candidate: str) -> str:
    profile = CANDIDATE_PROFILES.get(candidate)
    if profile is None:
        raise ValueError("unsupported Hosted candidate")
    return profile


def _candidate_root(root: Path, candidate: str) -> Path:
    if candidate == DEFAULT_CANDIDATE:
        return root / PLUGIN_ROOT
    _candidate_profile(candidate)
    return root / PLUGIN_ROOT / "candidates" / candidate


def _validate_definition(
    raw: dict[str, Any], *, expected_profile: str = commands.HOSTED_ALPHA_AGENT_PROFILE
) -> HostedDefinition:
    allowed = {
        "plugin_id",
        "version",
        "endpoint",
        "profile",
        "channel",
        "distribution_scope",
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
    endpoint = str(raw["endpoint"])
    if not endpoint.startswith("https://") or not endpoint.endswith("/mcp/v1"):
        raise ValueError("Hosted endpoint must be a fixed HTTPS versioned /mcp/v1 resource")
    if raw["channel"] != "production":
        raise ValueError("only the production channel is distributable")
    if endpoint != "https://substratesystems.io/api/exomem/mcp/v1":
        raise ValueError("production endpoint must be the canonical Hosted resource")
    if raw["profile"] != expected_profile:
        raise ValueError("Hosted definition must use the exact alpha profile")
    if raw["distribution_scope"] != "pending":
        raise ValueError("Hosted distribution scope remains pending real-client acceptance")
    for key in ("support_url", "privacy_url", "terms_url"):
        if not str(raw[key]).startswith("https://"):
            raise ValueError(f"{key} must be an HTTPS URL")
    return HostedDefinition(**{key: str(value) for key, value in raw.items()})


def load_definition_file(
    path: Path, *, expected_profile: str = commands.HOSTED_ALPHA_AGENT_PROFILE
) -> HostedDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Hosted definition must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Hosted definition must be an object")
    return _validate_definition(raw, expected_profile=expected_profile)


def load_definition(
    repo_root: Path | None = None, *, candidate: str = DEFAULT_CANDIDATE
) -> HostedDefinition:
    root = _repo_root(repo_root)
    return load_definition_file(
        _candidate_root(root, candidate) / "definition.json",
        expected_profile=_candidate_profile(candidate),
    )


def _skill_paths(root: Path, candidate: str = DEFAULT_CANDIDATE) -> tuple[Path, ...]:
    base = root / PLUGIN_ROOT / "skills"
    paths = tuple(base / name / "SKILL.md" for name in SKILL_NAMES)
    _candidate_profile(candidate)
    candidate_root = _candidate_root(root, candidate)
    return paths + tuple(
        candidate_root / "skills" / name / "SKILL.md"
        for name in CANDIDATE_SKILL_NAMES.get(candidate, ())
    )


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


def validate_skill_text(
    text: str, skill: Path, *, profile: str = commands.HOSTED_ALPHA_AGENT_PROFILE
) -> tuple[str, ...]:
    allowed = set(
        commands.product_commands_for_profile(profile, "rest")
    )
    allowed_names = {command.name for command in allowed}
    declared = _frontmatter(text, skill)["required_tools"]
    observed_names = set(TOOL_REFERENCE.findall(text))
    observed_names.update(
        name for name in PROSE_CALL_REFERENCE.findall(text) if name in _CANONICAL_CALLABLES
    )
    unavailable = (set(declared) | set(observed_names)) - allowed_names
    if unavailable:
        raise ValueError(f"{skill}: unavailable Hosted tools: {', '.join(sorted(unavailable))}")
    missing_declaration = observed_names - set(declared)
    if missing_declaration:
        raise ValueError(
            f"{skill}: callable tools must be declared: {', '.join(sorted(missing_declaration))}"
        )
    unused = set(declared) - observed_names
    if unused:
        raise ValueError(f"{skill}: declared tools are not used: {', '.join(sorted(unused))}")
    return tuple(declared)


def skill_dependencies(
    repo_root: Path | None = None, *, candidate: str = DEFAULT_CANDIDATE
) -> dict[str, tuple[str, ...]]:
    root = _repo_root(repo_root)
    definition = load_definition(root, candidate=candidate)
    result: dict[str, tuple[str, ...]] = {}
    for skill in _skill_paths(root, candidate):
        text = skill.read_text(encoding="utf-8")
        result[skill.parent.name] = validate_skill_text(text, skill, profile=definition.profile)
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


def _validate_public_json(value: Any, path: Path, *, evidence_path: bool) -> None:
    """Reject raw private data and credential literals in public JSON, including schemas."""

    def inspect_json(
        nested: Any,
        *,
        parent_key: str | None = None,
        credential_schema: bool = False,
    ) -> None:
        if isinstance(nested, dict):
            for key, child in nested.items():
                if (
                    evidence_path
                    and key in _RAW_PRIVATE_IDENTITY_FIELDS
                    and parent_key != "required_counts"
                ):
                    raise ValueError(
                        f"Hosted public artifact contains a raw private identifier: {path}"
                    )
                credential_property = parent_key == "properties" and re.search(
                    r"(?i)(?:token|secret|password)$", str(key)
                )
                if (
                    credential_schema
                    and key in {"default", "example", "examples", "enum", "const", "value"}
                    and child not in (None, "", [], {})
                ):
                    raise ValueError(f"Hosted public artifact contains a credential value: {path}")
                if (
                    evidence_path
                    and re.search(r"(?i)(?:token|secret|password)$", str(key))
                    and not credential_property
                ):
                    raise ValueError(f"Hosted public artifact contains a credential value: {path}")
                inspect_json(
                    child,
                    parent_key=key,
                    credential_schema=credential_schema or bool(credential_property),
                )
        elif isinstance(nested, list):
            for child in nested:
                inspect_json(
                    child,
                    parent_key=parent_key,
                    credential_schema=credential_schema,
                )

    inspect_json(value)


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
        hosted_root / "marketplace-definition.json",
        hosted_root / "marketplace-review-cases.json",
        hosted_root / "marketplace-review-fixture-v2.json",
    ]
    paths = [path for path in paths if path.exists()]
    paths.extend(_skill_paths(root))
    for candidate in CANDIDATE_PROFILES:
        if candidate == DEFAULT_CANDIDATE:
            continue
        candidate_root = _candidate_root(root, candidate)
        if candidate_root.exists():
            paths.extend(
                path
                for path in candidate_root.rglob("*")
                if path.is_file() or path.is_symlink()
            )
    directories = ["assets", "acceptance", "promotion", "directory"]
    if include_generated:
        directories.append("generated")
    for directory in directories:
        candidate = hosted_root / directory
        if candidate.exists():
            paths.extend(
                path
                for path in candidate.rglob("*")
                if (path.is_file() or path.is_symlink())
                and (include_generated or "generated" not in path.parts)
            )
    # `edit_memory`'s canonical MCP description documents the literal
    # `transition_token=<returned transition_token>`, and the generated
    # compatibility descriptor copies that description wholesale -- so the
    # credential-assignment heuristic below fails every candidate whose profile
    # exposes `edit_memory`. The description is registry-canonical; editing it
    # would move the registered external connector fingerprint.
    #
    # The exemption is therefore scoped to the *placeholder*, not the
    # identifier. A real transition token is base64url-encoded JSON that carries
    # a vault-relative page path in cleartext, and base64 already slips past the
    # `vault_path` alternations -- so this rule is the only thing standing
    # between such a value and a public artifact. Exempting the identifier alone
    # would have let a real one through. `API_TOKEN=`, bare `token:`,
    # `x_transition_token=`, and `transition_token=<a real value>` all still
    # fail closed.
    forbidden = re.compile(
        r"(?i)(\[todo:|\$\{[^}]+\}|exomem_vault_path|localhost|127\.0\.0\.1|"
        r"file://|(?<![A-Za-z0-9_])(?!transition_token\s*=\s*<returned\b)"
        r"[A-Z0-9_]*(?:token|secret|password)\s*[:=]|"
        r"\btenant[_-]?id\b|\bvault[_-]?path\b|"
        r"\breviewer[_-]?(?:email|identity)\b|\binvite[_-]?(?:url|link|token)\b|"
        r"\bdomain[_-]?challenge\b|(?:[A-Z]:\\[^\\\s]+\\[^\\\s]+|\\\\[^\\\s]+\\[^\\\s]+)|"
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
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Hosted public JSON is invalid: {path}") from exc

            _validate_public_json(
                payload,
                path,
                evidence_path=(
                    "promotion" in path.parts
                    or "acceptance" in path.parts
                    or "directory" in path.parts
                    or (
                        "candidates" in path.parts
                        and not CANDIDATE_PROFILES.keys().isdisjoint(path.parts)
                        and "generated" not in path.parts
                    )
                    or path.name.startswith("acceptance-")
                    or path.name.startswith("marketplace-")
                ),
            )
        if forbidden.search(text):
            raise ValueError(f"Hosted public artifact is unsafe: {path}")

    for path in paths:
        inspect(path)


def _marketplace_path(root: Path, name: str) -> Path:
    return root / PLUGIN_ROOT / name


def _load_marketplace_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _validate_public_https_url(value: Any, field: str) -> str:
    url = str(value)
    if not re.fullmatch(r"https://[^/\s]+(?:/[^\s]*)?", url) or re.search(
        r"(?i)(localhost|127\.0\.0\.1)", url
    ):
        raise ValueError(f"{field} must be a public HTTPS URL")
    return url


def _validate_fresh_utc_timestamp(value: Any, field: str) -> None:
    timestamp = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp):
        raise ValueError(f"{field} must be canonical UTC")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    if parsed > now or now - parsed > timedelta(hours=24):
        raise ValueError(f"{field} is stale")


def _validate_listing_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum} characters")
    lowered = value.lower()
    if "native assistant memory" in lowered or "arbitrary chat history" in lowered:
        raise ValueError(f"{field} makes an unsupported memory claim")
    return value


_OPENAI_SALE_LANGUAGE = re.compile(
    r"(?i)\b(?:buy|pro|subscribe(?:d|r|s|ing)?|subscription|upgrade|checkout)\b|\bpaid\s+plan\b"
)
_OPENAI_RELEASE_STAGE_LANGUAGE = re.compile(
    r"(?i)\bprivate[-\s]+alpha\b|\btrial\b|\bdemo\b|\bhypothetical\b|"
    r"\bnot[-\s]+yet(?:[-\s]+been)?[-\s]+built\b"
)
_OPENAI_BOOLEAN_ANNOTATIONS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def _validate_openai_sale_free_packet(packet: dict[str, Any]) -> None:
    if _OPENAI_SALE_LANGUAGE.search(_canonical_json(packet).decode("utf-8")):
        raise ValueError("openai-plugin packet may not sell or upsell subscriptions")


def _validate_openai_release_stage_language(fields: Iterable[str]) -> None:
    if any(_OPENAI_RELEASE_STAGE_LANGUAGE.search(field) for field in fields):
        raise ValueError("openai-plugin listing may not make release-stage claims")


def _openai_annotation_explanations(name: str, annotations: dict[str, Any]) -> dict[str, str]:
    if any(not isinstance(annotations.get(key), bool) for key in _OPENAI_BOOLEAN_ANNOTATIONS):
        raise ValueError("marketplace packet tool annotations are incomplete")
    write_explanations = {
        "remember": (
            "This tool records a durable conclusion in account-backed governed knowledge. "
            "Under the installed workflow guidance, a relevant durable conclusion may be "
            "captured automatically without the user using a magic Exomem command."
        ),
        "observe_memory": (
            "This tool records a durable observation in account-backed governed knowledge. "
            "Under the installed workflow guidance, a relevant durable outcome may be captured "
            "automatically without the user using a magic Exomem command."
        ),
        "capture_source": (
            "This tool preserves supplied raw source material; it does not turn that material "
            "into a durable conclusion automatically."
        ),
        "preserve_evidence": (
            "This tool writes supplied proof material as append-only evidence; it does not "
            "create a durable conclusion automatically."
        ),
        "triage_memory": (
            "This tool records the selected review decision against the current signal "
            "fingerprint; it does not capture a durable conclusion automatically."
        ),
        "connect_memory": (
            "This tool writes only an explicitly selected entity or accepted relation; its "
            "proposal operations remain read-only."
        ),
        "maintain_memory": (
            "This tool applies maintenance only for explicit write-capable modes and flags; "
            "its audit mode is read-only."
        ),
        "adoption_studio": (
            "This tool applies only an explicitly selected adoption proposal; proposal review "
            "does not write by itself."
        ),
        "transfer_artifact": (
            "This tool prepares the requested out-of-band artifact transfer and does not "
            "automatically capture a durable conclusion."
        ),
    }
    return {
        "readOnlyHint": (
            "This tool only reads governed knowledge and does not change stored content."
            if annotations["readOnlyHint"]
            else write_explanations.get(
                name,
                "This tool performs only its documented write operation and does not "
                "automatically capture a durable conclusion.",
            )
        ),
        "destructiveHint": (
            "This tool can remove or replace governed content when the user explicitly requests it."
            if annotations["destructiveHint"]
            else "This tool does not delete or replace governed content."
        ),
        "idempotentHint": (
            "Repeating this side-effect-free request is safe to retry after transient or "
            "warming failures."
            if annotations["idempotentHint"]
            else (
                "Repeating this request may create another action, so it is not retried automatically."
            )
        ),
        "openWorldHint": (
            "This tool interacts with the user's account-backed Hosted Exomem service."
            if annotations["openWorldHint"]
            else "This tool is limited to the local governed store and its configured service."
        ),
    }


def load_marketplace_definition(repo_root: Path | None = None) -> dict[str, Any]:
    """Load the public listing copy while keeping runtime identity in definition.json."""

    root = _repo_root(repo_root)
    value = _load_marketplace_json(
        _marketplace_path(root, "marketplace-definition.json"), "marketplace definition"
    )
    if set(value) != {"schema_version", "common", "channels"} or value.get("schema_version") != 1:
        raise ValueError("marketplace definition has unsupported fields")
    common = value.get("common")
    channels = value.get("channels")
    if not isinstance(common, dict) or not isinstance(channels, dict):
        raise ValueError("marketplace definition common and channels must be objects")
    required_common = {
        "product_name",
        "publisher",
        "description",
        "website_url",
        "documentation_url",
        "setup_url",
        "privacy_url",
        "terms_url",
        "support_url",
        "brand_asset",
        "categories",
        "use_cases",
        "capabilities",
        "regions",
        "release_notes",
        "user_prerequisites",
    }
    if set(common) != required_common or set(channels) != set(DIRECTORY_CHANNELS):
        raise ValueError("marketplace definition is incomplete")
    runtime = load_definition(root)
    if common["product_name"] != "Exomem Hosted":
        raise ValueError("marketplace product_name drifts from the Hosted runtime identity")
    if common["publisher"] != runtime.author_name:
        raise ValueError("marketplace publisher drifts from the Hosted runtime identity")
    for field in (
        "website_url",
        "privacy_url",
        "terms_url",
        "support_url",
        "documentation_url",
        "setup_url",
    ):
        _validate_public_https_url(common[field], f"marketplace {field}")
    if common["website_url"] != runtime.website_url:
        raise ValueError("marketplace website_url drifts from the Hosted runtime identity")
    if common["privacy_url"] != runtime.privacy_url or common["terms_url"] != runtime.terms_url:
        raise ValueError("marketplace policy URL drifts from the Hosted runtime identity")
    _validate_listing_text(common["description"], "marketplace description", 2000)
    _validate_listing_text(common["release_notes"], "marketplace release_notes", 500)
    brand_asset = common["brand_asset"]
    if (
        not isinstance(brand_asset, dict)
        or set(brand_asset) != {"path", "sha256"}
        or not isinstance(brand_asset["path"], str)
        or not re.fullmatch(r"assets/[A-Za-z0-9._-]+", brand_asset["path"])
        or not re.fullmatch(r"[0-9a-f]{64}", str(brand_asset["sha256"]))
        or not (root / PLUGIN_ROOT / brand_asset["path"]).is_file()
        or brand_asset["sha256"] != _sha256((root / PLUGIN_ROOT / brand_asset["path"]).read_bytes())
    ):
        raise ValueError("marketplace brand asset is invalid or stale")
    categories = common["categories"]
    if categories != ["Productivity"]:
        raise ValueError("marketplace categories must use canonical Productivity")
    use_cases = common["use_cases"]
    if (
        not isinstance(use_cases, list)
        or len(use_cases) < 2
        or any(
            not isinstance(use_case, str) or not use_case.strip() or len(use_case) > 160
            for use_case in use_cases
        )
    ):
        raise ValueError("marketplace use_cases must contain concise public use cases")
    capabilities = common["capabilities"]
    if not isinstance(capabilities, dict) or set(capabilities) != {"read", "write"}:
        raise ValueError("marketplace capabilities are incomplete")
    for capability, description in capabilities.items():
        _validate_listing_text(description, f"marketplace {capability} capability", 200)
    user_prerequisites = common["user_prerequisites"]
    if (
        not isinstance(user_prerequisites, dict)
        or set(user_prerequisites) != {"account", "admission"}
        or not isinstance(user_prerequisites["account"], str)
        or not user_prerequisites["account"].strip()
        or not isinstance(user_prerequisites["admission"], dict)
        or set(user_prerequisites["admission"]) != {"mode", "eligibility"}
        or user_prerequisites["admission"].get("mode") not in {"invite_only", "public"}
        or not isinstance(user_prerequisites["admission"].get("eligibility"), str)
        or not user_prerequisites["admission"]["eligibility"].strip()
    ):
        raise ValueError("marketplace user prerequisites are invalid")
    regions = common["regions"]
    if (
        not isinstance(regions, list)
        or not regions
        or any(not isinstance(region, str) for region in regions)
    ):
        raise ValueError("marketplace regions must be a non-empty string list")
    for channel in DIRECTORY_CHANNELS:
        overlay = channels[channel]
        if not isinstance(overlay, dict) or set(overlay) != {
            "title",
            "short_description",
            "starter_prompts",
        }:
            raise ValueError(f"marketplace channel {channel} is incomplete")
        title_limit = 100 if channel.startswith("claude-") else 80
        tagline_limit = 55 if channel.startswith("claude-") else 30
        _validate_listing_text(overlay["title"], f"{channel} title", title_limit)
        _validate_listing_text(
            overlay["short_description"], f"{channel} short_description", tagline_limit
        )
        if channel.startswith("claude-") and (
            len(overlay["title"]) > 100
            or len(overlay["short_description"]) > 55
            or len(common["description"]) > 2000
        ):
            raise ValueError(f"{channel} exceeds current Claude listing limits")
        prompts = overlay["starter_prompts"]
        if (
            not isinstance(prompts, list)
            or len(prompts) < 3
            or any(
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt) > (128 if channel == "openai-plugin" else 300)
                for prompt in prompts
            )
        ):
            raise ValueError(f"{channel} starter_prompts must contain at least three short prompts")
        if channel == "openai-plugin" and any(
            term
            in " ".join(
                (
                    common["description"],
                    common["release_notes"],
                    overlay["title"],
                    overlay["short_description"],
                    *prompts,
                )
            ).lower()
            for term in ("subscription", "checkout", "paid plan", "upgrade")
        ):
            raise ValueError("openai-plugin listing may not sell or upsell subscriptions")
        if channel == "openai-plugin":
            _validate_openai_release_stage_language(
                (
                    common["description"],
                    common["release_notes"],
                    overlay["title"],
                    overlay["short_description"],
                    *prompts,
                )
            )
    return value


def _load_marketplace_review_fixture(root: Path) -> dict[str, Any]:
    fixture = _load_marketplace_json(
        _marketplace_path(root, "marketplace-review-fixture-v2.json"),
        "marketplace review fixture",
    )
    if set(fixture) != {"schema_version", "fixture_version", "payload", "payload_sha256", "reset"}:
        raise ValueError("marketplace review fixture has unsupported fields")
    if fixture["schema_version"] != 1 or not re.fullmatch(r"v[1-9]\d*", str(fixture["fixture_version"])):
        raise ValueError("marketplace review fixture version is invalid")
    payload = fixture["payload"]
    if not isinstance(payload, dict) or set(payload) != {"notes", "absent_notes"}:
        raise ValueError("marketplace review fixture payload is invalid")
    notes = payload["notes"]
    absent_notes = payload["absent_notes"]
    if not isinstance(notes, list) or not notes or not isinstance(absent_notes, list) or not absent_notes:
        raise ValueError("marketplace review fixture notes are invalid")
    references: set[str] = set()
    keys: set[str] = set()
    for note in notes:
        if not isinstance(note, dict) or set(note) != {"reference", "key", "title", "content"}:
            raise ValueError("marketplace review fixture note is invalid")
        if any(not isinstance(note[field], str) or not note[field].strip() for field in note):
            raise ValueError("marketplace review fixture note is invalid")
        references.add(note["reference"])
        keys.add(note["key"])
    absent_by_reference: dict[str, dict[str, Any]] = {}
    for target in absent_notes:
        if not isinstance(target, dict) or set(target) != {
            "reference",
            "key",
            "title",
            "create_tool",
            "note_type",
            "content",
        }:
            raise ValueError("marketplace review fixture absent target is invalid")
        if any(not isinstance(target[field], str) or not target[field].strip() for field in target):
            raise ValueError("marketplace review fixture absent target is invalid")
        if target["create_tool"] != "remember":
            raise ValueError("marketplace review fixture absent target must use remember")
        if target["note_type"] not in commands.note_module.NOTE_TYPES:
            raise ValueError("marketplace review fixture absent target note_type is invalid")
        if target["reference"] in references or target["key"] in keys:
            raise ValueError("marketplace review fixture absent target collides with seeded content")
        references.add(target["reference"])
        keys.add(target["key"])
        absent_by_reference[target["reference"]] = target
    if (
        len(references) != len(notes) + len(absent_notes)
        or len(keys) != len(notes) + len(absent_notes)
        or len(absent_by_reference) != len(absent_notes)
    ):
        raise ValueError("marketplace review fixture references must be unique")
    payload_sha256 = fixture["payload_sha256"]
    if not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("marketplace review fixture payload digest is invalid")
    if payload_sha256 != _sha256(_canonical_json(payload)):
        raise ValueError("marketplace review fixture payload digest is stale")
    reset = fixture["reset"]
    if (
        not isinstance(reset, dict)
        or set(reset)
        != {
            "disposable_reference",
            "disposable_key",
            "disposable_title",
            "create_tool",
            "procedure",
        }
        or reset["disposable_reference"] not in absent_by_reference
        or reset["procedure"] != "delete_created_note"
    ):
        raise ValueError("marketplace review fixture reset is invalid")
    target = absent_by_reference[reset["disposable_reference"]]
    if any(
        reset[field] != target[field.replace("disposable_", "")]
        for field in ("disposable_key", "disposable_title")
    ) or reset["create_tool"] != target["create_tool"]:
        raise ValueError("marketplace review fixture reset is invalid")
    return fixture


_FIXTURE_REVIEW_REASON = "No honest relation exists in the deterministic reviewer fixture."


def _fixture_note_path(note: Mapping[str, Any]) -> str:
    return f"Knowledge Base/Notes/Insights/{note['key']}.md"


def _fixture_tool_error_code(result: Mapping[str, Any]) -> str | None:
    if result.get("success") is not False:
        return None
    error = result.get("error")
    if not isinstance(error, Mapping):
        return "malformed_error"
    code = error.get("code")
    return str(code).strip().lower() if code else "malformed_error"


def _verified_fixture_readback(
    note: Mapping[str, Any], result: Mapping[str, Any], *, allow_missing: bool
) -> bool:
    error_code = _fixture_tool_error_code(result)
    if error_code is not None:
        if allow_missing and error_code in {"not_found", "not-found"}:
            return False
        raise MarketplaceFixtureSeedError(
            f"fixture note {note['key']} readback failed: {error_code}"
        )
    frontmatter = result.get("frontmatter")
    body = result.get("body")
    expected_body = f"# {note['title']}\n\n{str(note['content']).rstrip()}\n"
    if (
        not isinstance(frontmatter, Mapping)
        or frontmatter.get("title") != note["title"]
        or body != expected_body
    ):
        raise MarketplaceFixtureSeedError(
            f"fixture note {note['key']} exact readback mismatch"
        )
    return True


def seed_marketplace_review_fixture(
    fixture: Mapping[str, Any],
    call_tool: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Seed and exactly verify one checked reviewer fixture through product tools.

    ``call_tool`` is deliberately transport-neutral. Local conformance binds it
    to the real command leaves; the live bootstrap binds it to authenticated MCP.
    Existing exact pages count as verified partial progress. Existing mismatches
    fail closed and are never overwritten.
    """
    payload = fixture.get("payload")
    payload_digest = fixture.get("payload_sha256")
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload_digest, str)
        or payload_digest != _sha256(_canonical_json(payload))
    ):
        raise MarketplaceFixtureSeedError("fixture payload identity is invalid")
    notes = payload.get("notes")
    if not isinstance(notes, list) or not notes:
        raise MarketplaceFixtureSeedError("fixture notes are invalid")

    for note in notes:
        if not isinstance(note, Mapping):
            raise MarketplaceFixtureSeedError("fixture note is invalid")
        path = _fixture_note_path(note)
        try:
            existing = call_tool("read_memory", {"path": path})
        except Exception as error:  # noqa: BLE001 - callback is a transport boundary
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} initial readback failed: {type(error).__name__}"
            ) from error
        if not isinstance(existing, Mapping):
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} initial readback was malformed"
            )
        if _verified_fixture_readback(note, existing, allow_missing=True):
            continue

        base_arguments = {
            "title": note["title"],
            "slug": note["key"],
            "content": note["content"],
            "note_type": "insight",
            "suggestions": False,
        }
        try:
            validation = call_tool(
                "remember", {**base_arguments, "validate_only": True}
            )
        except Exception as error:  # noqa: BLE001 - callback is a transport boundary
            code = str(error).partition(":")[0].strip().lower() or type(error).__name__.lower()
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} validation failed: {code}"
            ) from error
        if not isinstance(validation, Mapping):
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} validation response was malformed"
            )
        validation_error = _fixture_tool_error_code(validation)
        if validation_error is not None:
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} validation failed: {validation_error}"
            )
        if validation.get("has_non_review_blockers") is not False:
            findings = validation.get("contract_result")
            finding_code = "non_review_blocker"
            if isinstance(findings, Mapping):
                blocking = findings.get("blocking_findings")
                if isinstance(blocking, list) and blocking and isinstance(blocking[0], Mapping):
                    finding_code = str(blocking[0].get("code") or finding_code).lower()
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} validation failed: {finding_code}"
            )

        draft_fields: dict[str, str] = {}
        for field in ("draft_id", "draft_hash", "draft_token"):
            value = validation.get(field)
            if not isinstance(value, str) or not value:
                raise MarketplaceFixtureSeedError(
                    f"fixture note {note['key']} validation omitted {field}"
                )
            draft_fields[field] = value
        if validation.get("destination") != path:
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} validation destination mismatch"
            )

        commit_arguments: dict[str, Any] = {**base_arguments, **draft_fields}
        if validation.get("reviewed_none_required") is True:
            relation_hash = validation.get("relation_review_hash")
            if not isinstance(relation_hash, str) or relation_hash != draft_fields["draft_hash"]:
                raise MarketplaceFixtureSeedError(
                    f"fixture note {note['key']} relation review response was malformed"
                )
            commit_arguments.update(
                {
                    "relation_disposition": "reviewed_none",
                    "relation_review_hash": relation_hash,
                    "relation_review_reason": _FIXTURE_REVIEW_REASON,
                }
            )
        elif validation.get("committable_without_review") is not True:
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} is not committable"
            )

        try:
            committed = call_tool("remember", commit_arguments)
        except Exception as error:  # noqa: BLE001 - callback is a transport boundary
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} commit failed: {type(error).__name__}"
            ) from error
        if not isinstance(committed, Mapping) or committed.get("path") != path:
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} commit response was malformed"
            )
        try:
            readback = call_tool("read_memory", {"path": path})
        except Exception as error:  # noqa: BLE001 - callback is a transport boundary
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} final readback failed: {type(error).__name__}"
            ) from error
        if not isinstance(readback, Mapping):
            raise MarketplaceFixtureSeedError(
                f"fixture note {note['key']} final readback was malformed"
            )
        _verified_fixture_readback(note, readback, allow_missing=False)

    return {
        "fixture_version": fixture.get("fixture_version"),
        "payload_sha256": payload_digest,
        "note_count": len(notes),
        "verified": True,
    }


def _remember_review_prompt(target: dict[str, Any]) -> str:
    return (
        f"Create an {target['note_type']} titled {target['title']} with slug {target['key']} "
        f"and copy this exact Markdown verbatim\n\n{target['content']}"
    )


def load_marketplace_review_cases(repo_root: Path | None = None) -> dict[str, Any]:
    root = _repo_root(repo_root)
    value = _load_marketplace_json(
        _marketplace_path(root, "marketplace-review-cases.json"), "marketplace review cases"
    )
    if (
        set(value) != {"schema_version", "fixture", "positive", "negative"}
        or value.get("schema_version") != 1
    ):
        raise ValueError("marketplace review cases have unsupported fields")
    fixture = _load_marketplace_review_fixture(root)
    fixture_binding = value["fixture"]
    expected_fixture_binding = {
        "fixture_version": fixture["fixture_version"],
        "payload_sha256": fixture["payload_sha256"],
    }
    if fixture_binding != expected_fixture_binding:
        raise ValueError("marketplace review cases fixture version or payload digest is stale")
    positive = value.get("positive")
    negative = value.get("negative")
    if not isinstance(positive, list) or len(positive) < 5:
        raise ValueError("marketplace review cases require at least five positive cases")
    if not isinstance(negative, list) or len(negative) < 3:
        raise ValueError("marketplace review cases require at least three negative cases")
    known_references = {
        item["reference"]
        for group in ("notes", "absent_notes")
        for item in fixture["payload"][group]
    }
    absent_by_reference = {
        target["reference"]: target for target in fixture["payload"]["absent_notes"]
    }
    write_tools = {
        entry["name"]
        for entry in compatibility_manifest(root)["agent_contract"]["commands"]
        if entry["mcp_tool"]["annotations"].get("readOnlyHint") is False
    }
    for category, cases in (("positive", positive), ("negative", negative)):
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError(f"marketplace {category} review case is incomplete")
            required = {"prompt", "expected_tools", "expected_outcome"}
            if category == "negative":
                required.add("rationale")
            has_write_tool = False
            if category == "positive":
                required |= {"fixture_version", "fixture_references"}
                has_write_tool = bool(write_tools.intersection(case.get("expected_tools", [])))
                if has_write_tool:
                    required.add("fixture_reset")
            if set(case) != required:
                if has_write_tool and "fixture_reset" not in case:
                    raise ValueError("marketplace review case fixture reset is required")
                if category == "negative" and "rationale" not in case:
                    raise ValueError("marketplace negative review case rationale is required")
                raise ValueError(f"marketplace {category} review case is incomplete")
            if not isinstance(case["prompt"], str) or not case["prompt"].strip():
                raise ValueError(f"marketplace {category} review case prompt is invalid")
            if (
                not isinstance(case["expected_outcome"], str)
                or not case["expected_outcome"].strip()
            ):
                raise ValueError(f"marketplace {category} review case outcome is invalid")
            if category == "negative" and (
                not isinstance(case["rationale"], str) or not case["rationale"].strip()
            ):
                raise ValueError("marketplace negative review case rationale is invalid")
            if not isinstance(case["expected_tools"], list) or any(
                tool not in _CANONICAL_CALLABLES for tool in case["expected_tools"]
            ):
                raise ValueError(f"marketplace {category} review case tools are invalid")
            if category == "positive":
                if case["fixture_version"] != fixture["fixture_version"]:
                    raise ValueError("marketplace review case fixture version is stale")
                references = case["fixture_references"]
                if (
                    not isinstance(references, list)
                    or not references
                    or any(reference not in known_references for reference in references)
                ):
                    raise ValueError("marketplace review case fixture reference is unknown")
                if (
                    write_tools.intersection(case["expected_tools"])
                    and fixture["reset"]["create_tool"] not in case["expected_tools"]
                ):
                    raise ValueError("marketplace review case fixture reset tool is invalid")
                if (
                    write_tools.intersection(case["expected_tools"])
                    and (
                        case["fixture_reset"] != fixture["reset"]
                        or fixture["reset"]["disposable_reference"] not in references
                    )
                ):
                    raise ValueError("marketplace review case fixture reset is invalid")
                if write_tools.intersection(case["expected_tools"]):
                    target = absent_by_reference[fixture["reset"]["disposable_reference"]]
                    if (
                        write_tools.intersection(case["expected_tools"])
                        != {fixture["reset"]["create_tool"]}
                    ):
                        raise ValueError("marketplace review case fixture reset is incomplete")
                    if (
                        references != [target["reference"]]
                        or case["prompt"] != _remember_review_prompt(target)
                    ):
                        raise ValueError("marketplace review case fixture prompt is invalid")
    negative_text = " ".join(
        f"{case['prompt']} {case['expected_outcome']}" for case in negative
    ).lower()
    if "do not capture" not in negative_text or "native assistant memory" not in negative_text:
        raise ValueError(
            "marketplace negative cases must cover no-capture and native-memory boundaries"
        )
    if not all(term in negative_text for term in ("tenant", "credential", "internal")):
        raise ValueError(
            "marketplace negative cases must cover tenant, credential, and internal-data boundaries"
        )
    if any(
        term
        in " ".join(f"{case['prompt']} {case['expected_outcome']}" for case in positive).lower()
        for term in ("subscription", "checkout", "paid plan", "upgrade")
    ):
        raise ValueError("marketplace review cases may not sell or upsell subscriptions")
    return {"fixture": fixture_binding, "positive": positive, "negative": negative}


def _directory_submission_directory(root: Path, channel: str) -> Path:
    if channel not in DIRECTORY_CHANNELS:
        raise ValueError("unsupported directory channel")
    return _marketplace_path(root, f"directory/submissions/{channel}")


def _validate_directory_submission(record: dict[str, Any], channel: str) -> dict[str, Any]:
    base = {"schema_version", "channel", "state", "listing_version"}
    transition = {"previous_submission_sha256", "transition_from_submission_sha256"}
    state = record.get("state")
    expected = {
        "draft": (base, base | transition),
        "submitted": (base | transition | {"receipt"},),
        "in_review": (base | transition | {"receipt"},),
        "approved": (base | transition | {"receipt"},),
        "published": (base | transition | {"receipt"},),
        "rejected": (base | transition,),
        "withdrawn": (base | transition | {"target_submission_sha256"},),
    }
    if record.get("schema_version") != 1 or record.get("channel") != channel:
        raise ValueError("directory submission has an invalid identity")
    if state not in DIRECTORY_STATES:
        raise ValueError("directory submission has an invalid state")
    if set(record) not in expected[state]:
        raise ValueError("directory submission has unsupported fields")
    if not isinstance(record.get("listing_version"), str) or not record["listing_version"].strip():
        raise ValueError("directory submission requires a listing version")
    for field in transition | {"target_submission_sha256"}:
        if field in record and not re.fullmatch(r"[0-9a-f]{64}", str(record[field])):
            raise ValueError("directory submission has an invalid transition binding")
    return record


def _directory_submissions(root: Path, channel: str) -> dict[str, dict[str, Any]]:
    directory = _directory_submission_directory(root, channel)
    if not directory.is_dir():
        raise ValueError("directory submission history is missing")
    records: dict[str, dict[str, Any]] = {}
    for path in directory.glob("*.json"):
        record = _validate_directory_submission(
            _load_marketplace_json(path, "directory submission"), channel
        )
        digest = _sha256(_canonical_json(record))
        if digest in records:
            raise ValueError("directory submission history has duplicate records")
        records[digest] = record
    if not records:
        raise ValueError("directory submission history is empty")
    return records


def _latest_directory_submission(root: Path, channel: str) -> tuple[str, dict[str, Any]]:
    records = _directory_submissions(root, channel)
    predecessors = {
        record.get("previous_submission_sha256")
        for record in records.values()
        if isinstance(record.get("previous_submission_sha256"), str)
    }
    leaves = sorted(set(records) - predecessors)
    if len(leaves) != 1:
        raise ValueError("directory submission history has no unambiguous latest record")
    digest = leaves[0]
    return digest, records[digest]


def _directory_listing_heads(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    versions = {record["listing_version"] for record in records.values()}
    heads: dict[str, str] = {}
    for version in versions:
        candidates = {
            digest for digest, record in records.items() if record["listing_version"] == version
        }
        predecessors: set[str] = set()
        for record in records.values():
            previous = (
                record.get("transition_from_submission_sha256")
                or record.get("previous_submission_sha256")
                if record["listing_version"] == version
                else None
            )
            target = record.get("target_submission_sha256")
            for digest in (previous, target):
                if digest in candidates:
                    predecessors.add(digest)
        leaves = sorted(candidates - predecessors)
        if len(leaves) != 1:
            raise ValueError("directory listing version has no unambiguous latest record")
        heads[version] = leaves[0]
    return heads


def _directory_publication_path(root: Path, channel: str) -> Path:
    if channel not in DIRECTORY_CHANNELS:
        raise ValueError("unsupported directory channel")
    return _marketplace_path(root, f"directory/publication/{channel}.json")


def _load_directory_publication(root: Path, channel: str) -> dict[str, Any]:
    pointer = _load_marketplace_json(
        _directory_publication_path(root, channel), "directory publication pointer"
    )
    if set(pointer) != {"schema_version", "channel", "active_submission_sha256"}:
        raise ValueError("directory publication pointer has unsupported fields")
    if pointer.get("schema_version") != 1 or pointer.get("channel") != channel:
        raise ValueError("directory publication pointer has an invalid identity")
    active = pointer.get("active_submission_sha256")
    if active is not None and not re.fullmatch(r"[0-9a-f]{64}", str(active)):
        raise ValueError("directory publication pointer has an invalid active submission")
    return pointer


def directory_record_sha256(repo_root: Path | None, channel: str) -> str:
    root = _repo_root(repo_root)
    return _latest_directory_submission(root, channel)[0]


def _directory_bindings(root: Path, channel: str, *, openai_app_id: str | None) -> dict[str, Any]:
    compatibility = compatibility_manifest(root)
    platform = "openai" if channel == "openai-plugin" else "claude"
    if platform == "openai":
        files = candidate_files(root, platform="openai", openai_app_id=openai_app_id)
        package_lock = json.loads(files["openai.lock.json"])
        archive_lock = json.loads(files["openai.zip.lock.json"])
    else:
        generated = root / PLUGIN_ROOT / "generated"
        package_lock = _load_marketplace_json(generated / "claude.lock.json", "Claude package lock")
        archive_lock = _load_marketplace_json(
            generated / "claude.zip.lock.json", "Claude archive lock"
        )
    return {
        "platform": platform,
        "compatibility_sha256": compatibility["compatibility_sha256"],
        "runtime_definition_sha256": compatibility["definition_sha256"],
        "package_lock_sha256": _sha256(_canonical_json(package_lock)),
        "archive_lock_sha256": _sha256(_canonical_json(archive_lock)),
        **(
            {"registered_app_id_sha256": package_lock["registered_app_id_sha256"]}
            if platform == "openai"
            else {}
        ),
    }


def directory_packets(
    repo_root: Path | None = None,
    *,
    channel: str = "all",
    openai_app_id: str | None = None,
) -> dict[str, bytes]:
    """Render provider packets without changing provider state."""

    root = _repo_root(repo_root)
    if channel not in (*DIRECTORY_CHANNELS, "all"):
        raise ValueError("unsupported directory channel")
    selected = DIRECTORY_CHANNELS if channel == "all" else (channel,)
    definition = load_marketplace_definition(root)
    review_cases = load_marketplace_review_cases(root)
    validate_hosted_public_inputs(root, include_generated=False)
    compatibility = compatibility_manifest(root)
    tool_entries = [
        {
            "name": entry["name"],
            "description": entry["mcp_tool"]["description"],
            "input_schema": entry["mcp_tool"]["inputSchema"],
            "output_schema": entry["mcp_tool"].get("outputSchema"),
            "retry_semantics": (
                "idempotent"
                if entry["mcp_tool"]["annotations"].get("idempotentHint")
                else "do_not_retry"
            ),
            "annotations": {
                key: entry["mcp_tool"]["annotations"][key]
                for key in ("title", "readOnlyHint", "destructiveHint", "openWorldHint")
            },
            "mcp_annotations": entry["mcp_tool"]["annotations"],
        }
        for entry in compatibility["agent_contract"]["commands"]
    ]
    if any(
        set(tool["annotations"]) != {"title", "readOnlyHint", "destructiveHint", "openWorldHint"}
        for tool in tool_entries
    ):
        raise ValueError("marketplace packet tool annotations are incomplete")
    packets: dict[str, bytes] = {}
    for selected_channel in selected:
        overlay = definition["channels"][selected_channel]
        tools = [
            {key: value for key, value in tool.items() if key != "mcp_annotations"}
            for tool in tool_entries
        ]
        if selected_channel == "openai-plugin":
            tools = [
                {
                    **{
                        key: value
                        for key, value in tool.items()
                        if key != "mcp_annotations"
                    },
                    "annotations": {
                        "title": tool["annotations"]["title"],
                        **{
                            key: raw_annotations[key]
                            for key in _OPENAI_BOOLEAN_ANNOTATIONS
                            if key in raw_annotations
                        },
                    },
                    "annotation_explanations": _openai_annotation_explanations(
                        tool["name"], raw_annotations
                    ),
                }
                for tool, raw_annotations in (
                    (item, item["mcp_annotations"]) for item in tool_entries
                )
            ]
        packet = {
            "schema_version": 1,
            "channel": selected_channel,
            "product_name": definition["common"]["product_name"],
            "publisher": definition["common"]["publisher"],
            "description": definition["common"]["description"],
            "title": overlay["title"],
            "short_description": overlay["short_description"],
            "starter_prompts": overlay["starter_prompts"],
            "release_notes": definition["common"]["release_notes"],
            "regions": definition["common"]["regions"],
            "brand_asset": definition["common"]["brand_asset"],
            "categories": definition["common"]["categories"],
            "documentation_url": definition["common"]["documentation_url"],
            "setup_url": definition["common"]["setup_url"],
            "use_cases": definition["common"]["use_cases"],
            "capabilities": definition["common"]["capabilities"],
            "public_urls": {
                key: definition["common"][key]
                for key in ("website_url", "privacy_url", "terms_url", "support_url")
            },
            "endpoint": load_definition(root).endpoint,
            "tools": tools,
            "review_cases": review_cases,
            "screenshots": {"status": "not_applicable", "reason": "no MCP App UI"},
            "operator_prerequisites": [
                "provider registration",
                "verified publisher",
                "policy approval",
                "seeded reviewer account",
            ],
            "bindings": _directory_bindings(root, selected_channel, openai_app_id=openai_app_id),
        }
        if selected_channel == "openai-plugin":
            packet["acceptance_surfaces"] = ["chatgpt", "codex"]
            packet["operator_prerequisites"].append("verified domain")
            packet["review_recording"] = {
                "required": True,
                "operator_supplied": True,
            }
            _validate_openai_sale_free_packet(packet)
        else:
            packet["acceptance_surfaces"] = ["claude"]
            packet["user_prerequisites"] = definition["common"]["user_prerequisites"]
        _validate_public_json(
            packet,
            Path(f"directory-packet-{selected_channel}.json"),
            evidence_path=True,
        )
        packet["listing_sha256"] = _sha256(_canonical_json(packet))
        packets[selected_channel] = _canonical_json(packet) + b"\n"
    return packets


def directory_render(
    repo_root: Path | None = None,
    output: Path | None = None,
    *,
    channel: str = "all",
    openai_app_id: str | None = None,
) -> Path:
    root = _repo_root(repo_root)
    if channel not in (*DIRECTORY_CHANNELS, "all"):
        raise ValueError("unsupported directory channel")
    target = output or _marketplace_path(root, "directory/generated")
    if (
        target.resolve() == _marketplace_path(root, "directory/generated").resolve()
        and channel in ("openai-plugin", "all")
    ):
        _validate_repository_openai_app_id(root, openai_app_id)
    packets = directory_packets(root, channel=channel, openai_app_id=openai_app_id)
    target.mkdir(parents=True, exist_ok=True)
    selected = DIRECTORY_CHANNELS if channel == "all" else (channel,)
    for item in selected:
        _write_json_atomic(target / f"{item}.json", json.loads(packets[item]))
    return target


def directory_check(
    repo_root: Path | None = None,
    *,
    channel: str = "all",
    openai_app_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate and return the selected redacted packets without publishing them."""

    packets = directory_packets(repo_root, channel=channel, openai_app_id=openai_app_id)
    root = _repo_root(repo_root)
    checked = {name: json.loads(packet) for name, packet in packets.items()}
    generated = _marketplace_path(root, "directory/generated")
    for name, packet in checked.items():
        path = generated / f"{name}.json"
        if not path.is_file():
            raise ValueError(f"generated directory packet is missing: {path}")
        actual = _load_marketplace_json(path, "generated directory packet")
        if actual != packet:
            paths = ", ".join(_json_difference_paths(actual, packet))
            raise ValueError(f"generated directory packet is stale: {paths}")
    return checked


def _skills_digest(root: Path, candidate: str = DEFAULT_CANDIDATE) -> str:
    payload = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8").replace("\r\n", "\n")
        for path in _skill_paths(root, candidate)
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


def compatibility_manifest(
    repo_root: Path | None = None, *, candidate: str = DEFAULT_CANDIDATE
) -> dict[str, Any]:
    root = _repo_root(repo_root)
    definition = load_definition(root, candidate=candidate)
    dependencies = skill_dependencies(root, candidate=candidate)
    contract = hosted_gateway.build_agent_gateway_contract(profile=definition.profile)
    oauth_overlay = oauth_discovery_overlay(contract)
    # The descriptor identifies the contract surface, not the build that emitted
    # it. `exomem_release` belongs to the running server's contract, and keeping
    # it here made `compatibility_sha256` move on every version bump -- which
    # invalidated the committed artifacts and, worse, any live promotion record
    # for a plugin whose contract had not changed at all. The running release is
    # still reported by `build_agent_gateway_contract`; a published artifact just
    # is not the place to pin it.
    #
    # The contract's own digest hashes a base that includes `exomem_release`, so
    # re-digesting the published contract is part of the decoupling rather than a
    # cosmetic tidy: leaving the runtime digest in place would carry the release
    # back into `schema_contract_sha256` and keep the churn. `schema_contract_sha256`
    # never leaves this module -- descriptor, lock, promotion evidence -- and
    # nothing cross-checks it against the running gateway's digest, so this stays
    # out of the runtime contract, which still advertises its release-inclusive
    # digest unchanged.
    published_base = {
        key: value for key, value in contract.items() if key not in {"exomem_release", "digest"}
    }
    published_contract = {
        **published_base,
        "digest": {"algorithm": "sha256", "value": _sha256(_canonical_json(published_base))},
    }
    raw_definition = json.loads((_candidate_root(root, candidate) / "definition.json").read_text(encoding="utf-8"))
    commands_in_order = tuple(item["name"] for item in contract["commands"])
    base = {
        "schema_version": 1,
        "plugin_id": definition.plugin_id,
        "plugin_version": definition.version,
        "endpoint": definition.endpoint,
        "profile": definition.profile,
        "commands": list(commands_in_order),
        "command_surface_sha256": contract["agent_profile"]["active_capability_sha256"],
        "schema_contract_sha256": published_contract["digest"]["value"],
        "definition_sha256": _sha256(_canonical_json(raw_definition)),
        "skills_sha256": _skills_digest(root, candidate),
        "skills": {name: list(required_tools) for name, required_tools in dependencies.items()},
        "agent_contract": published_contract,
        "oauth_discovery": oauth_overlay,
        "oauth_discovery_sha256": _sha256(_canonical_json(oauth_overlay)),
    }
    if candidate in RECORDS_CANDIDATES:
        base["minimum_records_reader_version"] = 2
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
    candidate: str = DEFAULT_CANDIDATE,
) -> dict[str, Any]:
    definition = load_definition(root, candidate=candidate)
    compatibility = compatibility_manifest(root, candidate=candidate)
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
    if candidate in RECORDS_CANDIDATES:
        lock["minimum_records_reader_version"] = 2
        selection_path = _candidate_root(root, candidate) / "selection-cases.json"
        lock["selection_cases_sha256"] = _sha256(
            _canonical_json(json.loads(selection_path.read_text(encoding="utf-8")))
        )
    return lock


def _validate_openai_app_id(value: str | None) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9]+", clean):
        raise ValueError("OpenAI candidate requires a registered OpenAI app release input")
    return clean


def _validate_repository_openai_app_id(root: Path, value: str | None) -> str:
    """Keep fixture IDs out of the committed production candidate artifacts."""

    app_id = _validate_openai_app_id(value)
    if root.resolve() == _repo_root().resolve() and app_id != REGISTERED_OPENAI_APP_ID:
        raise ValueError("repository OpenAI artifacts may not use a fixture app ID")
    return app_id


def _registered_app_id_sha256(value: str) -> str:
    return _sha256(_validate_openai_app_id(value).encode("utf-8"))


def _directory_plugin_id_sha256(value: str) -> str:
    clean = str(value).strip()
    if not re.fullmatch(r"plugin_asdk_app_[A-Za-z0-9]+", clean):
        raise ValueError("directory receipt has an invalid provider directory identity")
    return _sha256(clean.encode("utf-8"))


def _validate_directory_receipt(
    root: Path,
    channel: str,
    state: str,
    receipt: Any,
    *,
    listing_version: str,
    openai_app_id: str | None,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
    require_fresh_timestamp: bool,
    require_current_listing: bool,
    label: str = "directory receipt",
) -> str | None:
    """Return a public failure for one provider receipt, or ``None`` when bound."""

    required = {
        "schema_version",
        "channel",
        "state",
        "listing_version",
        "listing_sha256",
        "compatibility_sha256",
        "package_lock_sha256",
        "archive_lock_sha256",
        "promotion_record_sha256",
        "provider_directory_id_sha256",
        "recorded_at",
        "deployment_sha256",
        "operator_key_id",
        "operator_signature",
        "public_url",
    }
    expected = required | ({"registered_app_id_sha256"} if channel == "openai-plugin" else set())
    requires_directory_id = channel == "openai-plugin" and state == "published"
    if not isinstance(receipt, dict):
        return f"{label} requires an exact provider receipt"
    if requires_directory_id:
        expected.add("directory_plugin_id")
    elif receipt.get("directory_plugin_id") is not None:
        expected.add("directory_plugin_id")
    if set(receipt) != expected or receipt.get("schema_version") != 1:
        return f"{label} has unsupported fields"
    digest_fields = {
        "listing_sha256",
        "compatibility_sha256",
        "package_lock_sha256",
        "archive_lock_sha256",
        "promotion_record_sha256",
        "provider_directory_id_sha256",
        "deployment_sha256",
    }
    if channel == "openai-plugin":
        digest_fields.add("registered_app_id_sha256")
    if not all(
        isinstance(receipt.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", receipt[key])
        for key in digest_fields
    ):
        return f"{label} has an invalid digest"
    if (
        not isinstance(receipt.get("channel"), str)
        or not isinstance(receipt.get("state"), str)
        or not isinstance(receipt.get("listing_version"), str)
        or not receipt["listing_version"].strip()
        or not isinstance(trusted_key_id, str)
        or not trusted_key_id
        or not isinstance(trusted_secret, str)
        or not trusted_secret
        or receipt.get("operator_key_id") != trusted_key_id
        or not isinstance(receipt.get("operator_signature"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["operator_signature"])
    ):
        return f"{label} is unsigned or untrusted"
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "operator_signature"}
    expected_signature = hmac.new(
        trusted_secret.encode("utf-8"), _canonical_json(unsigned_receipt), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(receipt["operator_signature"], expected_signature):
        return f"{label} has an invalid operator signature"
    if receipt["deployment_sha256"] != deployment_sha256:
        return f"{label} has stale bindings"
    try:
        bindings = _directory_bindings(root, channel, openai_app_id=openai_app_id)
        packet = (
            json.loads(
                directory_packets(root, channel=channel, openai_app_id=openai_app_id)[channel]
            )
            if require_current_listing
            else None
        )
        if require_fresh_timestamp:
            _validate_fresh_utc_timestamp(receipt["recorded_at"], f"{label} recorded_at")
        else:
            timestamp = receipt["recorded_at"]
            if not isinstance(timestamp, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp
            ):
                raise ValueError(f"{label} recorded_at must be canonical UTC")
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) > datetime.now(UTC):
                raise ValueError(f"{label} recorded_at is in the future")
        _validate_public_https_url(receipt["public_url"], f"{label} public_url")
    except ValueError as exc:
        return str(exc)
    if any(
        receipt[key] != bindings[key]
        for key in ("compatibility_sha256", "package_lock_sha256", "archive_lock_sha256")
    ):
        return f"{label} does not bind the current artifact"
    if (
        receipt["channel"] != channel
        or receipt["state"] != state
        or receipt["listing_version"] != listing_version
        or receipt["promotion_record_sha256"]
        != promotion_record_sha256(root, "openai" if channel == "openai-plugin" else "claude")
    ):
        return f"{label} does not bind the current publication state"
    if (
        require_current_listing
        and packet is not None
        and receipt["listing_sha256"] != packet["listing_sha256"]
    ):
        return f"{label} does not bind the current listing"
    if channel == "openai-plugin":
        if receipt["registered_app_id_sha256"] != bindings["registered_app_id_sha256"]:
            return f"{label} does not bind the registered application"
        directory_plugin_id = receipt.get("directory_plugin_id")
        if requires_directory_id and not isinstance(directory_plugin_id, str):
            return f"published OpenAI {label} requires a provider directory identity"
        if directory_plugin_id is not None:
            try:
                directory_identity_sha256 = _directory_plugin_id_sha256(directory_plugin_id)
            except ValueError:
                return f"{label} has an invalid provider directory identity"
            if receipt["provider_directory_id_sha256"] != directory_identity_sha256:
                return f"{label} has an invalid provider directory identity"
    return None


def _generated_openai_app_id(generated: Path) -> str:
    try:
        app_id = json.loads((generated / "openai" / ".app.json").read_text(encoding="utf-8"))[
            "apps"
        ]["exomem"]["id"]
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
        mcp = json.loads((package / ".mcp.json").read_text(encoding="utf-8"))
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
    if mcp != {
        "mcp_servers": {
            "exomem": {"type": "http", "url": "https://substratesystems.io/api/exomem/mcp/v1"}
        }
    }:
        raise ValueError("OpenAI MCP connection must use the universal plugin shape")
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
    repo_root: Path | None = None,
    *,
    platform: str = "claude",
    openai_app_id: str | None = None,
    candidate: str = DEFAULT_CANDIDATE,
) -> dict[str, bytes]:
    """Return deterministic candidate bytes without creating a staging directory."""

    root = _repo_root(repo_root)
    definition = load_definition(root, candidate=candidate)
    skill_dependencies(root, candidate=candidate)
    validate_hosted_public_inputs(root, include_generated=False)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    app_id = _validate_openai_app_id(openai_app_id) if "openai" in selected else None
    files: dict[str, bytes] = {}
    for item in selected:
        if item == "openai":
            assert app_id is not None
        prefix = f"{item}/"
        for source in _skill_paths(root, candidate):
            files[prefix + f"skills/{source.parent.name}/SKILL.md"] = (
                source.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            )
        for source in (root / PLUGIN_ROOT / "assets").rglob("*"):
            if source.is_file():
                files[
                    prefix
                    + "assets/"
                    + source.relative_to(root / PLUGIN_ROOT / "assets").as_posix()
                ] = source.read_bytes()
        mcp_servers = {"exomem": {"type": "http", "url": definition.endpoint}}
        files[prefix + ".mcp.json"] = (
            _canonical_json(
                {"mcpServers": mcp_servers} if item == "claude" else {"mcp_servers": mcp_servers}
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
            candidate=candidate,
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
                        {"registered_app_id_sha256": _registered_app_id_sha256(cast(str, app_id))}
                        if item == "openai"
                        else {}
                    ),
                }
            )
            + b"\n"
        )
    files["compatibility.json"] = (
        _canonical_json(compatibility_manifest(root, candidate=candidate)) + b"\n"
    )
    return files


def render(
    repo_root: Path | None = None,
    output: Path | None = None,
    *,
    openai_app_id: str | None = None,
    platform: str = "claude",
    staging_root: Path | None = None,
    candidate: str = DEFAULT_CANDIDATE,
) -> Path:
    root = _repo_root(repo_root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    generated_root = root / PLUGIN_ROOT / "generated"
    destination = (
        output
        or (generated_root if candidate == DEFAULT_CANDIDATE else generated_root / "candidates" / candidate)
    ).resolve()
    allowed_root = (staging_root or (root / PLUGIN_ROOT)).resolve()
    if allowed_root not in destination.parents or destination == allowed_root:
        raise ValueError("render output must be below the explicit staging root")
    if destination == root or destination in root.parents:
        raise ValueError("render output must not be at or above the repository")
    managed_destination = (
        generated_root
        if candidate == DEFAULT_CANDIDATE
        else generated_root / "candidates" / candidate
    ).resolve()
    if destination.exists() and destination != managed_destination:
        raise ValueError("render output already exists; refuse to replace an unchecked directory")
    selected = PLATFORMS if platform == "all" else (platform,)
    if destination == managed_destination and "openai" in selected:
        _validate_repository_openai_app_id(root, openai_app_id)
    with ExitStack() as release_locks:
        if destination == managed_destination:
            for selected_platform in sorted(PLATFORMS):
                release_locks.enter_context(_promotion_mutex(root, selected_platform))
        rendered_files = candidate_files(
            root, platform=platform, openai_app_id=openai_app_id, candidate=candidate
        )
        nonce = uuid4().hex
        destination.parent.mkdir(parents=True, exist_ok=True)
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
            for relative, contents in rendered_files.items():
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
    candidate: str = DEFAULT_CANDIDATE,
) -> None:
    root = _repo_root(repo_root)
    validate_hosted_public_inputs(root)
    if candidate == DEFAULT_CANDIDATE:
        check_compatibility_descriptor(root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    generated_root = root / PLUGIN_ROOT / "generated"
    expected = (
        generated_root
        if candidate == DEFAULT_CANDIDATE
        else generated_root / "candidates" / candidate
    )
    if "openai" in selected:
        generated_app_id = _generated_openai_app_id(expected)
        _validate_repository_openai_app_id(root, generated_app_id)
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
                or (path.parent == expected and path.name
                in {
                    "compatibility.json",
                *(f"{item}.lock.json" for item in selected),
                *(f"{item}.zip" for item in selected),
                    *(f"{item}.zip.lock.json" for item in selected),
                })
        )
    }
    actual_files = candidate_files(
        root, platform=platform, openai_app_id=openai_app_id, candidate=candidate
    )
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
    candidate: str = DEFAULT_CANDIDATE,
) -> Path:
    root = _repo_root(repo_root)
    if platform not in (*PLATFORMS, "all"):
        raise ValueError("unsupported platform")
    selected = PLATFORMS if platform == "all" else (platform,)
    with ExitStack() as release_locks:
        for selected_platform in sorted(selected):
            release_locks.enter_context(_promotion_mutex(root, selected_platform))
        validate_hosted_public_inputs(root)
        check(root, openai_app_id=openai_app_id, platform=platform, candidate=candidate)
        output_root = output or root / "dist" / "hosted"
        output_root.mkdir(parents=True, exist_ok=True)
        for selected_platform in selected:
            generated = root / PLUGIN_ROOT / "generated"
            if candidate != DEFAULT_CANDIDATE:
                generated = generated / "candidates" / candidate
            package = generated / selected_platform
            archive_path = output_root / f"{selected_platform}.zip"
            archive_bytes = _archive_bytes(package)
            _write_bytes_atomic(archive_path, archive_bytes)
            lock = {
                "platform": selected_platform,
                "archive_sha256": _sha256(archive_bytes),
            }
            if selected_platform == "openai":
                lock["registered_app_id_sha256"] = _registered_app_id_sha256(
                    _generated_openai_app_id(generated)
                )
            _write_json_atomic(
                output_root / f"{selected_platform}.zip.lock.json",
                lock,
            )
    return output_root


def promotion_record(
    repo_root: Path | None, platform: str, *, candidate: str = DEFAULT_CANDIDATE
) -> Path:
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    root = _repo_root(repo_root) / PLUGIN_ROOT / "promotion"
    return root / f"{platform}.json" if candidate == DEFAULT_CANDIDATE else root / "candidates" / candidate / f"{platform}.json"


def promotion_record_sha256(
    repo_root: Path | None, platform: str, *, candidate: str = DEFAULT_CANDIDATE
) -> str:
    path = promotion_record(repo_root, platform, candidate=candidate)
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


def _selection_cases(
    root: Path, lock: Mapping[str, Any], *, candidate: str = LIFECYCLE_CANDIDATE
) -> tuple[dict[str, str], dict[str, tuple[str, str, str, bool]], dict[str, tuple[str, str, str, str]]]:
    if candidate not in RECORDS_CANDIDATES:
        raise ValueError("selection cases belong to a Records-bearing candidate")
    path = _candidate_root(root, candidate) / "selection-cases.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Records selection cases must be valid JSON") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "client_contracts", "cases"}
        or raw["schema_version"] != 1
    ):
        raise ValueError("Records selection cases have an invalid schema")
    cases = raw["cases"]
    raw_contracts = raw["client_contracts"]
    if not isinstance(cases, list) or not isinstance(raw_contracts, dict):
        raise ValueError("Records selection cases must be a list")
    contracts: dict[str, tuple[str, str, str, str]] = {}
    for client in ("codex", "claude-code"):
        contract = raw_contracts.get(client)
        if not isinstance(contract, dict) or set(contract) != {
            "client", "client_version", "model_version", "system_contract_version"
        }:
            raise ValueError("Records selection cases have invalid client contracts")
        values = tuple(contract[key] for key in ("client", "client_version", "model_version", "system_contract_version"))
        if not all(isinstance(value, str) and value for value in values) or values[0] != client:
            raise ValueError("Records selection cases have invalid client contracts")
        contracts[client] = values
    if set(raw_contracts) != set(contracts):
        raise ValueError("Records selection cases have invalid client contracts")
    expected_pairs = {
        ("codex-existing-collection", "codex", "append"),
        ("claude-code-existing-collection", "claude-code", "append"),
        ("codex-no-collection", "codex", "proposal"),
        ("claude-code-no-collection", "claude-code", "proposal"),
    }
    actual_pairs: set[tuple[str, str, str]] = set()
    prompts: dict[str, str] = {}
    results: dict[str, tuple[str, str, str, bool]] = {}
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "client", "expected", "prompt_sha256"}:
            raise ValueError("Records selection cases have invalid fields")
        identifier, client, expected, digest = (
            case["id"], case["client"], case["expected"], case["prompt_sha256"]
        )
        if not all(isinstance(value, str) for value in (identifier, client, expected, digest)) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Records selection cases have invalid values")
        actual_pairs.add((identifier, client, expected))
        prompts[identifier] = digest
        results[identifier] = (
            client,
            "append" if expected == "append" else "proposal",
            "committed" if expected == "append" else "completed",
            expected == "append",
        )
    if actual_pairs != expected_pairs or len(prompts) != len(cases):
        raise ValueError("Records selection cases do not cover the required client matrix")
    digest = _sha256(_canonical_json(raw))
    if lock.get("selection_cases_sha256") != digest:
        raise ValueError("Records candidate lock does not bind the selection cases")
    return prompts, results, contracts


def _current_release_binding(
    root: Path, platform: str, *, candidate: str = DEFAULT_CANDIDATE
) -> tuple[dict[str, Any], dict[str, Any]]:
    generated = root / PLUGIN_ROOT / "generated"
    if candidate != DEFAULT_CANDIDATE:
        generated = generated / "candidates" / candidate
    try:
        lock = json.loads(
            (generated / f"{platform}.lock.json").read_text(encoding="utf-8")
        )
        archive_path = generated / f"{platform}.zip"
        archive_lock = json.loads(
            (generated / f"{platform}.zip.lock.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "promotion requires a committed generated package and archive lock"
        ) from exc
    check(root, platform=platform, candidate=candidate)
    package = generated / platform
    archive_bytes = archive_path.read_bytes()
    if (
        _files_digest(package) != lock.get("artifact_sha256")
        or archive_bytes != _archive_bytes(package)
        or _sha256(archive_bytes) != archive_lock.get("archive_sha256")
    ):
        raise ValueError("promotion candidate bytes are stale")
    return lock, archive_lock


def _validate_records_acceptance(
    root: Path, evidence: dict[str, Any], *, compatibility: dict[str, Any], lock: dict[str, Any], expectation: Any,
    require_fresh: bool = True,
    candidate: str = LIFECYCLE_CANDIDATE,
) -> None:
    if not isinstance(expectation, dict) or set(expectation) != {
        "deployment_sha256", "vault_purpose", "reset_epoch", "principal_hmac_sha256",
        "audience_hmac_sha256", "client_contracts", "graph_proof_digest", "prompt_cases", "selection_cases_sha256",
    }:
        raise ValueError("Records promotion requires exact operator expectations")
    facts = evidence.get("records_acceptance")
    if not isinstance(facts, dict):
        raise ValueError("Records promotion requires closed lifecycle evidence")
    if lock.get("minimum_records_reader_version") != 2:
        raise ValueError("Records lifecycle candidate does not bind reader floor 2")
    prompt_cases, prompt_results, contracts = _selection_cases(root, lock, candidate=candidate)
    if (
        expectation["selection_cases_sha256"] != lock["selection_cases_sha256"]
        or expectation["prompt_cases"] != prompt_cases
        or expectation["client_contracts"] != {
            client: list(contract) for client, contract in contracts.items()
        }
    ):
        raise ValueError("Records operator expectations do not match the committed selection cases")
    live = _records_live_acceptance_module()
    expected = live.RecordsEvidenceExpectation(
        deployment_sha256=expectation["deployment_sha256"],
        package="exomem",
        release_version=lock["plugin_version"],
        # Resolve rather than assume. Candidate and profile are the same string
        # for every candidate registered today, but this is a security-relevant
        # acceptance binding and the indirection exists precisely so the two can
        # diverge without silently binding evidence to the wrong profile.
        profile=_candidate_profile(candidate),
        minimum_records_reader_version=2,
        surface_digest=compatibility["schema_contract_sha256"],
        vault_purpose=expectation["vault_purpose"],
        reset_epoch=expectation["reset_epoch"],
        principal_hmac_sha256=expectation["principal_hmac_sha256"],
        audience_hmac_sha256=expectation["audience_hmac_sha256"],
        client_contracts=contracts,
        required_actions=frozenset({"describe", "validate", "create", "inspect", "query", "append", "update", "revise", "rebaseline"}),
        required_prompt_cases=prompt_cases,
        required_prompt_case_results=prompt_results,
        graph_proof_digest=expectation["graph_proof_digest"],
    )
    try:
        live.validate_records_live_evidence(
            facts, expected=expected, now=datetime.now(UTC), require_fresh=require_fresh
        )
    except ValueError as exc:
        raise ValueError(f"Records lifecycle evidence is invalid: {exc}") from exc


def _validate_promotion_evidence(
    root: Path,
    platform: str,
    evidence: dict[str, Any],
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    require_fresh: bool = True,
    candidate: str = DEFAULT_CANDIDATE,
    records_expectation: dict[str, Any] | None = None,
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
    if platform == "openai":
        required_strings.add("registered_app_id_sha256")
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
    if candidate in RECORDS_CANDIDATES:
        required.add("records_acceptance")
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

    compatibility = compatibility_manifest(root, candidate=candidate)
    definition = load_definition(root, candidate=candidate)
    lock, archive_lock = _current_release_binding(root, platform, candidate=candidate)
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
    if platform == "openai":
        registered_app_id_sha256 = lock.get("registered_app_id_sha256")
        if (
            not isinstance(registered_app_id_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", registered_app_id_sha256)
            or archive_lock.get("registered_app_id_sha256") != registered_app_id_sha256
        ):
            raise ValueError("promotion candidate does not bind the registered app identity")
        if evidence["registered_app_id_sha256"] != registered_app_id_sha256:
            raise ValueError("promotion evidence has a different registered app identity")
    if any(evidence[key] != value for key, value in expected_identity.items()):
        raise ValueError("promotion evidence has a different compatibility or package identity")
    digest_keys: tuple[str, ...] = (
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
    if platform == "openai":
        digest_keys += ("registered_app_id_sha256",)
    if not all(re.fullmatch(r"[0-9a-f]{64}", evidence[key]) for key in digest_keys):
        raise ValueError("promotion evidence digests must be SHA-256 values")
    if not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id:
        raise ValueError("promotion requires an operator-trusted signing key")
    unsigned = {key: value for key, value in evidence.items() if key != "operator_signature"}
    expected_signature = hmac.new(
        trusted_secret.encode("utf-8"), _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(evidence["operator_signature"], expected_signature):
        raise ValueError("promotion operator signature is invalid")
    if candidate in RECORDS_CANDIDATES:
        _validate_records_acceptance(
            root,
            evidence,
            compatibility=compatibility,
            lock=lock,
            expectation=records_expectation,
            require_fresh=require_fresh,
            candidate=candidate,
        )
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
    candidate: str = DEFAULT_CANDIDATE,
    records_expectation: dict[str, Any] | None = None,
) -> None:
    """Promote only evidence from a real, content-bearing clean-client journey."""
    if platform not in PLATFORMS:
        raise ValueError("unsupported platform")
    if "operator_key_id" in evidence and (
        not trusted_key_id or not trusted_secret or evidence["operator_key_id"] != trusted_key_id
    ):
        raise ValueError("promotion requires an operator-trusted signing key")
    root = _repo_root(repo_root)
    expected_states = {"pending", "failed"}
    if candidate in RECORDS_CANDIDATES:
        expected_states.add("live")
    if expected_state not in expected_states or not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_record_sha256 or "")
    ):
        raise ValueError("promotion requires expected state and record digest")
    record_path = promotion_record(root, platform, candidate=candidate)
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
        if expected_state == "live" and prior.get("evidence") == evidence:
            compatibility, lock, _archive_lock = _validate_promotion_evidence(
                root,
                platform,
                evidence,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                require_fresh=False,
                candidate=candidate,
                records_expectation=records_expectation,
            )
            if (
                prior.get("package_lock") != lock
                or prior.get("compatibility_sha256") != compatibility["compatibility_sha256"]
            ):
                raise ValueError("promotion record changed; refresh before retrying")
            return
        compatibility, lock, _archive_lock = _validate_promotion_evidence(
            root,
            platform,
            evidence,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            candidate=candidate,
            records_expectation=records_expectation,
        )
        promoted = {
            "schema_version": 1,
            "platform": platform,
            **(
                {"candidate": candidate, "minimum_records_reader_version": 2}
                if candidate in RECORDS_CANDIDATES
                else {}
            ),
            "state": "live",
            "package_lock": lock,
            "compatibility_sha256": compatibility["compatibility_sha256"],
            "evidence": evidence,
        }
        if expected_state == "live":
            if prior != promoted:
                raise ValueError("promotion record changed; refresh before retrying")
            return
        _write_json_atomic(record_path, promoted)


def demote(
    repo_root: Path | None,
    platform: str,
    reason: str,
    *,
    expected_state: str | None = None,
    expected_record_sha256: str | None = None,
    candidate: str = DEFAULT_CANDIDATE,
) -> None:
    root = _repo_root(repo_root)
    if reason not in DEMOTION_REASONS:
        raise ValueError("demotion requires a stable reason code")
    if expected_state != "live" or not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_record_sha256 or "")
    ):
        raise ValueError("demotion requires expected live state and record digest")
    record_path = promotion_record(root, platform, candidate=candidate)
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
                **({"candidate": candidate, "minimum_records_reader_version": 2} if candidate in RECORDS_CANDIDATES else {}),
                "state": "failed",
                "reason": reason,
            },
        )


def distribution_manifest(
    repo_root: Path | None = None,
    *,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
    candidate: str = DEFAULT_CANDIDATE,
    records_expectation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _repo_root(repo_root)
    records = {
        platform: json.loads(promotion_record(root, platform, candidate=candidate).read_text(encoding="utf-8"))
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
            candidate=candidate,
            records_expectation=records_expectation,
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


def _contains_private_reviewer_field(value: Any) -> bool:
    prohibited = {
        "accesstoken",
        "apikey",
        "bearertoken",
        "credentialid",
        "credentialidentifier",
        "invitation",
        "invitelink",
        "invitetoken",
        "inviteurl",
        "password",
        "refreshtoken",
        "revieweremail",
        "revieweridentity",
        "samplecontent",
        "tenantid",
        "userid",
        "username",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized in prohibited
                or normalized.endswith(("username", "userid", "userids"))
                or _contains_private_reviewer_field(nested)
            ):
                return True
    elif isinstance(value, list):
        return any(_contains_private_reviewer_field(nested) for nested in value)
    return False


def _load_signed_directory_evidence(
    root: Path,
    name: str,
    evidence_type: str,
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> dict[str, Any]:
    path = _marketplace_path(root, f"directory/{name}.json")
    if not path.is_file():
        raise ValueError(f"{name} is missing")
    value = _load_marketplace_json(path, name)
    if evidence_type == "directory-reviewer-access" and _contains_private_reviewer_field(value):
        raise ValueError(f"{name} contains private material")
    required = {
        "schema_version",
        "evidence_type",
        "deployment_sha256",
        "checked_at",
        "expires_at",
        "operator_key_id",
        "operator_signature",
    }
    payload_fields = {
        "directory-production-probes": {
            "surfaces",
            "compatibility_sha256",
            "command_surface_sha256",
            "schema_contract_sha256",
            "full_tool_contract_sha256",
            "origin_rejection",
            "response_minimization",
            "sampled_output_sale_free",
        },
        "directory-prerequisites": {"channels"},
        "directory-public-admission": {"admission"},
        "directory-reviewer-access": {"channels"},
        "directory-post-install": {
            "channel",
            "submission_sha256",
            "listing_sha256",
            "package_lock_sha256",
            "public_url",
            "checks",
            "sampled_output_sale_free",
        },
    }.get(evidence_type)
    if (
        payload_fields is None
        or set(value) != required | payload_fields
        or value.get("schema_version") != 1
        or value.get("evidence_type") != evidence_type
    ):
        raise ValueError(f"{name} has an unsupported schema")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("deployment_sha256"))):
        raise ValueError(f"{name} has an invalid deployment binding")
    if not re.fullmatch(r"[0-9a-f]{64}", str(deployment_sha256 or "")):
        raise ValueError("directory deployment SHA-256 is required")
    if value["deployment_sha256"] != deployment_sha256:
        raise ValueError(f"{name} binds a different deployment")
    if not all(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", str(value[key]))
        for key in ("checked_at", "expires_at")
    ):
        raise ValueError(f"{name} has an invalid timestamp")
    try:
        checked_at = datetime.fromisoformat(str(value["checked_at"]).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid timestamp") from exc
    if (
        checked_at.tzinfo is None
        or expires_at.tzinfo is None
        or expires_at <= checked_at
        or expires_at - checked_at > timedelta(hours=24)
        or checked_at.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5)
        or datetime.now(UTC) > expires_at.astimezone(UTC)
    ):
        raise ValueError(f"{name} is expired or exceeds its TTL")
    if (
        not trusted_key_id
        or not trusted_secret
        or value.get("operator_key_id") != trusted_key_id
        or not isinstance(value.get("operator_signature"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["operator_signature"])
    ):
        raise ValueError(f"{name} is unsigned or untrusted")
    unsigned = {key: nested for key, nested in value.items() if key != "operator_signature"}
    expected = hmac.new(
        trusted_secret.encode("utf-8"), _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(value["operator_signature"], expected):
        raise ValueError(f"{name} has an invalid operator signature")
    return value


def _directory_prerequisites(
    root: Path,
    channel: str,
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> dict[str, bool]:
    value = _load_signed_directory_evidence(
        root,
        "prerequisite-evidence",
        "directory-prerequisites",
        trusted_key_id=trusted_key_id,
        trusted_secret=trusted_secret,
        deployment_sha256=deployment_sha256,
    )
    channels = value.get("channels")
    if not isinstance(channels, dict) or set(channels) != set(DIRECTORY_CHANNELS):
        raise ValueError("directory operator prerequisites are incomplete")
    requirements = channels.get(channel) if isinstance(channels, dict) else None
    expected = {
        "provider_registration",
        "publisher_verified",
        "policy_approved",
        "reviewer_seeded",
        *(
            {"domain_verified", "review_recording_prepared"}
            if channel == "openai-plugin"
            else set()
        ),
    }
    if (
        not isinstance(requirements, dict)
        or set(requirements) != expected
        or any(type(complete) is not bool for complete in requirements.values())
    ):
        raise ValueError("directory operator prerequisites are incomplete")
    return requirements


def _validate_reviewer_access_entry(
    channel: str,
    access: Any,
    fixture: dict[str, Any],
    *,
    require_active: bool,
) -> None:
    expected = {
        "provider",
        "feature_enabled",
        "credential_active",
        "credential_expires_at",
        "fixture_version",
        "payload_sha256",
    }
    if not isinstance(access, dict) or set(access) != expected:
        raise ValueError("reviewer access evidence has an invalid channel entry")
    if access["provider"] != ("openai" if channel == "openai-plugin" else "anthropic"):
        raise ValueError("reviewer access provider does not match the channel")
    if type(access["feature_enabled"]) is not bool or type(access["credential_active"]) is not bool:
        raise ValueError("reviewer access evidence has an invalid channel entry")
    if require_active and (
        access["feature_enabled"] is not True or access["credential_active"] is not True
    ):
        raise ValueError("reviewer access feature or credential is inactive")
    credential_expires_at = access["credential_expires_at"]
    if not isinstance(credential_expires_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", credential_expires_at
    ):
        raise ValueError("reviewer access credential expiry is invalid")
    try:
        credential_expiry = datetime.fromisoformat(credential_expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewer access credential expiry is invalid") from exc
    if require_active and credential_expiry <= datetime.now(UTC) + DIRECTORY_MINIMUM_REVIEW_WINDOW:
        raise ValueError("reviewer access credential expires before the minimum review window")
    if access["fixture_version"] != fixture["fixture_version"]:
        raise ValueError("reviewer access fixture version is stale")
    if access["payload_sha256"] != fixture["payload_sha256"]:
        raise ValueError("reviewer access fixture payload digest is stale")


def _directory_reviewer_access(
    root: Path,
    channel: str,
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> None:
    value = _load_signed_directory_evidence(
        root,
        "reviewer-access-evidence",
        "directory-reviewer-access",
        trusted_key_id=trusted_key_id,
        trusted_secret=trusted_secret,
        deployment_sha256=deployment_sha256,
    )
    channels = value.get("channels")
    if not isinstance(channels, dict) or set(channels) != set(DIRECTORY_CHANNELS):
        raise ValueError("reviewer access evidence is incomplete")
    fixture = _load_marketplace_review_fixture(root)
    for candidate_channel in DIRECTORY_CHANNELS:
        _validate_reviewer_access_entry(
            candidate_channel,
            channels[candidate_channel],
            fixture,
            require_active=candidate_channel == channel,
        )
    checked_at = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
    if datetime.now(UTC) - checked_at > DIRECTORY_REVIEWER_EVIDENCE_MAX_AGE:
        raise ValueError("reviewer access evidence is stale")


def _directory_probe_blockers(
    root: Path,
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> list[str]:
    try:
        value = _load_signed_directory_evidence(
            root,
            "production-evidence",
            "directory-production-probes",
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
    except ValueError as exc:
        return [str(exc)]
    required = (
        "website_url",
        "documentation_url",
        "setup_url",
        "privacy_url",
        "terms_url",
        "support_url",
        "oauth_discovery",
        "mcp_authorization",
        "mcp_initialize",
        "tool_discovery",
    )
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, dict) or set(surfaces) != set(required):
        return ["production probes are invalid"]
    blockers: list[str] = []
    compatibility = compatibility_manifest(root)
    for key in (
        "compatibility_sha256",
        "command_surface_sha256",
        "schema_contract_sha256",
    ):
        if value.get(key) != compatibility[key]:
            blockers.append(f"production probe {key} is stale")
    expected_full_tool_contract_sha256 = _full_tool_contract_sha256(compatibility)
    if value.get("full_tool_contract_sha256") != expected_full_tool_contract_sha256:
        blockers.append("production probe full_tool_contract_sha256 is stale")
    if value.get("origin_rejection") is not True:
        blockers.append("production probe origin rejection is unhealthy")
    if value.get("response_minimization") is not True:
        blockers.append("production probe response minimization is unhealthy")
    if value.get("sampled_output_sale_free") is not True:
        blockers.append("production probe sampled output sale-freedom is unhealthy")
    for name in required:
        evidence = surfaces.get(name)
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"ok", "content_sha256"}
            or evidence.get("ok") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("content_sha256")))
        ):
            blockers.append(f"production probe {name} is unhealthy")
    return blockers


def _post_install_blockers(
    root: Path,
    channel: str,
    active_submission_sha256: str,
    active_record: dict[str, Any],
    *,
    openai_app_id: str | None,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> list[str]:
    try:
        evidence = _load_signed_directory_evidence(
            root,
            f"post-install-evidence/{channel}/{active_submission_sha256}",
            "directory-post-install",
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
    except ValueError as exc:
        return [str(exc)]
    receipt = active_record.get("receipt")
    receipt_error = _validate_directory_receipt(
        root,
        channel,
        "published",
        receipt,
        listing_version=active_record.get("listing_version", ""),
        openai_app_id=openai_app_id,
        trusted_key_id=trusted_key_id,
        trusted_secret=trusted_secret,
        deployment_sha256=deployment_sha256,
        require_fresh_timestamp=False,
        require_current_listing=False,
        label="persisted directory receipt",
    )
    if receipt_error is not None:
        return [receipt_error]
    receipt_mapping = cast(dict[str, Any], receipt)
    required_checks = {
        "fresh_non_reviewer_oauth",
        "tool_and_skill_discovery",
        "governed_recall_with_citation",
        "durable_capture",
        "fresh_chat_recall",
        "do_not_capture",
        "revocation",
    }
    if (
        evidence.get("channel") != channel
        or evidence.get("submission_sha256") != active_submission_sha256
        or evidence.get("listing_sha256") != receipt_mapping.get("listing_sha256")
        or evidence.get("package_lock_sha256") != receipt_mapping.get("package_lock_sha256")
        or evidence.get("public_url") != receipt_mapping.get("public_url")
        or evidence.get("sampled_output_sale_free") is not True
        or evidence.get("checks") != {check: True for check in required_checks}
    ):
        return ["post-install evidence does not bind the active publication"]
    return []


def _public_admission_blockers(
    root: Path,
    *,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> list[str]:
    """Require signed public-admission proof before activating advertised regions."""

    marketplace = load_marketplace_definition(root)["common"]
    if not marketplace["regions"]:
        return []
    if marketplace["user_prerequisites"]["admission"] != {
        "mode": "public",
        "eligibility": "Public access is available to eligible users.",
    }:
        return ["marketplace user prerequisites do not advertise public admission"]
    try:
        admission = _load_signed_directory_evidence(
            root,
            "public-admission-evidence",
            "directory-public-admission",
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
    except ValueError:
        return ["public admission evidence is incomplete"]
    required_admission = {
        "ordinary_acquisition",
        "capacity",
        "quotas",
        "abuse_controls",
        "spend_alarms",
        "support_coverage",
        "pricing_decision",
    }
    if admission.get("admission") != {key: True for key in required_admission}:
        return ["public admission evidence is incomplete"]
    return []


def directory_status(
    repo_root: Path | None = None,
    *,
    openai_app_id: str | None = None,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
    deployment_sha256: str | None = None,
) -> dict[str, Any]:
    """Return channel-specific public readiness without mutating promotions."""

    root = _repo_root(repo_root)
    common_blockers: list[str] = []
    try:
        load_marketplace_definition(root)
        load_marketplace_review_cases(root)
        validate_hosted_public_inputs(root, include_generated=False)
    except ValueError as exc:
        common_blockers.append(str(exc))
    common_blockers.extend(
        _directory_probe_blockers(
            root,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
    )
    channels: dict[str, dict[str, Any]] = {}
    for channel in DIRECTORY_CHANNELS:
        record_sha256, record = _latest_directory_submission(root, channel)
        records = _directory_submissions(root, channel)
        listing_heads = _directory_listing_heads(records)
        publication = _load_directory_publication(root, channel)
        blockers = list(common_blockers)
        platform = "openai" if channel == "openai-plugin" else "claude"
        promotion = json.loads(promotion_record(root, platform).read_text(encoding="utf-8"))
        if promotion.get("state") != "live":
            blockers.append("promotion is not live")
        else:
            try:
                bindings = _directory_bindings(root, channel, openai_app_id=openai_app_id)
                if promotion.get("compatibility_sha256") != bindings["compatibility_sha256"]:
                    blockers.append("live promotion compatibility binding is stale")
                if (
                    promotion.get("package_lock")
                    and _sha256(_canonical_json(promotion["package_lock"]))
                    != bindings["package_lock_sha256"]
                ):
                    blockers.append("live promotion package binding is stale")
            except ValueError as exc:
                blockers.append(str(exc))
        try:
            prerequisites = _directory_prerequisites(
                root,
                channel,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                deployment_sha256=deployment_sha256,
            )
            blockers.extend(
                f"operator prerequisite {name} is incomplete"
                for name, complete in sorted(prerequisites.items())
                if not complete
            )
        except ValueError as exc:
            blockers.append(str(exc))
        try:
            _directory_reviewer_access(
                root,
                channel,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                deployment_sha256=deployment_sha256,
            )
        except ValueError as exc:
            blockers.append(str(exc))
        submission_blockers = list(blockers)
        blockers.extend(
            _public_admission_blockers(
                root,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                deployment_sha256=deployment_sha256,
            )
        )
        active_submission_sha256 = publication["active_submission_sha256"]
        active_record = _directory_submissions(root, channel).get(active_submission_sha256)
        if active_submission_sha256 is not None and active_record is None:
            blockers.append("active publication pointer does not bind a submission")
        if active_record is not None and active_record.get("state") != "published":
            blockers.append("active publication pointer does not bind a published submission")
        if (
            active_record is not None
            and active_submission_sha256 is not None
            and listing_heads.get(active_record["listing_version"]) != active_submission_sha256
        ):
            blockers.append("active publication pointer is stale for its listing version")
        if active_record is not None and active_submission_sha256 is not None:
            blockers.extend(
                _post_install_blockers(
                    root,
                    channel,
                    active_submission_sha256,
                    active_record,
                    openai_app_id=openai_app_id,
                    trusted_key_id=trusted_key_id,
                    trusted_secret=trusted_secret,
                    deployment_sha256=deployment_sha256,
                )
            )
        ready = not blockers
        channels[channel] = {
            "state": record["state"] if len(listing_heads) == 1 else None,
            "latest_event_state": record["state"],
            "submission_ready": not submission_blockers,
            "submission_blockers": submission_blockers,
            "ready": ready,
            "public": active_record is not None and not blockers,
            "blockers": blockers,
            "record_sha256": record_sha256,
            "active_submission_sha256": active_submission_sha256,
            "listing_versions": {
                version: {
                    "record_sha256": digest,
                    "state": records[digest]["state"],
                }
                for version, digest in sorted(listing_heads.items())
            },
        }
    return {
        "channels": channels,
        "public_channels": [channel for channel, status in channels.items() if status["public"]],
    }


def record_directory_state(
    repo_root: Path | None,
    channel: str,
    state: str,
    *,
    expected_state: str,
    expected_record_sha256: str,
    receipt: dict[str, Any] | None = None,
    openai_app_id: str | None = None,
    expected_active_submission_sha256: str | None = None,
    trusted_key_id: str | None = None,
    trusted_secret: str | None = None,
    listing_version: str | None = None,
    target_submission_sha256: str | None = None,
    deployment_sha256: str | None = None,
) -> None:
    """Append a provider revision and update only the channel publication pointer."""

    root = _repo_root(repo_root)
    if state not in DIRECTORY_STATES:
        raise ValueError("directory submission requires a stable state")
    with _promotion_mutex(root, f"directory-{channel}"):
        records = _directory_submissions(root, channel)
        latest_digest, _latest = _latest_directory_submission(root, channel)
        prior = records.get(expected_record_sha256)
        publication = _load_directory_publication(root, channel)
        if (
            prior is None
            or prior.get("state") != expected_state
            or _directory_listing_heads(records).get(prior["listing_version"])
            != expected_record_sha256
        ):
            retries = [
                record
                for record in records.values()
                if record.get("transition_from_submission_sha256") == expected_record_sha256
                and record.get("state") == state
                and record.get("receipt") == receipt
                and record.get("target_submission_sha256") == target_submission_sha256
                and (listing_version is None or record.get("listing_version") == listing_version)
            ]
            if len(retries) == 1:
                if (
                    state == "withdrawn"
                    and publication["active_submission_sha256"] == target_submission_sha256
                ):
                    _write_json_atomic(
                        _directory_publication_path(root, channel),
                        {
                            "schema_version": 1,
                            "channel": channel,
                            "active_submission_sha256": None,
                        },
                    )
                return
            raise ValueError("directory submission changed; refresh before retrying")
        if publication["active_submission_sha256"] != expected_active_submission_sha256:
            raise ValueError("directory publication pointer changed; refresh before retrying")
        transitions = {
            "draft": {"submitted", "rejected", "withdrawn"},
            "submitted": {"in_review", "rejected", "withdrawn"},
            "in_review": {"approved", "rejected", "withdrawn"},
            "approved": {"published", "rejected", "withdrawn"},
            "published": {"withdrawn", "draft"},
            "rejected": {"draft"},
            "withdrawn": {"draft"},
        }
        if state not in transitions[prior["state"]]:
            raise ValueError("directory submission transition is not allowed")
        if state in {"submitted", "in_review", "approved", "published"}:
            status = directory_status(
                root,
                openai_app_id=openai_app_id,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                deployment_sha256=deployment_sha256,
            )["channels"][channel]
            if state != "published" and not status["submission_ready"]:
                raise ValueError("directory submission requires current submission readiness")
            if state == "published" and not status["ready"]:
                raise ValueError("directory submission requires current public readiness")
            receipt_error = _validate_directory_receipt(
                root,
                channel,
                state,
                receipt,
                listing_version=listing_version or prior["listing_version"],
                openai_app_id=openai_app_id,
                trusted_key_id=trusted_key_id,
                trusted_secret=trusted_secret,
                deployment_sha256=deployment_sha256,
                require_fresh_timestamp=True,
                require_current_listing=True,
            )
            if receipt_error is not None:
                raise ValueError(receipt_error)
        elif receipt is not None:
            raise ValueError("draft, rejected, and withdrawn submissions do not accept a receipt")
        if state == "draft":
            if not isinstance(listing_version, str) or not listing_version.strip():
                raise ValueError("new draft directory submission requires a listing version")
        elif listing_version is not None:
            raise ValueError("only a new draft directory submission may set listing version")
        if state == "withdrawn":
            if not re.fullmatch(r"[0-9a-f]{64}", str(target_submission_sha256 or "")):
                raise ValueError("withdrawal requires an exact submission target")
            if target_submission_sha256 not in _directory_submissions(root, channel):
                raise ValueError("withdrawal target is not a known submission")
        elif target_submission_sha256 is not None:
            raise ValueError("only withdrawal may select a target submission")
        record: dict[str, Any] = {
            "schema_version": 1,
            "channel": channel,
            "state": state,
            "listing_version": (
                prior["listing_version"]
                if state == "withdrawn"
                else listing_version or prior["listing_version"]
            ),
            "previous_submission_sha256": latest_digest,
            "transition_from_submission_sha256": expected_record_sha256,
        }
        if receipt is not None:
            record["receipt"] = receipt
        if target_submission_sha256 is not None:
            record["target_submission_sha256"] = target_submission_sha256
        record_digest = _sha256(_canonical_json(record))
        path = _directory_submission_directory(root, channel) / f"{record_digest}.json"
        if path.exists():
            raise ValueError("directory submission revision already exists")
        _write_json_atomic(path, record)
        if (
            state == "withdrawn"
            and publication["active_submission_sha256"] == target_submission_sha256
        ):
            _write_json_atomic(
                _directory_publication_path(root, channel),
                {"schema_version": 1, "channel": channel, "active_submission_sha256": None},
            )


def activate_directory_submission(
    repo_root: Path | None,
    channel: str,
    *,
    target_submission_sha256: str,
    expected_active_submission_sha256: str | None,
    openai_app_id: str | None = None,
    trusted_key_id: str | None,
    trusted_secret: str | None,
    deployment_sha256: str | None,
) -> None:
    """CAS-activate one already-published revision after its post-install proof exists."""

    root = _repo_root(repo_root)
    if channel not in DIRECTORY_CHANNELS:
        raise ValueError("unsupported directory channel")
    with _promotion_mutex(root, f"directory-{channel}"):
        publication = _load_directory_publication(root, channel)
        record = _directory_submissions(root, channel).get(target_submission_sha256)
        if record is None or record.get("state") != "published":
            raise ValueError("directory activation requires an exact published submission")
        admission_blockers = _public_admission_blockers(
            root,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
        if admission_blockers:
            raise ValueError(admission_blockers[0])
        blockers = _post_install_blockers(
            root,
            channel,
            target_submission_sha256,
            record,
            openai_app_id=openai_app_id,
            trusted_key_id=trusted_key_id,
            trusted_secret=trusted_secret,
            deployment_sha256=deployment_sha256,
        )
        if blockers:
            raise ValueError(blockers[0])
        if publication["active_submission_sha256"] == target_submission_sha256:
            return
        if publication["active_submission_sha256"] != expected_active_submission_sha256:
            raise ValueError("directory publication pointer changed; refresh before retrying")
        _write_json_atomic(
            _directory_publication_path(root, channel),
            {
                "schema_version": 1,
                "channel": channel,
                "active_submission_sha256": target_submission_sha256,
            },
        )
