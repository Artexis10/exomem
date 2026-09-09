## 1. Retain the replaced configuration

- [ ] 1.1 Add failing coverage proving that publishing a managed service environment over an existing one retains the predecessor with the previous contents and owner-only permissions.
- [ ] 1.2 Retain the predecessor before the replacement becomes visible and report its path; keep the publish atomic and leave the first-render case unchanged.

## 2. Refuse an unintended vault rebinding

- [ ] 2.1 Add failing coverage for an existing service whose managed environment records a different vault path than the render produces, including the permitted opt-in case and the unaffected first-install case.
- [ ] 2.2 Read the recorded vault path from the managed file, refuse a differing render without the opt-in, and name both paths and the flag; add the opt-in flag and document it in the usage text.

## 3. Delivery verification

- [ ] 3.1 Run the installer-scoped test suite and lint, then run strict OpenSpec validation.
- [ ] 3.2 Commit the intended scope, push, and open a ready Conventional Commit PR carrying the reproduction and the verification evidence.
