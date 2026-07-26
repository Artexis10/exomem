# Hosted client plugin candidates

`plugins/hosted/definition.json` is the single tenant-neutral release input. It
pins the production `https://substratesystems.io/api/exomem/mcp/v1` resource, the
immutable `hosted-alpha-agent-v1` command profile, package version, legal links,
and a `pending` distribution scope.

Render candidates only with the registered OpenAI app ID supplied by the release
operator:

```powershell
python scripts/hosted-plugin.py render --openai-app-id asdk_app_<registered-id>
python scripts/hosted-plugin.py check --openai-app-id asdk_app_<registered-id>
python scripts/hosted-plugin.py archive --openai-app-id asdk_app_<registered-id>
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
results are rejected. Demotion withdraws the platform candidate without deleting
tenant data.

The gateway supplies the OAuth discovery overlay for the raw Hosted schema:
read-only tools require `exomem.read`, mutating tools require `exomem.write`, and
runtime responses must carry `_meta['mcp/www_authenticate']`. The overlay is
included in the shared compatibility identity without altering raw cell tool
schemas, descriptions, or annotations.
