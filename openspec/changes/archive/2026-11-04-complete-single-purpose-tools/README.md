# Typed tool catalogue delivery

The complete catalogue shipped in [PR #2139](https://github.com/Artexis10/exomem/pull/2139)
at `a5091aa8dfddce4317433c7dd4977b4423d7dcb2`.
The containing [v0.149.0 release](https://github.com/Artexis10/exomem/releases/tag/v0.149.0)
is `9afb6ab681f3127b3ba18b3290289cd195d15854`.

## Implementation and package proof

Tasks 1.1–2.4 and 3.1–3.3 are covered by the merged implementation and its
[exact-head Full CI](https://github.com/Artexis10/exomem/actions/runs/40726076102)
at `14594ef9b8e242d93ffe584e1d67df1fcc6c774b`.
The resolver and MCP suites cover conditional effects, credentials, aliases,
shared schemas, graph and dataset reads, imports and native media responses.
`tests/test_related_jobs_mcp.py` covers live-overlay previews, retained custom
entries, collisions, the registry cap and refusal to contact a writer.

The [installed-wheel journey](https://github.com/Artexis10/exomem/actions/runs/40726076102/job/122969621250)
passed the governed lifecycle, restart persistence, transport shutdown and
structured-collection workflow through stdio and HTTP.
The separate wheel onboarding check passed in the same run.
These checks use isolated state; they do not establish connected-client acceptance.

## Catalogue decision

Tasks 4.1–4.4 preserve the completed development and ten-pair smoke evidence.
The smoke recorded 10/10 grouped successes, 9/10 compact successes and no wrong writes.
The preview-selection failure led to the offline live-overlay repair.
The completed scores remain unchanged, and no post-repair model run occurred.
This ten-pair smoke check cannot certify the 10-point loss bound.
It establishes no population quality, non-inferiority or token-efficiency claim.
The unrun 375-pair protocol is retired.

The shipped batch also integrates the import naming and guidance changes.
Those changes were outside the completed model measurement.
Hidden compatibility spellings retain their execution identities.

## Review and publication

Task 5.1 is covered by the [independent exact-content approval](https://github.com/Artexis10/exomem/pull/2139#issuecomment-6474755668)
and the successful Full run above.
The merge preserves the reviewed feature content; later base changes touched disjoint paths.

Task 5.2 is covered by the [v0.149.0 publisher](https://github.com/Artexis10/exomem/actions/runs/40857280151).
It completed successfully on 2026-10-29.
The release owner qualified the official wheel and source archive against the original producer and published bytes.
The owner separately qualified all five signed image roles against their source, producer and immutable aliases.
Publication does not establish a deployment, service upgrade or provider acceptance.

Task 5.3 synchronizes the command-surface requirements and archives this delivered change through the OpenSpec CLI.
Strict validation runs before and after the archive.

## Remaining operational obligations

The pilot change `expose-single-purpose-tools` retains task 4.4 for the actual
Cloud deployment and directory rescan receipt.
The relevant Cloud changes retain their engine, artifact and client acceptance tasks.
This archive does not close those tasks or enable an excluded operation.
