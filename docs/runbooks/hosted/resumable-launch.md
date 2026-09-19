<!-- authority:non-specification -->

# Resumable hosted launch

`accept_hosted_service.py launch` advances one durable owner or friends launch attempt. The default invocation performs only read-only preflight. `--execute` permits the two existing runtime control effects and, after ordinary owner consent, one reviewed memory write.

Keep `--state-dir` private and reuse the same `--run-id`, mode, milestone, and config for every retry. The runner rejects identity or config drift, fences concurrent invocations, and preserves stage deadlines and backoff across process restarts.

```bash
python infra/scripts/accept_hosted_service.py launch \
  --mode live \
  --milestone owner \
  --config /private/owner-launch.json \
  --state-dir /private/exomem-launch-state \
  --run-id owner-20260919
```

Inspect the JSON result before enabling effects. Deployment image and lock values in preflight are **declared** config. The current public status contract cannot independently observe them, so the report records their observation as unavailable rather than verified.

```bash
python infra/scripts/accept_hosted_service.py launch \
  --mode live \
  --milestone owner \
  --config /private/owner-launch.json \
  --state-dir /private/exomem-launch-state \
  --run-id owner-20260919 \
  --execute
```

The config is a closed schema. It names the environment, existing invitation reference, selected host, release and deployment identity, resource maximum, stage deadlines, polling bounds, and environment-variable references for the invitation token, operator credential, and owner session. Never put secret values in the config. For an owner launch, `resource_maximum.tenants` must be exactly one. `deadlines_seconds` must contain `preflight`, `runtime_target`, `runtime_activation`, `consent`, `service_ready`, and `milestone`.

The first executing invocation imports and activates the configured trusted runtime candidate through `/api/exomem/admin/contracts`. Every effect is journaled before the request. If an acknowledgement is lost, rerun the same command: the runner reads authoritative contract state and confirms the original effect without issuing a duplicate request.

Consent is an explicit checkpoint. The runner stores PKCE request state in `runs/<run-id>/launch-oauth-request.json` with mode `0600`. Open its `url`, complete ordinary browser OAuth for this runner's registered client, then resume with the callback returned through that client's redirect:

```bash
python infra/scripts/accept_hosted_service.py launch \
  --mode live --milestone owner \
  --config /private/owner-launch.json \
  --state-dir /private/exomem-launch-state \
  --run-id owner-20260919 --execute \
  --authorization-code "$OAUTH_CALLBACK_CODE" \
  --callback-state "$OAUTH_CALLBACK_STATE"
```

The owner session named by `oauth.owner_session` must also be present in the environment. Once protected OAuth and session state exist, the runner does not inspect the invitation again; a consumed invitation remains the same launch identity.

A token exchange has no public readback. If its response is lost or invalid, the runner marks that grant uncertain and refuses to replay its potentially consumed code. Rerun without callback values to create a new authorization grant for the same owner, client, and invitation identity, complete consent again, then resume with the new callback. Do not create another tenant, client, or invitation.

After consent, the runner reads `/api/exomem/status`. A retryable `preparing` or `degraded` state remains pending under the original deadline. `ready` advances to MCP initialization, canonical tool discovery, a reviewed and idempotent `remember` mutation, paraphrased recall, and `read_memory` of the returned citation. If the memory acknowledgement is lost, rerunning replays the same reviewed payload with the same idempotency key and reads the durable result.

The current command stops with `needs-attention` after the useful-memory check. For the owner milestone, attach independent evidence for host confirmation, continuity, governance readiness, and backup/restore. For friends, attach paid-path, isolation, capacity, and recovery evidence. The runner does not infer those claims from configuration or mark the milestone complete.

Reports and manifests redact resolved secret values. OAuth tokens, PKCE state, and mutation receipts stay under the private state directory. Treat any malformed or unknown state as an error; do not delete or hand-edit state to force progress. Start a new run ID only for a genuinely new launch attempt.

The launch lock currently requires Linux or WSL. Existing non-launch acceptance actions remain importable on other platforms.

A complete configuration template follows. Every digest and identity is a placeholder; select one reviewed registry entry and copy its ten canonical runtime target fields exactly. Compute `runtime_target_digest` as SHA-256 of the key-sorted compact JSON object with one trailing newline. This template does not claim that any named release is deployable.

```json
{
  "schema_version": 1,
  "environment": "owner-alpha",
  "control_base_url": "https://substratesystems.io",
  "invitation": {
    "reference": "existing-invitation-reference",
    "token": {"source": "env", "name": "EXOMEM_OWNER_INVITATION_TOKEN"}
  },
  "selected_host": "claude",
  "oauth": {
    "authorization_server_metadata": "https://substratesystems.io/.well-known/oauth-authorization-server/api/exomem/oauth",
    "resource": "https://substratesystems.io/api/exomem/mcp/v1",
    "client_id": "existing-registered-client-id",
    "redirect_uri": "http://127.0.0.1:8765/callback",
    "owner_session": {"source": "env", "name": "EXOMEM_OWNER_SESSION"}
  },
  "release": {
    "candidate_id": "00000000-0000-7000-8000-000000000000",
    "runtime_target": {
      "releaseVersion": "0.0.0",
      "sourceCommit": "0000000000000000000000000000000000000000",
      "runtimeImage": "ghcr.io/artexis10/exomem@sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "runtimeCandidateSha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "protocolVersion": "replace-with-reviewed-value",
      "agentProfile": "replace-with-reviewed-value",
      "gatewayContractDigest": "0000000000000000000000000000000000000000000000000000000000000000",
      "commandFingerprint": "0000000000000000000000000000000000000000000000000000000000000000",
      "schemaDigest": "0000000000000000000000000000000000000000000000000000000000000000",
      "compatibilityDigest": "0000000000000000000000000000000000000000000000000000000000000000"
    },
    "runtime_target_digest": "replace-with-canonical-runtime-target-sha256"
  },
  "deployment": {
    "source_commit": "0000000000000000000000000000000000000000",
    "lock_digest": "0000000000000000000000000000000000000000000000000000000000000000",
    "revision": "replace-with-deployment-revision",
    "operator_credential": {"source": "env", "name": "EXOMEM_OPERATOR_CREDENTIAL"}
  },
  "resource_maximum": {
    "tenants": 1,
    "storage_bytes": 10737418240,
    "runtime_slots": 1,
    "provision_claims": 1
  },
  "deadlines_seconds": {
    "preflight": 30,
    "runtime_target": 60,
    "runtime_activation": 60,
    "consent": 3600,
    "service_ready": 3600,
    "milestone": 604800
  },
  "polling": {"initial_seconds": 1, "maximum_seconds": 30}
}
```

`local` and `cluster` mode accept loopback control, OAuth, and MCP destinations only. `live` requires HTTPS on a non-loopback destination. The mode therefore cannot be used as a label for evidence gathered against another environment.

The PKCE URL belongs to this runner attempt and the already registered `client_id`. Its callback must return through that client's configured redirect. It is not proof that Claude or OpenAI performed consent, and an authorization code already consumed by a native host must never be supplied here. Native-host consent and use remain separate milestone evidence. The runner does not register a new client or allocate a replacement invitation.

If a stage reaches its deadline, ordinary reruns keep it paused with the original timestamps. After reviewing the checkpoint, pass `--resume` with the same launch identity to record a new bounded window; the manifest retains the prior deadline history.
