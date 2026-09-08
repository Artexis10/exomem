from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.testclient import TestClient

from exomem.governance import egress
from exomem.governance.principal import effective_principal
from exomem.server_owner import register_owner_routes


class App:
    def __init__(self):
        self.routes = []

    def custom_route(self, path, *, methods):
        def register(handler):
            self.routes.append(Route(path, handler, methods=methods))
            return handler

        return register


class Auth:
    async def require_session(self, request):
        if request.headers.get("x-test-owner") != "yes":
            raise HTTPException(401)
        return SimpleNamespace(owner_id="github:123", csrf_token="csrf")

    async def require_submission(self, request, csrf_token):
        session = await self.require_session(request)
        if csrf_token != session.csrf_token:
            raise HTTPException(403)
        return session


class Control:
    def __init__(self):
        self.reviews = self
        self.accepted = []
        self.stage = "active"
        self.current_review = None
        self.session_items = []
        self.request_items = []
        self.grant_items = []

    def setup_status(self, **kwargs):
        return self.stage

    def requests(self, session_id, **kwargs):
        return {"items": self.request_items, "next": None}

    def grants(self, session_id, **kwargs):
        return {"items": self.grant_items, "next": None}

    def sessions(self, **kwargs):
        return {"items": self.session_items, "next": None}

    def prepare(self, **kwargs):
        self.prepared = kwargs
        return SimpleNamespace(review_id="review-1")

    def get(self, *args, **kwargs):
        if self.current_review is not None:
            return self.current_review
        return SimpleNamespace(
            review_id="review-1",
            state="prepared",
            action="approve",
            result=None,
            display={
                "audience_id": "agent",
                "write_images": [
                    {
                        "path": "Notes/example.md",
                        "before": None,
                        "after": "<script>alert('proposal')</script>",
                    }
                ],
            },
        )

    def accept(self, review_id, **kwargs):
        self.accepted.append(review_id)
        if self.current_review is not None:
            self.current_review.state = "accepted"
        return SimpleNamespace(state="completed")


def client(tmp_path):
    app, control = App(), Control()
    register_owner_routes(
        app, vault_root=tmp_path, owner_auth=Auth(), control_factory=lambda: control
    )
    return TestClient(Starlette(routes=app.routes)), control


def _owner_policy(vault, *, ceiling: int) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Notes\npaths: ["Notes/**"]\ndefault_deny: true\n',
        encoding="utf-8",
    )
    (governance / "rules" / "owner.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: owner\n'
        f"ceiling: {ceiling}\n",
        encoding="utf-8",
    )


def _write_review(control, *, path: str, before, after="proposed") -> None:
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state="prepared",
        action="approve",
        expired=False,
        result=None,
        body={},
        display={
            "audience_id": "agent",
            "write_images": [{"path": path, "before": before, "after": after}],
        },
    )


def test_owner_page_requires_browser_identity_not_an_agent_bearer(tmp_path):
    browser, control = client(tmp_path)
    response = browser.get(
        "/owner", headers={"authorization": "Bearer ordinary-agent"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/owner/login"
    assert control.accepted == []


def test_exact_review_is_escaped_and_get_never_approves(tmp_path):
    browser, control = client(tmp_path)
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert "Notes/example.md" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert control.accepted == []


def test_acceptance_requires_explicit_csrf_submission(tmp_path):
    browser, control = client(tmp_path)
    url = "/owner/review/review-1/accept"
    assert (
        browser.post(url, headers={"x-test-owner": "yes"}, data={"csrf": "wrong"}).status_code
        == 403
    )
    assert control.accepted == []
    response = browser.post(
        url, headers={"x-test-owner": "yes"}, data={"csrf": "csrf"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert control.accepted == ["review-1"]


@pytest.mark.parametrize("stage", ["policy", "migration", "activation"])
def test_setup_home_prepares_current_finite_stage(tmp_path, stage):
    browser, control = client(tmp_path)
    control.stage = stage
    response = browser.get("/owner", headers={"x-test-owner": "yes"})
    assert response.status_code == 200
    assert f'name="action" value="{stage}"' in response.text
    assert "Review " in response.text
    assert "Allow additive work" not in response.text
    response = browser.post(
        "/owner/review",
        headers={"x-test-owner": "yes"},
        data={"csrf": "csrf", "action": stage},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert control.prepared["action"] == stage
    assert control.prepared["body"] == {}


@pytest.mark.parametrize("state", ["accepted", "applying"])
def test_accepted_maintenance_displays_exact_quoted_operator_command(tmp_path, state):
    browser, control = client(tmp_path)
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state=state,
        action="activation",
        expired=False,
        result=None,
        body={"service_unit": "/tmp/service units/owner.service", "service_interpreter": "/tmp/service venv/bin/python", "raw_control": "PRIVATE_BASE64"},
        display={
            "default_grants": [],
            "renewal": "same-authority",
            "service_unit": "/tmp/service units/owner.service",
        },
    )
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert response.status_code == 200
    assert "-m exomem.native_owner_maintenance_runner --unit-file" in response.text
    assert "&#x27;/tmp/service venv/bin/python&#x27; -m" in response.text
    assert "--request-id review-1" in response.text
    assert ("--resume" in response.text) is (state == "applying")
    assert "brief service pause" in response.text
    assert "No default grants" in response.text
    assert "PRIVATE_BASE64" not in response.text
    assert "/accept" not in response.text


def test_policy_review_displays_canonical_yaml_and_consequences(tmp_path):
    browser, control = client(tmp_path)
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state="prepared",
        action="policy",
        expired=False,
        result=None,
        body={},
        display={
            "canonical_yaml": {
                "scopes/existing-access.yaml": 'name: Existing access\npaths: ["**"]\ndefault_deny: false\n'
            },
            "consequences": {"direction": "widening", "changed": "<unsafe>"},
        },
    )
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert "Existing access" in response.text
    assert "default_deny: false" in response.text
    assert "Transition consequences" in response.text
    assert "&lt;unsafe&gt;" in response.text
    assert "<unsafe>" not in response.text


def test_active_home_lists_real_pending_requests_and_revocable_grants(tmp_path):
    browser, control = client(tmp_path)
    control.session_items = [
        {"session_id": "session-1", "audience_id": "<agent>", "issuer_family": "oauth"}
    ]
    control.request_items = ["request-actual"]
    control.grant_items = [
        {
            "authority_id": "grant-actual",
            "actions": ["edge.add"],
            "scope": {"kind": "vault"},
            "expires_at": 2_000_000_000,
        }
    ]
    response = browser.get("/owner", headers={"x-test-owner": "yes"})
    assert "request-actual" in response.text and "grant-actual" in response.text
    assert 'name="action" value="approve"' in response.text
    assert 'name="action" value="deny"' in response.text
    assert 'name="action" value="revoke"' in response.text
    assert "&lt;agent&gt;" in response.text
    assert " checked" not in response.text
    response = browser.post(
        "/owner/review",
        headers={"x-test-owner": "yes"},
        data={
            "csrf": "csrf",
            "action": "revoke",
            "session_id": "session-1",
            "authority_id": "grant-actual",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert control.prepared["body"] == {"session_id": "session-1", "authority_id": "grant-actual"}


@pytest.mark.parametrize(
    "fields",
    [
        "csrf=csrf&action=policy&unknown=1",
        "csrf=csrf&csrf=csrf&action=policy",
        "csrf=csrf&action=policy&action=policy",
        "csrf=csrf&action=grant&session_id=s&days=1&actions=edge.add&actions=edge.add",
    ],
)
def test_form_fields_are_finite_after_authentication(tmp_path, fields):
    browser, control = client(tmp_path)
    response = browser.post(
        "/owner/review",
        headers={"x-test-owner": "yes", "content-type": "application/x-www-form-urlencoded"},
        content=fields,
    )
    assert response.status_code == 400
    assert not hasattr(control, "prepared")


@pytest.mark.parametrize("endpoint", ["/owner/review", "/owner/review/review-1/accept"])
def test_expected_controller_conflicts_are_friendly_and_do_not_leak(tmp_path, endpoint):
    browser, control = client(tmp_path)

    def conflict(*args, **kwargs):
        raise RuntimeError("PRIVATE_SECRET<script>")

    control.prepare = control.accept = conflict
    response = browser.post(
        endpoint,
        headers={"x-test-owner": "yes"},
        data={"csrf": "csrf", "action": "policy"}
        if endpoint == "/owner/review"
        else {"csrf": "csrf"},
    )
    assert response.status_code == 409
    assert "PRIVATE_SECRET" not in response.text
    assert "<script>" not in response.text
    assert "fresh review" in response.text


def test_completed_deployment_requires_fresh_activation_review(tmp_path):
    browser, control = client(tmp_path)
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state="completed",
        action="activation",
        expired=True,
        body={},
        display={},
        result={"status": "deployment_ready", "activation": "owner_review_required"},
    )
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert "fresh activation review" in response.text
    assert 'href="/owner"' in response.text


def test_setup_review_acceptance_returns_maintenance_instructions(tmp_path):
    browser, control = client(tmp_path)
    control.stage = "migration"
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state="prepared",
        action="migration",
        expired=False,
        result=None,
        body={"service_unit": "/tmp/owner.service", "service_interpreter": "/tmp/venv/bin/python"},
        display={"plan_digest": "a" * 64, "maintenance_required": True},
    )
    owner = {"x-test-owner": "yes"}
    response = browser.post(
        "/owner/review", headers=owner, data={"csrf": "csrf", "action": "migration"}
    )
    assert response.status_code == 200
    assert "Approve maintenance" in response.text
    assert control.accepted == []
    response = browser.post("/owner/review/review-1/accept", headers=owner, data={"csrf": "csrf"})
    assert response.status_code == 200
    assert (
        "/tmp/venv/bin/python -m exomem.native_owner_maintenance_runner --unit-file /tmp/owner.service --request-id review-1"
        in response.text
    )
    assert control.accepted == ["review-1"]
    assert control.current_review.state == "accepted"


def test_valid_multiple_actions_are_allowed_but_auth_precedes_field_validation(tmp_path):
    browser, control = client(tmp_path)
    headers = {"x-test-owner": "yes", "content-type": "application/x-www-form-urlencoded"}
    response = browser.post(
        "/owner/review",
        headers=headers,
        content="csrf=csrf&action=grant&session_id=s&days=1&actions=edge.add&actions=entity.create",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert control.prepared["body"]["actions"] == ["edge.add", "entity.create"]
    response = browser.post(
        "/owner/review", headers=headers, content="csrf=wrong&action=unknown&extra=value"
    )
    assert response.status_code == 403
    response = browser.post(
        "/owner/review/review-1/accept", headers=headers, content="csrf=wrong&extra=value"
    )
    assert response.status_code == 403
    response = browser.post(
        "/owner/review/review-1/accept", headers=headers, content="csrf=csrf&extra=value"
    )
    assert response.status_code == 400


def test_read_unavailability_does_not_render_exception_details(tmp_path):
    browser, control = client(tmp_path)

    def unavailable(**kwargs):
        raise OSError("PRIVATE_SERVICE_PATH")

    control.setup_status = unavailable
    response = browser.get("/owner", headers={"x-test-owner": "yes"})
    assert response.status_code == 503
    assert "PRIVATE_SERVICE_PATH" not in response.text


def test_owner_lists_use_bounded_continuation_for_the_selected_session(tmp_path):
    browser, control = client(tmp_path)
    control.session_items = [{"session_id": "s", "audience_id": "agent", "issuer_family": "oauth"}]
    seen = []

    def requests(session_id, **kwargs):
        seen.append((session_id, kwargs["after"]))
        return {"items": [], "next": "next<&"}

    control.requests = requests
    response = browser.get(
        "/owner?session=s&requests_after=current", headers={"x-test-owner": "yes"}
    )
    assert response.status_code == 200
    assert seen == [("s", "current")]
    assert "More pending requests" in response.text
    assert "requests_after=next%3C%26" in response.text


@pytest.mark.parametrize("ceiling", [0, 5])
def test_review_requires_full_owner_release_for_existing_bytes(tmp_path, ceiling):
    browser, control = client(tmp_path)
    target = tmp_path / "Knowledge Base" / "Notes" / "current.md"
    target.parent.mkdir(parents=True)
    target.write_text("withheld original bytes", encoding="utf-8")
    _owner_policy(tmp_path, ceiling=ceiling)
    _write_review(control, path="Knowledge Base/Notes/current.md", before="withheld original bytes")

    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})

    assert response.status_code == 409
    assert "withheld original bytes" not in response.text
    response = browser.post(
        "/owner/review/review-1/accept", headers={"x-test-owner": "yes"}, data={"csrf": "csrf"}
    )
    assert response.status_code == 409
    assert control.accepted == []


def test_review_allows_full_path_only_release_for_a_still_absent_file(tmp_path, monkeypatch):
    seen = []
    original = egress.postfilter

    def check_principal(*args):
        principal = effective_principal()
        seen.append((principal.audience_id, principal.issuer_family, principal.resolved))
        return original(*args)

    monkeypatch.setattr(egress, "postfilter", check_principal)
    browser, control = client(tmp_path)
    _owner_policy(tmp_path, ceiling=6)
    _write_review(control, path="Knowledge Base/Notes/new.md", before=None)

    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})

    assert response.status_code == 200
    assert "Knowledge Base/Notes/new.md" in response.text
    assert seen == [("owner", "native-owner-github", True)]
    assert browser.post(
        "/owner/review/review-1/accept", headers={"x-test-owner": "yes"},
        data={"csrf": "csrf"}, follow_redirects=False,
    ).status_code == 303
    assert control.accepted == ["review-1"]


def test_accept_refetches_and_rechecks_policy_after_successful_get(tmp_path):
    browser, control = client(tmp_path)
    target = tmp_path / "Knowledge Base" / "Notes" / "current.md"
    target.parent.mkdir(parents=True)
    target.write_text("current", encoding="utf-8")
    _owner_policy(tmp_path, ceiling=6)
    _write_review(control, path="Knowledge Base/Notes/current.md", before="current")
    assert browser.get(
        "/owner/review/review-1", headers={"x-test-owner": "yes"}
    ).status_code == 200

    _owner_policy(tmp_path, ceiling=0)
    response = browser.post(
        "/owner/review/review-1/accept",
        headers={"x-test-owner": "yes"},
        data={"csrf": "csrf"},
    )

    assert response.status_code == 409
    assert control.accepted == []


def test_nested_review_secret_or_receipt_failure_never_exposes_or_accepts(
    tmp_path, monkeypatch
):
    browser, control = client(tmp_path)
    secret = "ghp_" + "a" * 36
    control.current_review = SimpleNamespace(
        review_id="review-1",
        state="prepared",
        action="approve",
        expired=False,
        result=None,
        body={},
        display={"consequences": {"nested": secret}},
    )
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert response.status_code == 409
    assert secret not in response.text

    control.current_review.display = {"consequences": {"safe": "value"}}
    monkeypatch.setattr(
        egress, "emit_boundary_receipt", lambda _collector: (_ for _ in ()).throw(OSError())
    )
    response = browser.post(
        "/owner/review/review-1/accept",
        headers={"x-test-owner": "yes"},
        data={"csrf": "csrf"},
    )
    assert response.status_code in {409, 503}
    assert control.accepted == []
    assert not effective_principal().resolved


def test_home_metadata_is_postfiltered_before_interpolation(tmp_path):
    browser, control = client(tmp_path)
    secret = "ghp_" + "a" * 36
    control.session_items = [
        {"session_id": "session-1", "audience_id": secret, "issuer_family": "oauth"}
    ]

    response = browser.get("/owner", headers={"x-test-owner": "yes"})

    assert response.status_code == 503
    assert secret not in response.text
    assert not effective_principal().resolved


@pytest.mark.parametrize("location", ["before", "after", "canonical_operation", "canonical_yaml", "command"])
def test_all_rendered_review_content_is_filtered_on_get_and_direct_post(tmp_path, location):
    browser, control = client(tmp_path)
    secret = "ghp_" + "a" * 36
    _write_review(control, path="note.md", before=None)
    review = control.current_review
    if location in {"before", "after"}:
        review.display["write_images"][0][location] = secret
        if location == "before":
            (tmp_path / "note.md").write_text(secret, encoding="utf-8")
    elif location == "command":
        review.action, review.state = "migration", "accepted"
        review.body = {"service_unit": str(tmp_path / secret), "service_interpreter": "/usr/bin/python"}
    else:
        review.display[location] = {"nested": {"value": secret}}
    for response in (
        browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"}),
        browser.post("/owner/review/review-1/accept", headers={"x-test-owner": "yes"}, data={"csrf": "csrf"}),
    ):
        assert response.status_code == 409
        assert secret not in response.text
        assert '<form ' not in response.text
    assert control.accepted == []


@pytest.mark.parametrize("change", ["changed", "missing", "appeared", "symlink", "large"])
def test_stale_or_unsafe_file_images_refuse_display_and_acceptance(tmp_path, change):
    from exomem.vocabulary_effects import _MAX_IMAGE_BYTES

    browser, control = client(tmp_path)
    target = tmp_path / "note.md"
    before = None if change == "appeared" else "original bytes"
    _write_review(control, path="note.md", before=before)
    if change == "symlink":
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text(before, encoding="utf-8")
        target.symlink_to(elsewhere)
    elif change != "missing":
        target.write_text("x" * (_MAX_IMAGE_BYTES + 1) if change == "large" else "different", encoding="utf-8")
    for response in (
        browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"}),
        browser.post("/owner/review/review-1/accept", headers={"x-test-owner": "yes"}, data={"csrf": "csrf"}),
    ):
        assert response.status_code in {409, 503}
        assert "original bytes" not in response.text
        assert '<form ' not in response.text
    assert control.accepted == []


def test_blocked_policy_refuses_even_a_pathless_review(tmp_path):
    from exomem.governance import policy

    browser, control = client(tmp_path)
    _owner_policy(tmp_path, ceiling=6)
    (tmp_path / "Knowledge Base/_Governance/rules/owner.yaml").write_text("invalid: [", encoding="utf-8")
    assert policy.load(tmp_path).blocked
    _write_review(control, path="note.md", before=None)
    control.current_review.display = {"audience_id": "agent"}
    assert browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"}).status_code == 409
    assert browser.post(
        "/owner/review/review-1/accept", headers={"x-test-owner": "yes"}, data={"csrf": "csrf"},
    ).status_code == 409
    assert control.accepted == []


def test_mutating_filter_cannot_modify_sealed_review_and_principal_is_restored(tmp_path, monkeypatch):
    from exomem.governance.principal import RequestPrincipal, request_scope
    from exomem.server_owner import _released_review
    from exomem.vocabulary_control import _freeze

    _, control = client(tmp_path)
    _write_review(control, path="note.md", before=None)
    review = control.current_review
    review.display = _freeze({"canonical_operation": {"body": {"value": "original"}}})

    def mutate(_command, value, _root):
        assert effective_principal().audience_id == "owner"
        value["display"]["canonical_operation"]["body"]["value"] = "changed"
        return value

    agent = RequestPrincipal(audience_id="agent", surface="mcp")
    with request_scope(agent):
        assert _released_review(tmp_path, review)["display"]["canonical_operation"]["body"]["value"] == "original"
        assert effective_principal() == agent
        monkeypatch.setattr(egress, "postfilter", mutate)
        with pytest.raises(ValueError, match="fully disclosed"):
            _released_review(tmp_path, review)
        assert effective_principal() == agent
    assert review.display["canonical_operation"]["body"]["value"] == "original"


def test_concurrent_retag_cannot_authorize_the_old_restricted_snapshot(tmp_path, monkeypatch):
    from exomem import server_owner

    browser, control = client(tmp_path)
    _owner_policy(tmp_path, ceiling=0)
    (tmp_path / "Knowledge Base/_Governance/scopes/notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Confidential\ntags: ["confidential"]\ndefault_deny: true\n',
        encoding="utf-8",
    )
    target = tmp_path / "note.md"
    private = "---\ntags: [confidential]\n---\nRestricted original bytes"
    _write_review(control, path="note.md", before=private)
    original_read = server_owner._current_image

    def race(root, relative):
        result = original_read(root, relative)
        target.write_text("Public replacement", encoding="utf-8")
        return result

    monkeypatch.setattr(server_owner, "_current_image", race)
    for method, path, kwargs in (
        (browser.get, "/owner/review/review-1", {}),
        (browser.post, "/owner/review/review-1/accept", {"data": {"csrf": "csrf"}}),
    ):
        target.write_text(private, encoding="utf-8")
        response = method(path, headers={"x-test-owner": "yes"}, **kwargs)
        assert response.status_code == 409
        assert "Restricted original bytes" not in response.text
    assert control.accepted == []


def test_existing_yaml_image_allows_complete_path_based_release(tmp_path):
    browser, control = client(tmp_path)
    _owner_policy(tmp_path, ceiling=6)
    target = tmp_path / "Knowledge Base/Notes/registry.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("version: 1\n", encoding="utf-8")
    _write_review(control, path="Knowledge Base/Notes/registry.yaml", before="version: 1\n")
    response = browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"})
    assert response.status_code == 200
    assert "version: 1" in response.text


@pytest.mark.parametrize("existing", [False, True])
def test_non_markdown_review_refuses_unresolved_semantic_membership(tmp_path, existing):
    browser, control = client(tmp_path)
    _owner_policy(tmp_path, ceiling=6)
    (tmp_path / "Knowledge Base/_Governance/scopes/notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Confidential\ntags: ["confidential"]\ndefault_deny: true\n',
        encoding="utf-8",
    )
    before = "version: 1\n" if existing else None
    if existing:
        (tmp_path / "registry.yaml").write_text(before, encoding="utf-8")
    _write_review(control, path="registry.yaml", before=before)
    assert browser.get("/owner/review/review-1", headers={"x-test-owner": "yes"}).status_code == 409
    assert browser.post(
        "/owner/review/review-1/accept", headers={"x-test-owner": "yes"}, data={"csrf": "csrf"},
    ).status_code == 409
    assert control.accepted == []
