<!-- authority:non-specification -->

# Exomem Cloud plugin operations

Claude and OpenAI packages use the same hand-authored skill and workflow files
under `src/exomem/_scaffold/_Schema/`. `plugins/cloud/definition.json` contains
public identity and listing URLs. The existing Cloud MCP/OAuth connection serves
the `product-cloud` surface. Self-hosted Claude Code remains the separate
`plugins/claude-code` package; historical Hosted Alpha packages are not current
Cloud distributions.

## Build and check

From a checkout with the pinned project uv available:

```bash
uv run --frozen python scripts/cloud-plugin.py build
uv run --frozen python scripts/cloud-plugin.py check
uv run --frozen python -m pytest -q tests/test_cloud_plugins.py tests/test_cloud_plugin_evals.py tests/test_package_skills.py
uv run --frozen python scripts/validate-public-artifacts.py --repository
```

Output is `plugins/cloud/generated/`: Claude's fixed HTTP `.mcp.json` and
`.claude-plugin/plugin.json`, OpenAI's portable Agent Plugins 1.0 `plugin.json`
and `mcp.json`, complete canonical skills, public assets, two deterministic ZIPs,
and `release.json`. Check regenerates into temporary storage and rejects drift,
missing/extra files and symlinks. A valid bundle is not proof of native behaviour.

Claude's bundle also registers the release-owned retrieval, capture and
continuation hooks through `hooks/hooks.json`. The adapter copies canonical
scripts byte-for-byte, passes `--client claude` and a writable plugin-local
state home, and uses `--activation-mode mcp` for retrieval/capture. The hooks
delegate to the client's admitted MCP connection; they do not read local
service credentials or directly inject a compiler packet. Exec-form registrations
invoke the canonical Python scripts directly and require a real `python3`
executable on a supported, normally trusted execution surface. They do not
run in Claude Chat. Skills remain the same portable operating contract.

Hook installation health does not prove that the client loaded or executed the
registration. Native acceptance must observe a normally trusted fresh session,
its compiler call, and governed capture/readback. Explicit MCP mode instructs
the agent to check live capabilities before capture; a legacy local restart marker is not
MCP availability evidence. Nested Codex code-mode JavaScript or printed output
does not establish successful writes or available tools for reserved owner gates.

The public OpenAI ZIP deliberately has no lifecycle hooks or app references:
[current submission rules](https://developers.openai.com/plugins/deploy/submission)
reject both. This is separate from local Codex hook support and the release's
`install-hook` profile provisioning. Workflow Skills already provide the
user-invocable workflows; additional command/agent copies are not required.

The package version comes from the checked-out `pyproject.toml`, not an installed
Python distribution. Regenerate when the release version, canonical skill,
definition or `plugins/cloud/evals/cases.json` changes. Release Please regenerates
the Cloud packages on its release branch. GitHub release assets include
`exomem-cloud-claude.zip`, `exomem-cloud-openai.zip` and
`exomem-cloud-release.json`. Verify their digests against the tagged source before
uploading them to a directory.

## Native acceptance

Use a dedicated sample account/vault. Never put original conversations, OAuth
tokens, reviewer credentials or customer memory in Git or a plugin ZIP. Store
captures in an operator-owned directory outside public build inputs.

Install the generated package, connect through OAuth and run the same
`plugins/cloud/evals/cases.json` journeys in Claude Chat, Cowork, Code, ChatGPT
and Codex. Retain each surface's actual clean-install/authentication observation
and a bootstrap tool result. Confirm the deployed image/version is current and
the protected resource matches the package. Bootstrap must report current
`server.version`, `active_capabilities.profile`, available tools and live
engagement/envelope; the canonical digest is sent on the session request. A
successful session profile proves that request's digest matched. The stale-skill
case uses a disposable fault-injected install, records compact fallback, then
restores the original package. Never patch the canonical source for that test.

Run ordinary conversation, not “call tool X” prompts. In particular, proactive
capture uses the stated outcome without asking to save. Writes need an actual
successful tool result and a bound readback from a different conversation. Fresh
recall prompts must not supply the hidden synthetic answer. Test the recall case
with the plugin disabled as well, and read a Claude-written result from OpenAI
and an OpenAI-written result from Claude. Each journey needs a separate native
conversation identity. Trace observations cover the journey from bootstrap to
the final answer; cached bootstrap is not invented as another tool call.

Normalize observations without changing arguments, results or user turns:

```json
{
  "case_id": "grounded-recall",
  "kind": "native",
  "surface": "codex",
  "conversation_id": "native-session-id",
  "observed_at": "2026-09-30T12:00:00Z",
  "identity": {"version": "source-release", "skill_contract": "digest", "corpus_digest": "digest", "profile": "product-cloud", "resource": "https://exomem.substratesystems.io/mcp"},
  "package_sha256": "installed-archive-digest",
  "marker": "unique-synthetic-marker",
  "plugin_enabled": true,
  "observations": [
    {"kind": "user", "text": "What retrieval approach did the sample project choose?"},
    {"kind": "tool", "name": "bootstrap", "arguments": {}, "result": {}},
    {"kind": "assistant", "text": "actual answer"}
  ]
}
```

This structural example intentionally cannot pass: replace placeholders with
observed facts, include actual activation/grounding, and retain native sources.
Tool names in the common projection omit provider namespace prefixes; preserve
the original names in the native export. Installation observations use
`kind: "installation"`, actual `package_sha256`, `resource`,
`oauth_flow: "authorization_code_pkce"` and `result: "connected"` after observing
successful native authentication.

For a capture, include `readback` with its own `conversation_id`, `surface`,
`identity`, timestamp, prompt and observations, including `read_memory(path=...)`
bound to the write's returned path/reference. Its result and answer must contain
the unique content marker. Give that readback its own native evidence too.
Its observed user turn must exactly match the separate `prompt` field, without
supplying the marker or answer. Grounding binds each citation to the page/hit
containing the marker. `observe_memory` validation is not a capture: acceptance
requires an add/update with `mutated: true`. Proactive capture requires the live
`engagement.envelope.classes.proactive_capture.disposition` to be `silent`.

Each native record has an `evidence` object:

```json
{
  "native_export": {"path": "exports/original.jsonl", "sha256": "original-digest"},
  "projection": {"path": "projections/journey.json", "sha256": "projection-digest"},
  "native_locator": "codex://session/native-session-id",
  "inspection": {"method": "native-export-inspection", "reviewer": "operator-label", "reviewed_at": "2026-09-30T12:05:00Z", "native_sha256": "original-digest", "projection_sha256": "projection-digest"}
}
```

The projection file is the exact native record minus `evidence`. Retain the
original UTF-8 JSON/JSONL/text export, including conversation identity, user turns
and tool trace. An independent inspector compares the projection with the actual
native export/UI and records that inspection; do not relabel a unit fixture.
Native locators are Claude conversation URLs, ChatGPT conversation URLs, or
`claude-code://session/`, `claude-cowork://session/`, `codex://session/` identifiers.
The checker verifies integrity/completeness, not cryptographic platform origin.
It cannot replace this inspection or recover observations omitted by a client.
Evidence expires after seven days and after any package/runtime/corpus change.

The private `manifest.json` has `schema_version: 1`, `surfaces` (one object per
surface with `surface`, `installation`, `cases`, `disabled_comparison`) and
`cross_readbacks`. Each cross readback names `write_surface`, `write_case`,
`read_surface` and its independently captured `readback`. Disabled comparisons
use the grounded-recall prompt in a fresh chat, `plugin_enabled: false`, and
cannot contain Exomem calls or the hidden marker. Missing records fail closed.

```bash
uv run --frozen python scripts/cloud-plugin.py evaluate --trace <private-trace.json>
uv run --frozen python scripts/cloud-plugin.py readiness --evidence <private-evidence-directory>
```

## Directory preparation and submission

Prepare five positive and three negative review cases from the shared corpus.
The OpenAI ZIP already includes those cases. Keep the same publisher and Cloud
resource on Claude's connector and plugin bundle entries. Validate the bundle
with the native provider validator; repository schema checks are not its result.

Use all platform-supported countries without an extra product allowlist.
OpenAI's `publication.countries: []` removes country restrictions. Launch as an
existing-account connection, with `review.commerce: false`: no subscription sale,
checkout links or upgrade promotion through the plugin. Website billing remains
separate. These are current [OpenAI targeting/submission rules](https://developers.openai.com/plugins/deploy/submission)
and [digital-subscription commerce rules](https://developers.openai.com/plugins/plugin-guidelines).

Private preparation metadata supplies `publisher_name`,
`publisher_verification_reference`, `countries: []`, `commerce: false`,
`demo_recording_url`, `demo_access_check_reference`,
`reviewer_access_portal_reference`, and `verified_policy_urls` matching the four
URLs in the definition. Verify page contents, not merely HTTP success. Record a
real accessible demo and enter dedicated sample reviewer access in the secure
portal. Do not put credentials or reviewer instructions in package metadata.

```bash
uv run --frozen python scripts/cloud-plugin.py materials --metadata <private-metadata.json> --evidence <private-evidence-directory>
```

This prepares fields and enforces readiness; it does not submit or accept legal
terms. Submit Claude's connector and bundle through the same publisher, and the
one OpenAI package to its universal ChatGPT/Codex directory. Retain actual portal
submission IDs/states and reviewed archive digests separately from approval and
publication. An uploaded draft, passing local tests or generated materials are
not a submitted listing. The operator accepts applicable legal attestations.

Platform contract sources audited 2026-09-30:
[Claude platform support](https://claude.com/docs/plugins/platform-support),
[Claude directory publishing](https://claude.com/docs/directory/publish),
[OpenAI portable packaging](https://developers.openai.com/plugins/build/plugins),
[OpenAI submission](https://developers.openai.com/plugins/deploy/submission).
