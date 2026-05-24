"""Vault path resolution + safe-write helpers used by the add tool.

Also hosts the Tier 2 shared helpers — curated/append-only tree guards,
generic path resolution, frontmatter parse/serialize, inbound-wikilink
scan — used by the filesystem-parity operations (create_file,
list_directory, etc.).
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify as _slugify


DESKTOP_VAULT = Path(r"D:\Archive\Personal Archive\50 Notes\Obsidian")
LAPTOP_VAULT = Path(r"C:\Users\win-laptop\Documents\Obsidian")

SLUG_MAX_LENGTH = 100

# Curated trees are desk-managed; Tier 2 writes refuse by default and
# require explicit `allow_curated=true` to land. Reads are unrestricted
# (consistent with the existing `get` tool).
CURATED_TREES: tuple[str, ...] = (
    "Cognitive Core",
    "Domains",
    "Prompt Bank",
    "Products",
    "Personal Context",
    # Hugo's vault uses "Personal Context (Evolving)" — match both forms.
    "Personal Context (Evolving)",
    "Systems Thinking",
)

# Append-only trees inside the KB. Tier 2 ops refuse writes here regardless
# of any override — use `add` (for Sources) or `preserve` (for Evidence).
APPEND_ONLY_KB_SUBPATHS: tuple[str, ...] = (
    "Sources",
    "Evidence",
)

# When scanning the full vault for inbound wikilinks, skip these.
VAULT_SCAN_SKIP_DIRS = frozenset({
    ".obsidian", ".git", ".trash", "_attachments", "_archive",
})

# `[[Target]]` or `[[Target|Alias]]`.
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")
_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)


def resolve_vault(env_var: str = "KB_MCP_VAULT_PATH") -> Path:
    """Return the Obsidian vault root that contains Knowledge Base/.

    Priority: env override → desktop → laptop. Raises if none resolve.
    """
    override = os.environ.get(env_var)
    if override:
        path = Path(override)
        if not _is_vault(path):
            raise RuntimeError(
                f"{env_var}={override!r} does not look like a vault "
                f"(no Knowledge Base/_Schema/SKILL.md found)"
            )
        return path
    for candidate in (DESKTOP_VAULT, LAPTOP_VAULT):
        if _is_vault(candidate):
            return candidate
    raise RuntimeError(
        "Knowledge Base vault not found at the desktop or laptop path. "
        f"Set {env_var} to override."
    )


def _is_vault(path: Path) -> bool:
    return (path / "Knowledge Base" / "_Schema" / "SKILL.md").exists()


def kb_root(vault: Path) -> Path:
    return vault / "Knowledge Base"


def slugify_title(title: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Lowercase, dash-separated, alphanumeric-only, length-capped."""
    slug = _slugify(title, max_length=max_length, word_boundary=True, lowercase=True)
    return slug or "untitled"


def slugify_with_truncation_check(
    title: str, max_length: int = SLUG_MAX_LENGTH
) -> tuple[str, str | None]:
    """Return (slug, warning). `warning` is non-None if the slug was truncated.

    The warning names both the truncated and full slug so the caller can
    decide whether to abort, shorten the title, or accept.
    """
    slug = slugify_title(title, max_length=max_length)
    full = _slugify(title, max_length=0, word_boundary=True, lowercase=True) or "untitled"
    if slug != full:
        return slug, (
            f"slug truncated to {slug!r} (full would have been {full!r}); "
            f"shorten the title if the truncation drops meaning"
        )
    return slug, None


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    """Return a path that doesn't exist yet, appending -2, -3, ... on collision."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@dataclass
class PlannedWrite:
    """One target file in a batch write: destination path + final content."""

    path: Path
    content: str


def batch_atomic_write(writes: Iterable[PlannedWrite]) -> list[Path]:
    """Stage each write as a sibling .tmp file, then os.replace() them into place.

    On any exception during staging, no replacements happen — temps are cleaned.
    Once replacement starts, files are flipped one at a time. A mid-flip failure
    leaves a partially-updated tree: already-replaced files stand, remaining
    temps are cleaned, the exception re-raises so the caller can warn.
    """
    writes = list(writes)
    staged: list[tuple[Path, Path]] = []  # (final, tmp)
    try:
        for w in writes:
            w.path.parent.mkdir(parents=True, exist_ok=True)
            # NamedTemporaryFile would need delete=False + cross-platform care;
            # explicit tmp sibling is simpler and survives os.replace.
            fd, tmp_str = tempfile.mkstemp(
                prefix=f".{w.path.name}.", suffix=".tmp", dir=str(w.path.parent)
            )
            os.close(fd)
            tmp = Path(tmp_str)
            tmp.write_text(w.content, encoding="utf-8", newline="\n")
            staged.append((w.path, tmp))
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise

    replaced: list[Path] = []
    try:
        for final, tmp in staged:
            os.replace(tmp, final)
            replaced.append(final)
    except Exception:
        # Replace failed mid-batch. Clean up remaining temps; replaced files stay.
        replaced_paths = {s[0] for s, _ in zip(staged, staged) if s[0] in replaced}
        for final, tmp in staged:
            if final not in replaced_paths and tmp.exists():
                tmp.unlink(missing_ok=True)
        raise
    return replaced


@contextmanager
def chdir(path: Path):
    """Temporary cwd switch — used in tests."""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(prev)


# ---------------- Tier 2 shared helpers ----------------


class VaultPathError(Exception):
    """Raised when a path can't be resolved under the vault root."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


def resolve_under_vault(
    vault_root: Path,
    path: str,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> tuple[Path, str]:
    """Resolve a vault-relative path; guard against escape; normalize.

    Returns `(absolute_path, vault_relative_posix)`. The relative form is
    always forward-slashed, never starts with `/`. The leading
    `Knowledge Base/` is preserved as-is (we don't auto-strip it like
    `get_page` does — Tier 2 ops take explicit paths).

    Raises VaultPathError with code in {INVALID_PATH, NOT_FOUND,
    NOT_A_FILE, NOT_A_DIR}.
    """
    if path is None:
        raise VaultPathError(code="INVALID_PATH", reason="path is required")
    raw = str(path).strip()
    if not raw:
        raise VaultPathError(code="INVALID_PATH", reason="path is empty")

    rel = raw.replace("\\", "/").lstrip("/")
    # Reject absolute paths (drive letters or leading drive)
    if re.match(r"^[a-zA-Z]:", rel):
        raise VaultPathError(
            code="INVALID_PATH",
            reason=f"absolute paths are not allowed: {raw!r}",
        )

    candidate = vault_root / rel
    try:
        resolved = candidate.resolve()
        vault_resolved = vault_root.resolve()
        resolved.relative_to(vault_resolved)
    except (ValueError, OSError) as e:
        raise VaultPathError(
            code="INVALID_PATH",
            reason=f"path escapes vault root: {raw!r} ({e})",
        ) from None

    if must_exist and not candidate.exists():
        raise VaultPathError(
            code="NOT_FOUND",
            reason=f"path does not exist: {rel}",
        )
    if must_be_file and candidate.exists() and not candidate.is_file():
        raise VaultPathError(
            code="NOT_A_FILE",
            reason=f"path is not a regular file: {rel}",
        )
    if must_be_dir and candidate.exists() and not candidate.is_dir():
        raise VaultPathError(
            code="NOT_A_DIR",
            reason=f"path is not a directory: {rel}",
        )

    # Normalize the *returned* rel-form. resolved.relative_to(...) lowercases
    # the drive on Windows; use the literal candidate-form for stability.
    return candidate, rel


def in_curated_tree(rel_path: str) -> str | None:
    """Return the curated-tree name if `rel_path` is inside one, else None.

    `rel_path` is vault-relative POSIX form (e.g. "Cognitive Core/foo.md").
    """
    head = rel_path.split("/", 1)[0]
    if head in CURATED_TREES:
        return head
    return None


def in_append_only_tree(rel_path: str) -> str | None:
    """Return the subpath name ("Sources" or "Evidence") if matched.

    Matches both `Knowledge Base/Sources/...` and bare `Sources/...` —
    callers may pass either form.
    """
    parts = rel_path.split("/")
    if not parts:
        return None
    if parts[0] == "Knowledge Base" and len(parts) > 1:
        head = parts[1]
    else:
        head = parts[0]
    if head in APPEND_ONLY_KB_SUBPATHS:
        return head
    return None


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """Split a markdown file into (frontmatter_dict, body, frontmatter_text).

    Returns ({}, text, None) when no frontmatter block is present.
    `body` has no leading newline (mirrors find._parse_page).
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text, None
    fm_text = m.group(1)
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, body, fm_text


def serialize_frontmatter(fm: dict[str, Any]) -> str:
    """YAML-serialize a frontmatter dict into the inner block (no `---` fences).

    Uses block-flow style consistent with the rest of the codebase: scalars
    are inline, lists are inline `[a, b, c]` for short lists.
    """
    if not fm:
        return ""
    lines: list[str] = []
    for key, value in fm.items():
        lines.append(_format_yaml_line(key, value))
    return "\n".join(lines)


def _format_yaml_line(key: str, value: Any) -> str:
    """Format a single `key: value` line matching add/note/link style."""
    if value is None:
        return f"{key}:"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        # Inline form for short string lists; matches add.py's tags rendering.
        items = ", ".join(_yaml_scalar(v) for v in value)
        return f"{key}: [{items}]"
    if isinstance(value, dict):
        # Fall back to PyYAML block-style for nested dicts.
        block = yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False)
        return block.rstrip("\n")
    return f"{key}: {_yaml_scalar(value)}"


def _yaml_scalar(value: Any) -> str:
    """Render a scalar, quoting if it contains YAML-special chars."""
    s = str(value)
    needs_quote = any(c in s for c in [":", "#", "[", "]", "{", "}", ","]) or s.strip() != s
    if needs_quote:
        return yaml.safe_dump(s, default_flow_style=True).strip().rstrip("\n...").strip()
    return s


def walk_vault_md(vault_root: Path):
    """Yield every .md path under vault_root, skipping config/cruft dirs.

    Walks the FULL vault, not just Knowledge Base/. Used by Tier 2 inbound-
    wikilink scans and move/delete safety checks.
    """
    def walk(d: Path):
        try:
            children = list(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name in VAULT_SCAN_SKIP_DIRS:
                    continue
                yield from walk(child)
            elif child.is_file() and child.suffix.lower() == ".md":
                yield child
    yield from walk(vault_root)


@dataclass
class InboundLink:
    path: str          # vault-relative POSIX of the file containing the link
    line_number: int   # 1-based
    context: str       # the line text (trimmed)
    raw_target: str    # the exact text inside [[...]]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "context": self.context,
            "raw_target": self.raw_target,
        }


def find_inbound_wikilinks(
    vault_root: Path, target_rel_path: str
) -> list[InboundLink]:
    """Return every wikilink in the vault that resolves to `target_rel_path`.

    `target_rel_path` is vault-relative POSIX, with or without `.md`. Matches
    three forms:
    - full path with leading `Knowledge Base/`: `[[Knowledge Base/Notes/Insights/foo]]`
    - KB-stripped path: `[[Notes/Insights/foo]]`
    - bare basename (only if unambiguous in the vault): `[[foo]]`

    The bare-basename match only fires if the target's basename is unique
    across the vault — otherwise an inbound `[[foo]]` could mean any
    same-named file, so we don't claim it.
    """
    target = target_rel_path.replace("\\", "/").removesuffix(".md")
    target_full = target if target.startswith("Knowledge Base/") else "Knowledge Base/" + target
    target_stripped = target_full.removeprefix("Knowledge Base/")
    target_basename = target.rsplit("/", 1)[-1]

    # Check basename uniqueness across the vault.
    basename_count = 0
    for md in walk_vault_md(vault_root):
        if md.stem == target_basename:
            basename_count += 1
            if basename_count > 1:
                break
    basename_unique = basename_count == 1

    matches: list[InboundLink] = []
    for md in walk_vault_md(vault_root):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Skip the target file itself (self-references aren't inbound).
        try:
            md_rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        if md_rel.removesuffix(".md") in (target_full, target_stripped):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _WIKILINK_PATTERN.finditer(line):
                raw = m.group(1).strip()
                normalized = raw.removesuffix(".md")
                if normalized == target_full or normalized == target_stripped:
                    matches.append(InboundLink(
                        path=md_rel,
                        line_number=lineno,
                        context=line.strip()[:240],
                        raw_target=raw,
                    ))
                elif basename_unique and "/" not in normalized and normalized == target_basename:
                    matches.append(InboundLink(
                        path=md_rel,
                        line_number=lineno,
                        context=line.strip()[:240],
                        raw_target=raw,
                    ))
    return matches


def prepend_log_entry(
    log_text: str,
    *,
    date_iso: str,
    op: str,
    rel_path_no_ext: str,
    body: str,
) -> str:
    """Insert a `## [date] <op> | <rel>` block after the log's `---` separator.

    `rel_path_no_ext` is vault-relative POSIX without `.md`. The leading
    `Knowledge Base/` is stripped from the title for compactness (matches
    the existing add/edit/preserve log style); paths outside KB keep the
    full vault-relative form so curated-tree writes stay traceable.
    """
    title = rel_path_no_ext
    if title.startswith("Knowledge Base/"):
        title = title[len("Knowledge Base/"):]
    new_entry = f"## [{date_iso}] {op} | {title}\n\n{body}\n"
    # Reuse the same separator the indexes module emits.
    separator = "\n---\n"
    sep_idx = log_text.find(separator)
    if sep_idx == -1:
        return log_text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(separator)
    return log_text[:insertion_point] + "\n" + new_entry + "\n" + log_text[insertion_point:]


def write_log_entry(
    vault_root: Path,
    *,
    date_iso: str,
    op: str,
    rel_path_no_ext: str,
    body: str,
) -> str | None:
    """Read, update, and write log.md in one go. Returns warning if missing.

    Returns None on success; a warning string if log.md was missing (so the
    op can include it in its warnings list). Atomic via `replace`.
    """
    log_file = kb_root(vault_root) / "log.md"
    if not log_file.exists():
        return "Knowledge Base/log.md missing; skipped log entry"
    text = log_file.read_text(encoding="utf-8")
    new_text = prepend_log_entry(
        text,
        date_iso=date_iso,
        op=op,
        rel_path_no_ext=rel_path_no_ext,
        body=body,
    )
    batch_atomic_write([PlannedWrite(path=log_file, content=new_text)])
    return None
