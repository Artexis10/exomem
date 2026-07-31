## 1. Last-Good Policy Cache Cannot Serve An Open Policy

- [x] 1.1 Add failing coverage: with a warmed last-good cache from a previously empty/open load, a guarded load returns a policy that is neither empty nor blocked-bypassing, and every content-returning surface withholds.
- [x] 1.2 Add failing coverage: a load resolving to the empty open singleton leaves the last-good cache unchanged.
- [x] 1.3 Add failing coverage: a warmed-cache process and a cold-cache process reach the identical disclosure decision for the same item, audience and pending guard.
- [x] 1.4 Add passing coverage: a genuinely compiled last-good policy is still served through the guard with its scopes, rules and grants intact, findings appended.
- [x] 1.5 Gate last-good retention on the compile having produced governance (neither the empty sentinel nor the blocked sentinel).
- [x] 1.6 Confirm the no-governance-tree fast path never reaches the guarded fallback and is byte-identical.

## 2. Sync Conflict Copies Refuse Policy Compile

- [x] 2.1 Add failing coverage: a sync-conflict copy of a deleted grant refuses compile, does not take effect, and the last good governed policy continues to be served.
- [x] 2.2 Add failing coverage: a sync-conflict copy alongside its original is refused as a conflict before duplicate-identifier compilation, and does not fall back to a weaker policy.
- [x] 2.3 Add failing coverage: a sync-conflict copy inside the receipt tree is detected and receipt append fails closed rather than extending the chain.
- [x] 2.4 Add passing coverage: a policy document whose name contains neither conflict marker compiles with no conflict finding.
- [x] 2.5 Widen conflict-copy detection to a marker collection covering the parenthesised and hyphenated forms, matched case-insensitively.
- [x] 2.6 Apply the widened detection at policy document discovery, the governance file walk, and the receipt tree.

## 3. Error Payloads Cross The Same Terminal Boundary

- [x] 3.1 Add failing coverage: resolving an identifier that matches more than one stored item returns the ambiguity code and match count, carrying no path, title or reference.
- [x] 3.2 Add failing coverage: a governed command that raises with a withheld reference in its payload has that reference removed before the error crosses the dispatcher.
- [x] 3.3 Add passing coverage: an ungoverned vault's error text is unchanged apart from existing secret scrubbing.
- [x] 3.4 Route error payloads through the terminal filter at the shared dispatcher for both the read-only and mutation paths.
- [x] 3.5 Make the identity-collision error content-free at the raise site, keeping its error code.
- [x] 3.6 Verify no caller depends on parsing paths out of the collision message.

## 4. Reverse Provenance Is Stripped Below Full Release

- [x] 4.1 Add failing coverage: a source released below full level omits the reverse citation field, and no withheld compiled item's path, title or reference appears anywhere in the response.
- [x] 4.2 Add passing coverage: the same source released at full level to a permitted audience carries the reverse citation field complete.
- [x] 4.3 Add the reverse citation field to the frontmatter provenance strip set.

## 5. Operational Run State Is Not Released As Knowledge

- [x] 5.1 Add failing coverage: a recall query whose terms match run state returns no run-state item and identical result counts to a vault with no run.
- [x] 5.2 Add failing coverage: a released run summary carries counts and the run reference only, with no source path, target path or content hash.
- [x] 5.3 Add passing coverage: the owner reads full per-item run detail through the command that owns the run.
- [x] 5.4 Exclude the adoption run directory from the content corpus.
- [x] 5.5 Render the run manifest summary as counts plus the run reference; leave the run object unchanged.
- [x] 5.6 Report, do not work around, any existing test that asserts a run manifest is findable — that assertion encodes the leak.
- [x] 5.7 Add failing coverage that recall at every scope, including the full-vault scope, reaches neither the run tree nor the governance tree; add both names to the full-vault scan set as well as the corpus exclusion.
- [x] 5.8 Add failing coverage that the stateless save-manifest report embeds no path enumeration or machine-readable dump; render its human-readable sections only.

## 6. Verification

- [x] 6.1 Run the focused governance, adoption, and corpus tests with embeddings disabled.
- [x] 6.2 Run the governance overhead and latency gates and confirm the empty-policy budget and receipt latency ceilings are unchanged.
- [x] 6.3 Run `ruff check`, `git diff --check`, and the scaffold leak gate.
- [x] 6.4 Run `openspec validate fix-governance-egress-defects --strict`.
- [x] 6.5 Run the lean suite, then the full suite.
- [ ] 6.6 Independent review of the exact diff against the threat model, with the reviewer rechecking their own findings after fixes.
