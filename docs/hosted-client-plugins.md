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

The registered `asdk_app_*` value is package-only: it belongs in the OpenAI
`.app.json`, package locks, and promotion evidence. A portal-issued public
plugin identifier is a separate directory-submission concern. Never replace the
package registration ID with a directory identifier or add either private value
to a tracked listing input.

The provider-issued OpenAI directory ID is handoff-only. If a provider assigns
one, record only its SHA-256 in the signed receipt; never place the raw value in
a package, lock, archive, documentation, checked-in input, or log.

## Hosted user journey

Once a channel is actually published, the normal path is deliberately simple:
install or connect the official directory entry, complete the Hosted OAuth login
once, then use Claude, ChatGPT, or Codex normally. The remote MCP service and
bundled skills retrieve relevant governed context and preserve durable
conclusions where the client supports them. Users should not need a vault path,
manual MCP JSON, copied API token, or repeated `use Exomem` instruction.

Global custom instructions are only a fallback for a chat surface that connects
the MCP server but does not activate bundled skills. Keep that fallback concise:
tell the assistant to retrieve relevant material from the user's governed
Exomem store, cite retrieved material, and capture only durable conclusions or
explicitly requested facts. It must not claim access to native assistant memory,
arbitrary conversation history, or data outside the authorized Hosted store.

## Public-directory release materials

`plugins/hosted/marketplace-definition.json` is the canonical public listing
input; it imports runtime identity from `definition.json` rather than copying
endpoint or package fields. `marketplace-review-cases.json` carries the governed
positive and negative reviewer cases. The CLI only creates redacted local
materials; it never sends anything to a provider:

```powershell
python scripts/hosted-plugin.py directory-check --channel claude-connector
python scripts/hosted-plugin.py directory-render --channel claude-connector
python scripts/hosted-plugin.py directory-status --deployment-sha256 <trusted-deployment-sha256>
```

Each packet also carries the checked-in icon path and digest, canonical
`Productivity` category, documentation and setup URLs, concise read/write
capability metadata, and concrete use cases needed by current provider forms.
The operator confirms the provider portal's current category enum at submission
time; no credentials or provider-specific private values belong in the packet.
Claude packet validation additionally enforces the current name (100), tagline
(55), and description (2,000) character limits.

All signed directory evidence, receipts, status checks, submission transitions,
and activation must bind the same trusted deployment SHA-256. Record every
provider event with its exact listing version, prior record digest, expected
active digest, and signed receipt. A published revision is deliberately not
public yet: first store fresh post-install evidence at
`directory/post-install-evidence/<channel>/<published-submission-sha256>.json`,
then activate that exact revision with a compare-and-swap pointer:

```powershell
python scripts/hosted-plugin.py directory-record --channel claude-connector --directory-state published --expected-state approved --expected-record-sha256 <approved-record-sha256> --expected-active-submission-sha256 none --receipt <signed-receipt.json> --deployment-sha256 <trusted-deployment-sha256>
python scripts/hosted-plugin.py directory-activate --channel claude-connector --target-submission-sha256 <published-submission-sha256> --expected-active-submission-sha256 none --deployment-sha256 <trusted-deployment-sha256>
```

`none` is an explicit CAS assertion that no revision is currently active. The
commands are safe to retry after an interruption: an already appended event is
not duplicated, and retrying a withdrawal completes its pointer clear. Status
reports authoritative per-listing-version heads; an in-review v2 therefore does
not hide an active v1, and withdrawing v1 does not mutate v2.

For an OpenAI receipt, `provider_directory_id_sha256` is the lowercase SHA-256
of the raw UTF-8 provider-issued directory identity, after trimming outer
whitespace—the same byte-hash convention used for the registered `asdk_app_*`
package identity. The raw directory ID is optional before publication and
required when an OpenAI revision is published; its hash must match exactly.
Every signed production and post-install evidence document also carries the
exact boolean `sampled_output_sale_free: true`. It attests that the sampled
public response set contains no buy, Pro, subscribe, upgrade, checkout, or
other subscription-sale prompt; false or missing evidence blocks readiness or
activation.

There are three independent public channels: the Claude Connector Directory
(the remote MCP endpoint), a Claude community/public plugin distribution (the
public bundle that reuses that connector), and one universal OpenAI Plugin
Directory entry for both ChatGPT and Codex. The connector covers Claude.ai,
Desktop, Mobile, Code, and Cowork. Bundled plugin skills apply to Code and
Cowork; they do not enforce skill activation in Claude.ai. The universal OpenAI
entry covers ChatGPT and Codex. The OpenAI candidate uses the current
`mcp_servers` connection map; the Claude bundle retains its `mcpServers` map.

The directory packets are drafts, not listings. Each provider submission needs
an authorized operator, verified publisher, policy approval, and domain proof
where required. Submission readiness is deliberately narrower than public
activation: provider-matched reviewer access can unblock a draft submission
while broad admission stays closed and `ready`/`public` remain false.

## Marketplace reviewer and operator handoff

Follow the paired [Substrate Hosted Alpha operator runbook](https://github.com/substrate-systems/substrate/blob/main/docs/runbooks/exomem-hosted-alpha.md#marketplace-reviewer-access)
in `substrate-systems/substrate` at
`docs/runbooks/exomem-hosted-alpha.md`. It creates a dedicated immutable
reviewer-purpose tenant, seeds the checked-in generic fixture through normal
governed MCP writes, and, when the provider credential is issued, atomically
seals the temporary setup access. The fixture payload, version, digest, and
exact non-sensitive content are intentionally checked in; operators must seed
that exact fixture. Only raw reviewer credentials, reviewer identities, tenant
IDs and invites, live tenant exports or content, and content-bearing native
client acceptance evidence stay in the approved secret-manager/provider handoff
and out of Git and logs.

The signed, secret-free reviewer-access evidence is distinct from the provider
credential. It binds the matching provider and deployment, enabled feature
state, active credential state and bounded expiry, plus the fixture version and
payload digest. It proves only that the prepared reviewer flow is available; it
does not substitute for a native client review or make the listing public.

OpenAI has one additional submission handoff: signed prerequisite evidence must
attest that the walkthrough recording is prepared. The recording URL itself is
operator-supplied manually in the OpenAI portal and must never be checked in or
rendered. Claude packets must not inherit that OpenAI-only field, app identity,
or annotation explanation.

Before a provider submission, run the reviewed cases in clean native clients:
ChatGPT and Codex for the universal OpenAI entry, and Claude for its independent
channels. Prove OAuth, discovery, governed recall against the seeded fixture,
durable capture, later-chat recall, no-capture behavior, and revocation. Keep
content-bearing proof in the controlled acceptance workflow, not repository
evidence; validator-only, mocked, OAuth-only, and metadata-only checks are not
enough.

Provider portal submission, any manual recording upload, provider receipt,
rejection, approval, publication, smoke test, and withdrawal are operator
actions. Use `directory-record` only with the exact signed receipt and the
artifact/listing bindings from release tooling. Do not create a receipt or claim
portal success until the provider has actually returned one.

A reviewer credential, successful friends cohort, or provider approval is not
broad public-admission evidence. Before recording publication, operators still
need signed proof that ordinary eligible users can acquire access and that
capacity, quotas, abuse controls, spend alarms, support coverage, and the public
pricing decision are ready. Before activating a published revision, separately
record fresh non-reviewer install evidence for OAuth, discovery, governed
recall, durable capture, later-chat recall, no-capture behavior, and revocation.
The OpenAI plugin must never sell or upsell a digital subscription inside a
plugin interaction.

Provider submission, rejection, publication, smoke testing, and emergency
withdrawal are operator actions. Record a provider result only with the exact
listing and artifact bindings produced by the release tooling. If OAuth, MCP,
privacy, revocation, tenant isolation, or the exact promoted artifact regresses,
withdraw the affected directory channel immediately and separately demote the
package when the incident is a runtime or security failure. Withdrawing a
listing never deletes hosted tenant data; a rejected listing returns to draft
without demoting a healthy private package.

The paired Substrate deployment must provide truthful Hosted product, setup, and documentation copy,
privacy, terms, and support pages; the OpenAI domain proof route; OAuth discovery
and authorization challenge; and healthy MCP initialization and tool discovery.
`directory-status` stays fail-closed until fresh production probes for every
surface, exact live promotion bindings, and all operator prerequisites are
present. Current pending promotions therefore remain non-public.

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

OpenAI evidence additionally carries the signed `registered_app_id_sha256`.
Promotion requires it only for OpenAI, validates lowercase 64-hex form, and
requires exact equality with both the current OpenAI package lock and archive
lock. Claude evidence must not carry the field. `hosted-plugin.py status`
exposes the persisted value only as a digest.

The shared Claude CIMD test vector is canonical JSON
`{"admission_mode":"cimd","client_id":"https://claude.example.com/oauth/client","platform":"claude","redirect_uris":["https://claude.example.com/oauth/callback","https://claude.example.com/oauth/return"],"token_endpoint_auth_method":"none"}`
and digest `3c8bbd83906d29816f59d21b48a7e5a859379b124108b2abb1aa9a309ec3a339`.
The JSON has no `v` member; the version is solely in the domain prefix.

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
