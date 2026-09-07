from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.testclient import TestClient

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
