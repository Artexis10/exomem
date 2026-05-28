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
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import add as add_module
from . import append_to_file as append_to_file_module
from . import audit as audit_module
from . import audit_fix as audit_fix_module
from . import create_directory as create_directory_module
from . import create_file as create_file_module
from . import delete_directory as delete_directory_module
from . import delete_file as delete_file_module
from . import edit as edit_module
from . import find as find_module
from . import get_frontmatter as get_frontmatter_module
from . import get_page as get_page_module
from . import link as link_module
from . import list_directory as list_directory_module
from . import list_inbound_links as list_inbound_links_module
from . import list_trash as list_trash_module
from . import move_file as move_file_module
from . import note as note_module
from . import preserve as preserve_module
from . import recover_from_trash as recover_from_trash_module
from . import replace as replace_module
from . import schema
from . import set_frontmatter_field as set_frontmatter_field_module
from .vault import resolve_vault


log = logging.getLogger(__name__)
_call_log = logging.getLogger("kb_mcp.calls")


class CallTraceMiddleware(Middleware):
    """Per-call traceability: log every tool invocation with name + duration.

    Service-log only (`logs/kb-mcp.log`). The durable content history lives
    in `Knowledge Base/log.md` (writes only, KB-scoped) — this layer is
    operational: which tool was called, when, by whom, did it succeed.
    Reads land here too, by design, so we can answer "did the connector
    actually invoke X?" without polluting log.md.

    Payloads (args + results) deliberately NOT logged — they can be large
    (`add(content=...)` is unbounded). Tool name + duration + outcome is
    what's useful for traceability; deeper inspection goes through the
    audit + the log.md content history.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        import time
        tool_name = _extract_tool_name(context.message)
        # For find calls, log query + mode + scope so failure modes
        # ("hybrid whiffed on X") are reproducible without a screenshot.
        # Other tools log only tool name + duration — their payloads can be
        # huge (add(content=...)) or sensitive (preserve()).
        extras = _find_call_summary(context.message) if tool_name == "find" else ""
        _call_log.info(f"event=tool_start tool={tool_name}{extras}")
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
            dur = round((time.perf_counter() - t0) * 1000, 2)
            _call_log.info(
                f"event=tool_success tool={tool_name} duration_ms={dur}{extras}"
            )
            return result
        except Exception as e:
            dur = round((time.perf_counter() - t0) * 1000, 2)
            # `e` may include full tracebacks if rendered as str; trim to keep
            # the call-log line readable. Full tracebacks land in service.err.log.
            err = type(e).__name__
            _call_log.error(
                f"event=tool_error tool={tool_name} duration_ms={dur} err={err}{extras}"
            )
            raise


def _extract_tool_name(message) -> str:
    """Pull the tool name out of a tools/call request payload, defensively."""
    # FastMCP's MiddlewareContext.message for tool calls carries the request
    # body; the actual shape varies by version. Try common access patterns
    # and fall back to "?" so logging never raises.
    for accessor in (
        lambda m: m.params.name,
        lambda m: m.name,
        lambda m: m["params"]["name"],
        lambda m: m["name"],
    ):
        try:
            v = accessor(message)
            if v:
                return str(v)
        except (AttributeError, KeyError, TypeError):
            continue
    return "?"


def _extract_tool_args(message) -> dict:
    """Pull the tool-call arguments out of the request payload, defensively.

    Returns `{}` when the shape doesn't match (so logging never raises).
    """
    for accessor in (
        lambda m: m.params.arguments,
        lambda m: m["params"]["arguments"],
        lambda m: m.arguments,
    ):
        try:
            v = accessor(message)
            if isinstance(v, dict):
                return v
        except (AttributeError, KeyError, TypeError):
            continue
    return {}


def _find_call_summary(message) -> str:
    """One-line summary of find()'s key args for the call log.

    Keeps the query truncated so a long natural-language query doesn't blow
    up log lines, but long enough that "hybrid whiffed on X" is debuggable.
    """
    args = _extract_tool_args(message)
    if not args:
        return ""
    q = str(args.get("query", ""))
    if len(q) > 120:
        q = q[:117] + "..."
    q = q.replace('"', "'")  # keep the log line single-quote-friendly
    mode = args.get("mode", "hybrid")
    scope = args.get("scope", "kb")
    return f' query="{q}" mode={mode} scope={scope}'


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

    # Preload bge-base so the first hybrid query in this process is fast
    # (otherwise the HF-Hub HEAD redirects + tokenizer load happen on the
    # first user-facing call — ~30s of dead air). Tests set
    # KB_MCP_DISABLE_EMBEDDINGS to skip this entirely.
    if not os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):
        try:
            from . import embeddings
            log.info("preloading embedding model %s", embeddings.MODEL_NAME)
            embeddings.get_model()
            log.info("embedding model ready")
        except Exception as e:  # noqa: BLE001 — preload is best-effort
            log.warning(
                "embedding preload failed (%s); first hybrid query will "
                "pay the cost", e,
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
    mcp.add_middleware(CallTraceMiddleware())

    if auth is not None:
        # claude.ai's OAuth gateway probes /.well-known/oauth-protected-resource
        # (the bare path, no resource suffix) before following the
        # WWW-Authenticate `resource_metadata` pointer. When it 404s there,
        # the connect flow aborts with `mcp_registration_failed`. FastMCP only
        # serves the RFC-9728 path-specific variant (/oauth-protected-resource/mcp),
        # so mirror the metadata at the bare path to make registration reliable.
        _resource_url = f"{base_url}/mcp"
        _issuer_url = f"{base_url}/"

        @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
        async def _oauth_protected_resource_bare(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "resource": _resource_url,
                    "authorization_servers": [_issuer_url],
                    "scopes_supported": [],
                    "bearer_methods_supported": ["header"],
                },
                headers={"Cache-Control": "public, max-age=3600"},
            )

    @mcp.tool
    def find(
        query: str = "",
        types: list[str] | None = None,
        projects: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int = 15,
        scope: str = "kb",
        mode: str = "hybrid",
        graph: bool = True,
        rerank: bool = False,
        prefer_compiled: bool = True,
    ) -> list[dict]:
        """Search the vault. Filters are AND'd; tag/project lists are OR'd within.

        Args:
            query: Free-text search string. In "hybrid"/"vector" mode it's
                embedded with bge-base for semantic recall. In "keyword" mode
                it's tokenized on whitespace and every token must appear in
                title or body (any order) — `contract employment` matches a
                page about "employment contract". Empty string always falls
                back to "most-recent filtered" behaviour regardless of mode.
            types: Filter to these page types (source, research-note, insight, failure, pattern, experiment, production-log, entity).
            projects: Filter to pages whose `project` or `projects:` includes any of these keys.
            tags: Filter to pages whose `tags:` includes any of these (case-insensitive).
            limit: Max hits to return. Default 15, hard cap 100.
            scope: "kb" (default) searches only Knowledge Base/. "vault"
                walks the whole vault including curated trees
                (Cognitive Core/, Domains/, Prompt Bank/, Products/,
                Personal Context/, Systems Thinking/). Use "vault" when
                you need to discover content outside the KB —
                free-text queries work the same; structured filters
                won't match curated pages that lack those frontmatter
                fields. `_Schema/`, `_trash/`, `_attachments/`, and
                `.obsidian/` are excluded either way.
            mode: Ranker. "hybrid" (default) fuses BM25 + local vector
                embeddings via reciprocal rank fusion — best recall on
                natural-language queries. "keyword" preserves the original
                case-insensitive substring matching, sorted by `updated:`.
                "vector" is vector-only (testing aid). BM25 corpus is
                Snowball-stemmed so "regulation" reaches pages with
                "regulator"; keyword mode stays strict-substring. If the
                embedding sidecar hasn't been built yet, hybrid degrades
                to BM25-only; run `audit_fix(rebuild_embeddings=true)` to
                populate it.
            graph: When true (default) and mode is hybrid/vector, outbound
                wikilinks of top BM25/vector candidates contribute a third
                ranking — surfaces 1-hop neighbours of strong matches.
            rerank: When true (off by default), runs the top fused
                candidates through bge-reranker-base (a CrossEncoder) for
                higher-precision ordering. Adds ~50ms/candidate; useful
                when ambiguous queries float topically-off vector matches
                to the top.
            prefer_compiled: When true (default), applies a small boost to
                compiled types (insight, pattern, failure, research-note,
                entity) and a small penalty to raw `source` after fusion
                AND rerank. Reflects the KB's epistemic hierarchy. Set
                false to retrieve raw source discussion verbatim (e.g.
                "what did I capture from Dr. X").

        Returns:
            List of {path, type, scope, title, updated, excerpt[, signals]}.
            In hybrid mode `excerpt` shows the best-matching chunk; in
            keyword mode it's a snippet anchored to the literal query
            match. `signals` (hybrid/vector only) carries per-ranker
            position: {bm25_rank?, vector_rank?, vector_score?, graph_hop?,
            graph_in_degree?, rerank_score?}. `graph_in_degree` is the
            number of top-N seeds whose body wikilinks to this hit —
            independent of graph_hop, which only fires for graph-only
            results.
        """
        hits = find_module.find(
            vault_root,
            query=query,
            types=types,
            projects=projects,
            tags=tags,
            limit=limit,
            scope=scope,
            mode=mode,
            graph=graph,
            rerank=rerank,
            prefer_compiled=prefer_compiled,
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
                Rendered as a leading blockquote in the source body, between
                the `# Source: ...` header and the `## Capture` section.

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
            content: Full markdown body, written verbatim after the
                frontmatter. Should start with `# <title>` (the H1 matching
                the title arg) followed by the section conventions per type:
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
        - `broken_wikilink`: `[[X]]` whose target file doesn't exist.
          Skips wikilinks inside fenced code blocks and inline code spans.
          Bare names resolve against filename stems AND frontmatter `title:`
          (so date-prefixed sources with a title match are not flagged).
        - `orphan_entity`: `Entities/...` file with no inbound wikilinks
        - `unprocessed_source`: source with empty `ingested_into:` (no notes
          have compiled from it yet)
        - `index_drift`: top-level `index.md` Counts disagree with on-disk counts
        - `tag_inconsistency`: case/separator variants of the same tag
          (`warning_letter_incident` vs `warning-letter-incident` vs
          `Warning-Letter-Incident`). Mechanical drift only; semantic
          near-duplicates like `metabolism` vs `metabolic` aren't flagged.
        - `frontmatter_compliance`: per-page-type required-field gaps,
          `tenant:` set without `project: q`, patterns using singular
          `project:` instead of plural `projects:`.

        Args:
            categories: Optional filter; only run these checks. Each must be
                one of the five above. Omit to run all.

        Returns:
            {findings: [{category, severity, path, detail, proposed_fix}],
             summary: {category: count}}.
        """
        report = audit_module.audit(vault_root, categories=categories)
        return report.as_dict()

    @mcp.tool
    def audit_fix(dry_run: bool = False, rebuild_embeddings: bool = False) -> dict:
        """Run audit + auto-apply safe fixes; propose-only for risky categories.

        Closes the lint-finds-but-doesn't-fix loop. Safe categories get
        rewritten in-place via atomic batch writes; risky categories
        surface in `proposed` for human/LLM review.

        Safe categories (auto-applied):
        - Canonical wikilink form across all compiled material (body +
          frontmatter). Skips Sources/ and Evidence/ (append-only).
        - Frontmatter required-field backfill with safe defaults:
          - production-log missing created/updated → use started/shipped/today
          - research-note/insight/failure/pattern missing status → "active"
          - research-note/insight/failure/pattern missing updated →
            use created, else today
          - experiment missing duration → computed from started+concluded
          - source missing captured → use created (if present)
        - Pattern with singular `project:` → plural `projects: [<value>]`
          (auto-merged into existing projects: list if present).
        - Sub-folder index refresh + top-index count refresh.

        Risky categories (propose-only, surfaced in `proposed` list):
        - broken_wikilink residuals after canonicalization (forward refs,
          missing files, audit limitations).
        - orphan_entity (deletion is too big to auto-apply).
        - unprocessed_source (compilation is a thinking task).
        - tag_inconsistency (renames can break user mental models).
        - frontmatter_compliance: tenant: misuse (might be intentional).
        - source missing source_type (folder→type inference is brittle).

        Idempotent: running twice on a clean vault produces no changes.

        Args:
            dry_run: If true, compute what would change without writing.
                Default false.
            rebuild_embeddings: If true, wipe and rebuild the vector sidecar
                at `<vault>/Knowledge Base/.embeddings.sqlite` after the fix
                sweep. Use on first run, after a machine swap, or when the
                sidecar has drifted from disk. Ignored when `dry_run=true`.

        Returns:
            {fixed: [{category, path, detail, action}, ...],
             proposed: [<audit findings>],
             files_rewritten: int,
             summary: {fixed: N, proposed: N, fixed_<category>: N,
                       embeddings_chunks?: N},
             dry_run: bool}
        """
        report = audit_fix_module.audit_fix(
            vault_root,
            dry_run=dry_run,
            rebuild_embeddings=rebuild_embeddings,
        )
        return report.as_dict()

    @mcp.tool
    def get(path: str) -> dict:
        """Read a full vault file by path. Returns frontmatter + body + raw content.

        Reads anywhere under the vault root — not just `Knowledge Base/`.
        This lets you cite from Hugo's curated trees (`Cognitive Core/`,
        `Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`) when
        compiling. Those are read-only by KB skill convention; `get` honors
        that by only reading.

        Use this when `find` gives you a path and you need the whole page
        (to cite, build on, or rewrite). `find` only returns excerpts.

        Args:
            path: Vault-relative path. Accepted shapes:
                - `Knowledge Base/Notes/Insights/foo.md`
                - `Cognitive Core/Strategy.md`
                - `Notes/Insights/foo` (auto-prepends `Knowledge Base/` if
                  literal path doesn't resolve; auto-adds `.md`).

        Returns:
            {path, frontmatter, body, content}. `content` is the raw file
            text (including frontmatter delimiters); `body` is just the
            markdown after the frontmatter.

        Errors:
            INVALID_PATH (path escapes vault root or empty);
            NOT_FOUND (no such file); UNREADABLE (parse failure).
        """
        try:
            result = get_page_module.get_page(vault_root, path=path)
        except get_page_module.GetError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def edit(
        path: str,
        why: str,
        new_body: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Lightweight in-place edit of a page (body and/or tags).

        For tweaks — typo fixes, sentence additions, tag corrections —
        without going through full supersession via `replace`. Use `replace`
        for substantial rewrites; use `edit` when creating a new file +
        superseded-link chain would be silly for what you're changing.

        What changes:
        - `new_body` (if provided) replaces the markdown body.
        - `tags` (if provided) replaces the `tags:` frontmatter field.
        - `updated:` is always bumped to today.

        What stays:
        - All other frontmatter fields (type, project, status, sources,
          superseded_by, etc.). If you need to change those, use `replace`.

        No type allowlist: any frontmatter-bearing page outside Sources/
        Evidence is editable, regardless of `type:`. Works on novel page
        types (`identity`, future types) without code changes.

        Refuses:
        - Sources/ and Evidence/ paths (rule 2: append-only). Add a
          corrective source or compile a downstream note instead.
        - Pages without a frontmatter block (won't synthesize one).
        - Pages already marked `status: superseded` (don't edit history;
          supersede the active page instead).

        Args:
            path: Vault-relative path to the compiled page (same shape as
                `get` accepts).
            why: One-line rationale for the edit. Required — lands in the
                log entry so the change is auditable.
            new_body: New markdown body (everything after frontmatter).
                Omit to keep the existing body.
            tags: New tags list (replaces existing). Lowercase dash-
                separated; the server normalizes. Omit to keep existing tags.

        Returns:
            {path, warnings}.

        Errors:
            INVALID_EDIT (missing required, both new_body and tags omitted,
            path in Sources/Evidence); NOT_FOUND; ALREADY_SUPERSEDED;
            UNREADABLE.
        """
        try:
            result = edit_module.edit(
                vault_root,
                path=path,
                why=why,
                new_body=new_body,
                tags=tags,
            )
        except edit_module.EditError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
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

        Use this for substantial rewrites of an existing page — not minor
        tweaks (the desk-side flow handles those better since you see a
        live diff). Cannot supersede sources or evidence (append-only).
        No type allowlist beyond the append-only guard: novel page types
        (`identity`, future types) can be superseded without code changes.

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

    # ---------------- Tier 2: filesystem-parity escape hatches ----------------
    #
    # Tier 1 (above) is primary: type-routed operations that encode the KB's
    # discipline. Tier 2 (below) covers cases that don't fit Tier 1 — new
    # folder structures, files that aren't typed notes, surgical edits that
    # the Tier 1 set can't express. Use Tier 1 first; fall back to Tier 2
    # only when nothing in Tier 1 fits.
    #
    # Discipline preserved across both tiers:
    # - Sources/ and Evidence/ are append-only (write via `add` / `preserve`).
    # - Cognitive Core/, Domains/, Prompt Bank/, Products/, Personal Context/
    #   are write-protected by default; pass `allow_curated=true` to override.
    # - Every write logs to Knowledge Base/log.md.

    @mcp.tool
    def create_file(
        path: str,
        content: str,
        frontmatter: dict | None = None,
        overwrite: bool = False,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: write a file at an arbitrary vault path.

        Escape hatch for files that don't fit Tier 1 type routing — new folder
        structures (`Identity/`, `Templates/`), skill files, scratch. For
        typed notes use `note`/`add`/`link`/`preserve`.

        If `frontmatter` is a dict, this op prepends a YAML block built from
        it (and auto-fills `created`/`updated` to today if not provided);
        `content` is the body in that case. If `frontmatter` is omitted,
        `content` is written verbatim — the caller is responsible for any
        frontmatter already in it.

        Refuses:
        - Sources/, Evidence/ (append-only — use `add` or `preserve`).
        - Curated trees (Cognitive Core/, Domains/, Prompt Bank/, Products/,
          Personal Context/) unless `allow_curated=true` is passed.
        - Existing files unless `overwrite=true`.

        Args:
            path: Vault-relative, e.g. `Knowledge Base/Identity/Career.md`.
                Forward or back slashes accepted. Path-escape guarded.
            content: File body (or full file if `frontmatter` is None).
            frontmatter: Optional dict prepended as YAML frontmatter.
            overwrite: If true, replace existing file. Default false.
            allow_curated: Required to write under a curated tree. Default false.

        Returns: {path, warnings}.
        Errors: INVALID_PATH; APPEND_ONLY; CURATED_PROTECTED; FILE_EXISTS;
                NOT_A_FILE.
        """
        try:
            result = create_file_module.create_file(
                vault_root,
                path=path,
                content=content,
                frontmatter=frontmatter,
                overwrite=overwrite,
                allow_curated=allow_curated,
            )
        except create_file_module.CreateFileError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def list_directory(
        path: str = "",
        recursive: bool = False,
        include_hidden: bool = False,
    ) -> dict:
        """Tier 2: list files and subfolders at a vault path. Read-only.

        Works anywhere under vault root including curated trees (consistent
        with `get`). For .md files, surfaces the frontmatter `type` field
        so callers can scan typed content quickly.

        Args:
            path: Vault-relative. Empty string lists vault root. Auto-handles
                forward/back slashes.
            recursive: If true, walk subfolders. Default false.
            include_hidden: If true, include dotfiles and _attachments/.
                Default false.

        Returns: {path, entries: [{name, type, path, size_bytes, updated,
                 frontmatter_type}]}.

        Errors: INVALID_PATH; NOT_FOUND; NOT_A_DIR.
        """
        try:
            result = list_directory_module.list_directory(
                vault_root,
                path=path,
                recursive=recursive,
                include_hidden=include_hidden,
            )
        except list_directory_module.ListDirectoryError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def create_directory(
        path: str,
        parents: bool = True,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: create a folder at a vault path. Idempotent.

        Curated trees require `allow_curated=true`. Append-only trees
        (Sources/, Evidence/) are allowed at the directory level — those
        subfolders auto-materialize on `add`/`preserve` writes anyway.

        Args:
            path: Vault-relative folder path.
            parents: If true (default), create intermediate folders (mkdir -p).
            allow_curated: Required to create folders under curated trees.

        Returns: {path, created (bool), warnings}.
        Errors: INVALID_PATH; CURATED_PROTECTED; NOT_A_DIR; MISSING_PARENT;
                MKDIR_FAILED.
        """
        try:
            result = create_directory_module.create_directory(
                vault_root,
                path=path,
                parents=parents,
                allow_curated=allow_curated,
            )
        except create_directory_module.CreateDirectoryError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def move_file(
        old_path: str,
        new_path: str,
        update_wikilinks: bool = True,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: relocate a file, optionally rewriting inbound wikilinks.

        Refuses moves out of OR into Sources/ and Evidence/ (append-only).
        Curated trees on either end need `allow_curated=true`. Refuses to
        overwrite existing destinations.

        When `update_wikilinks=true` (default), scans the full vault for
        `[[<old>]]`, `[[<old.md>]]`, and `[[<old_basename>]]` (only when the
        basename is unique vault-wide) and rewrites them to point at the
        new location. Preserves full-form vs stripped-form per link.

        Args:
            old_path: Vault-relative source.
            new_path: Vault-relative destination (must not exist).
            update_wikilinks: Default true.
            allow_curated: Required if either end is in a curated tree.

        Returns: {old_path, new_path, wikilinks_updated, files_touched, warnings}.
        Errors: INVALID_PATH; NOT_FOUND; DEST_EXISTS; APPEND_ONLY;
                CURATED_PROTECTED.
        """
        try:
            result = move_file_module.move_file(
                vault_root,
                old_path=old_path,
                new_path=new_path,
                update_wikilinks=update_wikilinks,
                allow_curated=allow_curated,
            )
        except move_file_module.MoveFileError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def delete_file(
        path: str,
        confirm: bool,
        force_orphan: bool = False,
        force_superseded: bool = False,
        allow_curated: bool = False,
        expected_dead_inbound: list[str] | None = None,
    ) -> dict:
        """Tier 2: trash a file. Reversible — file moves to _trash/, not /dev/null.

        Deletes are NEVER permanent at this layer. The file moves to
        `Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>.md`
        with a `.meta.json` sidecar capturing original path, timestamp,
        inbound link count, and which force-flags were used. Recovery is
        `move_file` from the trash path back. Permanent removal happens
        desk-side via `rm Knowledge Base/_trash/...`.

        Per SKILL.md rule 6, supersession via `replace` is still preferred
        for compiled material. Use this op for scratch, mistakes outside the
        typed-note set, and cleanup of files that genuinely shouldn't exist.

        Refuses:
        - Sources/, Evidence/ (append-only).
        - Files already in `_trash/` (already trashed — recover via move_file).
        - Curated trees unless `allow_curated=true`.
        - When `confirm=false`.
        - When `superseded_by:` is set (history) unless `force_superseded=true`.
        - When inbound wikilinks exist (after `expected_dead_inbound` filtering)
          unless `force_orphan=true`.

        Args:
            path: Vault-relative.
            confirm: Must be `true` explicitly. Marks the action deliberate.
            force_orphan: Allow trash even if inbound wikilinks exist.
            force_superseded: Allow trash of a file in the supersession chain.
            allow_curated: Required to trash under a curated tree.
            expected_dead_inbound: Vault-relative paths whose inbound links
                to this file should be ignored. Use when you're trashing
                multiple files in one workflow (e.g. cleaning a supersession
                chain) and don't want each step to false-positive on
                links that will die in the same batch.

        Returns: {path, trash_path, inbound_link_count, inbound_ignored_count, warnings}.
        Errors: UNCONFIRMED; INVALID_PATH; NOT_FOUND; ALREADY_TRASHED;
                APPEND_ONLY; CURATED_PROTECTED; SUPERSEDED_HISTORY;
                INBOUND_LINKS; TRASH_FAILED.
        """
        try:
            result = delete_file_module.delete_file(
                vault_root,
                path=path,
                confirm=confirm,
                force_orphan=force_orphan,
                force_superseded=force_superseded,
                allow_curated=allow_curated,
                expected_dead_inbound=expected_dead_inbound,
            )
        except delete_file_module.DeleteFileError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def delete_directory(
        path: str,
        confirm: bool,
        recursive: bool = False,
        force_orphan: bool = False,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: trash a folder (whole tree). Reversible via _trash/.

        Symmetric with `create_directory`. Like `delete_file`, this NEVER
        does a permanent delete — the folder is moved to
        `Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>/`
        with a `.meta.json` sidecar.

        Refuses:
        - Sources/, Evidence/ (append-only at any granularity).
        - The `_trash/` subtree (it's already trashed).
        - Curated trees unless `allow_curated=true`.
        - When `confirm=false`.
        - Non-empty directories unless `recursive=true`.
        - When .md files in the tree have EXTERNAL inbound wikilinks
          (i.e. from outside the doomed tree) unless `force_orphan=true`.

        Args:
            path: Vault-relative folder.
            confirm: Must be `true` explicitly.
            recursive: Required for non-empty directories. Acknowledges
                you know it has contents.
            force_orphan: Allow trash even if external inbound wikilinks
                point into the tree.
            allow_curated: Required under curated trees.

        Returns: {path, trash_path, file_count, inbound_link_count, warnings}.
        Errors: UNCONFIRMED; INVALID_PATH; NOT_FOUND; NOT_A_DIR;
                ALREADY_TRASHED; APPEND_ONLY; CURATED_PROTECTED; NOT_EMPTY;
                INBOUND_LINKS; TRASH_FAILED.
        """
        try:
            result = delete_directory_module.delete_directory(
                vault_root,
                path=path,
                confirm=confirm,
                recursive=recursive,
                force_orphan=force_orphan,
                allow_curated=allow_curated,
            )
        except delete_directory_module.DeleteDirectoryError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def append_to_file(
        path: str,
        content: str,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: append text to an existing file.

        Refuses Sources/ (immutable). Allowed on Evidence/ sidecars and
        general vault files. Curated trees need `allow_curated=true`.
        Ensures a single newline boundary between existing tail and new
        content.

        Args:
            path: Vault-relative.
            content: Text to append.
            allow_curated: Required under curated trees.

        Returns: {path, bytes_appended, warnings}.
        Errors: INVALID_APPEND; INVALID_PATH; NOT_FOUND; NOT_A_FILE;
                APPEND_ONLY; CURATED_PROTECTED.
        """
        try:
            result = append_to_file_module.append_to_file(
                vault_root,
                path=path,
                content=content,
                allow_curated=allow_curated,
            )
        except append_to_file_module.AppendError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def get_frontmatter(path: str) -> dict:
        """Tier 2: read only the frontmatter of a file. Read-only.

        Lightweight counterpart to `get` — useful when scanning many files
        ("find all files where status: active and project: substrate")
        without loading bodies.

        Args:
            path: Vault-relative.

        Returns: {path, frontmatter (dict), has_frontmatter (bool)}.
        Errors: INVALID_PATH; NOT_FOUND; NOT_A_FILE; UNREADABLE.
        """
        try:
            result = get_frontmatter_module.get_frontmatter(
                vault_root, path=path
            )
        except get_frontmatter_module.GetFrontmatterError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def set_frontmatter_field(
        path: str,
        field: str,
        value: str | int | float | bool | list | dict | None,
        why: str,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: surgical edit of one frontmatter field. Bumps `updated:`.

        Lighter than `edit` (which rewrites body or tags). Use this when you
        need to change a single field — `status`, `project`, `tenant`,
        `superseded_by`, etc. — without touching the body. Always bumps
        `updated:` to today.

        Refuses Sources/, Evidence/. Curated trees need `allow_curated=true`.
        `why` is required and lands in the log entry.

        Args:
            path: Vault-relative.
            field: Frontmatter key to set. Cannot be `updated` (auto-bumped).
            value: New value. JSON-compatible scalar, list, or dict.
            why: One-line rationale (required — auditable).
            allow_curated: Required under curated trees.

        Returns: {path, field, old_value, new_value, warnings}.
        Errors: INVALID_SET; INVALID_PATH; NOT_FOUND; NOT_A_FILE;
                APPEND_ONLY; CURATED_PROTECTED; UNREADABLE.
        """
        try:
            result = set_frontmatter_field_module.set_frontmatter_field(
                vault_root,
                path=path,
                field=field,
                value=value,
                why=why,
                allow_curated=allow_curated,
            )
        except set_frontmatter_field_module.SetFrontmatterError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def list_trash(date: str | None = None) -> dict:
        """Tier 2: enumerate recoverable trash entries. Read-only.

        Walks Knowledge Base/_trash/YYYY-MM-DD/ and parses each .meta.json
        sidecar. Returns entries most-recent-first with original path,
        timestamp, kind (file or directory), and which force-flags fired
        at trash time. Also surfaces drift: orphan_sidecars (sidecars with
        no target file) and orphan_files (trashed files with no sidecar).
        Pair with `recover_from_trash` to undo.

        Args:
            date: Optional YYYY-MM-DD filter to scope to one day.

        Returns: {entries: [{trash_path, meta_path, original_path,
                 trashed_at, kind, file_count, ...}], count,
                 orphan_sidecars, orphan_files}.
        """
        result = list_trash_module.list_trash(vault_root, date=date)
        return result.as_dict()

    @mcp.tool
    def recover_from_trash(
        trash_path: str,
        restore_path: str | None = None,
        allow_curated: bool = False,
    ) -> dict:
        """Tier 2: undo a delete_file/delete_directory.

        Reads the .meta.json sidecar to discover where the file lived
        before being trashed, moves it back there, and cleans up the
        sidecar. If `restore_path` is provided, uses that instead of the
        sidecar's original location (useful when the original parent
        directory has been removed).

        Refuses to overwrite existing files at the restore destination.
        Refuses restore into Sources/Evidence (append-only). Curated trees
        need `allow_curated=true`.

        Args:
            trash_path: Vault-relative path to the trashed entry
                (under `Knowledge Base/_trash/...`).
            restore_path: Optional override; defaults to the original
                location from the sidecar.
            allow_curated: Required if restoring into a curated tree.

        Returns: {trash_path, restored_path, kind, warnings}.
        Errors: INVALID_PATH; NOT_FOUND; NOT_IN_TRASH; NO_RESTORE_PATH;
                RESTORE_INTO_TRASH; APPEND_ONLY; CURATED_PROTECTED;
                DEST_EXISTS; RECOVER_FAILED.
        """
        try:
            result = recover_from_trash_module.recover_from_trash(
                vault_root,
                trash_path=trash_path,
                restore_path=restore_path,
                allow_curated=allow_curated,
            )
        except recover_from_trash_module.RecoverError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @mcp.tool
    def list_inbound_links(target: str) -> dict:
        """Tier 2: find files whose wikilinks resolve to `target`. Read-only.

        Useful before `move_file` (preview what update_wikilinks will touch)
        or `delete_file` (preview what would break). Matches three forms:
        - Full path: `[[Knowledge Base/Notes/Insights/foo]]`
        - KB-stripped: `[[Notes/Insights/foo]]`
        - Bare basename (only when unique vault-wide): `[[foo]]`

        Args:
            target: Vault-relative path or bare basename. `.md` optional.

        Returns: {target, inbound: [{path, line_number, context, raw_target}],
                 count}.
        Errors: INVALID_TARGET; INVALID_PATH.
        """
        try:
            result = list_inbound_links_module.list_inbound_links(
                vault_root, target=target
            )
        except list_inbound_links_module.ListInboundLinksError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
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
