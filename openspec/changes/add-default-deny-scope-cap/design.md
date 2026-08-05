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
override the authored allowance. Emptiness is resolved PER DECLARING SCOPE: a standing
rule names only the scopes in that rule, and every declared member it does not name keeps
its restrictive default:

```python
named_scope_ids = {
    scope_id for rule in standing for scope_id in rule.scope_ids
} & item_scope_ids
default_deny_scope_ids = {
    scope_id for scope_id in item_scope_ids
    if policy.scopes[scope_id].default_deny and scope_id not in named_scope_ids
}
ceilings = [rule.ceiling for rule in standing]
if default_deny_scope_ids:
    ceilings.append(DISCLOSURE_MIN)
standing_min = min(ceilings, default=DISCLOSURE_MAX)
```

Everything downstream then works untouched: `grant_max` still raises off the floor, so
"unless a grant names them" needs no special case.

### Reserved audience ids stay outside the authored grammar

The evaluator uses a NUL-prefixed audience namespace for the unresolved fail-closed
identity and the unnamed-audience transition probe. YAML can decode an escaped `\0` into
that namespace, so every authored audience-bearing field (`audience` and `to_audience`)
rejects any NUL as an error finding. A rule or grant therefore cannot capture either
reserved identity or mint a lookalike inside the reserved prefix.

### Transition previews expose the unnamed default separately

`target_ceiling` remains the maximum post-change ceiling among authored audiences for
compatibility. Proposal consequences also always carry `unnamed_audience_ceiling` (nullable
when the concrete membership is empty), computed from the post-change unnamed-audience
probe. Removing `default_deny` can therefore report authored `target_ceiling: 1` and
`unnamed_audience_ceiling: 6` without hiding the credential-rotation consequence.

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
