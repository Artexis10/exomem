"""FastMCP server construction: tool registration + GitHub OAuth + transports.

Tools (registered in build_server):
- `find` — read-only hybrid search across Knowledge Base/
- `add`  — capture a `source` page with full SKILL.md rule-7 writes
- ~20 more (edit / note / preserve / audit / link / reconcile / …)

Auth (HTTP transports):
- GitHub OAuth via FastMCP `OAuthProxy`. `SingleUserGitHubVerifier` gates access
  to a single GitHub login (`EXOMEM_GITHUB_USERNAME`); any other login is rejected.
- Requires `EXOMEM_BASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
  `EXOMEM_GITHUB_USERNAME` (build_server raises if any are missing). Optional
  `EXOMEM_JWT_SIGNING_KEY` pins the token-store signing key for a stable
  connector across restarts / FastMCP upgrades.
- stdio needs no auth; OAuth is only wired in when require_auth is set (HTTP).

Transports:
- stdio (local; no auth needed)
- streamable-http aka http (public; GitHub OAuth required)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import mcp.types
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

from . import cf_access, cli_ops, extract, guards, schema, upload_tokens
from . import commands as commands_module
from . import preserve as preserve_module
from . import project_keys as project_keys_module
from .vault import (
    VaultPathError,
    resolve_under_vault,
    resolve_vault,
)

log = logging.getLogger(__name__)
_call_log = logging.getLogger("exomem.calls")

# Text-write tools → the argument field(s) whose value must not be a base64
# binary blob. The model pays for those characters as output tokens before the
# request even arrives, so we reject them at the boundary and point at /upload.
# Single source of truth in the command registry (MCP write boundary + REST coercion).
_GUARDED_WRITE_FIELDS = commands_module.GUARDED_WRITE_FIELDS


class CallTraceMiddleware(Middleware):
    """Per-call traceability: log every tool invocation with name + duration.

    Service-log only (`logs/exomem.log`). The durable content history lives
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


class _RestJSONResponse(JSONResponse):
    """JSONResponse that renders frontmatter dates (datetime.date) as ISO strings.

    YAML parses `created: 2026-01-01` to a `datetime.date`, which Starlette's default
    json.dumps can't serialize. The MCP transport sidesteps this via pydantic; the
    REST facade needs its own `default=str` fallback.
    """

    def render(self, content) -> bytes:  # noqa: ANN001
        return json.dumps(
            content, ensure_ascii=False, allow_nan=False, default=str
        ).encode("utf-8")


# Single definition in the command registry; used by the REST `get` route.
_link_summary = commands_module._link_summary


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
    """Reject any GitHub token whose login isn't the allowed user.

    Caches *validated* tokens for a short TTL (``EXOMEM_AUTH_CACHE_TTL`` seconds,
    default 300). Without it every MCP request — and a single connector operation
    fires several (ListTools + ListResources + ListPrompts + CallTool) — re-hits the
    GitHub API to revalidate the same token, adding seconds of `api.github.com`
    round-trips to the hot path. GitHub OAuth-App tokens carry no time-expiry, so the
    only invalidation is a deliberate revoke / secret rotation, and a re-authorized
    client presents a *new* token (a new cache key) that validates immediately — so
    the TTL only bounds how long a revoked-but-still-presented token keeps working,
    which doesn't happen for a single user. Only successes are cached (a rejection is
    re-checked every call, so recovery is instant); set the TTL to 0 to disable.
    """

    _DEFAULT_TTL = 300.0
    _MAX_ENTRIES = 64  # single user → tiny; cap guards against unbounded distinct tokens

    def __init__(self, *, allowed_login: str, **kwargs):
        super().__init__(**kwargs)
        self._allowed_login = allowed_login.lower()
        # NOTE: this MUST NOT be named ``_cache`` — the parent GitHubTokenVerifier
        # owns ``self._cache`` (a fastmcp ``TokenCache``) and unpacks its
        # ``.get()`` as a 2-tuple inside ``super().verify_token``. Shadowing it
        # with a plain dict made that line raise "cannot unpack non-iterable
        # NoneType object", failing the OAuth token-swap → 401 on every request.
        self._login_cache: dict[str, tuple[float, AccessToken]] = {}

    def _ttl(self) -> float:
        raw = os.environ.get("EXOMEM_AUTH_CACHE_TTL")
        if raw is None:
            return self._DEFAULT_TTL
        try:
            return max(0.0, float(raw))
        except ValueError:
            return self._DEFAULT_TTL

    async def verify_token(self, token: str) -> AccessToken | None:
        ttl = self._ttl()
        key = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        if ttl > 0 and key:
            hit = self._login_cache.get(key)
            if hit is not None:
                ts, cached = hit
                if time.monotonic() - ts < ttl:
                    return cached
                del self._login_cache[key]  # expired

        access = await super().verify_token(token)
        if access is None:
            # GitHub rejected the token (expired / revoked / invalid). This is what
            # makes claude.ai re-authorize, so log it (with a timestamp via the log
            # formatter) to diagnose recurring re-auth churn: frequent regular
            # intervals point to GitHub OAuth-App token expiry; clustering around a
            # restart points to the token store / signing key.
            log.info("exomem auth: token rejected by GitHub (expired/revoked/invalid); client will re-authorize")
            return None
        login = (access.claims.get("login") or "").lower()
        if login != self._allowed_login:
            log.warning("rejecting token for github login=%r", login)
            return None

        if ttl > 0 and key:
            now = time.monotonic()
            if len(self._login_cache) >= self._MAX_ENTRIES:
                # Drop expired entries before inserting; cheap, the cache is tiny.
                self._login_cache = {
                    k: v for k, v in self._login_cache.items() if now - v[0] < ttl
                }
            self._login_cache[key] = (now, access)
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
    # EXOMEM_DISABLE_EMBEDDINGS to skip this entirely.
    if not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
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
        # Preload CLIP too so the first image-aware find()/upload doesn't pay the load.
        if embeddings.clip_enabled():
            try:
                log.info("preloading CLIP model %s", embeddings.CLIP_MODEL_NAME)
                embeddings.get_clip_model()
                log.info("CLIP model ready")
            except Exception as e:  # noqa: BLE001 — preload is best-effort
                log.warning("CLIP preload failed (%s); first image query will pay the cost", e)

    # Warm the lexical/derived caches too (parsed pages, BM25 corpora for both
    # scopes, wikilink resolver, embedding/CLIP matrices when enabled) so the
    # first find doesn't pay the first-build stemming cliff. Best-effort;
    # EXOMEM_DISABLE_WARMUP skips it.
    try:
        from . import warmup
        warmup.warm_caches(vault_root)
    except Exception as e:  # noqa: BLE001 — warm-up is best-effort
        log.warning("cache warm-up failed (%s); first find pays the cost", e)

    # Media-extraction worker: transcribes/OCRs binaries uploaded without text, off
    # the request path, and fills their sidecars (then re-embeds). Disabled in tests
    # and on lean boxes via EXOMEM_DISABLE_MEDIA_EXTRACTION (mirrors the embeddings flag).
    media_worker = None
    if extract.extraction_enabled():
        from . import media_worker as media_worker_module

        media_worker = media_worker_module.MediaWorker(vault_root)
        media_worker.start()
        try:
            media_worker.scan_pending()  # re-enqueue anything a prior run left pending
        except Exception as e:  # noqa: BLE001 — startup scan is best-effort
            log.warning("media worker startup scan failed: %s", e)

    # Live file-watcher: re-embed out-of-band Obsidian/mobile/filesystem `.md` edits in
    # ~1s instead of waiting for a manual `reconcile`. Off when EXOMEM_DISABLE_FILE_WATCHER
    # is set, and also when embeddings are disabled (no point watching if we can't embed —
    # the test suite sets EXOMEM_DISABLE_EMBEDDINGS, so the watcher never starts in tests).
    file_watcher = None
    if not os.environ.get("EXOMEM_DISABLE_FILE_WATCHER") and not os.environ.get(
        "EXOMEM_DISABLE_EMBEDDINGS"
    ):
        from . import file_watcher as file_watcher_module

        file_watcher = file_watcher_module.FileWatcher(vault_root)
        try:
            file_watcher.start()  # soft no-op if watchdog isn't installed
        except Exception as e:  # noqa: BLE001 — the watcher must never break startup
            log.warning("file watcher start failed: %s", e)

    # Public base URL (e.g. https://kb.example.com). Used by the OAuth
    # discovery route AND the mint_upload_token tool, so define it unconditionally.
    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    auth = None
    if require_auth:
        gh_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
        gh_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
        gh_username = os.environ.get("EXOMEM_GITHUB_USERNAME", "").strip()
        missing = [
            k for k, v in {
                "EXOMEM_BASE_URL": base_url,
                "GITHUB_CLIENT_ID": gh_id,
                "GITHUB_CLIENT_SECRET": gh_secret,
                "EXOMEM_GITHUB_USERNAME": gh_username,
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
        # signing key (and the store) stable across both. Set EXOMEM_JWT_SIGNING_KEY
        # (any long random string) in .env; leave unset to keep the derived default.
        jwt_signing_key = os.environ.get("EXOMEM_JWT_SIGNING_KEY", "").strip() or None
        if jwt_signing_key is None:
            log.info(
                "EXOMEM_JWT_SIGNING_KEY not set; OAuth signing key derives from the "
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

    mcp = FastMCP("exomem", auth=auth, icons=_server_icons())
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
    # a bearer token (EXOMEM_UPLOAD_TOKEN) and/or a *signature-verified* Cloudflare
    # Access JWT (EXOMEM_CF_ACCESS_TEAM_DOMAIN + EXOMEM_CF_ACCESS_AUD). With neither
    # configured the route refuses every request (never an open write hole). NOTE:
    # the claude.ai web code sandbox is network-locked and CANNOT reach this route;
    # it's for the user's own browser / phone / CLI.
    upload_token = os.environ.get("EXOMEM_UPLOAD_TOKEN", "").strip() or None
    upload_max_bytes = int(
        os.environ.get("EXOMEM_UPLOAD_MAX_BYTES", str(preserve_module.MAX_UPLOAD_BYTES))
    )
    # Alternate upload host NOT behind the ~100 MB Cloudflare edge cap (e.g. a
    # Tailscale Funnel) — surfaced to clients as `large_upload_url` for the rare
    # >100 MB web upload that Cloudflare would 413. Optional; inert when unset.
    large_upload_base = (
        os.environ.get("EXOMEM_LARGE_UPLOAD_BASE_URL", "").strip().rstrip("/") or None
    )
    cf_team = os.environ.get("EXOMEM_CF_ACCESS_TEAM_DOMAIN", "").strip() or None
    cf_aud = os.environ.get("EXOMEM_CF_ACCESS_AUD", "").strip() or None
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
                    "reason": "uploads are off: set EXOMEM_UPLOAD_TOKEN (or configure "
                    "Cloudflare Access via EXOMEM_CF_ACCESS_TEAM_DOMAIN + EXOMEM_CF_ACCESS_AUD)",
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
        # Full extracted/OCR'd text of the artifact (the sandbox does the
        # extraction). Lands in the sidecar body so the binary becomes findable.
        text = str(form.get("text") or "").strip() or None
        filename = str(form.get("filename") or "").strip() or (
            getattr(upload, "filename", "") or ""
        )
        # Stream the spooled upload straight to Evidence/, off the event loop. The
        # part size is already bounded by max_part_size above; preserve_stream copies
        # in chunks, so even a multi-hundred-MB file never lands in RAM (no read() of
        # the whole body, no base64 round-trip).
        try:
            result = await run_in_threadpool(
                preserve_module.preserve_stream,
                vault_root,
                scope=scope,
                category=category,
                filename=filename,
                stream=upload.file,
                description=description,
                text=text,
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
        # Off-request-path media processing for the uploaded binary:
        #  - OCR/ASR/PDF text when no `text` was supplied (fills the pending sidecar);
        #  - CLIP-embed every IMAGE and VIDEO (keyframes) so visual content — including a
        #    silent video with no transcript — is findable. Both run in the worker; 201 now.
        if media_worker is not None and result.sidecar_path:
            media_type = extract.media_type_for(filename)
            if media_type:
                do_ocr = text is None
                do_clip = media_type in ("image", "video") and not os.environ.get("EXOMEM_DISABLE_CLIP")
                if do_ocr or do_clip:
                    media_worker.enqueue(
                        binary_path=vault_root / result.path,
                        sidecar_path=vault_root / result.sidecar_path,
                        media_type=media_type,
                        do_ocr=do_ocr,
                        do_clip=do_clip,
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
<title>exomem upload</title>
<style>body{{font:16px system-ui;max-width:34rem;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.75rem 0 .2rem}}input,textarea{{width:100%;padding:.5rem;font:inherit}}
button{{margin-top:1rem;padding:.6rem 1rem;font:inherit}}#out{{margin-top:1rem;white-space:pre-wrap}}</style>
<h1>Add evidence to the KB</h1>
<form id=f>
<label>File <small>(max {upload_max_bytes // (1024 * 1024)} MB; a public link may be capped lower by the proxy)</small></label><input type=file name=file required>
<label>Scope</label><input name=scope value="{_attr('scope')}" placeholder="e.g. Yolo" required>
<label>Category</label><input name=category value="{_attr('category')}" placeholder="e.g. 01 - Check-in" required>
<label>Filename (optional)</label><input name=filename value="{_attr('filename')}">
<label>Description (optional)</label><input name=description value="{_attr('description')}">
<label>Extracted text (optional — makes the file searchable)</label><textarea name=text rows=4 placeholder="OCR / transcribed text"></textarea>
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

    # ---- Out-of-band binary DOWNLOAD: the reverse of /upload ----
    # The sandbox pulls a vault file (dataset, evidence, scan) to analyze it.
    # Read-only, token-gated (download-scoped), path confined to the vault root.
    def _download_authorized(request: Request) -> bool:
        # Same model as /upload: the long-lived master token, a *download*-scoped
        # minted token, or a verified CF Access JWT. Never a spoofable header.
        if upload_token is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented, upload_token):
                    return True
                if upload_tokens.verify(presented, upload_token, scope="download"):
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

    @mcp.custom_route("/download", methods=["GET"])
    async def _download(request: Request):
        if not upload_enabled:
            return JSONResponse(
                {
                    "code": "DOWNLOAD_DISABLED",
                    "reason": "downloads are off: set EXOMEM_UPLOAD_TOKEN (or configure "
                    "Cloudflare Access via EXOMEM_CF_ACCESS_TEAM_DOMAIN + EXOMEM_CF_ACCESS_AUD)",
                },
                status_code=503,
            )
        if not _download_authorized(request):
            return JSONResponse(
                {"code": "UNAUTHORIZED", "reason": "missing or invalid download credential"},
                status_code=401,
            )
        path = request.query_params.get("path", "")
        if not path.strip():
            return JSONResponse(
                {"code": "INVALID_PATH", "reason": "query param `path` (vault-relative) is required"},
                status_code=400,
            )
        try:
            abs_path, _rel = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_file=True
            )
        except VaultPathError as e:
            status = 404 if e.code == "NOT_FOUND" else 400
            return JSONResponse({"code": e.code, "reason": e.reason}, status_code=status)
        return FileResponse(abs_path, filename=abs_path.name)

    # ---- Personal REST facade: /api/<tool> JSON wrappers over the SAME leaves ----
    #
    # A token-gated HTTP/JSON surface for scripting the KB from your own tools
    # (a cron job, a shell script, a phone shortcut) WITHOUT the MCP/OAuth dance.
    # This is NOT a public/ChatGPT plugin surface: single long-lived API key
    # (EXOMEM_REST_API_KEY), no /.well-known/ai-plugin.json. Disabled (503) unless
    # the key is set — opt-in only. Each handler calls the exact same leaf function
    # the matching MCP tool calls (no duplicated business logic) and maps the JSON
    # body to kwargs, returning the leaf's structured error as 400 {error, reason}.
    rest_api_key = os.environ.get("EXOMEM_REST_API_KEY", "").strip() or None
    rest_enabled = rest_api_key is not None

    def _rest_authorized(request: Request) -> bool:
        # Same non-spoofable model as /upload: the long-lived REST key (constant-time
        # compared), a short-lived `rest`-scoped minted token, or a verified CF Access
        # JWT. Never the plaintext cf-access-* email header.
        if rest_api_key is not None:
            header = request.headers.get("authorization", "")
            if header.startswith("Bearer "):
                presented = header[len("Bearer ") :].strip()
                if secrets.compare_digest(presented, rest_api_key):
                    return True
                if upload_tokens.verify(presented, rest_api_key, scope="rest"):
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

    def _rest_err(
        code: str, message: str, status: int, remediation: str | None = None
    ) -> JSONResponse:
        return _RestJSONResponse(
            cli_ops.envelope(
                False, error={"code": code, "message": message, "remediation": remediation}
            ),
            status_code=status,
        )

    def _rest_gate(request: Request) -> JSONResponse | None:
        """503 when the facade is off, 401 when unauthorized, else None (proceed)."""
        if not rest_enabled:
            return _rest_err(
                "REST_DISABLED",
                "REST API is off: set EXOMEM_REST_API_KEY to enable the /api/* facade",
                503,
            )
        if not _rest_authorized(request):
            return _rest_err("UNAUTHORIZED", "missing or invalid REST API key", 401)
        return None

    async def _rest_body(request: Request) -> dict | None:
        """Parse the JSON request body to a dict. `{}` for empty; None if malformed."""
        try:
            raw = await request.body()
        except Exception:  # noqa: BLE001
            return {}
        if not raw or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    # ---- Personal REST facade + OpenAPI: generated from the command registry ----
    # Every op marked `rest` gets POST /api/<name> via one generic handler
    # (gate -> JSON body -> registry coercion -> threadpool leaf -> shared envelope).
    # The previously hand-wired 9 routes keep their names + leaf calls (pinned by
    # tests/test_rest_registry.py); ops that lacked a route (replace, link,
    # provenance_report, query_data, ...) now have one. Tier-2 ops drop out when
    # EXOMEM_DISABLE_TIER2 is set.
    _expose_tier2 = not os.environ.get("EXOMEM_DISABLE_TIER2")
    _rest_commands = commands_module.commands_for("rest", expose_tier2=_expose_tier2)

    def _register_rest(cmd: commands_module.Command) -> None:
        @mcp.custom_route(f"/api/{cmd.name}", methods=["POST"])
        async def _handler(request: Request, _cmd: commands_module.Command = cmd) -> JSONResponse:
            gate = _rest_gate(request)
            if gate is not None:
                return gate
            body = await _rest_body(request)
            if body is None:
                return _rest_err("INVALID_BODY", "request body must be a JSON object", 400)
            try:
                kwargs = cli_ops.coerce(
                    _cmd.params, body, guarded_fields=_cmd.guarded_fields, tool=_cmd.name
                )
                injected = (vault_root, source_schema) if _cmd.needs_schema else (vault_root,)
                result = await run_in_threadpool(_cmd.leaf, *injected, **kwargs)
            except (cli_ops.OpError, ValueError, TypeError) as e:
                err = cli_ops.error_dict(e)
                return _RestJSONResponse(
                    cli_ops.envelope(False, error=err),
                    status_code=cli_ops.http_status_for(err["code"]),
                )
            return _RestJSONResponse(cli_ops.envelope(True, data=result))

        _handler.__name__ = f"_api_{cmd.name}"

    for _cmd in _rest_commands:
        _register_rest(_cmd)

    _OPENAPI_TYPES = {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "bool": {"type": "boolean"},
        "list[str]": {"type": "array", "items": {"type": "string"}},
        "dict": {"type": "object"},
        "json": {},
    }

    @mcp.custom_route("/api/openapi.json", methods=["GET"])
    async def _api_openapi(request: Request) -> JSONResponse:
        # Self-documentation only — OpenAPI 3.1 generated from the command registry
        # (real per-parameter schemas). NOT a public plugin manifest.
        if not rest_enabled:
            return _rest_err("REST_DISABLED", "set EXOMEM_REST_API_KEY to enable", 503)
        paths: dict = {}
        for cmd in _rest_commands:
            properties: dict = {}
            required: list[str] = []
            for prm in cmd.params:
                schema_obj = dict(_OPENAPI_TYPES.get(prm.type, {}))
                if prm.help:
                    schema_obj["description"] = prm.help
                properties[prm.name] = schema_obj
                if prm.required:
                    required.append(prm.name)
            request_schema: dict = {"type": "object", "properties": properties}
            if required:
                request_schema["required"] = required
            summary = (cmd.description or cmd.name).strip().splitlines()[0]
            paths[f"/api/{cmd.name}"] = {
                "post": {
                    "operationId": cmd.name,
                    "summary": summary,
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "content": {"application/json": {"schema": request_schema}}
                    },
                    "responses": {
                        "200": {"description": "{success: true, data: ...}"},
                        "400": {"description": "{success: false, error: {code, message, remediation}}"},
                        "401": {"description": "missing/invalid API key"},
                        "503": {"description": "REST API disabled"},
                    },
                }
            }
        return JSONResponse(
            {
                "openapi": "3.1.0",
                "info": {"title": "exomem personal REST facade", "version": "1.0.0"},
                "components": {
                    "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
                },
                "paths": paths,
            }
        )

    # ---- MCP tools: generated from the command registry (commands.COMMANDS) ----
    # One declarative entry per op drives the MCP tool, the REST route, the CLI
    # subcommand, and the OpenAPI path. bind_vault presents each leaf's signature
    # (minus vault_root) + its docstring, so each generated tool is byte-identical to
    # the former hand-written wrapper (pinned by tests/test_mcp_schema_fidelity.py).
    # Tier-2 escape hatches drop out of the surface when EXOMEM_DISABLE_TIER2 is set
    # (_expose_tier2 was resolved with the REST facade above).
    for _cmd in commands_module.commands_for("mcp", expose_tier2=_expose_tier2):
        if _cmd.name in commands_module.HAND_REGISTERED_EXCEPTIONS:
            continue
        _injected = (vault_root, source_schema) if _cmd.needs_schema else (vault_root,)
        mcp.tool(
            commands_module.bind_vault(
                _cmd.leaf, *_injected, name=_cmd.name, description=_cmd.doc
            ),
            annotations=_cmd.mcp_annotations,
        )

    # `note` stays a hand-registered MCP exception: its description carries the live
    # per-vault project-key hint, injected here. Same leaf the REST/CLI surfaces use.
    mcp.tool(
        commands_module.bind_vault(
            commands_module.op_note,
            vault_root,
            name="note",
            description=commands_module.note_description(project_keys_hint),
        ),
        annotations=commands_module.mcp_tool_annotations("note", read_only=False),
    )

    @mcp.tool(
        annotations=commands_module.mcp_tool_annotations(
            "mint_upload_token", read_only=False
        )
    )
    def mint_upload_token() -> dict:
        """Mint a short-lived bearer token for the HTTP `/upload` endpoint.

        This is how you file BINARY evidence (images, PDFs, any file) from a
        claude.ai web session — the bytes travel out-of-band, never through the
        model. Steps:

        1. Call this tool → `{token, ttl_seconds, upload_url[, large_upload_url]}`.
        2. In the code sandbox, multipart-POST the user's ATTACHED files to
           `upload_url` with header `Authorization: Bearer <token>` and form
           fields `file`, `scope`, `category` (optional `filename`, `description`,
           `text`). Files must be ATTACHMENTS — inline-pasted images never reach
           the sandbox disk and cannot be sent.
        3. To make the binary SEARCHABLE, extract its text in the sandbox (OCR an
           image/scan, read a PDF, transcribe audio/video) and pass it as the
           `text` field above — it lands in an embedded sidecar so the otherwise-
           opaque file is findable by its content.

        **Large files (>100 MB):** `upload_url` is fronted by Cloudflare, which
        413s any body over ~100 MB at the edge. If a file exceeds ~100 MB (or a
        POST to `upload_url` returns 413), send it to `large_upload_url` instead
        when that field is present — same token, same form fields, an alternate
        host with no edge cap. If `large_upload_url` is absent, only `upload_url`
        is available and >100 MB must go desk-side.

        The token is Evidence-write only and expires after `ttl_seconds`; the
        server's long-lived secret never leaves the server.

        Returns: {token, ttl_seconds, upload_url, large_upload_url?}.
        Raises: UPLOAD_DISABLED if the server has no upload token configured.
        """
        return upload_tokens.mint_for_endpoint(
            upload_token, base_url, large_base_url=large_upload_base
        )

    @mcp.tool(
        annotations=commands_module.mcp_tool_annotations(
            "mint_download_token", read_only=True
        )
    )
    def mint_download_token() -> dict:
        """Mint a short-lived bearer token for the HTTP `/download` endpoint.

        Use to PULL a vault file into the sandbox — a dataset, an evidence
        binary, a scan — so you can analyze it. Call this, then from the code
        sandbox GET `download_url?path=<vault-relative path>` with header
        `Authorization: Bearer <token>`. Read-only; the token is download-scoped
        (can't write) and expires after `ttl_seconds`.

        Returns: {token, ttl_seconds, download_url}.
        Raises: DOWNLOAD_DISABLED if the server has no upload token configured.
        """
        return upload_tokens.mint_for_endpoint(upload_token, base_url, scope="download")

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

    Bind host precedence: $EXOMEM_HOST > the passed `host` > 127.0.0.1. The env var
    wins (and is resolved AFTER build_server() loads .env) so the deployment can flip
    the bind from .env without changing the service's launch args. Set
    EXOMEM_HOST=0.0.0.0 to also serve a non-Cloudflare route (e.g. a direct Tailscale
    connection to the origin) for uploads larger than the Cloudflare edge cap.
    """
    from .logging_config import configure_logging

    if log_dir is None:
        log_dir = Path(__file__).resolve().parents[2] / "logs"
    configure_logging(log_dir)

    require_auth = transport != "stdio"
    mcp = build_server(require_auth=require_auth)  # loads .env (EXOMEM_HOST, etc.)

    if transport == "stdio":
        log.info("exomem starting on stdio")
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("EXOMEM_HOST") or host or "127.0.0.1"
        log.info("exomem starting on %s host=%s port=%s", transport, host, port)
        mcp.run(transport=transport, host=host, port=port)
