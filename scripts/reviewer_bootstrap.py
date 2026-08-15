#!/usr/bin/env python3
"""Drive the virgin-install reviewer OAuth bootstrap end to end.

The procedure in `substrate/docs/runbooks/exomem-hosted-alpha.md` (Virgin-install
reviewer OAuth bootstrap) is a dozen ordered control-plane calls with two hard
timing constraints and several preconditions that are enforced only in SQL. Run
by hand it is reliably wrong; every failure burns a single-use invite, an email
alias, a stage and a client, and a failed attempt strands a tenant that then
blocks the retry.

This encodes the whole sequence. Three subcommands:

    preflight   Report every known blocker before anything is consumed.
    prepare     Create the stage, pinned client and invite. Nothing is spent yet.
    run         Given the emailed invite token, run the rest with no gaps.

Design notes, each of which is a bug this script exists to prevent:

* The bootstrap client MUST use a `localhost` loopback redirect. The server
  validates a requested `127.0.0.1` redirect as `localhost`, so a client
  registered on the IP literal can never authorize.
* `POST /access/redeem` is a browser-shaped route: it needs an `Origin` header
  matching `Host` or it returns CSRF_REJECTED.
* The token exchange accepts EXACTLY six form fields and an exact `resource`.
  Any extra or missing field is `invalid_request`, not `invalid_grant`.
* The internal-canary credential is joined to the bootstrap's OWN outcome
  assignment, and that assignment expires while the cell provisions (~15 min).
  So `run` issues both sibling credentials IMMEDIATELY after the authority is
  consumed, in parallel with provisioning. It never waits for CELL_READY.
* Creating the authority is the irreversible step: it spends the invite whether
  it later succeeds, expires or is revoked. `prepare` therefore stops short of
  it, so the invite keeps its full 7-day life while a human fetches the token.

Secrets are never printed. Issued reviewer credentials are written to the state
directory with mode 0600 and only their presence is reported.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

CANDIDATE_PROFILE = "hosted-alpha-agent-v1"
LOOPBACK_REDIRECT = "http://localhost:47831/callback"
CLAUDE_CIMD_CLIENT_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CIMD_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SCOPES = "exomem.read exomem.write offline_access"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx instead of following it.

    `/oauth/authorize` answers 303 and sets the transaction cookie ON THAT
    RESPONSE. urlopen follows the redirect by default, so the caller sees the
    final 200 HTML page and — because no cookie processor is installed — the
    transaction cookie is dropped with the intermediate response. Returning None
    here makes urllib raise HTTPError for the 3xx, which the caller already
    handles, keeping both the status and the Set-Cookie headers.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(delta: timedelta) -> str:
    return (utc_now() + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def client_config_sha256(
    *, platform: str, admission_mode: str, client_id: str, redirect_uris: list[str]
) -> str:
    """Canonical OAuth client-config digest.

    Domain-separated SHA-256 over compact JSON with sorted keys and sorted
    redirect URIs. Must match `oauthClientConfigSha256` in the control plane
    byte for byte or the stage and client will not join.
    """
    payload = {
        "admission_mode": admission_mode,
        "client_id": client_id,
        "platform": platform,
        "redirect_uris": sorted(redirect_uris),
        "token_endpoint_auth_method": "none",
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"exomem-oauth-client-config:v1\0" + body).hexdigest()


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


class ControlPlane:
    """Thin control-plane client that records every exchange."""

    def __init__(self, base_url: str, admin_token: str, state_dir: Path):
        self.base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cookies: dict[str, str] = {}

    def _record(self, label: str, payload: object) -> None:
        path = self.state_dir / f"{label}.json"
        path.write_text(json.dumps(payload, indent=1, sort_keys=True))
        path.chmod(0o600)

    def call(
        self,
        method: str,
        path: str,
        *,
        label: str,
        body: dict | None = None,
        admin: bool = True,
        form: dict | None = None,
        headers: dict | None = None,
        send_cookies: bool = False,
    ) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data: bytes | None = None
        request_headers = dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        if admin:
            request_headers["Authorization"] = f"Bearer {self._admin_token}"
        if send_cookies and self.cookies:
            request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None:
            self._record(f"{label}.request", body)

        request = urllib.request.Request(url, data=data, method=method)
        for key, value in request_headers.items():
            request.add_header(key, value)
        try:
            with _OPENER.open(request) as response:
                status, raw, cookie_headers = (
                    response.status,
                    response.read(),
                    response.headers.get_all("Set-Cookie") or [],
                )
                location = response.headers.get("Location")
        except urllib.error.HTTPError as error:  # noqa: PERF203 - explicit branch
            status, raw = error.code, error.read()
            cookie_headers = error.headers.get_all("Set-Cookie") or []
            location = error.headers.get("Location")

        for cookie in cookie_headers:
            name, _, rest = cookie.partition("=")
            value = rest.split(";", 1)[0]
            if value:
                self.cookies[name.strip()] = value
            else:
                self.cookies.pop(name.strip(), None)

        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            parsed = {"_raw": raw.decode(errors="replace")[:400]}
        if location:
            parsed = {**parsed, "_location": location}
        self._record(f"{label}.response", {"status": status, **parsed})
        return status, parsed


def preflight(cp: ControlPlane, candidate_id: str) -> bool:
    """Report every blocker. Returns True when a bootstrap can succeed."""
    ok = True

    status, contracts = cp.call("GET", "/api/exomem/admin/contracts", label="preflight-contracts")
    if status != 200:
        print(f"  FAIL  contracts endpoint returned {status}")
        return False

    live = contracts.get("liveCohortCandidateId")
    print(f"  {'ok  ' if live is None else 'FAIL'}  live cohort: {live or 'none'}")
    ok &= live is None

    rollout = next(
        (r for r in contracts.get("rolloutStatus", []) if r["candidateId"] == candidate_id),
        None,
    )
    if rollout is None:
        print(f"  FAIL  candidate {candidate_id[:8]} not present")
        return False
    good = rollout["state"] == "pending"
    print(
        f"  {'ok  ' if good else 'FAIL'}  candidate {candidate_id[:8]}: "
        f"{rollout['state']} {rollout['sourceRelease']} "
        f"routable={rollout['routableCellCount']}"
    )
    ok &= good

    status, clients = cp.call("GET", "/api/exomem/admin/oauth-clients", label="preflight-clients")
    active = [a for a in clients.get("bootstrapAuthorities", []) if a.get("state") == "active"]
    print(f"  {'ok  ' if not active else 'FAIL'}  active bootstrap authorities: {len(active)}")
    ok &= not active

    status, capacity = cp.call("GET", "/api/exomem/admin/capacity", label="preflight-capacity")
    cap = capacity.get("capacity", {})
    free_runtime = cap.get("runtimeCapacitySlots", 0) - cap.get("reservedRuntimeSlots", 0)
    free_claims = cap.get("provisionClaimCapacity", 0) - cap.get("activeProvisionClaims", 0)
    good = free_runtime > 0 and free_claims > 0
    print(
        f"  {'ok  ' if good else 'FAIL'}  capacity: {free_runtime} runtime slot(s), "
        f"{free_claims} provision claim(s) free"
    )
    ok &= good

    print(
        "\n  NOTE  A reviewer-purpose tenant with a bound or CELL_READY cell also blocks\n"
        "        the bootstrap, and is not visible through any admin endpoint. If the\n"
        "        authority call returns 400 with everything above green, that is almost\n"
        "        certainly the cause; the stranded tenant must be deleted first."
    )
    return bool(ok)


def prepare(cp: ControlPlane, candidate_id: str, email: str, locks: dict) -> dict:
    """Create stage, pinned client and invite. Spends nothing irreversible."""
    client_id = f"exomem-reviewer-bootstrap-{uuid.uuid4()}"
    verifier, challenge = pkce_pair()
    config_sha = client_config_sha256(
        platform="claude",
        admission_mode="pinned",
        client_id=client_id,
        redirect_uris=[LOOPBACK_REDIRECT],
    )

    status, stage = cp.call(
        "POST",
        "/api/exomem/admin/contracts",
        label="prepare-stage",
        body={
            "action": "create-stage",
            "candidateId": candidate_id,
            "platform": "claude",
            "expiresAt": stamp(timedelta(minutes=55)),
            "packageSha256": locks["claude_package"],
            "archiveSha256": locks["claude_archive"],
            "compatibilitySha256": locks["compatibility"],
            "contractSha256": locks["contract"],
            "pluginVersion": locks["plugin_version"],
            "oauthClientConfigSha256": config_sha,
            "registeredAppIdSha256": None,
        },
    )
    if status != 200:
        raise SystemExit(f"create-stage failed: {status} {stage}")
    stage_id = stage["stage"]["id"]

    status, client = cp.call(
        "POST",
        "/api/exomem/admin/oauth-clients",
        label="prepare-client",
        body={
            "action": "register_pinned",
            "platform": "claude",
            "stagedClientReleaseId": stage_id,
            "clientId": client_id,
            "redirectUris": [LOOPBACK_REDIRECT],
        },
    )
    if status != 200:
        raise SystemExit(f"register_pinned failed: {status} {client}")

    status, invite = cp.call(
        "POST",
        "/api/exomem/admin/invites",
        label="prepare-invite",
        body={
            "email": email,
            "source": "complimentary",
            "marketplaceReviewerPurpose": True,
        },
    )
    if status != 201:
        raise SystemExit(f"invite failed: {status} {invite}")

    context = {
        "candidateId": candidate_id,
        "stageId": stage_id,
        "oauthClientId": client["id"],
        "clientId": client_id,
        "inviteId": invite["inviteId"],
        "email": email,
        "codeVerifier": verifier,
        "codeChallenge": challenge,
        "state": base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("="),
    }
    path = cp.state_dir / "bootstrap-context.json"
    path.write_text(json.dumps(context, indent=1))
    path.chmod(0o600)
    return context


def run(cp: ControlPlane, context: dict, token: str, locks: dict) -> None:
    """Authority through both canary credentials, with no waiting in between."""
    resource = f"{cp.base_url}/api/exomem/mcp/v1"

    status, authority = cp.call(
        "POST",
        "/api/exomem/admin/oauth-clients",
        label="run-authority",
        body={
            "action": "create_reviewer_bootstrap",
            "inviteId": context["inviteId"],
            "stagedClientReleaseId": context["stageId"],
            "oauthClientId": context["oauthClientId"],
            "expiresAt": stamp(timedelta(minutes=28)),
        },
    )
    if status != 200:
        raise SystemExit(
            f"create_reviewer_bootstrap failed: {status} {authority}\n"
            "If preflight was green this is most likely a stranded reviewer tenant "
            "holding a bound/CELL_READY cell. Delete it and retry."
        )
    print(f"  authority {authority['authority']['id'][:8]} active")

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": context["clientId"],
            "redirect_uri": LOOPBACK_REDIRECT,
            "resource": resource,
            "scope": SCOPES,
            "state": context["state"],
            "code_challenge": context["codeChallenge"],
            "code_challenge_method": "S256",
        }
    )
    status, _ = cp.call(
        "GET", f"/api/exomem/oauth/authorize?{query}", label="run-authorize", admin=False
    )
    if status != 303:
        raise SystemExit(f"authorize expected 303, got {status}")
    print("  authorized (303)")

    origin_headers = {"Origin": cp.base_url}
    status, redeemed = cp.call(
        "POST",
        "/api/exomem/access/redeem",
        label="run-redeem",
        body={"token": token},
        admin=False,
        headers=origin_headers,
        send_cookies=True,
    )
    if status != 200 or not redeemed.get("destination"):
        raise SystemExit(f"redeem failed: {status} {redeemed}")
    code = urllib.parse.parse_qs(urllib.parse.urlparse(redeemed["destination"]).query)["code"][0]
    print("  invite redeemed, authorization code issued")

    status, tokens = cp.call(
        "POST",
        "/api/exomem/oauth/token",
        label="run-token",
        admin=False,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": context["clientId"],
            "redirect_uri": LOOPBACK_REDIRECT,
            "code_verifier": context["codeVerifier"],
            "resource": resource,
        },
    )
    print(
        "  token exchange: "
        + ("ok" if status == 200 else f"{status} {tokens.get('error')} (non-fatal)")
    )

    status, clients = cp.call(
        "GET", "/api/exomem/admin/oauth-clients", label="run-authority-outcome"
    )
    outcome = next(
        (
            a
            for a in clients.get("bootstrapAuthorities", [])
            if a["id"] == authority["authority"]["id"]
        ),
        None,
    )
    if not outcome or outcome.get("state") != "consumed":
        raise SystemExit(f"authority did not reach consumed: {outcome}")
    tenant_id = outcome["outcomeTenantId"]
    assignment_id = outcome["outcomeAssignmentId"]
    generation = outcome.get("outcomeAssignmentGeneration") or 1
    print(
        f"  authority consumed; tenant {tenant_id[:8]} assignment {assignment_id[:8]} gen {generation}"
    )

    # The assignment expires while the cell provisions. Issue credentials NOW.
    # Both platforms in this one pass: promote-cohort needs a claudeArtifactId AND
    # an openaiArtifactId, and each canary credential is welded to this bootstrap's
    # own assignment/generation, so a second pass later cannot supply the other one.
    # OpenAI registers `pinned` on a loopback redirect: there is no published
    # ChatGPT client-metadata-document URL to admit by CIMD.
    siblings = (
        (
            "claude",
            "cimd",
            CLAUDE_CIMD_CLIENT_ID,
            [CLAUDE_CIMD_REDIRECT],
            locks["claude_package"],
            locks["claude_archive"],
            None,
        ),
        (
            "openai",
            "pinned",
            f"exomem-reviewer-openai-{uuid.uuid4()}",
            [LOOPBACK_REDIRECT],
            locks["openai_package"],
            locks["openai_archive"],
            locks["openai_registered_app"],
        ),
    )
    for platform, admission, sibling_client, redirects, package, archive, app_digest in siblings:
        config_sha = client_config_sha256(
            platform=platform,
            admission_mode=admission,
            client_id=sibling_client,
            redirect_uris=redirects,
        )
        status, stage = cp.call(
            "POST",
            "/api/exomem/admin/contracts",
            label=f"run-sibling-stage-{platform}",
            body={
                "action": "create-stage",
                "candidateId": context["candidateId"],
                "platform": platform,
                "expiresAt": stamp(timedelta(minutes=55)),
                "packageSha256": package,
                "archiveSha256": archive,
                "compatibilitySha256": locks["compatibility"],
                "contractSha256": locks["contract"],
                "pluginVersion": locks["plugin_version"],
                "oauthClientConfigSha256": config_sha,
                "registeredAppIdSha256": app_digest,
            },
        )
        if status != 200:
            raise SystemExit(f"{platform} sibling stage failed: {status} {stage}")

        status, sibling = cp.call(
            "POST",
            "/api/exomem/admin/oauth-clients",
            label=f"run-sibling-client-{platform}",
            body={
                "action": f"register_{admission}",
                "platform": platform,
                "stagedClientReleaseId": stage["stage"]["id"],
                "clientId": sibling_client,
                "redirectUris": redirects,
                # The stage row is matched with
                #   stage.registered_app_id_sha256 IS NOT DISTINCT FROM <this>
                # so omitting it on an OpenAI client whose stage carries the
                # registered-app digest silently matches no stage and 400s.
                **({"registeredAppIdSha256": app_digest} if app_digest else {}),
            },
        )
        if status != 200:
            raise SystemExit(
                f"{platform} sibling registration failed: {status} {sibling}\n"
                "A 400 here usually means the client_id host is missing from "
                "EXOMEM_CIMD_ALLOWED_HOSTS."
            )

        status, credential = cp.call(
            "POST",
            "/api/exomem/admin/reviewer-access",
            label=f"run-canary-{platform}",
            body={
                "credentialKind": "internal_canary",
                "platform": platform,
                "tenantId": tenant_id,
                "candidateId": context["candidateId"],
                "assignmentId": assignment_id,
                "assignmentGeneration": generation,
                "stagedClientReleaseId": stage["stage"]["id"],
                "oauthClientId": sibling["id"],
                "fixtureVersion": locks["fixture_version"],
                "fixturePayloadDigest": locks["fixture_digest"],
                "expiresAt": stamp(timedelta(hours=24)),
            },
        )
        if status != 201:
            raise SystemExit(f"{platform} canary credential failed: {status} {credential}")
        print(
            f"  {platform} canary credential issued "
            f"(username/password saved to run-canary-{platform}.response.json, mode 600)"
        )

    print("\n  Bootstrap complete. The cell provisions in the background; poll the")
    print("  owner status view for CELL_READY before the clean-client evidence run.")


def load_locks(repo: Path) -> dict:
    generated = repo / "plugins" / "hosted" / "generated"
    claude = json.loads((generated / "claude.lock.json").read_text())
    claude_zip = json.loads((generated / "claude.zip.lock.json").read_text())
    openai = json.loads((generated / "openai.lock.json").read_text())
    openai_zip = json.loads((generated / "openai.zip.lock.json").read_text())
    fixture = json.loads(
        (repo / "plugins" / "hosted" / "marketplace-review-fixture-v1.json").read_text()
    )
    return {
        "claude_package": claude["artifact_sha256"],
        "claude_archive": claude_zip["archive_sha256"],
        "openai_package": openai["artifact_sha256"],
        "openai_archive": openai_zip["archive_sha256"],
        "openai_registered_app": openai["registered_app_id_sha256"],
        "compatibility": claude["compatibility_sha256"],
        "contract": claude["schema_contract_sha256"],
        "plugin_version": claude["plugin_version"],
        "fixture_version": fixture["fixture_version"],
        "fixture_digest": fixture["payload_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "prepare", "run"))
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--email", help="reviewer alias, required for prepare")
    parser.add_argument("--token", help="emailed invite token, required for run")
    args = parser.parse_args()

    base_url = os.environ.get("EXOMEM_PUBLIC_BASE_URL")
    admin_token = os.environ.get("EXOMEM_ADMIN_TOKEN")
    if not base_url or not admin_token:
        print("EXOMEM_PUBLIC_BASE_URL and EXOMEM_ADMIN_TOKEN must be set", file=sys.stderr)
        return 2

    cp = ControlPlane(base_url, admin_token, args.state_dir)
    locks = load_locks(args.repo)

    if args.command == "preflight":
        print("preflight:")
        return 0 if preflight(cp, args.candidate_id) else 1

    if args.command == "prepare":
        if not args.email:
            print("--email is required for prepare", file=sys.stderr)
            return 2
        print("preflight:")
        if not preflight(cp, args.candidate_id):
            print("\nrefusing to prepare while preflight is red")
            return 1
        print("\nprepare:")
        context = prepare(cp, args.candidate_id, args.email, locks)
        print(f"  stage   {context['stageId']}")
        print(f"  client  {context['oauthClientId']}")
        print(f"  invite  {context['inviteId']}")
        print(f"\n  Invite sent to {args.email}.")
        print("  Nothing irreversible has been spent: the invite keeps its full expiry")
        print("  until `run` creates the authority. Fetch the token from the email's")
        print("  text/plain part (the tracked HTML link drops the URL fragment), then:")
        print(f"\n    {sys.argv[0]} run --candidate-id {args.candidate_id} \\")
        print(f"      --state-dir {args.state_dir} --token <token>")
        return 0

    if not args.token:
        print("--token is required for run", file=sys.stderr)
        return 2
    context = json.loads((args.state_dir / "bootstrap-context.json").read_text())
    print("run:")
    run(cp, context, args.token, locks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
