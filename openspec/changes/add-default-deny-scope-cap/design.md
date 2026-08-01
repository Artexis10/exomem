## Context

`_decide_at` (`governance/decisions.py`) evaluates a pure, order-free lattice:

```python
standing_min = min((r.ceiling for r in standing), default=DISCLOSURE_MAX)
grant_max    = max([g.ceiling for g in matched_grants] + [standing_min])
org_cap_min  = min((r.ceiling for r in org_caps), default=DISCLOSURE_MAX)
level        = min(org_cap_min, grant_max)
```

The `default=DISCLOSURE_MAX` on `standing_min` is the entire default-open behaviour. Every
other part of the lattice is already correct for this change: grants raise, org caps lower,
purpose narrows. So the change is one default, conditioned on the scope and the audience.

Wave 0 established that this matters in practice: audiences are exact-id matched
(`principal.normalize_audience`), and for non-OAuth MCP clients the subject derives from
the bearer credential, so a credential rotation produces an audience no rule names.

## Goals / Non-Goals

**Goals**

- Make "nobody but me unless I name them" expressible for a scope.
- Compose with the existing lattice rather than sitting beside it.
- Keep the owner's access unconditional.
- Leave undeclared scopes and ungoverned vaults byte-identical.

**Non-Goals**

- Not a new document kind. The declaration belongs to the scope it describes.
- Not a global default. Flipping the whole vault to default-deny is a different, larger
  decision with a migration; this is per scope and opt-in.
- Not a replacement for org caps. An org cap bounds an authored allowance from above; this
  supplies a floor where nothing was authored at all.

## Decisions

### A scope field, not a rule kind

Adding a third `_RULE_KINDS` value would mean a rule that matches no audience — a rule
whose entire purpose is to describe the absence of other rules. That inverts what a rule
is and would have to be excluded from `matched_rules`, `options` merging, and the
`rule_ids` a decision reports. A boolean on `Scope` says what it means: this scope is
closed unless opened.

### Change the default, do not add a synthetic rule

The obvious implementation — inject a ceiling-0 standing rule for declared scopes — is
wrong, and the reason is worth recording. `standing_min` is a `min` over matched rules, so
a synthetic 0 would combine with an explicit `ceiling: 3` as `min(3, 0) = 0` and silently
override the authored allowance. The declaration must apply only when the standing set is
EMPTY:

```python
if standing:
    standing_min = min(r.ceiling for r in standing)
else:
    standing_min = DISCLOSURE_MIN if _denied_by_default(...) else DISCLOSURE_MAX
```

Everything downstream then works untouched: `grant_max` still raises off the floor, so
"unless a grant names them" needs no special case.

### Owner exemption is by audience identity, at the default site

`OWNER_AUDIENCE` is a reserved id that `normalize_audience` cannot produce, so testing it
is not a string-comparison hazard. The exemption applies where the default is chosen, not
as a post-hoc override, so an authored rule that deliberately restricts the owner still
takes effect.

### Any declared scope wins

Membership is a set, and a scope cannot be widened by adding another scope alongside it —
that would make the declaration defeatable by authoring a broad undeclared scope, which is
exactly the mistake this change exists to prevent.

## Risks / Trade-offs

- **A declared scope with no rules is invisible until someone is denied.** Mitigated by
  `explain` naming the declaring scope rather than reporting "no rule matched", so the
  owner can tell a default denial from a missing item.
- **Latency.** The default path needs the scope objects for an item's already-resolved
  scope ids — a dict lookup per scope, only on the branch where no rule matched, and only
  for governed vaults. The overhead gate covers the empty-policy budget.
- **It is opt-in, so it does not fix existing scopes.** Deliberate: silently inverting the
  default for policies already authored would change live disclosure outcomes on upgrade.
  The consolidation work is expected to author declared scopes from the start.

## Migration Plan

None. Documents omitting the field compile and decide exactly as today; no stored format,
schema version, or sidecar changes.

## Open Questions

None blocking. Whether the vault-wide default should eventually invert is deliberately out
of scope and would need its own change with a migration path.
