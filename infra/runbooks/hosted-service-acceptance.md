# Hosted service acceptance

Run this only after an operator has reserved the two named synthetic tenants
and frozen the exact runtime identity. The runner is deliberately unable to
create a tenant, preview deployment, cloud database branch, or provider
resource. It is not a way to reset a customer vault.

Create a public configuration file outside the report directory:

```json
{
  "schema_version": 1,
  "oauth": {
    "issuer": "https://issuer.example",
    "audience": "https://mcp.example",
    "client_id": "approved-hosted-acceptance-client",
    "redirect_uri": "http://127.0.0.1:8765/callback"
  },
  "mcp_endpoint": "https://mcp.example/mcp",
  "runtime": {
    "release": "0.73.1",
    "profile": "hosted-agent",
    "contract_digest": "<64 lowercase hex characters>",
    "runtime_image": "ghcr.io/artexis10/exomem@sha256:<64 lowercase hex characters>"
  },
  "tenants": {
    "synthetic": {"tenant_id": "reserved-synthetic", "reservation": "operator record"},
    "isolation": {"tenant_id": "reserved-isolation", "reservation": "operator record"}
  }
}
```

Issuer and public MCP endpoints must be HTTPS. The loopback redirect is the
approved local callback; it is not an exception for a live issuer or MCP
endpoint. Keep `--state-dir` on a private task-owned filesystem. It contains
PKCE state and, after a public authorization-code exchange, rotating tokens;
reports and configuration never contain token material.

```bash
python3 infra/scripts/accept_hosted_service.py prepare \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907

python3 infra/scripts/accept_hosted_service.py authorize \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume
```

Open the emitted authorization URL and complete ordinary customer consent.
Resume it with the returned code and state:

```bash
python3 infra/scripts/accept_hosted_service.py run \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume \
  --authorization-code '<code>' --callback-state '<state>' \
  --report ./hosted-acceptance-report.json
```

The manifest fixes runtime identity, tenants and per-stage mutation request
IDs atomically. A resumed run refuses identity drift and never repeats a
committed mutation. `blocked` is a checkpoint with one exact operator action;
it is neither a pass nor a reason to stop independent stages. In particular,
OAuth/MCP protocol evidence cannot certify Claude or OpenAI hosts.

Generate the deterministic reserved-cell corpus only under run-owned fixture
state; it has at least 1,000 notes and 10 MiB of public-safe varied content:

```bash
python3 infra/scripts/accept_hosted_service.py corpus \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume
```

Cleanup removes only fixture paths first registered under that exact run. It
does not delete a tenant, vault, backup, provider resource, or report:

```bash
python3 infra/scripts/accept_hosted_service.py cleanup \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume
```

Live host consent, privileged failure injection, provider cleanup, and
governed isolated restore remain separately authorized actions. Record their
unavailable prerequisites as blocked stages; do not turn a generic protocol
run into host certification.
