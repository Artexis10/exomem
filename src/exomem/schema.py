"""Parse Knowledge Base schema docs at startup; validate `add` calls against the spec.

The schema lives at `<vault>/Knowledge Base/_Schema/`. Two docs matter for the
MCP's scope:
- `references/frontmatter.md` — required source-page fields + source_type enum.
- `references/page-types.md` — source location + naming convention.

Both are markdown with embedded tables. Parsing is conservative: we extract the
narrow facts we need; if either doc changes shape and parsing fails, we raise
loudly at startup so exomem never silently drifts from the canonical schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname

SOURCE_TYPE_FIELD_PATTERN = re.compile(
    r"\|\s*`source_type`\s*\|\s*yes\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
# Matches each `<enum>` inside the source_type cell, e.g. "`article`, `session`, ...".
ENUM_TOKEN_PATTERN = re.compile(r"`([a-z]+)`")

# Required fields appear in the source frontmatter section as "| <field> | yes |".
REQUIRED_FIELD_ROW_PATTERN = re.compile(
    r"\|\s*`([a-z_]+)`\s*\|\s*yes\s*\|", re.IGNORECASE
)


@dataclass(frozen=True)
class SourceSchema:
    """The narrow slice of schema exomem's `add` tool needs to enforce."""

    source_types: tuple[str, ...]
    required_fields: tuple[str, ...]
    conditional_url_types: tuple[str, ...]
    location_pattern: str  # e.g. "Sources/<type>/"
    naming_pattern: str  # e.g. "YYYY-MM-DD-<slug>.md"


class SchemaParseError(RuntimeError):
    """Raised at startup when a schema doc can't be parsed.

    Carries the doc path and a hint about which section failed so you
    can diff against the canonical version.
    """


def load_source_schema(vault_path: Path) -> SourceSchema:
    """Parse the schema docs and return the source-page contract.

    Raises SchemaParseError if anything looks wrong.
    """
    schema_dir = vault_path / kb_dirname() / "_Schema" / "references"
    frontmatter_doc = schema_dir / "frontmatter.md"
    page_types_doc = schema_dir / "page-types.md"

    for doc in (frontmatter_doc, page_types_doc):
        if not doc.exists():
            raise SchemaParseError(f"Schema doc missing: {doc}")

    fm_text = frontmatter_doc.read_text(encoding="utf-8")
    pt_text = page_types_doc.read_text(encoding="utf-8")

    source_section = _slice_section(fm_text, "### source", next_heading_prefix="###")
    if not source_section:
        raise SchemaParseError(
            f"Couldn't find '### source' section in {frontmatter_doc}"
        )

    enum_match = SOURCE_TYPE_FIELD_PATTERN.search(source_section)
    if not enum_match:
        raise SchemaParseError(
            f"Couldn't extract source_type enum row from {frontmatter_doc}"
        )
    enum_values = tuple(ENUM_TOKEN_PATTERN.findall(enum_match.group(1)))
    if not enum_values:
        raise SchemaParseError(
            f"source_type enum row in {frontmatter_doc} had no `<enum>` tokens"
        )

    required = tuple(REQUIRED_FIELD_ROW_PATTERN.findall(source_section))
    if "source_type" not in required:
        raise SchemaParseError(
            f"source_type not marked required in {frontmatter_doc}"
        )

    page_section = _slice_section(pt_text, "## source", next_heading_prefix="##")
    if not page_section:
        raise SchemaParseError(
            f"Couldn't find '## source' section in {page_types_doc}"
        )

    location = _extract_field_line(page_section, "Location:")
    naming = _extract_field_line(page_section, "Naming:")
    if not location or not naming:
        raise SchemaParseError(
            f"Missing Location: or Naming: line in {page_types_doc} '## source' section"
        )

    # The frontmatter spec says url is "conditional" — required for some source_types.
    # The wording in the spec is "required for articles, videos, papers" (plural).
    # We map to singular enum keys.
    url_row_match = re.search(r"\|\s*`url`\s*\|\s*conditional\s*\|\s*(.+?)\s*\|", source_section, re.IGNORECASE)
    if url_row_match:
        url_note = url_row_match.group(1).lower()
        conditional = tuple(
            token
            for token in ("article", "video", "paper")
            if f"{token}s" in url_note or token in url_note
        )
    else:
        conditional = ()

    return SourceSchema(
        source_types=enum_values,
        required_fields=required,
        conditional_url_types=conditional,
        location_pattern=location,
        naming_pattern=naming,
    )


def _slice_section(text: str, heading: str, next_heading_prefix: str) -> str | None:
    """Return text from `heading` (inclusive) up to the next heading at the same level."""
    start = text.find(heading)
    if start == -1:
        return None
    rest = text[start + len(heading) :]
    # Find next heading at same level. Look for newline + prefix + space (not equal-level deeper).
    pattern = re.compile(rf"\n{re.escape(next_heading_prefix)} ", re.MULTILINE)
    match = pattern.search(rest)
    end = match.start() if match else len(rest)
    return heading + rest[:end]


def _extract_field_line(section: str, label: str) -> str | None:
    """Find `**Label:** value` or `Label: value` and return the value."""
    pattern = re.compile(rf"\*\*{re.escape(label)}\*\*\s*(.+)")
    match = pattern.search(section)
    if match:
        return match.group(1).strip()
    pattern = re.compile(rf"^{re.escape(label)}\s*(.+)$", re.MULTILINE)
    match = pattern.search(section)
    return match.group(1).strip() if match else None


@dataclass(frozen=True)
class ValidationError:
    code: str
    missing: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": list(self.missing), "reason": self.reason}


def validate_source(
    schema: SourceSchema,
    *,
    content: str,
    source_type: str,
    title: str,
    url: str | None,
) -> ValidationError | None:
    """Return a structured error if the proposed `add` call would violate schema."""
    missing: list[str] = []
    reasons: list[str] = []

    if not content or not content.strip():
        missing.append("content")
        reasons.append("content is empty")
    if not title or not title.strip():
        missing.append("title")
        reasons.append("title is empty")
    if source_type not in schema.source_types:
        return ValidationError(
            code="INVALID_SOURCE",
            missing=("source_type",),
            reason=(
                f"source_type {source_type!r} is not in the schema enum "
                f"{list(schema.source_types)}"
            ),
        )
    if source_type in schema.conditional_url_types and not url:
        missing.append("url")
        reasons.append(
            f"url is required for source_type={source_type} per frontmatter spec"
        )

    if missing:
        return ValidationError(
            code="INVALID_SOURCE",
            missing=tuple(missing),
            reason="; ".join(reasons),
        )
    return None
