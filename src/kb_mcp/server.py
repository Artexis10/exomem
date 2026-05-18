"""FastMCP server construction: tool registration + bearer-token auth + transports.

Tools:
- `find` — read-only search across Knowledge Base/
- `add`  — capture a `source` page with full SKILL.md rule-7 writes

Auth:
- `StaticTokenVerifier` requires `Authorization: Bearer <token>` on every request.
- Token comes from the `KB_MCP_BEARER_TOKEN` env var (in `.env` or set by the
  service wrapper). If unset, the server refuses to start on any non-stdio
  transport — a public endpoint without auth is a footgun.

Transports:
- stdio (local; no auth needed)
- streamable-http aka http (public; bearer required)
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import mcp.types
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

from . import add as add_module
from . import audit as audit_module
from . import find as find_module
from . import get_page as get_page_module
from . import link as link_module
from . import note as note_module
from . import preserve as preserve_module
from . import replace as replace_module
from . import schema
from .vault import resolve_vault


log = logging.getLogger(__name__)


def _server_icons() -> list[mcp.types.Icon]:
    """Load icon.svg from the package dir and return an mcp.types.Icon list.

    Embeds as a base64 data URI so the server has no external fetch
    dependency; claude.ai's connector renders it directly from the
    initialize response.
    """
    icon_path = Path(__file__).parent / "icon.svg"
    if not icon_path.exists():
        return []
    svg_bytes = icon_path.read_bytes()
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    return [mcp.types.Icon(
        src=f"data:image/svg+xml;base64,{b64}",
        mimeType="image/svg+xml",
        sizes=["any"],
    )]


class SingleUserGitHubVerifier(GitHubTokenVerifier):
    """Reject any GitHub token whose login isn't the allowed user."""

    def __init__(self, *, allowed_login: str, **kwargs):
        super().__init__(**kwargs)
        self._allowed_login = allowed_login.lower()

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        login = (access.claims.get("login") or "").lower()
        if login != self._allowed_login:
            log.warning("rejecting token for github login=%r", login)
            return None
        return access


def build_server(*, require_auth: bool) -> FastMCP:
    """Construct and return the FastMCP app, ready to .run().

    `require_auth` controls whether the GitHub OAuth flow is wired in. Always
    True for HTTP transports; False for stdio.
    """
    load_dotenv(override=True)

    vault_root = resolve_vault()
    source_schema = schema.load_source_schema(vault_root)
    log.info(
        "vault=%s source_types=%s", vault_root, source_schema.source_types
    )

    auth = None
    if require_auth:
        base_url = os.environ.get("KB_MCP_BASE_URL", "").strip().rstrip("/")
        gh_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
        gh_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
        gh_username = os.environ.get("KB_MCP_GITHUB_USERNAME", "").strip()
        missing = [
            k for k, v in {
                "KB_MCP_BASE_URL": base_url,
                "GITHUB_CLIENT_ID": gh_id,
                "GITHUB_CLIENT_SECRET": gh_secret,
                "KB_MCP_GITHUB_USERNAME": gh_username,
            }.items() if not v
        ]
        if missing:
            raise RuntimeError(
                f"Missing required env vars for GitHub OAuth: {', '.join(missing)}. "
                "See README.md § Install for setup steps."
            )
        auth = OAuthProxy(
            upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
            upstream_token_endpoint="https://github.com/login/oauth/access_token",
            upstream_client_id=gh_id,
            upstream_client_secret=gh_secret,
            token_verifier=SingleUserGitHubVerifier(allowed_login=gh_username),
            base_url=base_url,
        )

    mcp = FastMCP("kb-mcp", auth=auth, icons=_server_icons())

    @mcp.tool
    def find(
        query: str = "",
        types: list[str] | None = None,
        projects: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 15,
    ) -> list[dict]:
        """Search the Knowledge Base. Filters are AND'd; tag/project lists are OR'd within.

        Args:
            query: Case-insensitive substring matched against title + body. Empty string returns most-recent filtered hits.
            types: Filter to these page types (source, research-note, insight, failure, pattern, experiment, production-log, entity).
            projects: Filter to pages whose `project` or `projects:` includes any of these keys.
            tags: Filter to pages whose `tags:` includes any of these (case-insensitive).
            limit: Max hits to return. Default 15, hard cap 100.

        Returns:
            List of {path, type, scope, title, updated, excerpt}.
        """
        hits = find_module.find(
            vault_root,
            query=query,
            types=types,
            projects=projects,
            tags=tags,
            limit=limit,
        )
        return [h.as_dict() for h in hits]

    @mcp.tool
    def add(
        content: str,
        source_type: str,
        title: str,
        url: str | None = None,
        tags: list[str] | None = None,
        why_captured: str | None = None,
    ) -> dict:
        """Capture raw content as an immutable source page in the Knowledge Base.

        Writes a frontmatter-compliant page to Sources/<Type>/YYYY-MM-DD-<slug>.md
        and updates Sources/index.md, the top-level index.md (Recent activity
        + Counts), and log.md. Per SKILL.md rule 7.

        Args:
            content: Full text body to capture (markdown OK).
            source_type: One of article, session, book, paper, video, other.
            title: Human title; used to derive the filename slug.
            url: Required when source_type is article, paper, or video.
            tags: Lowercase dash-separated; the server normalizes case/spacing.
            why_captured: One short paragraph on why this is worth keeping.

        Returns:
            {path, warnings}. On schema violation, raises a structured error
            with code=INVALID_SOURCE, the missing fields, and the reason.
        """
        try:
            result = add_module.add(
                vault_root,
                source_schema,
                content=content,
                source_type=source_type,
                title=title,
                url=url,
                tags=tags,
                why_captured=why_captured,
            )
        except add_module.AddError as e:
            # FastMCP serializes raised exceptions; we want a structured shape.
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    @mcp.tool
    def note(
        content: str,
        note_type: str,
        title: str,
        project: str | None = None,
        projects: list[str] | None = None,
        sources: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        severity: str | None = None,
        pattern_type: str | None = None,
        domain: str | None = None,
        started: str | None = None,
        duration: str | None = None,
        hypothesis: str | None = None,
        n: int | None = None,
        concluded: str | None = None,
        medium: str | None = None,
        recorded: str | None = None,
        published: str | None = None,
        host: str | None = None,
        editor: str | None = None,
    ) -> dict:
        """Create a compiled note in the Knowledge Base.

        Use this for distilled thinking — not raw capture. For raw capture
        (an article you read, a session transcript), use `add` instead.

        Six note types:
        - `research-note`: project-scoped findings. `project` REQUIRED.
          → `Notes/Research/<Project>/<slug>.md`
        - `insight`: cross-cutting claim. Optional `projects` (plural).
          → `Notes/Insights/<slug>.md`
        - `failure`: documented failure mode. Optional `projects`, optional
          `severity` ∈ {minor, moderate, serious, critical}.
          → `Notes/Failures/<slug>.md`
        - `pattern`: reusable cross-cutting pattern. Optional `projects`,
          optional `pattern_type` ∈ {architectural, workflow, prompting,
          governance, pedagogical}.
          → `Notes/Patterns/<slug>.md`
        - `experiment`: hypothesis + protocol. `domain`, `started` (YYYY-MM-DD),
          and `duration` (e.g. "30 days", "ongoing") REQUIRED. Optional
          `hypothesis`, `n` (default 1), `concluded`.
          → `Notes/Experiments/<domain>/YYYY-MM-<slug>.md`
        - `production-log`: creative artifact log. `medium` REQUIRED (e.g.
          "Reels", "Episodes"). Optional `recorded`, `published`, `host`,
          `editor`, `projects`. Status enum is richer: {planned, recorded,
          edited, published, reflected, dropped, archived}; defaults to
          `planned`.
          → `Notes/Productions/<medium>/YYYY-MM-<slug>.md`

        For each `sources:` wikilink, appends this note's wikilink to that
        source's `ingested_into:` frontmatter (maintaining the source→note graph).

        Args:
            content: Full markdown body. Body section conventions per type:
                research-note: `## Question`/`## Findings`/`## Connections`.
                insight: `## Claim`/`## Why it holds`/`## Connections`.
                failure: `## What happened`/`## Mechanism`/`## Detection`/`## Mitigation`/`## Connections`.
                pattern: `## Problem`/`## Solution`/`## When to use`/`## When NOT to use`/`## Connections`.
                experiment: `## Hypothesis`/`## Protocol`/`## Baseline`/`## Intervention`/`## Results`/`## Conclusion`/`## Connections`.
                production-log: `## Frame`/`## Artifact`/`## Production session`/`## Outcomes`/`## Reflection`/`## Connections`.
                Conventions only — no shape is enforced.
            note_type: One of research-note, insight, failure, pattern,
                experiment, production-log.
            title: Human title; used to derive a kebab-case filename slug.
                Experiments and production-logs auto-prefix with YYYY-MM.
            project: REQUIRED for research-note. Valid: substrate, q, endstate,
                sift, tu, book-club, health, finance, creative, science, travel,
                personal.
            projects: List of project keys (plural). Optional for insight,
                failure, pattern, production-log. Must contain only valid keys.
            sources: Vault-relative wikilinks to existing pages this note draws
                from, e.g. `["Knowledge Base/Sources/Articles/2026-05-18-foo"]`
                or `["[[Knowledge Base/Sources/Articles/2026-05-18-foo]]"]`.
                Brackets and the leading `Knowledge Base/` are tolerated.
            tags: Lowercase dash-separated; the server normalizes case/spacing.
            status: Defaults to `active` for most types, `planned` for
                production-log. Valid set varies by type.
            severity: failure only. {minor, moderate, serious, critical}.
            pattern_type: pattern only. {architectural, workflow, prompting,
                governance, pedagogical}.
            domain: experiment only. Becomes the subfolder name (lowercased).
            started: experiment only. YYYY-MM-DD when the experiment began.
            duration: experiment only. Freeform, e.g. "30 days", "ongoing".
            hypothesis: experiment only. One-line claim being tested.
            n: experiment only. Sample size. Defaults to 1 (n-of-1).
            concluded: experiment only. YYYY-MM-DD when it ended (absent while ongoing).
            medium: production-log only. Subfolder, e.g. "Reels", "Episodes".
            recorded: production-log only. YYYY-MM-DD of recording session.
            published: production-log only. YYYY-MM-DD of publication.
            host: production-log only. Creator/talent name.
            editor: production-log only. Producer/editor name.

        Returns:
            {path, warnings}. On validation failure, raises a structured error
            with code=INVALID_NOTE, the missing fields, and the reason.
        """
        try:
            result = note_module.note(
                vault_root,
                content=content,
                note_type=note_type,
                title=title,
                project=project,
                projects=projects,
                sources=sources,
                tags=tags,
                status=status,
                severity=severity,
                pattern_type=pattern_type,
                domain=domain,
                started=started,
                duration=duration,
                hypothesis=hypothesis,
                n=n,
                concluded=concluded,
                medium=medium,
                recorded=recorded,
                published=published,
                host=host,
                editor=editor,
            )
        except note_module.NoteError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    @mcp.tool
    def audit(categories: list[str] | None = None) -> dict:
        """Read-only health check across the Knowledge Base.

        Returns a structured report Claude can read to propose follow-up
        edits via `note`/`add`. Does NOT modify anything.

        Categories (default: all):
        - `broken_wikilink`: `[[X]]` whose target file doesn't exist
        - `orphan_entity`: `Entities/...` file with no inbound wikilinks
        - `unprocessed_source`: source with empty `ingested_into:` (no notes
          have compiled from it yet)
        - `index_drift`: top-level `index.md` Counts disagree with on-disk counts

        Args:
            categories: Optional filter; only run these checks. Each must be
                one of the four above. Omit to run all.

        Returns:
            {findings: [{category, severity, path, detail, proposed_fix}],
             summary: {category: count}}.
        """
        report = audit_module.audit(vault_root, categories=categories)
        return report.as_dict()

    @mcp.tool
    def get(path: str) -> dict:
        """Read a full vault page by path. Returns frontmatter + body + raw content.

        Use this when `find` gives you a path and you need the whole page
        (to cite, build on, or rewrite). `find` only returns excerpts; `get`
        returns the full file.

        Args:
            path: Vault-relative path. All four shapes accepted:
                - `Knowledge Base/Notes/Insights/foo.md`
                - `Notes/Insights/foo.md`
                - either of the above without the `.md` suffix.

        Returns:
            {path, frontmatter, body, content}. `content` is the raw file
            text (including frontmatter delimiters); `body` is just the
            markdown after the frontmatter.

        Errors:
            INVALID_PATH (path escapes Knowledge Base/ or empty);
            NOT_FOUND (no such file); UNREADABLE (parse failure).
        """
        try:
            result = get_page_module.get_page(vault_root, path=path)
        except get_page_module.GetError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def replace(
        old_path: str,
        content: str,
        note_type: str,
        title: str,
        reason: str | None = None,
        project: str | None = None,
        projects: list[str] | None = None,
        sources: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        severity: str | None = None,
        pattern_type: str | None = None,
        domain: str | None = None,
        started: str | None = None,
        duration: str | None = None,
        hypothesis: str | None = None,
        n: int | None = None,
        concluded: str | None = None,
        medium: str | None = None,
        recorded: str | None = None,
        published: str | None = None,
        host: str | None = None,
        editor: str | None = None,
    ) -> dict:
        """Supersede an existing compiled page with a new one.

        Writes the new page at a fresh slug (via the same machinery as
        `note`), then patches the OLD page to set `status: superseded` and
        `superseded_by: "[[<new>]]"`. The NEW page gets `supersedes:
        "[[<old>]]"` in its frontmatter. The old page stays readable;
        readers follow the chain — inbound wikilinks are NOT retargeted
        (per SKILL.md rule 6).

        Use this for substantial rewrites of an existing compiled page —
        not minor tweaks (the desk-side flow handles those better since you
        see a live diff). Cannot supersede sources or evidence (append-only).

        Args:
            old_path: Vault-relative path of the page being superseded.
                Same path conventions as `get` and `find`.
            reason: Optional one-line explanation of why this replacement is
                happening; lands in the log entry body.
            (all other args): Same as the `note` tool — define the new page's
                content, type, project/projects, sources, etc.

        Returns:
            {old_path, new_path, warnings}.

        Errors:
            INVALID_REPLACE (old is in Sources/ or Evidence/, or not a
            supersedable type); OLD_NOT_FOUND; ALREADY_SUPERSEDED
            (old page is already marked superseded).
        """
        try:
            result = replace_module.replace(
                vault_root,
                old_path=old_path,
                reason=reason,
                content=content,
                note_type=note_type,
                title=title,
                project=project,
                projects=projects,
                sources=sources,
                tags=tags,
                status=status,
                severity=severity,
                pattern_type=pattern_type,
                domain=domain,
                started=started,
                duration=duration,
                hypothesis=hypothesis,
                n=n,
                concluded=concluded,
                medium=medium,
                recorded=recorded,
                published=published,
                host=host,
                editor=editor,
            )
        except replace_module.ReplaceError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        except note_module.NoteError as e:
            # New-page validation failed before the supersession could land.
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    @mcp.tool
    def link(
        entity_type: str,
        name: str,
        summary: str,
        why_in_kb: str | None = None,
        tags: list[str] | None = None,
        connections: list[str] | None = None,
        affiliation: str | None = None,
        relationship: str | None = None,
        domain: str | None = None,
        language: str | None = None,
        repo: str | None = None,
        license: str | None = None,
        used_in: list[str] | None = None,
        decided: str | None = None,
        project: str | None = None,
        decision_status: str | None = None,
    ) -> dict:
        """Create a typed entity under Entities/<Folder>/<Name>.md.

        Entities are the typed nodes of the KB graph — people, concepts,
        libraries, decisions. Name them after the thing they are (Title Case,
        not slugified): `Andrej Karpathy`, `Agentic RAG`, `pgvector`.

        Four entity types with conditional frontmatter:
        - `person`   → Entities/People/. Optional: `affiliation`, `relationship`.
        - `concept`  → Entities/Concepts/. Optional: `domain` (e.g.
          "retrieval", "metabolism", "infrastructure").
        - `library`  → Entities/Libraries/. Optional: `language`, `repo`,
          `license`, `used_in` (list of projects).
        - `decision` → Entities/Decisions/. Optional: `decided` (YYYY-MM-DD),
          `project` (project key), `decision_status` ∈ {proposed, accepted,
          superseded}.

        v1 is create-only. If the entity file already exists, returns
        ENTITY_EXISTS — use `replace` to supersede instead. Sub-folder index
        (e.g. Entities/Concepts/index.md categorization) is NOT auto-updated;
        reconcile via desk audit.

        Args:
            entity_type: One of person, concept, library, decision.
            name: Title Case, the entity's actual name. Will be the filename.
            summary: One-paragraph description for the `## Summary` section.
            why_in_kb: Optional `## Why in the KB` paragraph — explains what
                this entity is relevant to in Hugo's work.
            tags: Lowercase dash-separated; normalized by the server.
            connections: List of vault-relative wikilink targets to put under
                `## Connections`. Same path conventions as `note.sources`.
            (per-type fields): see the bullet list above.

        Returns:
            {path, warnings}.

        Errors:
            INVALID_LINK (bad entity_type, decision_status, missing required);
            ENTITY_EXISTS (use `replace` instead).
        """
        try:
            result = link_module.link(
                vault_root,
                entity_type=entity_type,
                name=name,
                summary=summary,
                why_in_kb=why_in_kb,
                tags=tags,
                connections=connections,
                affiliation=affiliation,
                relationship=relationship,
                domain=domain,
                language=language,
                repo=repo,
                license=license,
                used_in=used_in,
                decided=decided,
                project=project,
                decision_status=decision_status,
            )
        except link_module.LinkError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    @mcp.tool
    def preserve(
        scope: str,
        category: str,
        filename: str,
        content_base64: str | None = None,
        content: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Capture a binary or text artifact to Evidence/<scope>/<category>/.

        For raw factual artifacts — PDFs, screenshots, letters, medical
        records, legal documents — that need to be preserved as-received
        with no analytical processing. Per SKILL.md rule 2, Evidence is
        append-only. Analytical takes go in compiled notes that link to
        the evidence file.

        Exactly one of `content_base64` or `content` must be supplied:
        - `content_base64`: file bytes (binaries — PDF, images, .docx).
          5MB decoded size limit.
        - `content`: UTF-8 text (markdown, plain text, transcripts).

        Args:
            scope: Incident or domain key (e.g. "Yolo", "Mother Cancer").
                Creates the subfolder if it doesn't exist.
            category: Sub-category within scope (e.g. "letters", "labs",
                "court-docs"). Creates the subfolder if it doesn't exist.
            filename: The artifact's filename including extension
                (e.g. `2026-04-15-pathology-report.pdf`). Date-prefixing
                where temporal anchoring matters is the convention.
            content_base64: Base64-encoded file bytes. Use for binary
                artifacts.
            content: UTF-8 text. Use for text-based artifacts.
            description: Optional. If supplied, a sidecar `<filename>.md`
                is written alongside the artifact with frontmatter and the
                description under `## Description`.

        Returns:
            {path, sidecar_path, warnings}.

        Errors:
            INVALID_PRESERVE (missing required, both content modes set, etc.);
            ARTIFACT_EXISTS (file already exists — Evidence is append-only,
            pick a new filename); TOO_LARGE (>5MB decoded).
        """
        try:
            result = preserve_module.preserve(
                vault_root,
                scope=scope,
                category=category,
                filename=filename,
                content_base64=content_base64,
                content=content,
                description=description,
            )
        except preserve_module.PreserveError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    return mcp


def run(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    log_dir: Path | None = None,
) -> None:
    """CLI entry: configure logging, build the server, run it.

    Auth is required for HTTP transports; stdio runs auth-free.
    """
    from .logging_config import configure_logging

    if log_dir is None:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
    configure_logging(log_dir)

    require_auth = transport != "stdio"
    mcp = build_server(require_auth=require_auth)

    if transport == "stdio":
        log.info("kb-mcp starting on stdio")
        mcp.run(transport="stdio")
    else:
        log.info("kb-mcp starting on %s host=%s port=%s", transport, host, port)
        mcp.run(transport=transport, host=host, port=port)
