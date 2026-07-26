# Hosted client plugin candidates

`plugins/hosted/definition.json` is the single tenant-neutral release input. It
pins the production `https://substratesystems.io/api/exomem/mcp/v1` resource, the
immutable `hosted-alpha-agent-v1` command profile, package version, legal links,
and a `pending` distribution scope.

Render candidates only with the registered OpenAI app ID supplied by the release
operator:

```powershell
python scripts/hosted-plugin.py render --platform all --openai-app-id asdk_app_<registered-id>
python scripts/hosted-plugin.py check --platform all --openai-app-id asdk_app_<registered-id>
python scripts/hosted-plugin.py archive --platform all --openai-app-id asdk_app_<registered-id>
```

The generated Claude and OpenAI files share a compatibility identity but remain
pending. A locally discovered developer app ID is acceptable for package-shape
validation only; it does not prove that any friend can install the artifact.
Do not label either candidate available, private, unlisted, or cross-client ready
until a supported distribution channel and clean-account, content-bearing client
evidence have been recorded.

Promotion evidence must bind the generated lock and compatibility identity to a
native install, authorization, exact discovery, seeded content recall with a
citation, ordinary-conversation durable capture, and a later fresh-chat recall.
Validator, OAuth-only, discovery-only, bootstrap-only, metadata-only, and mocked
results are rejected. Cross-client resource equality is recorded only as
HMAC-SHA-256 values made with an operator-held per-run pairing key; raw client,
identity, tenant, entitlement, operation, cell, and volume identifiers never
enter the public promotion record. Demotion withdraws the platform candidate
without deleting tenant data and records only a stable public reason code.

Every Claude and OpenAI evidence record also carries the operator-signed,
64-hex `oauth_client_config_sha256`. The deployment computes it as the lowercase
SHA-256 digest of the exact byte string
`exomem-oauth-client-config:v1\0` followed by canonical UTF-8 JSON for
`{platform, admission_mode, client_id, redirect_uris(sorted exact raw strings),
token_endpoint_auth_method:'none'}`. Canonical JSON sorts object keys, has no
whitespace (`separators=(',', ':')`), and uses `ensure_ascii=false`. This tuple
is public; the evidence signature, not an additional HMAC secret, provides
authority. The package validator checks only the digest shape and evidence
signature. This is live promotion evidence, not package identity, so it must
not change a rendered archive, package lock, or compatibility digest.

The reusable promotion HMAC secret is read only from
`EXOMEM_HOSTED_PROMOTION_SECRET`; it is never accepted on the command line.
Run `hosted-plugin.py status` to obtain the current record digest, then pass its
state and SHA-256 through `--expected-state` and `--expected-record-sha256` for
compare-and-swap promotion or demotion. Live status validation requires the
trusted key ID plus that environment-provided secret and rechecks both package
and archive bytes. Its `oauth_client_config_sha256` map exposes the
redacted config digest for each platform (or `null` while no live evidence is
recorded), never the raw OAuth configuration.

The gateway supplies the OAuth discovery overlay for the raw Hosted schema:
read-only tools require `exomem.read`, mutating tools require `exomem.write`, and
runtime responses must carry `_meta['mcp/www_authenticate']`. The overlay is
included in the shared compatibility identity without altering raw cell tool
schemas, descriptions, or annotations.
