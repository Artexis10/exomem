"""Parse Knowledge Base schema docs at startup; validate `add` calls against the spec.

The schema lives at `<vault>/Knowledge Base/_Schema/`. Two docs matter for the
MCP's scope:
- `references/frontmatter.md` — required source-page fields.
- `references/page-types.md` — source location + naming convention.

Both are markdown with embedded tables. Parsing is conservative: we extract the
narrow facts we need; if either doc changes shape and parsing fails, we raise
loudly at startup so exomem never silently drifts from the canonical schema.

What this module deliberately no longer owns is the **source-kind vocabulary**.
It used to scrape a closed enum out of one markdown table row, using a token
pattern that could not express a hyphen — so a multi-word kind such as
`research-report` was unrepresentable, and every clearly classifiable artifact
that lacked a listed label was forced into `other`. The vocabulary now lives in
`source_taxonomy`, where it is open, normalizable, and vault-extensible. This
module keeps the parts a markdown table can honestly define: which fields are
required, and where sources live.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Required fields appear in the source frontmatter section as "| <field> | yes |".
REQUIRED_FIELD_ROW_PATTERN = re.compile(
    r"\|\s*`([a-z_]+)`\s*\|\s*yes\s*\|", re.IGNORECASE
)


@dataclass(frozen=True)
class SourceSchema:
    """The narrow slice of schema exomem's `add` tool needs to enforce."""

    required_fields: tuple[str, ...]
    location_pattern: str  # e.g. "Sources/<kind>/[<domain>/]"
    naming_pattern: str  # e.g. "YYYY-MM-DD-<slug>.md"
    #: Retained for callers that report the shipped defaults. This is a
    #: non-exhaustive sample of the open vocabulary, never a whitelist —
    #: `validate_source` does not consult it.
    source_types: tuple[str, ...] = field(default_factory=tuple)


class SchemaParseError(RuntimeError):
    """Raised at startup when a schema doc can't be parsed.

    Carries the doc path and a hint about which section failed so you
    can diff against the canonical version.
    """


def load_source_schema(vault_path: Path) -> SourceSchema:
    """Parse the schema docs and return the source-page contract.

    Raises SchemaParseError if anything looks wrong.
    """
    from .vault import shipped_schema_root

    schema_dir = shipped_schema_root(vault_path) / "references"
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

    # URL conditionality used to be scraped from prose and hard-coded to three
    # tokens, which reserved the property to three built-ins. It is now a
    # per-kind `requires_url` flag in the source-taxonomy registry, so a
    # user-defined kind can declare it too.
    return SourceSchema(
        required_fields=required,
        location_pattern=location,
        naming_pattern=naming,
        source_types=_default_kind_sample(),
    )


def _default_kind_sample() -> tuple[str, ...]:
    """The shipped source kinds, as a non-exhaustive sample for reporting."""
    from .source_taxonomy import builtin_kinds

    return tuple(sorted(builtin_kinds()))


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
    requires_url: bool = False,
) -> ValidationError | None:
    """Return a structured error if the proposed `add` call would violate schema.

    `source_type` is expected to be an already-resolved canonical key. This
    function deliberately does **not** check it against a permitted set: the
    vocabulary is open, and safety is established by `source_taxonomy.normalize`
    before the value arrives here. `requires_url` is the resolved kind's own
    declared requirement rather than a property of three hard-coded labels.
    """
    missing: list[str] = []
    reasons: list[str] = []

    if not content or not content.strip():
        missing.append("content")
        reasons.append("content is empty")
    if not title or not title.strip():
        missing.append("title")
        reasons.append("title is empty")
    if requires_url and not url:
        missing.append("url")
        reasons.append(f"url is required for source_type={source_type}")

    if missing:
        return ValidationError(
            code="INVALID_SOURCE",
            missing=tuple(missing),
            reason="; ".join(reasons),
        )
    return None
