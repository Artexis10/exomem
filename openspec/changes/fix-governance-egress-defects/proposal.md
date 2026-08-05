## Why

Five defects in the shipped governance kernel and release gate let content escape the
disclosure boundary or let a weaker policy take effect. Each was verified against current
`main`; none is hypothetical, and two are reachable on a synced multi-machine vault today.

1. **The last-good policy cache can serve an OPEN policy.** `_LAST_GOOD` is populated for
   any load that is neither blocked nor error-carrying — which includes `EMPTY_POLICY`.
   When a policy mutation is pending, `_guarded_policy` replaces only `findings`, so the
   returned policy keeps the `"missing"` fingerprint and `.empty` stays true: every read
   takes the fully-open fast path. The window is non-deterministic across processes — a
   freshly started runtime has an empty cache and correctly fails closed, while a
   long-lived runtime that predates governance returns OPEN for the same instant. This
   defeats policy-first activation ordering on its own.
2. **Policy discovery is blind to sync-conflict copies.** The conflict guard matches only
   Obsidian's `(conflicted copy` marker. Syncthing names conflicts `.sync-conflict-<ts>-<id>`,
   which every other walker in the codebase filters and the policy compiler does not. A
   conflict copy of a deleted grant compiles clean and silently restores revoked access;
   a conflict copy alongside a surviving original raises a duplicate-id error, which falls
   back to the last in-process compile — during a tightening operation, the weaker policy.
   The same blindness applies to the append-only receipt tree, where a conflict copy forks
   the hash chain.
3. **Error payloads bypass the terminal filter.** The universal postfilter is applied to
   the return value of a command; a raising command never reaches it. `AMBIGUOUS_REFERENCE`
   embeds the full list of colliding vault paths in its message, so a duplicate-identity
   probe is a path oracle for withheld content.
4. **`ingested_into` is not treated as provenance.** Compiled notes append their own
   wikilink to every source they cite, so a released source page enumerates the compiled
   notes that used it — including notes withheld from the caller. The frontmatter
   provenance strip set covers `sources`/`evidence`/supersession but not this reverse edge.
5. **Adoption run state is released as ordinary knowledge.** `_Adoption/` is absent from
   the excluded-directory set, so run manifests are indexed and findable, and the manifest
   body embeds a machine-readable dump of every source path, target path and content hash
   in the run. Page bodies are free text that the artifact-reference gate scans only for
   `handoff`/`prompt`/`resource` keys, so the dump is returned verbatim at L6 even when
   every individual path it names is withheld.

Defects 1 and 2 are fail-open policy defects; 3, 4 and 5 are disclosure leaks. All five
predate any consolidation work and weaken the governance claim as shipped.

## What Changes

- Never seed or serve the last-good policy cache with the empty/open singleton: a pending
  or guarded load with no *governed* last-good compile fails closed instead of opening.
- Treat sync-conflict filenames as conflict copies wherever Obsidian conflict copies are
  already recognised — policy document discovery, the governance file walk, and the
  receipt tree — so a conflict refuses compile rather than compiling as authority.
- Route error payloads crossing a command boundary through the same terminal filter as
  successful results, and make identity-collision errors carry a content-free code instead
  of vault paths.
- Add `ingested_into` to the frontmatter provenance fields stripped below full release.
- Stop embedding a per-path machine-readable summary in released adoption manifests — both
  the durable run manifest (counts and the run reference only; per-path detail stays in
  `run.json`, reachable through `adoption_studio(action="status")`) and the stateless
  `save-manifest` report (human-readable sections only; the full report stays available live
  from `adopt(mode="scan-only")`, where a disclosure decision applies).
- Exclude `_Adoption/` from the content corpus so operational run state is not indexed as
  knowledge, and close the second walker: recall at `scope="vault"` reaches
  `vault.walk_vault_md`/`VAULT_SCAN_SKIP_DIRS`, a set separate from the corpus exclusion that
  named neither `_Adoption` nor `_Governance`. Both names now appear in both sets, so the
  exclusion cannot be bypassed by widening scope.
- Add red-first regression coverage for each defect, including a cross-process probe for
  the last-good window and a sync-conflict grant-resurrection probe.

No policy grammar, disclosure ladder, receipt schema, or command surface changes. The
empty-policy fast path for genuinely ungoverned vaults is preserved exactly, including its
latency budget.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `governance-kernel`: last-good fallback may never serve an open policy; conflict-copy
  detection covers sync-conflict filenames across policy discovery and the governance tree.
- `release-gate`: error payloads are filtered at the same boundary as results; reverse
  provenance (`ingested_into`) is stripped below full release; operational run state is
  excluded from the content corpus and carries no per-path dump in released text.

## Impact

- Affected code: `governance/policy.py`, `governance/receipts.py`, `governance/egress.py`,
  `writer_lease.py`, `memory_refs.py`, `find_corpus.py`, `vault.py`, `adopt.py`, and focused
  governance/adoption tests.
- APIs: no request or response shape changes. `AMBIGUOUS_REFERENCE` keeps its error code
  and loses the path list from its message. Both adoption manifest bodies change shape;
  `run.json` and `proposals.json` are unchanged.
- Behaviour change to call out: `_Adoption/` and `_Governance/` leave the full-vault scan
  set, so anything that walked them — inbound-wikilink scans, id backfill, reconcile, the
  lexical index, the file watcher, media processing, the semantic census — no longer treats
  operational state as vault content. This is the intended correction; run manifests and
  policy documents were never knowledge.
- Behaviour change to call out: a vault whose `_Governance/` tree is present but currently
  yields no compiled policy will fail closed rather than open while a policy mutation is
  pending. A vault with no `_Governance/` tree at all is unaffected.
- Dependencies and runtime: no new dependency, model, background task, or sidecar migration.
