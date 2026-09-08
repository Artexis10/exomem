"""Browser-only native owner review routes; never part of the agent API."""

from __future__ import annotations

import html
import json
import shlex
import time
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from . import mutation_lock
from .governance import egress, policy
from .governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope
from .native_owner_control import NativeOwnerControl
from .vocabulary_control import _detach
from .vocabulary_effects import _MAX_IMAGE_BYTES

_LABELS = {
    "entity.create": "Create entity pages",
    "entity_type.add": "Add entity types",
    "relation_type.add": "Add relationship types",
    "edge.add": "Add relationships",
}
_STYLE = """
:root{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#111827;background:#f9fafb;color-scheme:light}
*{box-sizing:border-box}body{margin:0;padding:40px 16px}main{max-width:832px;margin:auto;background:white;border:1px solid #e5e7eb;border-radius:16px;padding:40px;box-shadow:0 4px 14px #11182708}
header{margin-bottom:28px}h1{font-size:24px;font-weight:600;margin:8px 0 16px}h2{font-size:19px;margin:28px 0 16px}h3{font-size:16px}p,li,label{font-size:15px;line-height:1.5}p{margin:12px 0}a{color:#1d4ed8}small{color:#4b5563}section{border-top:1px solid #e5e7eb;padding-top:8px;margin-top:24px}
label{display:block;margin:14px 0 6px}input,select,button{font:inherit}input[type=text],select{width:100%;padding:10px;border:1px solid #9ca3af;border-radius:6px}input[type=checkbox]{margin-right:8px;width:17px;height:17px;vertical-align:middle}button,.button{display:inline-block;background:#1d4ed8;color:white;border:0;border-radius:8px;padding:12px 18px;margin:18px 0 4px;text-decoration:none;cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #2563eb;outline-offset:3px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.5;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:8px;padding:16px}code{overflow-wrap:anywhere}.notice{padding:16px;border:1px solid #bfdbfe;background:#eff6ff;border-radius:8px}.success{background:#f0fdf4;border-color:#bbf7d0}.error{background:#fef2f2;border-color:#fecaca}details{margin:20px 0}summary{cursor:pointer;font-size:15px}fieldset{border:1px solid #e5e7eb;border-radius:8px;margin:20px 0;padding:8px 16px 16px}legend{font-weight:600}.brand{color:#4b5563;font-size:14px}
@media(max-width:640px){body{padding:8px}main{padding:24px}h1{margin-top:12px}pre{padding:12px}}
"""


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, content: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_escape(title)} · Exomem</title><style>{_STYLE}</style></head>"
        f'<body><main><header><span class="brand">Exomem</span><h1>{_escape(title)}</h1></header>'
        f"{content}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{_escape(name)}" value="{_escape(value)}">'


_SETUP = {
    "policy": (
        "Set up governance",
        'Review an “Existing access” policy that preserves existing access with paths ["**"] and default_deny: false. It creates no rules or grants.',
        "Review initial policy",
    ),
    "migration": (
        "Upgrade governance storage",
        "Review the exact governance upgrade and its verified backup. After approval, run the maintenance command on this installation for a brief service pause.",
        "Review governance upgrade",
    ),
    "activation": (
        "Activate memory permissions",
        "No default grants are created. You will review each agent’s permissions separately. Activation also enables automatic renewal of this installation’s existing serving proof; renewal adds no keys, identity or permissions.",
        "Review activation",
    ),
}
_FORM_FIELDS = {
    **{action: {"csrf", "action"} for action in _SETUP},
    "grant": {"csrf", "action", "session_id", "actions", "days"},
    "approve": {"csrf", "action", "session_id", "request_id"},
    "deny": {"csrf", "action", "session_id", "request_id"},
    "revoke": {"csrf", "action", "session_id", "authority_id"},
}


def _review_form(csrf: str, action: str, label: str, **fields: str) -> str:
    content = '<form method="post" action="/owner/review">'
    for name, value in {"csrf": csrf, "action": action, **fields}.items():
        content += _hidden(name, value)
    return content + f'<button type="submit">{_escape(label)}</button></form>'


def _maintenance_command(review) -> str:
    unit = review.body.get("service_unit")
    if not isinstance(unit, str) or not unit:
        raise ValueError("Maintenance service binding is missing")
    interpreter = review.body.get("service_interpreter")
    if not isinstance(interpreter, str) or not interpreter or not Path(interpreter).is_absolute():
        raise ValueError("Maintenance interpreter binding is missing")
    command = [
        interpreter, "-m", "exomem.native_owner_maintenance_runner",
        "--unit-file", unit, "--request-id", review.review_id,
    ]
    if review.state == "applying":
        command.append("--resume")
    return shlex.join(command)


def _current_image(root: Path, relative: str) -> bytes | None:
    """Read bounded current bytes through retained, non-aliasing parents."""
    path = Path(relative)
    if not relative or path.is_absolute() or path.as_posix() != relative or ".." in path.parts:
        raise ValueError("Invalid review path")
    with ExitStack() as stack:
        parent = mutation_lock.retain_secure_directory(root)
        stack.callback(parent.close)
        parents = [parent]
        try:
            for part in path.parts[:-1]:
                parent = mutation_lock.retain_child_directory(parent, part)
                stack.callback(parent.close)
                parents.append(parent)
            current = mutation_lock.retained_read_file(parent, path.name, limit=_MAX_IMAGE_BYTES)
        except FileNotFoundError:
            current = None
        if any(not mutation_lock._same_directory_path(item) for item in parents):
            raise ValueError("Review parent changed")
        return current


def _owner_release(root: Path, payload: dict, *, images=()) -> dict:
    """Release a complete projection only after browser owner authentication."""
    original = deepcopy(_detach(payload))
    principal = RequestPrincipal(
        audience_id=OWNER_AUDIENCE,
        surface="native-owner-control",
        issuer_family="native-owner-github",
        resolved=True,
    )
    with request_scope(principal), egress.disclosure_boundary(root, "native_owner_review") as collector:
        try:
            if policy.load(root).blocked:
                raise ValueError("Owner disclosure policy is unavailable")
            for image in images:
                before = image["before"]
                expected = None if before is None else before.encode("utf-8")
                if _current_image(root, image["path"]) != expected:
                    raise ValueError("Review image changed")
                if before is None or Path(image["path"]).suffix.lower() != ".md":
                    # New files and non-Markdown registries need a complete
                    # path-only decision; unresolved semantic membership refuses.
                    fully_released = egress.release_level_for_path_only(
                        root, image["path"], receipt_decision="released", allow_companions=False
                    ) == egress.LEVEL_FULL
                else:
                    # Classify the sealed bytes, even if the live path changes
                    # between the freshness check and the release decision.
                    released = egress.annotate_page(
                        root, {"path": image["path"]}, snapshot_content=expected, include_raw=True
                    )
                    fully_released = released is not None and released.get("content") == before
                if not fully_released:
                    raise ValueError("Review image cannot be fully disclosed")
            filtered = egress.postfilter("native_owner_review", deepcopy(original), root)
            if filtered != original:
                raise ValueError("Review cannot be fully disclosed")
        finally:
            egress.emit_boundary_receipt(collector)
    return original


def _released_review(root: Path, review) -> dict:
    projection = {"display": _detach(review.display)}
    if review.action in {"migration", "activation"} and (
        review.state == "applying"
        or (review.state == "accepted" and not getattr(review, "expired", False))
    ):
        projection["maintenance_command"] = _maintenance_command(review)
    return _owner_release(root, projection, images=projection["display"].get("write_images", ()))


def _review_content(review, csrf: str, projection: dict) -> str:
    display = projection["display"]
    content = '<p><a href="/owner">Memory permissions</a></p>'
    content += f"<p>Action: <strong>{_escape(review.action)}</strong></p>"
    if review.action in _SETUP:
        content += f"<p>{_escape(_SETUP[review.action][1])}</p>"
    if display.get("audience_id"):
        content += f"<p>Agent audience: <code>{_escape(display['audience_id'])}</code></p>"
    for path, document in display.get("canonical_yaml", {}).items():
        content += f"<section><h2>{_escape(path)}</h2><pre>{_escape(document)}</pre></section>"
    if "consequences" in display:
        content += "<h2>Transition consequences</h2><pre>"
        content += (
            _escape(json.dumps(display["consequences"], ensure_ascii=False, indent=2)) + "</pre>"
        )
    for image in display.get("write_images", ()):
        content += f"<section><h2>{_escape(image['path'])}</h2>"
        for key, label in (("before", "Current content"), ("after", "Proposed content")):
            content += f"<h3>{label}</h3>"
            content += (
                "<p>File does not exist.</p>"
                if image[key] is None
                else f"<pre>{_escape(image[key])}</pre>"
            )
        content += "</section>"
    manifest = display.get("grant_manifest")
    if manifest:
        labels = ", ".join(_LABELS[action] for action in manifest["actions"])
        content += f"<p>Allowed work: <strong>{_escape(labels)}</strong>.</p>"
        content += f"<p>Scope: <strong>{_escape(manifest['scope']['kind'])}</strong>.</p>"
        expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(manifest["expires_at"]))
        content += f"<p>Expires: <strong>{expires}</strong>.</p>"
        content += "<p>This grant applies to the named agent audience across its current sessions. Existing meanings, deletions and other changes remain outside these additive permissions.</p>"
    maintenance = review.action in {"migration", "activation"}
    if review.state == "completed":
        result = getattr(review, "result", None) or {}
        if (
            result.get("status") == "deployment_ready"
            or result.get("activation") == "owner_review_required"
        ):
            content += '<p class="notice">The deployment is ready. Return to memory permissions for a fresh activation review before permissions can be enabled.</p>'
        else:
            content += '<p class="notice success">Completed. Return to memory permissions to review the next step.</p>'
    elif review.state == "applying":
        content += '<p class="notice">The accepted action needs completion or recovery. It will not be submitted again automatically.</p>'
        if maintenance:
            content += "<p>Run this recovery command on the installation. It verifies the stopped service and allows a brief service pause before restarting safely.</p>"
            content += f"<pre>{_escape(projection['maintenance_command'])}</pre>"
    elif getattr(review, "expired", False):
        content += (
            '<p class="notice">This review has expired. Prepare a fresh review to continue.</p>'
        )
    elif review.state == "accepted" and maintenance:
        content += '<p class="notice success">Approved. Run this command on the installation to apply the reviewed change with a brief service pause. The browser has not stopped the service.</p>'
        content += f"<pre>{_escape(projection['maintenance_command'])}</pre>"
    elif review.state in {"prepared", "accepted"}:
        content += '<p class="notice">Approval applies only to the action and scope shown here. Exomem checks the current state again before applying it.</p>'
        content += (
            f'<form method="post" action="/owner/review/{quote(review.review_id, safe="")}/accept">'
        )
        label = "Approve maintenance" if maintenance else "Approve this action"
        content += _hidden("csrf", csrf) + f'<button type="submit">{label}</button></form>'
    else:
        content += '<p class="notice">Prepare a fresh review to continue.</p>'
    content += "<details><summary>Review details</summary><pre>"
    content += _escape(json.dumps(display, ensure_ascii=False, indent=2))
    return content + "</pre></details>"


async def _form(request: Request):
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16 * 1024:
            raise HTTPException(413, "Owner submission is too large")
        chunks.append(chunk)
    request._body = b"".join(chunks)
    return await request.form()


def _validate_form(form, allowed: set[str], *, actions: bool = False) -> None:
    if set(form) - allowed or any(not isinstance(value, str) for _, value in form.multi_items()):
        raise HTTPException(400, "Unsupported owner form fields")
    if any(len(form.getlist(key)) != 1 for key in form if not (actions and key == "actions")):
        raise HTTPException(400, "Repeated owner form fields")
    if actions:
        selected = form.getlist("actions")
        if not selected or len(selected) != len(set(selected)) or set(selected) - _LABELS.keys():
            raise HTTPException(400, "Choose distinct additive permissions")


def _failure(*, unavailable: bool = False) -> HTMLResponse:
    if unavailable:
        return _page(
            "Memory permissions unavailable",
            '<p class="notice error">This installation cannot load owner controls right now. Try again after its service is available.</p><p><a href="/owner">Return to memory permissions</a></p>',
            status=503,
        )
    return _page(
        "Review changed",
        '<p class="notice error">This review is no longer current or the selected action is unavailable. Return to memory permissions and prepare a fresh review.</p><p><a href="/owner">Return to memory permissions</a></p>',
        status=409,
    )


def _more_link(query: dict[str, str], label: str) -> str:
    return f'<p><a href="/owner?{_escape(urlencode(query))}">{_escape(label)}</a></p>'


def register_owner_routes(mcp_app, *, vault_root: Path, owner_auth, control_factory=None) -> None:
    factory = control_factory or (lambda: NativeOwnerControl(vault_root))

    @mcp_app.custom_route("/owner", methods=["GET"])
    async def home(request: Request):
        try:
            owner = await owner_auth.require_session(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/owner/login", status_code=303)
            raise
        content = "<p>Let your agent maintain knowledge within permissions you choose. Every new grant is reviewed here; signing in grants no permissions.</p>"
        now = int(time.time())
        try:
            control = factory()
            stage = await run_in_threadpool(control.setup_status, now=now)
            if stage in _SETUP:
                title, explanation, label = _SETUP[stage]
                content += f"<section><h2>{title}</h2><p>{_escape(explanation)}</p>"
                content += _review_form(owner.csrf_token, stage, label) + "</section>"
                return _page("Memory permissions", content)
            if stage != "active":
                return _failure(unavailable=True)
            after = request.query_params.get("after", "")
            page = await run_in_threadpool(control.sessions, now=now, after=after)
            details = []
            for session in page["items"]:
                session_id = session["session_id"]
                selected = request.query_params.get("session") == session_id
                pending = await run_in_threadpool(
                    control.requests,
                    session_id,
                    now=now,
                    after=request.query_params.get("requests_after", "") if selected else "",
                )
                grants = await run_in_threadpool(
                    control.grants,
                    session_id,
                    now=now,
                    after=request.query_params.get("grants_after", "") if selected else "",
                )
                details.append({"pending": pending, "grants": grants})
            released = await run_in_threadpool(
                _owner_release, vault_root, {"page": page, "details": details, "after": after}
            )
            page, details, after = released["page"], released["details"], released["after"]
            if not page["items"]:
                content += '<p class="notice">No current agent authorization sessions are available. Connect your agent and open an authorization session to review its permissions.</p>'
            for session, detail in zip(page["items"], details, strict=True):
                session_id = session["session_id"]
                pending, grants = detail["pending"], detail["grants"]
                content += f"<section><h2>Agent audience</h2><p><code>{_escape(session['audience_id'])}</code></p>"
                content += f"<p>Connected through {_escape(session['issuer_family'])}.</p>"
                content += "<h3>Pending requests</h3>"
                if not pending["items"]:
                    content += "<p>No pending requests on this page.</p>"
                for request_id in pending["items"]:
                    content += f"<p><code>{_escape(request_id)}</code></p>"
                    for action, label in (
                        ("approve", "Review approval"),
                        ("deny", "Review denial"),
                    ):
                        content += _review_form(
                            owner.csrf_token,
                            action,
                            label,
                            session_id=session_id,
                            request_id=request_id,
                        )
                if pending["next"]:
                    content += _more_link(
                        {"after": after, "session": session_id, "requests_after": pending["next"]},
                        "More pending requests",
                    )
                content += "<h3>Active grants</h3>"
                if not grants["items"]:
                    content += "<p>No active grants on this page.</p>"
                for grant in grants["items"]:
                    labels = ", ".join(_LABELS[action] for action in grant["actions"])
                    content += (
                        f"<p><code>{_escape(grant['authority_id'])}</code><br>{_escape(labels)}</p>"
                    )
                    content += f"<pre>{_escape(json.dumps(grant['scope'], ensure_ascii=False, indent=2))}</pre>"
                    expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(grant["expires_at"]))
                    content += f"<p>Expires: {expires}.</p>"
                    content += _review_form(
                        owner.csrf_token,
                        "revoke",
                        "Review revocation",
                        session_id=session_id,
                        authority_id=grant["authority_id"],
                    )
                if grants["next"]:
                    content += _more_link(
                        {"after": after, "session": session_id, "grants_after": grants["next"]},
                        "More active grants",
                    )
                content += (
                    '<form method="post" action="/owner/review">'
                    + _hidden("csrf", owner.csrf_token)
                    + _hidden("session_id", session_id)
                    + _hidden("action", "grant")
                )
                content += "<fieldset><legend>Allow additive work</legend>"
                for action, label in _LABELS.items():
                    content += f'<label><input type="checkbox" name="actions" value="{action}">{label}</label>'
                content += '</fieldset><label>Duration<select name="days"><option value="1">One day</option><option value="7">Seven days</option><option value="30">Thirty days</option></select></label>'
                content += '<p>Scope: this vault. No permissions are selected by default.</p><button type="submit">Review grant</button></form></section>'
            if page["next"]:
                content += _more_link({"after": page["next"]}, "More agent sessions")
        except (OSError, RuntimeError, ValueError):
            return _failure(unavailable=True)
        return _page("Memory permissions", content)

    @mcp_app.custom_route("/owner/review", methods=["POST"])
    async def prepare(request: Request):
        await owner_auth.require_session(request)
        form = await _form(request)
        owner = await owner_auth.require_submission(request, form.get("csrf"))
        action = form.get("action")
        if action not in _FORM_FIELDS:
            raise HTTPException(400, "Unsupported owner action")
        _validate_form(form, _FORM_FIELDS[action], actions=action == "grant")
        body = {} if action in _SETUP else {"session_id": form.get("session_id")}
        if action == "grant":
            if form.get("days") not in {"1", "7", "30"}:
                raise HTTPException(400, "Choose a grant duration")
            body.update(
                actions=form.getlist("actions"),
                scope="vault",
                expires_at=int(time.time()) + int(form["days"]) * 86400,
            )
        elif action in {"approve", "deny"}:
            body["request_id"] = form.get("request_id")
        elif action == "revoke":
            body["authority_id"] = form.get("authority_id")
        try:
            review = await run_in_threadpool(
                factory().prepare,
                owner_id=owner.owner_id,
                action=action,
                body=body,
                now=int(time.time()),
            )
        except OSError:
            return _failure(unavailable=True)
        except (RuntimeError, ValueError):
            return _failure()
        return RedirectResponse(
            f"/owner/review/{quote(review.review_id, safe='')}", status_code=303
        )

    @mcp_app.custom_route("/owner/review/{review_id}", methods=["GET"])
    async def review(request: Request):
        owner = await owner_auth.require_session(request)
        try:
            prepared = await run_in_threadpool(
                factory().reviews.get,
                request.path_params["review_id"],
                owner_id=owner.owner_id,
                now=int(time.time()),
            )
            projection = await run_in_threadpool(_released_review, vault_root, prepared)
            return _page(
                "Review memory permissions", _review_content(prepared, owner.csrf_token, projection)
            )
        except OSError:
            return _failure(unavailable=True)
        except (RuntimeError, ValueError):
            return _failure()

    @mcp_app.custom_route("/owner/review/{review_id}/accept", methods=["POST"])
    async def accept(request: Request):
        await owner_auth.require_session(request)
        form = await _form(request)
        owner = await owner_auth.require_submission(request, form.get("csrf"))
        _validate_form(form, {"csrf"})
        review_id = request.path_params["review_id"]
        try:
            control = factory()
            prepared = await run_in_threadpool(
                control.reviews.get, review_id, owner_id=owner.owner_id, now=int(time.time())
            )
            await run_in_threadpool(_released_review, vault_root, prepared)
            await run_in_threadpool(
                control.accept, review_id, owner_id=owner.owner_id, now=int(time.time())
            )
        except OSError:
            return _failure(unavailable=True)
        except (RuntimeError, ValueError):
            return _failure()
        return RedirectResponse(f"/owner/review/{quote(review_id, safe='')}", status_code=303)
