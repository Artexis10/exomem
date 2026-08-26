## Context

`compatibility_manifest()` builds a `base` dict and publishes
`compatibility_sha256 = sha256(canonical_json(base))`. Three members of `base`
carry the Exomem release:

1. `source_release`, copied from `definition.json`.
2. `agent_contract.exomem_release`, set from `__version__` by
   `hosted_gateway.build_agent_gateway_contract`.
3. `definition_sha256`, a digest over the raw `definition.json`, which contains
   `source_release`.

A guard requires `definition.source_release == contract["exomem_release"]`, so
the pinned value must track the package version. Release Please bumps the
version; nothing carried the definition with it; the guard then failed the
release PR. #342 automated the resync, which unblocked CI but left the coupling.

## Goals / Non-Goals

**Goals:**

- Make the descriptor's identity a function of the contract surface alone.
- Stop release-driven regeneration and re-promotion of an unchanged plugin.
- Keep the runtime contract's release reporting intact.

**Non-Goals:**

- Change what the running server reports as its release.
- Change promotion evidence, signing, or the operator protocol.
- Change plugin `version` (the plugin's own version, independent of Exomem's).

## Decisions

Remove `source_release` outright rather than deriving it. Deriving it from
`__version__` would keep it inside `base` and inside `definition_sha256`, so the
hash would still move every release — the churn, not the guard, is the problem.
Nothing outside `hosted_plugins.py` reads the field, so removal costs no
consumer.

Exclude `exomem_release` from the hashed base while leaving
`build_agent_gateway_contract` untouched. The runtime contract is the right
place to report a running release; a committed artifact is not. The descriptor
embeds the contract with that one key omitted, so a release that changes no
command, schema, or capability produces a byte-identical descriptor.

Keep the release out of the emitted descriptor entirely, not merely out of the
hash. A field that changes while the hash does not would leave the committed
file stale on every release — the exact failure this change exists to remove.
Provenance for a published artifact belongs to the promotion record, which is
stamped when a promotion actually happens.

Drop the equality guard. It exists only to enforce a relationship that no longer
has two sides. Retaining it as a soft check would preserve the trap it created:
`regenerate` was itself behind that guard, so the guard blocked its own remedy.

Retire `scripts/sync_hosted_release.py` and the release-branch resync. Once a
version bump does not perturb the artifacts, the release branch has nothing to
resync. The regeneration path stays available for genuine contract changes.

## Risks / Trade-offs

- [`compatibility_sha256` changes once on adoption] → Any live promotion record
  must be re-promoted on this release. Detected loudly by the existing
  `live promotion record has stale package bindings` check, not silently.
- [A descriptor without a release is harder to trace to a build] → The promotion
  record carries the deployment fact, and the package digests already bind the
  artifact. Repository provenance remains in git history.
- [A future contract field could reintroduce a version-derived value] → A
  regression test asserts a version bump alone leaves the descriptor unchanged,
  so reintroduction fails loudly.

## Migration Plan

Ship in a normal release. On that release, re-promote any live hosted plugin
once; subsequent releases require no regeneration or re-promotion. Roll back by
restoring `source_release` and the guard, which restores the previous hash.

## Open Questions

None before implementation.
