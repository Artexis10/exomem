## Implementation

- [x] 1. Import the companion cell schema and grants, including desired-size generation changes.
- [x] 2. Observe bound-claim capacity, identity, resource version and pending filesystem expansion.
- [x] 3. Persist satisfied-generation receipts through existing reconciliation without changing lifecycle semantics.
- [x] 4. Verify delayed expansion, identity conflict, existing local growth and split PostgreSQL authority.
- [ ] 5. Complete independent review, current checks, publication and operational acceptance under the existing rollout owner.

The local controller corpus passes 520 checks, including the cross-package import cleanup workflow.
Migration 0071 must precede the controller. No deployment or real volume expansion has run.
