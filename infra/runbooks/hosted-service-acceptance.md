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
    "authorization_server_metadata": "https://mcp.example/.well-known/oauth-authorization-server/api/exomem/oauth",
    "resource": "https://mcp.example/api/exomem/mcp/v1",
    "client_id": "approved-hosted-acceptance-client",
    "redirect_uri": "http://127.0.0.1:8765/callback"
  },
  "mcp_endpoint": "https://mcp.example/api/exomem/mcp/v1",
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

Authorization metadata, protected resource, and public MCP endpoint must be
HTTPS; `oauth.resource` must byte-match `mcp_endpoint`. The loopback redirect is the
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
  --run-id hosted-acceptance-20260907 --resume --tenant synthetic
```

Open the emitted authorization URL and complete ordinary customer consent.
Resume it with the returned code and state:

```bash
python3 infra/scripts/accept_hosted_service.py run \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume \
  --tenant synthetic --authorization-code '<code>' --callback-state '<state>' \
  --report ./hosted-acceptance-report.json
```

Repeat the public OAuth flow for `--tenant isolation`; each reserved tenant has
its own normal OAuth token family. The manifest fixes runtime identity, tenants and per-stage mutation request
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

Seed that fixture through the ordinary public MCP `remember` tool for both
reserved tenant token families before measuring. Each capture has a stable
idempotency key, so an interrupted seeding run resumes without a second write.
The benchmark refuses locally generated-only corpus: its performance evidence
records only corpus that was acknowledged by both cells.

```bash
python3 infra/scripts/accept_hosted_service.py seed-corpus \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume

python3 infra/scripts/accept_hosted_service.py benchmark \
  --config ./hosted-acceptance.json --state-dir ./private-acceptance-state \
  --run-id hosted-acceptance-20260907 --resume \
  --report ./hosted-acceptance-report.json
```

`benchmark` uses five concurrent normal MCP clients (three synthetic and two
isolation), records 100 warm samples per initialize/list/capture/recall and 20
fresh-client resets. A reset is exactly a new `MCPClient` instance: it does not
restart a process, cell, storage, tenant, or service. The report exposes the
configured runtime tuple separately from runtime evidence; configuration alone
is never a runtime verification. `run` checkpoints token expiry without
sleeping, persists a normal refresh-token rotation after the stored expiry, and
only passes continuity after the one-hour window when a fresh client performs a
cited recall and readback using that persisted OAuth state.

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
