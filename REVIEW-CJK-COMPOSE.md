# Review: CJK alias claim composed with the identity `distinct` decision

Scope: `integration/wave-bcd` at 6fb2c11f, plus the conflict resolutions of
merge 8d2eae52 in `link.py` and `commands.py`.

**Verdict: REQUEST_CHANGES.** The gate itself is sound. The fingerprint it
checks, though, does not bind the decision to the page that claims the alias
in exactly the cases HIGH 1 was about.

## Merge resolutions

Clean: `link()`, `op_link` and `op_connect_memory` pass `identity_decision`,
`facets` and `aliases` through together, and the alias guard follows the title
branch as it did on the CJK branch.

## Checks

| check | result |
|---|---|
| refused without a decision | yes: `ENTITY_EXISTS`, and the refusal names the fingerprint |
| accepted with a matching decision | yes (`test_a_claimed_alias_is_accepted_with_a_matching_distinct_decision`) |
| stale or foreign fingerprint | refused on create and on edit (`STALE_IDENTITY_DECISION`), **but see MEDIUM 1** |
| decision for A unlocks B | no. Create refuses B with `ENTITY_EXISTS` (`link.py:963-981`); edit refuses it with `STALE_IDENTITY_DECISION` (`commands.py:7391-7400`) |
| title decision unlocks an alias, or the reverse | no: the `alias` marker (`entity_candidates.py:54-65`) and `title_decided` (`link.py:907`) block both |
| restricted caller vs. withheld claimant | accepted; no refusal, fingerprint or path |
| owner vs. withheld claimant | refused with `ENTITY_EXISTS` |
| HIGH 1 cases with no decision | still refused: note title `ハヤブサ号`, `Dana’s Garage` (U+2019), `Products/ハヤブサ号`, and `テッ`+U+00AD+`サリー` |
| `tests/test_operator_site_cohort.py` | **7/7 passed** |

## MEDIUM 1: the alias fingerprint cannot see the claimants HIGH 1 guards against

`alias_claim_fingerprint` (`entity_candidates.py:54-65`) hashes
`resolve_entity_candidate`, which walks only `Entities/` and uses
`identity_key`. The guard itself uses `claimed_names`, which reads the index:
notes, stems, and the apostrophe, hyphen and soft-hyphen folds. When the
claimant is a note or a folded spelling, the resolution is `no_match`, so the
fingerprint is a pure function of the alias string.

- For `ハヤブサ号` (a note) and for `Dana’s Garage`, the refusal's fingerprint
  equals `candidate_fingerprint(name, "alias", no_match)`. A caller can
  compute it without ever being refused.
- After a second note starts answering to `ハヤブサ号`, the old decision is
  still accepted, with `distinct_from: []`. It is bound to nothing and never
  goes stale.

**Fix:** derive the fingerprint and `distinct_from` from the claimants that
`claimed_names` returns (already filtered by visibility). Add red-first tests
for a note claimant, a fold claimant, and a claimant set that changes after
the decision and must go stale.

## LOW 2: one decision per write

`identity_decision` is a single object, so one create cannot decide a shared
title and a shared alias, or two shared aliases. The workaround is to create,
then patch aliases one decision at a time. The tool description should say so.

## LOW 3: an edit records no decision

The edit path (`commands.py:7391-7393`) accepts a decision but writes no
`distinct_from` audit record, while create returns one.

## Tests and gates (integration head)

- `test_cjk_readiness`, `test_link`, `test_edit_operations`,
  `test_rest_registry`, `test_mcp_schema_fidelity`,
  `test_tool_surface_contract`, `test_tool_surface_fingerprint` and
  `test_hosted_agent_surface`: **233 passed**.
- Operator cohort: 7 passed.
- `ruff --select F` is clean.
- `generate-capabilities.py --check`: current.
- `hosted-plugin.py check`: current.

## Recheck at 72fd2f8f

**Verdict: APPROVE.** MEDIUM 1 is fixed. `alias_claim_fingerprint`
(`entity_candidates.py:54-86`) now hashes three things: the alias's folds,
each claimant from `claimed_names` (filtered by visibility), and each
claimant's content hash. `distinct_from` now lists the claimants the guard
refused on (`link.py:973`).

| check | result |
|---|---|
| note claimant (`ハヤブサ号`) | a precomputed no-match value is refused (`STALE_IDENTITY_DECISION`); a fresh value is accepted |
| folded spelling (`Dana’s Garage`) | a precomputed no-match value is refused; a fresh one is accepted with `distinct_from` naming the claimant |
| second claimant after the decision | refused as stale; the new decision lists both claimants |
| withheld claimant, restricted caller | editing the withheld page leaves the restricted caller's value unchanged; the owner's value differs; the create succeeds with no path leaked |
| owner | still refused |
| red-first | the 5 new tests fail on 6fb2c11f's `src` and pass at 72fd2f8f (same commit) |
| operator cohort | **7/7** |

Files touched:
- `commands.py:7382` and `benchmarks/.../contract.py:366` pass
  `exclude_path`, so the edit path and the fixture skip the page's own names,
  as the guard does.
- `link.py` drops the entity-only lookup.
- The tests in `test_cjk_readiness.py` pin every row above.

**Info (not blocking):**
- A caller who can read every claimant can still compute the value. It is a
  staleness binding, not a secret, and every input is visible to that caller.
- Any edit to a claimant makes an open decision stale, including routine
  edits. That is the intended strictness.
- LOW 2 (one decision per write) and LOW 3 (edits not audited) remain open as
  follow-ups.

**Tests and gates:**
- `test_cjk_readiness`, `test_link` and `test_edit_operations`: 149 passed.
- `ruff --select F` is clean.
- `generate-capabilities --check` and `hosted-plugin.py check` report current.
- The privacy gate is clean.
