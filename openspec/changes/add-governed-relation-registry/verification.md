## Verification evidence

### Broad corpus dry run

The relation inference path was run read-only over a broad real corpus on
2026-07-10. The mounted corpus was sharded by top-level subtree only to stay
inside the command runner's per-process I/O bound; every shard used the same
`infer_relation_registry` implementation and the results were aggregated.

- Parsed pages: 2,152
- Explicit typed observations: 30
- Portable core observations: 2
- Registered extension, alias, deprecated, or scope-violating observations: 0
- Unregistered advisory observations: 28 across 11 labels

The dominant unregistered labels were structural or navigation vocabulary.
No proposed label had enough reviewed semantic evidence to justify shipping a
domain extension. No Markdown, registry, profile, or graph sidecar was written.
This supports the selected policy: preserve explicit observations, keep them
semantically inert, and require a complete reviewed proposal before adoption.

### Product lifecycle

The installed-wheel product E2E builds and installs a wheel into a clean virtual
environment, initializes a fresh vault, and uses MCP to:

1. Read the empty registry hash and save three reviewed namespaced extensions.
2. Write independent target pages and a source page with cross-file epistemic,
   provenance, and causal relations.
3. Query all three built-in lenses and assert raw identity, canonical extension,
   portable parent, and registry status.
4. Delete the graph and reference sidecars, restart the server, reconcile, and
   assert the same relations again.
5. Start REST and HTTP MCP, verify authentication, inspect installed OpenAPI for
   the new parameters, perform a write, and shut down cleanly.

The final lifecycle run passed in 15.9 seconds on 2026-07-10.

### Final gates

- Lean suite: 1,797 passed, 19 dependency/platform skips, split into bounded
  alphabetical shards because the command runner terminates long processes.
- Ruff correctness baseline and expanded full gate: passed.
- Targeted mypy over six memory/registry/profile/graph modules: passed.
- Generated capability document and MCP schema fidelity: current.
- Scaffold leak guard, YAML parse, bytecode compilation, and `git diff --check`:
  passed.
- Strict OpenSpec validation: passed. The trailing telemetry DNS warning is
  external to validation and occurred after the successful result.

### OpenSpec verification scorecard

| Dimension | Result |
|---|---|
| Completeness | 29/29 tasks; 16/16 requirements implemented |
| Correctness | 22/22 scenarios covered by focused, compatibility, surface, or lifecycle tests |
| Coherence | Implementation follows the immutable-core, opt-in-extension, derived-graph, and read-only-profile design |

No critical issues, warnings, or pattern inconsistencies remain. The change is
ready for review and archive after merge.
