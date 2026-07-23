"""Fail-closed privacy validation for public repository and build artifacts.

The privacy boundary is provenance based, not corpus based: public builders may
read only declared repository inputs, every encountered format must have a text
scanner or an explicit binary provenance rule, and diagnostics never echo the
matched source.  No live vault or maintainer-specific token list is consulted.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import lzma
import re
import stat
import struct
import subprocess
import tarfile
import zipfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class PublicArtifactClass:
    """One explicit input or generated-output class in the public inventory."""

    name: str
    phase: str
    roots: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


PUBLIC_ARTIFACT_INVENTORY: tuple[PublicArtifactClass, ...] = (
    PublicArtifactClass("package-source", "input", roots=("src",)),
    PublicArtifactClass(
        "plugin-marketplace",
        "input",
        roots=("plugins", ".claude-plugin"),
    ),
    PublicArtifactClass(
        "documentation",
        "input",
        roots=("docs",),
        files=("README.md", "LICENSE"),
    ),
    PublicArtifactClass("openspec", "input", roots=("openspec",)),
    PublicArtifactClass(
        "tests-fixtures-examples",
        "input",
        roots=("tests", "examples"),
    ),
    PublicArtifactClass("example-scripts", "input", roots=("scripts",)),
    PublicArtifactClass(
        "generated-schemas-docs",
        "input",
        files=(
            "docs/capabilities.md",
            "src/exomem/tool_surface_contract.json",
            "tests/fixtures/mcp_tool_schemas.json",
        ),
    ),
    PublicArtifactClass(
        "build-metadata",
        "input",
        roots=(".github",),
        files=("pyproject.toml", "uv.lock", ".gitignore", "Dockerfile"),
    ),
    PublicArtifactClass(
        "root-docs-config-release",
        "input",
        files=(
            ".dockerignore",
            ".env.example",
            ".gitattributes",
            ".release-please-manifest.json",
            "AGENTS.md",
            "CHANGELOG.md",
            "CLAUDE.md",
            "CONTRIBUTING.md",
            "QUICKSTART.md",
            "compose.cuda.yaml",
            "compose.ml.yaml",
            "compose.yaml",
            "env.example",
            "release-please-config.json",
        ),
    ),
    PublicArtifactClass("agent-config", "input", roots=(".claude", ".codex")),
    PublicArtifactClass(
        "deployment-examples",
        "input",
        roots=("deploy", "infra"),
    ),
    PublicArtifactClass("sidecars", "input", roots=("sidecar",)),
    PublicArtifactClass("wheel-sdist", "output"),
    PublicArtifactClass("filesystem-installs", "output"),
    PublicArtifactClass("skill-plugin-archives", "output"),
)


@dataclass(frozen=True)
class BinaryProvenance:
    """Explicit handling for a binary path or archive-member glob."""

    pattern: str
    rationale: str

    def __post_init__(self) -> None:
        if not self.pattern.strip() or not self.rationale.strip():
            raise ValueError("binary provenance requires a path pattern and rationale")


@dataclass(frozen=True, order=True)
class PrivacyFinding:
    """Redacted finding: intentionally only rule, logical file, and line."""

    rule: str
    file: str
    line: int

    def as_dict(self) -> dict[str, str | int]:
        return {"rule": self.rule, "file": self.file, "line": self.line}

    def __str__(self) -> str:
        return f"{self.rule}: {self.file}:{self.line}"


@dataclass(frozen=True)
class PrivacyScanReport:
    scanned_files: int
    scanned_text_files: int
    findings: tuple[PrivacyFinding, ...]


class PublicArtifactPrivacyError(ValueError):
    """Raised when a public artifact violates the fail-closed privacy gate."""


_TEXT_SUFFIXES = frozenset(
    {
        ".b64",
        ".bat",
        ".cfg",
        ".cjs",
        ".cmd",
        ".conf",
        ".css",
        ".csv",
        ".dockerignore",
        ".env",
        ".example",
        ".gitattributes",
        ".gitignore",
        ".gitkeep",
        ".hcl",
        ".html",
        ".htm",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".j2",
        ".lock",
        ".md",
        ".mjs",
        ".mako",
        ".plist",
        ".ps1",
        ".py",
        ".pyi",
        ".rst",
        ".rego",
        ".service",
        ".sh",
        ".svg",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".tf",
        ".tpl",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_BASENAMES = frozenset(
    {
        ".dockerignore",
        ".env.example",
        ".gitattributes",
        ".gitignore",
        ".gitkeep",
        "Dockerfile",
        "env.example",
        "LICENSE",
        "METADATA",
        "PKG-INFO",
        "Procfile",
        "RECORD",
        "WHEEL",
    }
)
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".whl", ".zip", ".skill")
_ARCHIVE_ERRORS = (
    EOFError,
    OSError,
    RuntimeError,
    ValueError,
    lzma.LZMAError,
    struct.error,
    tarfile.TarError,
    zipfile.BadZipFile,
    zlib.error,
)
_PRIVATE_OUTPUT_MARKER = ".exomem-private-output.json"
# Binary handling is intentionally format-scoped and documented.  Adding another
# public binary format requires adding an equally explicit provenance declaration.
DEFAULT_BINARY_PROVENANCE: tuple[BinaryProvenance, ...] = (
    BinaryProvenance("*.ico", "repository-authored Exomem application icon"),
)

_CONTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "absolute_local_path",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_.-])[A-Z]:[\\/]+"
            r"(?!"
            r"(?:<|\{\{|%|\$)"
            r"|(?:path|fake|example)(?:[\\/]|$)"
            r"|Users[\\/]+(?:<|\{\{|(?:x|you|user|example)(?:[\\/]|$))"
            r")"
            r"(?=[^\\/\s:<>\"|?*])"
        ),
    ),
    (
        "absolute_local_path",
        re.compile(
            r"(?i)(?<!\\)\\\\"
            r"(?!<|\{\{|(?:example|server|host)(?:[\\/]|$))"
            r"[A-Z0-9][A-Z0-9._-]*[\\/]+"
            r"(?!<|\{\{)"
            r"(?=[^\\/\r\n:*?\"<>|]*[^\s\\/\r\n:*?\"<>|](?:[\\/]|$))"
            r"[^\\/\r\n:*?\"<>|]+(?:[\\/]|$)"
        ),
    ),
    (
        "absolute_local_path",
        re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/(?!<|\{\{)[^/\s<]+/"),
    ),
    ("absolute_local_path", re.compile(r"(?<![A-Za-z0-9_.-])/mnt/[a-z]/")),
)


def _display_path(path: Path, base: Path | None = None) -> str:
    if base is not None:
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def _kind(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(_ARCHIVE_SUFFIXES):
        return "archive"
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.name in _TEXT_BASENAMES or pure.suffix.lower() in _TEXT_SUFFIXES:
        return "text"
    return "unknown"


def _has_binary_provenance(
    logical_name: str,
    policies: tuple[BinaryProvenance, ...],
) -> bool:
    normalized = logical_name.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, policy.pattern) for policy in policies)


def _scan_text(text: str, logical_name: str) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in _CONTENT_RULES:
            if pattern.search(line):
                findings.append(PrivacyFinding(rule, logical_name, line_number))
    return findings


def _unsafe_member_path(member_name: str) -> bool:
    normalized = member_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return (
        normalized.startswith("/")
        or re.match(r"(?i)^[A-Z]:/", normalized) is not None
        or ".." in pure.parts
    )


def _member_logical_name(archive_name: str, member_name: str) -> str:
    digest = hashlib.sha256(
        member_name.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    return f"{archive_name}!member-{digest}"


def _decode_and_scan(data: bytes, logical_name: str) -> tuple[list[PrivacyFinding], int]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [PrivacyFinding("invalid_utf8", logical_name, 0)], 0
    return _scan_text(text, logical_name), 1


def _scan_member_bytes(
    data: bytes,
    logical_name: str,
    member_name: str,
    policies: tuple[BinaryProvenance, ...],
    *,
    depth: int,
) -> tuple[list[PrivacyFinding], int]:
    marker_finding = (
        [PrivacyFinding("personalized_artifact_in_public_build", logical_name, 0)]
        if PurePosixPath(member_name.replace("\\", "/")).name == _PRIVATE_OUTPUT_MARKER
        else []
    )
    kind = _kind(member_name)
    if kind == "text":
        findings, count = _decode_and_scan(data, logical_name)
        return marker_finding + findings, count
    if kind == "archive":
        if depth >= 3:
            return marker_finding + [
                PrivacyFinding("archive_nesting_limit", logical_name, 0)
            ], 0
        findings, count = _scan_archive_stream(
            data, logical_name, member_name, policies, depth=depth + 1
        )
        return marker_finding + findings, count
    if _has_binary_provenance(logical_name, policies) or _has_binary_provenance(
        member_name, policies
    ):
        return marker_finding, 0
    return marker_finding + [PrivacyFinding("format_provenance_missing", logical_name, 0)], 0


def _scan_zip_stream(
    source: Path | io.BytesIO,
    logical_name: str,
    policies: tuple[BinaryProvenance, ...],
    *,
    depth: int,
) -> tuple[list[PrivacyFinding], int]:
    findings: list[PrivacyFinding] = []
    text_files = 0
    try:
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                member_logical = _member_logical_name(logical_name, member.filename)
                findings.extend(_scan_text(member.filename, member_logical))
                if _unsafe_member_path(member.filename):
                    findings.append(PrivacyFinding("archive_member_path", member_logical, 0))
                is_directory = member.is_dir()
                if member.create_system == 3:
                    member_type = stat.S_IFMT(member.external_attr >> 16)
                    expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
                    if member_type not in (0, expected_type):
                        findings.append(
                            PrivacyFinding("archive_member_type", member_logical, 0)
                        )
                        continue
                if is_directory:
                    continue
                try:
                    data = archive.read(member)
                # Decompressors expose several backend-specific exception types.
                # Collapse all member-read failures to one redacted finding so
                # an exception message can never reveal the member name.
                except _ARCHIVE_ERRORS:
                    findings.append(PrivacyFinding("invalid_archive", logical_name, 0))
                    break
                member_findings, count = _scan_member_bytes(
                    data,
                    member_logical,
                    member.filename,
                    policies,
                    depth=depth,
                )
                findings.extend(member_findings)
                text_files += count
    except _ARCHIVE_ERRORS:
        findings.append(PrivacyFinding("invalid_archive", logical_name, 0))
    return findings, text_files


def _scan_tar_stream(
    source: Path | io.BytesIO,
    logical_name: str,
    policies: tuple[BinaryProvenance, ...],
    *,
    depth: int,
) -> tuple[list[PrivacyFinding], int]:
    findings: list[PrivacyFinding] = []
    text_files = 0
    try:
        kwargs = {"mode": "r:*"}
        if isinstance(source, Path):
            archive_context = tarfile.open(source, **kwargs)
        else:
            archive_context = tarfile.open(fileobj=source, **kwargs)
        with archive_context as archive:
            for member in archive.getmembers():
                member_logical = _member_logical_name(logical_name, member.name)
                findings.extend(_scan_text(member.name, member_logical))
                if _unsafe_member_path(member.name):
                    findings.append(PrivacyFinding("archive_member_path", member_logical, 0))
                if member.isdir():
                    continue
                if not member.isfile():
                    findings.append(PrivacyFinding("archive_member_type", member_logical, 0))
                    continue
                try:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        findings.append(
                            PrivacyFinding("archive_member_unreadable", member_logical, 0)
                        )
                        continue
                    data = extracted.read()
                except _ARCHIVE_ERRORS:
                    findings.append(PrivacyFinding("invalid_archive", logical_name, 0))
                    break
                member_findings, count = _scan_member_bytes(
                    data,
                    member_logical,
                    member.name,
                    policies,
                    depth=depth,
                )
                findings.extend(member_findings)
                text_files += count
    except _ARCHIVE_ERRORS:
        findings.append(PrivacyFinding("invalid_archive", logical_name, 0))
    return findings, text_files


def _scan_archive_stream(
    data: bytes,
    logical_name: str,
    member_name: str,
    policies: tuple[BinaryProvenance, ...],
    *,
    depth: int,
) -> tuple[list[PrivacyFinding], int]:
    stream = io.BytesIO(data)
    lowered = member_name.lower()
    if lowered.endswith((".tar.gz", ".tgz")):
        return _scan_tar_stream(stream, logical_name, policies, depth=depth)
    return _scan_zip_stream(stream, logical_name, policies, depth=depth)


def _scan_artifact_with_count(
    path: Path,
    *,
    label: str,
    policies: tuple[BinaryProvenance, ...],
) -> tuple[list[PrivacyFinding], int]:
    try:
        entry_mode = path.lstat().st_mode
    except OSError:
        return [PrivacyFinding("artifact_unreadable", label, 0)], 0
    if not stat.S_ISREG(entry_mode):
        rule = (
            "external_input_provenance"
            if stat.S_ISLNK(entry_mode)
            else "filesystem_entry_type"
        )
        return [PrivacyFinding(rule, label, 0)], 0
    if path.name == _PRIVATE_OUTPUT_MARKER:
        return [PrivacyFinding("personalized_artifact_in_public_build", label, 0)], 0
    kind = _kind(path.name)
    if kind == "archive":
        if path.name.lower().endswith((".tar.gz", ".tgz")):
            return _scan_tar_stream(path, label, policies, depth=0)
        return _scan_zip_stream(path, label, policies, depth=0)
    if kind == "text":
        try:
            return _decode_and_scan(path.read_bytes(), label)
        except OSError:
            return [PrivacyFinding("artifact_unreadable", label, 0)], 0
    if _has_binary_provenance(label, policies):
        return [], 0
    return [PrivacyFinding("format_provenance_missing", label, 0)], 0


def scan_artifact(
    path: Path,
    *,
    label: str | None = None,
    binary_provenance: Iterable[BinaryProvenance] = DEFAULT_BINARY_PROVENANCE,
) -> tuple[PrivacyFinding, ...]:
    """Scan one file or archive and return redacted deterministic findings."""

    source = Path(path)
    policies = tuple(binary_provenance)
    findings, _ = _scan_artifact_with_count(
        source,
        label=label or source.name,
        policies=policies,
    )
    return tuple(sorted(set(findings)))


def _repository_files(repo_root: Path) -> list[Path]:
    # Public builds consume tracked files plus non-ignored additions.  Using the
    # same boundary here is critical: ignored maintainer-only files may contain a
    # private overlay or local leakguard corpus and must not be read by the public
    # privacy validator itself.
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise PublicArtifactPrivacyError(
            "public repository inventory requires an accessible git checkout"
        ) from error
    if completed.returncode != 0:
        raise PublicArtifactPrivacyError(
            "public repository inventory could not enumerate distributable inputs"
        )
    try:
        relative_paths = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        raise PublicArtifactPrivacyError(
            "public repository inventory contains a non-UTF-8 path"
        ) from error
    # Git's two populations have the exact boundary we need: --cached is every
    # tracked public input regardless of an ignore-shaped path, while --others
    # with --exclude-standard omits only genuinely ignored untracked scratch.
    return sorted(repo_root / relative for relative in relative_paths if relative)


def repository_input_paths(repo_root: Path) -> tuple[str, ...]:
    """Return every tracked or non-ignored public repository input."""

    root = Path(repo_root).resolve()
    return tuple(path.relative_to(root).as_posix() for path in _repository_files(root))


def scan_repository_inputs(repo_root: Path) -> PrivacyScanReport:
    """Scan all declared distributable inputs without consulting a vault or env."""

    root = Path(repo_root).resolve()
    findings: list[PrivacyFinding] = []
    scanned_text_files = 0
    files = _repository_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            # The repository intentionally publishes AGENTS.md as an in-tree
            # alias. Validate only its link metadata and scan its regular target
            # independently; never follow arbitrary aliases during inventory.
            try:
                link_target = path.readlink().as_posix()
            except OSError:
                link_target = ""
            if relative != "AGENTS.md" or link_target != "CLAUDE.md":
                findings.append(
                    PrivacyFinding("external_input_provenance", relative, 0)
                )
            continue
        path_findings, text_count = _scan_artifact_with_count(
            path,
            label=relative,
            policies=DEFAULT_BINARY_PROVENANCE,
        )
        findings.extend(path_findings)
        scanned_text_files += text_count
    return PrivacyScanReport(
        scanned_files=len(files),
        scanned_text_files=scanned_text_files,
        findings=tuple(sorted(set(findings))),
    )


def assert_public_artifacts_clean(
    paths: Iterable[Path],
    *,
    labels: Mapping[Path, str] | None = None,
    binary_provenance: Iterable[BinaryProvenance] = DEFAULT_BINARY_PROVENANCE,
) -> None:
    """Raise a redacted error when any public generated artifact is unsafe."""

    label_map = labels or {}
    findings: list[PrivacyFinding] = []
    for raw_path in paths:
        path = Path(raw_path)
        findings.extend(
            scan_artifact(
                path,
                label=label_map.get(raw_path, label_map.get(path, path.name)),
                binary_provenance=binary_provenance,
            )
        )
    unique = tuple(sorted(set(findings)))
    if unique:
        diagnostics = "\n".join(str(finding) for finding in unique)
        raise PublicArtifactPrivacyError(
            f"public artifact privacy validation failed ({len(unique)} findings):\n"
            f"{diagnostics}"
        )
