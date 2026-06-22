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
import secrets
from pathlib import Path

import mcp.types
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import add as add_module
from . import append_to_file as append_to_file_module
from . import audit as audit_module
from . import audit_fix as audit_fix_module
from . import cf_access
from . import compile_proposal as compile_proposal_module
from . import corpus_aware as corpus_aware_module
from . import create_directory as create_directory_module
from . import create_file as create_file_module
from . import delete_directory as delete_directory_module
from . import delete_file as delete_file_module
from . import edit as edit_module
from . import find as find_module
from . import get_frontmatter as get_frontmatter_module
from . import get_page as get_page_module
from . import guards
from . import link as link_module
from . import list_directory as list_directory_module
from . import list_inbound_links as list_inbound_links_module
from . import list_trash as list_trash_module
from . import move_file as move_file_module
from . import multi_edit as multi_edit_module
from . import note as note_module
from . import preserve as preserve_module
from . import project_keys as project_keys_module
from . import provenance as provenance_module
from . import query_data as query_data_module
from . import query_log
from . import reconcile as reconcile_module
from . import recover_from_trash as recover_from_trash_module
from . import replace as replace_module
from . import schema
from . import set_frontmatter_field as set_frontmatter_field_module
from . import set_take as set_take_module
from . import upload_tokens
from .vault import (
    VaultPathError,
    find_body_wikilinks,
    resolve_under_vault,
    resolve_vault,
)


log = logging.getLogger(__name__)
_call_log = logging.getLogger("kb_mcp.calls")

# Text-write tools → the argument field(s) whose value must not be a base64
# binary blob. The model pays for those characters as output tokens before the
# request even arrives, so we reject them at the boundary and point at /upload.
_GUARDED_WRITE_FIELDS = {
    "add": ("content",),
    "note": ("content",),
    "edit": ("new_body", "new_string"),
    "replace": ("content",),
    "create_file": ("content",),
    "append_to_file": ("content",),
    "preserve": ("content",),
}


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
        # Reject base64 binary blobs pushed into text-write tools BEFORE dispatch.
        # Can't refund the output tokens already spent generating the blob — but it
        # stops the vault being polluted and tells the model to use /upload instead.
        guarded_fields = _GUARDED_WRITE_FIELDS.get(tool_name)
        if guarded_fields:
            args = _extract_tool_args(context.message)
            for f in guarded_fields:
                guards.guard_text_content(args.get(f), tool=tool_name, field=f)
            if tool_name == "edit":
                for item in args.get("edits") or []:
                    if isinstance(item, dict):
                        guards.guard_text_content(
                            item.get("new_string"),
                            tool=tool_name,
                            field="edits[].new_string",
                        )
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
            # GitHub rejected the token (expired / revoked / invalid). This is what
            # makes claude.ai re-authorize, so log it (with a timestamp via the log
            # formatter) to diagnose recurring re-auth churn: frequent regular
            # intervals point to GitHub OAuth-App token expiry; clustering around a
            # restart points to the token store / signing key.
            log.info("kb-mcp auth: token rejected by GitHub (expired/revoked/invalid); client will re-authorize")
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

    # The project-key set is open and auto-grows (project_keys.register_project_key
    # appends unknown slugs on first write). Read the live list here so the tool
    # schemas advertise the *current* keys + the auto-register contract, instead
    # of a frozen docstring list that drifts. Re-read on each server start.
    project_keys_hint = project_keys_module.keys_hint(vault_root)

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
        # Reranker is opt-in via find(rerank=True), but the first call
        # without preload is ~30s of HF metadata + load. Preloading
        # alongside the embedder keeps that surprise off the user.
        try:
            from . import embeddings
            log.info("preloading reranker %s", embeddings.RERANKER_NAME)
            embeddings.get_reranker()
            log.info("reranker model ready")
        except Exception as e:  # noqa: BLE001 — preload is best-effort
            log.warning(
                "reranker preload failed (%s); first rerank=True call will "
                "pay the cost", e,
            )

    # Public base URL (e.g. https://kb.substratesystems.io). Used by the OAuth
    # discovery route AND the mint_upload_token tool, so define it unconditionally.
    base_url = os.environ.get("KB_MCP_BASE_URL", "").strip().rstrip("/")
    auth = None
    if require_auth:
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
        # Pin the JWT signing key to an explicit secret when provided. FastMCP
        # otherwise derives it from GITHUB_CLIENT_SECRET, and the encrypted
        # token-store path is fingerprinted from that key — so rotating the secret
        # or a FastMCP reinstall/upgrade that changes the derivation orphans the
        # store and forces claude.ai to re-authorize. An explicit key keeps the
        # signing key (and the store) stable across both. Set KB_MCP_JWT_SIGNING_KEY
        # (any long random string) in .env; leave unset to keep the derived default.
        jwt_signing_key = os.environ.get("KB_MCP_JWT_SIGNING_KEY", "").strip() or None
        if jwt_signing_key is None:
            log.info(
                "KB_MCP_JWT_SIGNING_KEY not set; OAuth signing key derives from the "
                "GitHub client secret, so connector re-auth can recur on secret "
                "rotation or FastMCP upgrades. Set it in .env for a stable connector."
            )
        auth = OAuthProxy(
            upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
            upstream_token_endpoint="https://github.com/login/oauth/access_token",
            upstream_client_id=gh_id,
            upstream_client_secret=gh_secret,
            token_verifier=SingleUserGitHubVerifier(allowed_login=gh_username),
            base_url=base_url,
            jwt_signing_key=jwt_signing_key,
            # GitHub OAuth-App tokens carry no expiry, so FastMCP falls back to a
            # 1-hour access token by default. For a single-user KB reached from a
            # phone over the Funnel, that short TTL forces re-auth churn whenever a
            # client (mobile app especially) can't silently refresh in time. Issue
            # 30-day access tokens instead; the refresh token already never expires,
            # and access is still gated to one GitHub login.
            fallback_access_token_expiry_seconds=30 * 24 * 60 * 60,  # 30 days
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

    # ---- Out-of-band binary upload: the token-free path for binaries ----
    #
    # The model never carries the bytes. A browser / phone / curl / Claude Code
    # POSTs multipart/form-data straight into Evidence/, so a multi-MB file costs
    # ZERO output tokens (unlike base64-through-a-tool-call). See the SKILL
    # "binary evidence" workflow.
    #
    # Reachability: this route is NOT behind the MCP GitHub OAuth (that guards
    # /mcp). It is publicly reachable via cloudflared, so it carries its OWN auth:
    # a bearer token (KB_MCP_UPLOAD_TOKEN) and/or a *signature-verified* Cloudflare
    # Access JWT (KB_MCP_CF_ACCESS_TEAM_DOMAIN + KB_MCP_CF_ACCESS_AUD). With neither
    # configured the route refuses every request (never an open write hole). NOTE:
    # the claude.ai web code sandbox is network-locked and CANNOT reach this route;
    # it's for Hugo's browser / phone / CLI.
    upload_token = os.environ.get("KB_MCP_UPLOAD_TOKEN", "").strip() or None
    upload_max_bytes = int(
        os.environ.get("KB_MCP_UPLOAD_MAX_BYTES", str(preserve_module.MAX_UPLOAD_BYTES))
    )
    cf_team = os.environ.get("KB_MCP_CF_ACCESS_TEAM_DOMAIN", "").strip() or None
    cf_aud = os.environ.get("KB_MCP_CF_ACCESS_AUD", "").strip() or None
    cf_jwks = cf_access.make_jwks_client(cf_team) if (cf_team and cf_aud) else None
    upload_enabled = upload_token is not None or cf_jwks is not None

    def _upload_authorized(request: Request) -> bool:
        # Two independent, non-spoofable credentials. (1) the bearer token,
        # constant-time compared. (2) a Cloudflare Access JWT whose SIGNATURE is
        # verified against the team JWKS (aud / iss / exp) — never the plaintext
        # cf-access-authenticated-user-email header, which is client-spoofable.
        if upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                # long-lived secret, or a short-lived token minted from it
                if secrets.compare_digest(presented, upload_token):
                    return True
                if upload_tokens.verify(presented, upload_token):
                    return True
        if cf_jwks is not None:
            if cf_access.verify(
                request.headers.get("cf-access-jwt-assertion"),
                jwks_client=cf_jwks,
                team_domain=cf_team,
                audience=cf_aud,
            ):
                return True
        return False

    @mcp.custom_route("/upload", methods=["POST"])
    async def _upload(request: Request) -> JSONResponse:
        if not upload_enabled:
            return JSONResponse(
                {
                    "code": "UPLOAD_DISABLED",
                    "reason": "uploads are off: set KB_MCP_UPLOAD_TOKEN (or configure "
                    "Cloudflare Access via KB_MCP_CF_ACCESS_TEAM_DOMAIN + KB_MCP_CF_ACCESS_AUD)",
                },
                status_code=503,
            )
        if not _upload_authorized(request):
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "missing or invalid upload credential"},
                status_code=401,
            )
        try:
            form = await request.form(max_part_size=upload_max_bytes)
        except MultiPartException as e:
            return JSONResponse(
                {
                    "code": "TOO_LARGE",
                    "reason": f"upload rejected (exceeds {upload_max_bytes:,}-byte "
                    f"limit or malformed): {e}",
                },
                status_code=413,
            )
        upload = form.get("file")
        if not hasattr(upload, "read"):
            return JSONResponse(
                {"code": "INVALID_UPLOAD", "reason": "multipart field `file` is required"},
                status_code=400,
            )
        scope = str(form.get("scope") or "").strip()
        category = str(form.get("category") or "").strip()
        description = str(form.get("description") or "").strip() or None
        filename = str(form.get("filename") or "").strip() or (
            getattr(upload, "filename", "") or ""
        )
        data = await upload.read()
        if len(data) > upload_max_bytes:
            return JSONResponse(
                {
                    "code": "TOO_LARGE",
                    "reason": f"{len(data):,} bytes exceeds the "
                    f"{upload_max_bytes:,}-byte upload limit",
                },
                status_code=413,
            )
        try:
            result = preserve_module.preserve_bytes(
                vault_root,
                scope=scope,
                category=category,
                filename=filename,
                data=data,
                description=description,
                max_bytes=upload_max_bytes,
            )
        except preserve_module.PreserveError as e:
            status = {
                "ARTIFACT_EXISTS": 409,
                "TOO_LARGE": 413,
                "INVALID_PRESERVE": 400,
            }.get(e.code, 400)
            return JSONResponse(
                {"code": e.code, "reason": e.reason, "missing": e.missing},
                status_code=status,
            )
        return JSONResponse(result.as_dict(), status_code=201)

    @mcp.custom_route("/upload", methods=["GET"])
    async def _upload_form(request: Request) -> HTMLResponse:
        # Minimal browser/phone uploader. Behind Cloudflare Access it just works;
        # otherwise paste the upload token. scope/category/description prefill from
        # query params so Claude can hand over a ready-to-tap link.
        q = request.query_params

        def _attr(name: str) -> str:
            return (q.get(name) or "").replace('"', "&quot;")

        html = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>kb-mcp upload</title>
<style>body{{font:16px system-ui;max-width:34rem;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.75rem 0 .2rem}}input{{width:100%;padding:.5rem;font:inherit}}
button{{margin-top:1rem;padding:.6rem 1rem;font:inherit}}#out{{margin-top:1rem;white-space:pre-wrap}}</style>
<h1>Add evidence to the KB</h1>
<form id=f>
<label>File <small>(max {upload_max_bytes // (1024 * 1024)} MB; a public link may be capped lower by the proxy)</small></label><input type=file name=file required>
<label>Scope</label><input name=scope value="{_attr('scope')}" placeholder="e.g. Yolo" required>
<label>Category</label><input name=category value="{_attr('category')}" placeholder="e.g. 01 - Check-in" required>
<label>Filename (optional)</label><input name=filename value="{_attr('filename')}">
<label>Description (optional)</label><input name=description value="{_attr('description')}">
<label>Upload token (blank if behind Cloudflare Access)</label><input name=token type=password>
<button type=submit>Upload</button></form>
<div id=out></div>
<script>
f.onsubmit=async e=>{{e.preventDefault();const fd=new FormData(f);const t=fd.get('token');fd.delete('token');
const h={{}};if(t)h['Authorization']='Bearer '+t;out.textContent='Uploading…';
try{{const r=await fetch('/upload',{{method:'POST',body:fd,headers:h}});
out.textContent=r.status+' '+await r.text();}}catch(err){{out.textContent='Error: '+err}}}};
</script>"""
        return HTMLResponse(html)

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
        """Search / find / look up / query / retrieve / recall pages in the Knowledge Base (KB vault): notes, sources, insights, failures, patterns, experiments, entities. Hybrid semantic + keyword search, read-only. Filters are AND'd; tag/project lists are OR'd within.

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
            scope: "kb" (default) searches Knowledge Base/ first and
                AUTO-WIDENS to the whole vault when the KB doesn't fill
                `limit` — so content in sibling folders (Tracking/,
                Reference/, Finance/, ... and curated trees like
                Cognitive Core/, Domains/, Prompt Bank/, Products/,
                Personal Context/) is never silently invisible. Widened
                hits carry `outside_kb: true`. "vault" always walks the
                whole vault. "kb-only" is the strict opt-out: KB only,
                never widens. Outside-KB recall is BM25/keyword (the
                vector sidecar is KB-scoped), with a relaxed gate so terse
                files (e.g. a numbers-heavy tracker) surface on a partial
                token match. `_Schema/`, `_trash/`, `_attachments/`, and
                `.obsidian/` are excluded under every scope. NOTE: an
                empty result means "not found in what I searched," NOT
                "doesn't exist" — say so, and try "vault" before
                concluding absence.
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
            List of {path, type, scope, title, updated, excerpt[, outside_kb]
            [, signals]}. `outside_kb: true` is present only on hits the
            "kb" auto-widen pulled from beyond Knowledge Base/ (the `path`
            also shows the sibling folder).
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
        # Durable structured log → feeds the offline retrieval feedback loop.
        # Best-effort; never affects the returned result.
        query_log.log_find_call(
            query=query, mode=mode, scope=scope,
            types=types, projects=projects, tags=tags,
            limit=limit, rerank=rerank, prefer_compiled=prefer_compiled,
            graph=graph, hits=hits,
        )
        return [h.as_dict() for h in hits]

    @mcp.tool
    def suggest_links(
        path: str | None = None,
        draft_title: str | None = None,
        draft_body: str | None = None,
        limit: int = 8,
        scope: str = "kb",
    ) -> list[dict]:
        """Suggest existing KB pages a note should link to. Read-only.

        Closes the corpus-blind-write gap: surfaces the related prior work a
        draft (or an existing page) should connect to, so the graph gets denser
        with every write instead of just bigger. For link suggestions only — it
        reuses the same hybrid ranker as `find`, prefers well-connected hubs, and excludes
        the page itself plus anything it already links. Suggestions are
        non-binding: YOU decide which to wire in (e.g. via a follow-up `edit`).

        Two call shapes:
        - `path`: suggest links for an EXISTING page (densify it retroactively).
          Same path conventions as `get`/`find`.
        - `draft_title` + `draft_body`: suggest links for a note you're about to
          create, BEFORE calling `note` — so you can cite/connect on first write.

        Args:
            path: Existing page to suggest links for. Mutually exclusive with
                the draft_* args.
            draft_title: Title of a not-yet-written note.
            draft_body: Body (markdown) of a not-yet-written note. Wikilinks
                already present in it are treated as "already linked" and excluded.
            limit: Max suggestions (default 8).
            scope: "kb" (default) or "vault" — same meaning as `find`.

        Returns:
            List of {path, title, type, why, excerpt}, best-first. `why`
            explains the match (e.g. "semantic #2, 4 shared link(s) (hub)").
            Empty list if nothing relevant or the draft/page is empty.

        Errors:
            INVALID_SUGGEST (neither path nor draft supplied); plus get-style
            path errors (NOT_FOUND, INVALID_PATH) when `path` doesn't resolve.
        """
        if path:
            try:
                gp = get_page_module.get_page(vault_root, path=path)
            except get_page_module.GetError as e:
                raise ValueError(f"{e.code}: {e.reason}") from e
            page = find_module._CACHE.get(vault_root / gp.path, vault_root)
            if page is None:
                raise ValueError(f"UNREADABLE: could not parse {gp.path}")
            existing_links = set(
                find_module._outbound_wikilink_paths(page, vault_root)
            )
            suggestions = corpus_aware_module.suggest_related(
                vault_root, title=page.title, body=page.body,
                self_path=page.rel_path, existing_links=existing_links,
                limit=limit, scope=scope,
            )
        elif draft_title or draft_body:
            body = draft_body or ""
            existing_links = set()
            for m in find_body_wikilinks(body):
                inner = m.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
                if inner:
                    existing_links.add(inner)
            suggestions = corpus_aware_module.suggest_related(
                vault_root, title=draft_title or "", body=body,
                self_path=None, existing_links=existing_links,
                limit=limit, scope=scope,
            )
        else:
            raise ValueError(
                "INVALID_SUGGEST: provide either `path` (existing page) or "
                "`draft_title`/`draft_body` (a note you're about to write)"
            )
        return [s.as_dict() for s in suggestions]

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
            content: Full text body to capture (markdown / plain text). For
                files or binaries, use the /upload endpoint instead.
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
        query_log.log_write_call(tool="add", written_path=result.path, cited_sources=[])
        return result.as_dict()

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
        project_category: str | None = None,
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
            project: REQUIRED for research-note. __PROJECT_KEYS_HINT__
            projects: List of project keys (plural). Optional for insight,
                failure, pattern, production-log. __PROJECT_KEYS_HINT__
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
                project_category=project_category,
            )
        except note_module.NoteError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        query_log.log_write_call(
            tool="note", written_path=result.path, cited_sources=sources
        )
        return result.as_dict()

    # Inject the live project-key list/contract into the schema the model sees,
    # then register. Done post-definition (not via `@mcp.tool`) because the hint
    # is computed at runtime; `mcp.tool(fn)` is exactly what the bare decorator
    # does. `.replace` (not `.format`) — the docstring has literal `{...}` braces.
    note.__doc__ = note.__doc__.replace("__PROJECT_KEYS_HINT__", project_keys_hint)
    mcp.tool(note)

    @mcp.tool
    def audit(categories: list[str] | None = None) -> dict:
        """Audit / lint / health-check the Knowledge Base: find orphans, broken wikilinks, supersession gaps, and stale unprocessed sources. Read-only.

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
    def reconcile(dry_run: bool = False) -> dict:
        """Heal vault drift from out-of-band edits in one pass.

        The writers keep the embedding sidecar, index.md count rows, and log.md
        current on every write. But editing the vault directly — in Obsidian,
        on mobile, or via a manual filesystem edit — bypasses those hooks, so
        the sidecar and the counts drift silently. `reconcile` is the
        first-class "I edited around the system, fix it" command:

        1. Index counts — recompute Sources/Notes/Entities count rows from
           on-disk reality and rewrite any that drifted (curated descriptions
           and Recent-activity are preserved; only count tokens move).
        2. Embeddings — incrementally re-embed only the *stale* files (those
           `embedding_drift` flags: on-disk mtime newer than the sidecar row),
           via the same path the writers use. Cheaper than
           `audit_fix(rebuild_embeddings=true)`'s full wipe-and-rebuild.
        3. Drift report — re-run index_drift + embedding_drift, return what
           remains.

        Narrower than `audit_fix`: it does NOT canonicalize wikilinks or
        backfill frontmatter (those are content rewrites you opt into).
        Idempotent; `dry_run=true` reports without writing.

        Args:
            dry_run: If true, compute what would change without writing.
                Default false.

        Returns:
            {indexes_updated: [<index path>, ...],
             embeddings_refreshed: int,
             embeddings_status: "current" | "refreshed" | "disabled",
             remaining_drift: [<audit findings>],
             dry_run: bool}
        """
        report = reconcile_module.reconcile(vault_root, dry_run=dry_run)
        return report.as_dict()

    @mcp.tool
    def provenance_report(
        tag: str | None = None,
        key: str | None = None,
        value: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Trace provenance: scan note bodies for `<!-- key:value -->` tags — where an opinion/take/flag came from. Read-only.

        On-demand scan over markdown bodies — no index, no sidecar. Use it to
        answer "show all conv:-derived takes" or "what's flagged add-to-imdb"
        without grepping. The opinion/taste rows carry provenance as HTML
        comments (e.g. `<!-- platform:imdb -->`, `<!-- conv:2026-06-01 -->`);
        this reads them in place. Tags inside fenced code are ignored; multiple
        comments and multiple key:value pairs on one line are all parsed.

        Args:
            tag: Shorthand filter — "key" or "key:value" (e.g. "platform:imdb").
            key: Filter to rows carrying this provenance key.
            value: With key, require this exact value.
            path: Restrict the scan to one vault-relative file (else the whole
                Knowledge Base is walked).

        Returns:
            {findings: [{path, line_number, row_text, tags}], summary:
             {key: count}}. line_number is body-relative (frontmatter excluded).
        """
        findings = provenance_module.scan_provenance(
            vault_root, tag=tag, key=key, value=value, path=path
        )
        summary: dict[str, int] = {}
        for f in findings:
            for k in f.tags:
                summary[k] = summary.get(k, 0) + 1
        return {"findings": [f.as_dict() for f in findings], "summary": summary}

    @mcp.tool
    def propose_compilation(
        sources: list[str],
        suggested_title: str | None = None,
    ) -> dict:
        """Draft / scaffold a compiled note from unprocessed source(s) — what to compile next, drain the source backlog. Read-only.

        The backlog-drain companion to `audit`'s `unprocessed_source` findings:
        point it at one or more raw sources and it hands back a ready-to-fill
        note skeleton — inferred note_type, a Question/Findings/Connections (or
        Claim/…) outline, the `sources[]` to cite, and adjacent compiled pages to
        link (computed via the same hybrid retrieval as `suggest_links`). It
        does NOT write anything: you fill the prose and call `note()` with the
        returned `suggested_sources` + `suggested_connections`.

        Group sources yourself before calling — pass a set that genuinely belongs
        in one note (the audit list is aged oldest-first to help you triage).

        Args:
            sources: Vault-relative paths/wikilinks to the source(s) to compile.
                Same path conventions as `note.sources` (brackets and the
                leading `Knowledge Base/` are tolerated).
            suggested_title: Optional title override; otherwise one is derived
                from the source titles.

        Returns:
            {suggested_note_type, suggested_title, suggested_sources,
             suggested_connections, outline_markdown, warnings}.

        Errors:
            INVALID_PROPOSE (no sources); SOURCES_NOT_FOUND (none resolved).
        """
        try:
            return compile_proposal_module.propose_compilation(
                vault_root, sources=sources, suggested_title=suggested_title
            )
        except compile_proposal_module.ProposeError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e

    @mcp.tool
    def get(path: str, frontmatter_only: bool = False) -> dict:
        """Read / open / fetch / load the full contents of a KB or vault page by path. Returns frontmatter + body + raw content.

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
            frontmatter_only: If true, return ONLY the frontmatter (no body) —
                cheap for scanning many files by field (folds in the former
                `get_frontmatter` tool). Returns {path, frontmatter,
                has_frontmatter} instead of the full page below.

        Returns:
            {path, frontmatter, body, content, content_hash, mtime}.
            `content` is the raw file text (including frontmatter delimiters);
            `body` is just the markdown after the frontmatter. `content_hash`
            is a sha256 you can echo back to `edit`/`multi_edit` via
            `expected_hash` to refuse a write if the file changed on disk since
            this read (two-writer drift guard); `mtime` is advisory.

        Errors:
            INVALID_PATH (path escapes vault root or empty);
            NOT_FOUND (no such file); UNREADABLE (parse failure).
        """
        if frontmatter_only:
            try:
                result = get_frontmatter_module.get_frontmatter(
                    vault_root, path=path
                )
            except get_frontmatter_module.GetFrontmatterError as e:
                raise ValueError(f"{e.code}: {e.reason}") from e
            return result.as_dict()
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
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        edits: list[dict] | None = None,
        row_key: str | None = None,
        take: str | None = None,
        overwrite: bool = False,
        field: str | None = None,
        value: str | int | float | bool | list | dict | None = None,
        allow_curated: bool = False,
        expected_hash: str | None = None,
        validate_only: bool = False,
    ) -> dict:
        """Lightweight in-place edit of a page (body, tags, a surgical snippet,
        a batch, an opinion row, or one frontmatter field).

        For tweaks — typo fixes, filling a row, appending one line, tag
        corrections — without going through full supersession via `replace`.
        Use `replace` for substantial rewrites; use `edit` when creating a new
        file + superseded-link chain would be silly for what you're changing.

        One mode per call. Three param-selected modes fold in former tools:
        - `edits=[...]` -> batch surgical edits in one atomic commit (was the
          `multi_edit` tool). Each item {old_string, new_string, replace_all?}
          applies sequentially.
        - `row_key=...` + `take=...` -> fill a `[take: ]` opinion row by its
          leading text without re-sending the body (was `set_take`).
        - `field=...` + `value=...` -> patch ONE frontmatter field; pass
          `allow_curated=true` for curated trees (was `set_frontmatter_field`).
        Otherwise the default (composable) body/tags/surgical modes:
        - `new_body` — replace the WHOLE body. Heavyweight; you re-send
          everything after the frontmatter.
        - `tags` — replace the `tags:` frontmatter field.
        - `old_string`/`new_string` — **surgical** string-replace inside the
          body. Token-cheap: send only the changed snippet, not the whole
          page. Ideal for filling a `[take: ]` row or appending one opinion
          (replace a section heading with itself + the new line). `updated:`
          is always bumped to today.

        Surgical-mode rules (mirrors a precise find-and-replace):
        - `old_string` must match the file EXACTLY, including whitespace.
        - By default it must occur exactly once — an ambiguous match is an
          error (AMBIGUOUS_MATCH) so you never edit the wrong row. Pass
          `replace_all=True` to replace every occurrence.
        - Cannot be combined with `new_body` (both rewrite the body); may be
          paired with `tags`.
        - Only the inserted snippet gets wikilink-normalized; the rest of the
          body is left byte-for-byte untouched.

        What stays in all modes:
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
            old_string: Exact snippet to find in the body (surgical mode).
            new_string: Replacement snippet (required with old_string; must
                differ from it).
            replace_all: Replace every occurrence instead of requiring a
                unique match. Default False.
            edits: Batch-surgical mode — list of {old_string, new_string,
                replace_all?} applied sequentially in one atomic commit.
            row_key: Take-row mode — natural leading text of the row to fill
                (e.g. "Whiplash (2014)"). Requires `take`.
            take: Text to write between `[take:` and `]` (take-row mode).
            overwrite: In take-row mode, also replace an already-filled take.
            field: Frontmatter-patch mode — the single frontmatter key to set
                (cannot be `updated`, which is auto-bumped).
            value: New value for `field` (scalar/list/dict).
            allow_curated: Allow a frontmatter patch under a curated tree.
            expected_hash: Optional drift guard. Pass the `content_hash` you
                got from `get`; the edit refuses (STALE_EDIT) if the file
                changed on disk since, so you never clobber another writer.
            validate_only: Preview a surgical match without writing. Needs
                `old_string`. Reports how many rows would be hit instead of
                committing — use it before a `replace_all` to avoid an
                ambiguous match silently touching more rows than intended.

        Returns:
            Shape varies by mode (take-row -> {path, row, warnings};
            frontmatter-patch -> {path, field, old_value, new_value, warnings};
            batch -> {path, edits_applied, warnings}). Default mode normally
            {path, warnings}. When validate_only=True:
            {path, validate_only, mode, match_count, matches} — `matches` is
            the line(s) around each occurrence; nothing is written.

        Errors:
            INVALID_EDIT (nothing to edit, old_string+new_body both given,
            new_string missing/equal, path in Sources/Evidence); NOT_FOUND;
            STRING_NOT_FOUND (surgical snippet absent); AMBIGUOUS_MATCH
            (snippet not unique and replace_all=False); ALREADY_SUPERSEDED;
            STALE_EDIT (expected_hash mismatch — file changed since read);
            UNREADABLE.
        """
        active = [n for n, on in (
            ("edits", edits is not None),
            ("row_key", row_key is not None),
            ("field", field is not None),
        ) if on]
        if len(active) > 1:
            raise ValueError(
                f"INVALID_EDIT: one edit mode at a time; got {', '.join(active)}"
            )
        try:
            if edits is not None:
                result = multi_edit_module.multi_edit(
                    vault_root, path=path, why=why, edits=edits,
                    expected_hash=expected_hash, validate_only=validate_only,
                )
            elif row_key is not None:
                if take is None:
                    raise ValueError("INVALID_EDIT: row_key mode requires `take`")
                result = set_take_module.set_take(
                    vault_root, path=path, row_key=row_key, take=take,
                    why=why, overwrite=overwrite,
                )
            elif field is not None:
                result = set_frontmatter_field_module.set_frontmatter_field(
                    vault_root, path=path, field=field, value=value,
                    why=why, allow_curated=allow_curated,
                )
            else:
                result = edit_module.edit(
                    vault_root, path=path, why=why, new_body=new_body,
                    tags=tags, old_string=old_string, new_string=new_string,
                    replace_all=replace_all, expected_hash=expected_hash,
                    validate_only=validate_only,
                )
        except (
            edit_module.EditError,
            set_take_module.SetTakeError,
            set_frontmatter_field_module.SetFrontmatterError,
        ) as e:
            msg = f"{e.code}: {e.reason}"
            if getattr(e, "missing", None):
                msg += f" (missing: {e.missing})"
            if getattr(e, "candidates", None):
                msg += f" (candidates: {e.candidates})"
            raise ValueError(msg) from e
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
        project_category: str | None = None,
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
                project_category=project_category,
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
        query_log.log_write_call(
            tool="replace", written_path=result.new_path, cited_sources=sources
        )
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
          `project` (project key — any slug; unknown keys auto-register on first
          use, same as `note`), `decision_status` ∈ {proposed, accepted,
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
        content: str,
        description: str | None = None,
    ) -> dict:
        """Capture a TEXT artifact to Evidence/<scope>/<category>/.

        For raw factual artifacts that are text — transcripts, pasted letters,
        email bodies — preserved as-received with no analytical processing. Per
        SKILL.md rule 2, Evidence is append-only; analytical takes go in compiled
        notes that link to the evidence file.

        BINARY artifacts (PDFs, images, .docx — any non-text file) are delivered
        out-of-band, not through this tool: call `mint_upload_token` and POST the
        bytes to `/upload`, or drop the file into Evidence/ desk-side via Obsidian
        Sync. The bytes never pass through the model.

        Args:
            scope: Incident or domain key (e.g. "Yolo", "Mother Cancer").
                Creates the subfolder if it doesn't exist.
            category: Sub-category within scope (e.g. "letters", "labs",
                "court-docs"). Creates the subfolder if it doesn't exist.
            filename: The artifact's filename including extension
                (e.g. `2026-04-15-statement.txt`).
            content: UTF-8 text to preserve as-received.
            description: Optional. If supplied, a sidecar `<filename>.md`
                is written alongside the artifact with frontmatter and the
                description under `## Description`.

        Returns:
            {path, sidecar_path, warnings}.

        Errors:
            INVALID_PRESERVE (missing required); ARTIFACT_EXISTS (file already
            exists — Evidence is append-only, pick a new filename).
        """
        try:
            result = preserve_module.preserve(
                vault_root,
                scope=scope,
                category=category,
                filename=filename,
                content=content,
                description=description,
            )
        except preserve_module.PreserveError as e:
            raise ValueError(
                f"{e.code}: {e.reason} (missing: {e.missing})"
            ) from e
        return result.as_dict()

    @mcp.tool
    def mint_upload_token() -> dict:
        """Mint a short-lived bearer token for the HTTP `/upload` endpoint.

        This is how you file BINARY evidence (images, PDFs, any file) from a
        claude.ai web session — the bytes travel out-of-band, never through the
        model. Steps:

        1. Call this tool → `{token, ttl_seconds, upload_url}`.
        2. In the code sandbox, multipart-POST the user's ATTACHED files to
           `upload_url` with header `Authorization: Bearer <token>` and form
           fields `file`, `scope`, `category` (optional `filename`, `description`).
           Files must be ATTACHMENTS — inline-pasted images never reach the
           sandbox disk and cannot be sent.
        3. Write the searchable text manifest separately via `preserve` / `note`.

        The token is Evidence-write only and expires after `ttl_seconds`; the
        server's long-lived secret never leaves the server.

        Returns: {token, ttl_seconds, upload_url}.
        Raises: UPLOAD_DISABLED if the server has no upload token configured.
        """
        return upload_tokens.mint_for_endpoint(upload_token, base_url)

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
    #
    # Tier 2 is opt-out: setting KB_MCP_DISABLE_TIER2 drops these 13 escape-hatch
    # tools from the registered surface, shrinking the deferred-tool list a client
    # must keyword-search to reach the Tier 1 read/write ops. The functions are
    # still defined either way; `tier2_tool` only decides whether to register.
    _expose_tier2 = not os.environ.get("KB_MCP_DISABLE_TIER2")

    def tier2_tool(fn):
        return mcp.tool(fn) if _expose_tier2 else fn

    @tier2_tool
    def query_data(
        path: str,
        record_path: str | None = None,
        filters: list[dict] | None = None,
        columns: list[str] | None = None,
        sort_by: str | None = None,
        descending: bool = False,
        limit: int = 100,
        offset: int = 0,
        aggregate: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date_column: str | None = None,
    ) -> dict:
        """Tier 2: structured query over a CSV/JSON data file under the vault.

        The retrieval half of the data-search pattern — `find` surfaces a
        dataset's markdown "card"; this reads the raw file the card points at
        and returns exact rows / aggregates (no whole-file dump). KB datasets
        are small, so it reads on demand — no index, no new infra.

        Formats: CSV / TSV, and JSON (a top-level array, or a nested array via
        `record_path` / common-key auto-detect). Column names may be dotted to
        reach nested JSON fields (e.g. "performer.name", "id.extension")
        anywhere a column is named (filters / columns / sort / aggregate).

        Args:
            path: vault-relative path to the `.csv` / `.tsv` / `.json` file.
            record_path: (JSON) dotted path to the array inside a nested
                object, e.g. "sections.work_incapacity". Omit for a top-level
                array or the common keys result/results/data/rows/items/entries.
            filters: list of `{column, op, value}`. `op` ∈ eq, ne, gt, gte, lt,
                lte, contains, icontains, startswith, in, nin, exists, missing.
                Numeric compares coerce tolerantly (comma decimals; lab
                operators like "<0.4"/">75" are stripped for the comparison).
            columns: project to these columns (dotted ok). Omit for all.
            sort_by / descending: sort by a column (numeric-aware).
            limit / offset: pagination (limit default 100, hard cap 1000).
            aggregate: instead of rows — "count", or "func:column" where func ∈
                min, max, sum, avg, latest, distinct.
            date_from / date_to / date_column: convenience date-range filter on
                `date_column` (defaults to a "date" column if present); ISO
                date strings, compared lexicographically.

        Returns:
            {path, format, total_rows, total_matched, returned, columns, rows,
             aggregate, truncated, warnings}.

        Errors: INVALID_PATH / NOT_FOUND (path); UNSUPPORTED_FORMAT; TOO_LARGE;
            BAD_JSON; BAD_RECORD_PATH; BAD_FILTER; BAD_OP; BAD_AGGREGATE.
        """
        try:
            result = query_data_module.query_data(
                vault_root,
                path=path,
                record_path=record_path,
                filters=filters,
                columns=columns,
                sort_by=sort_by,
                descending=descending,
                limit=limit,
                offset=offset,
                aggregate=aggregate,
                date_from=date_from,
                date_to=date_to,
                date_column=date_column,
            )
        except query_data_module.QueryDataError as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @tier2_tool
    def create_file(
        path: str,
        content: str = "",
        frontmatter: dict | None = None,
        overwrite: bool = False,
        allow_curated: bool = False,
        kind: str = "file",
        parents: bool = True,
    ) -> dict:
        """Tier 2: write a file — or, with `kind="dir"`, create a folder — at an
        arbitrary vault path.

        With `kind="dir"`, this creates a folder (mkdir -p when `parents=true`)
        and ignores `content`/`frontmatter`/`overwrite` (folds in the former
        `create_directory` tool); returns {path, created, warnings}.

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
            content: File body (or full file if `frontmatter` is None). Text
                only; for binaries use the /upload endpoint.
            frontmatter: Optional dict prepended as YAML frontmatter.
            overwrite: If true, replace existing file. Default false.
            allow_curated: Required to write under a curated tree. Default false.
            kind: "file" (default) or "dir". With "dir", creates a folder
                instead of a file (former `create_directory`).
            parents: In "dir" mode, create intermediate folders (mkdir -p).
                Default true.

        Returns: {path, warnings} for files; {path, created, warnings} for dirs.
        Errors: INVALID_PATH; APPEND_ONLY; CURATED_PROTECTED; FILE_EXISTS;
                NOT_A_FILE; (dir mode) NOT_A_DIR; MISSING_PARENT; MKDIR_FAILED.
        """
        if kind == "dir":
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

    @tier2_tool
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

    @tier2_tool
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

    @tier2_tool
    def delete(
        path: str,
        confirm: bool,
        recursive: bool = False,
        force_orphan: bool = False,
        force_superseded: bool = False,
        allow_curated: bool = False,
        expected_dead_inbound: list[str] | None = None,
    ) -> dict:
        """Tier 2: trash a file OR folder (auto-detected). Reversible — moves to
        _trash/, not /dev/null.

        Dispatches on the path: a directory is trashed whole (needs
        `recursive=true` if non-empty; folds in the former `delete_directory`),
        otherwise a single file. `force_superseded`/`expected_dead_inbound`
        apply to files; `recursive` applies to folders.

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
            recursive: For a non-empty FOLDER, required to confirm you know it
                has contents. Ignored for files.
            force_orphan: Allow trash even if inbound wikilinks exist.
            force_superseded: Allow trash of a file in the supersession chain.
            allow_curated: Required to trash under a curated tree.
            expected_dead_inbound: Vault-relative paths whose inbound links
                to this file should be ignored. Use when you're trashing
                multiple files in one workflow (e.g. cleaning a supersession
                chain) and don't want each step to false-positive on
                links that will die in the same batch.

        Returns (file): {path, trash_path, inbound_link_count,
                inbound_ignored_count, warnings}.
        Returns (dir): {path, trash_path, file_count, inbound_link_count,
                warnings}.
        Errors: UNCONFIRMED; INVALID_PATH; NOT_FOUND; ALREADY_TRASHED;
                APPEND_ONLY; CURATED_PROTECTED; SUPERSEDED_HISTORY;
                INBOUND_LINKS; TRASH_FAILED; (dir) NOT_A_DIR; NOT_EMPTY.
        """
        try:
            abs_path, _rel = resolve_under_vault(vault_root, path)
            is_dir = abs_path.is_dir()
        except VaultPathError:
            is_dir = False  # let the file backend raise the precise path error
        try:
            if is_dir:
                result = delete_directory_module.delete_directory(
                    vault_root,
                    path=path,
                    confirm=confirm,
                    recursive=recursive,
                    force_orphan=force_orphan,
                    allow_curated=allow_curated,
                )
            else:
                result = delete_file_module.delete_file(
                    vault_root,
                    path=path,
                    confirm=confirm,
                    force_orphan=force_orphan,
                    force_superseded=force_superseded,
                    allow_curated=allow_curated,
                    expected_dead_inbound=expected_dead_inbound,
                )
        except (
            delete_file_module.DeleteFileError,
            delete_directory_module.DeleteDirectoryError,
        ) as e:
            raise ValueError(f"{e.code}: {e.reason}") from e
        return result.as_dict()

    @tier2_tool
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
            content: Text to append (text only; binaries go via /upload).
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

    @tier2_tool
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

    @tier2_tool
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

    @tier2_tool
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
    host: str | None = None,
    port: int = 8765,
    log_dir: Path | None = None,
) -> None:
    """CLI entry: configure logging, build the server, run it.

    Auth is required for HTTP transports; stdio runs auth-free.

    Bind host precedence: $KB_MCP_HOST > the passed `host` > 127.0.0.1. The env var
    wins (and is resolved AFTER build_server() loads .env) so the deployment can flip
    the bind from .env without changing the service's launch args. Set
    KB_MCP_HOST=0.0.0.0 to also serve a non-Cloudflare route (e.g. a direct Tailscale
    connection to the origin) for uploads larger than the Cloudflare edge cap.
    """
    from .logging_config import configure_logging

    if log_dir is None:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
    configure_logging(log_dir)

    require_auth = transport != "stdio"
    mcp = build_server(require_auth=require_auth)  # loads .env (KB_MCP_HOST, etc.)

    if transport == "stdio":
        log.info("kb-mcp starting on stdio")
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("KB_MCP_HOST") or host or "127.0.0.1"
        log.info("kb-mcp starting on %s host=%s port=%s", transport, host, port)
        mcp.run(transport=transport, host=host, port=port)
