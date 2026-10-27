## 1. Carrier registry

- [x] 1.1 Add the `topic-carriers` overlay registry on the generic registry contract, with kinds `project`, `folder` and `tag`, topics, origin and rejected, and its validator (duplicates, unresolved, rejected and hand-declared topics, unknown keys, folders without entries, type and home folders and their ancestors, project-partitioned sub-folders, written-form tags).
- [x] 1.2 Resolve home topics through the domains resolver.

## 2. Membership

- [x] 2.1 Store each entry's frontmatter tags in an in-place `entry_tags` table that joins the entry digest, filled by one background full fold without a wipe.
- [x] 2.2 Join project carriers, folder carriers, homes and tag carriers into membership at read time, gated by caller-view admission of each overlay.
- [x] 2.3 Key the owner's cached map on the carrier and homes registry hashes.
- [x] 2.4 Count unfiled entries, carriers that would file them, unresolved carrier, home and project topics, and dangling carriers in the owner coverage.

## 3. Vocabulary family

- [x] 3.1 Register `topic-carrier/v1` under the agent-led vocabulary workflow with `defer` as its only decision, its choice contract, review-family mapping, freshness check and bootstrap listing.
- [x] 3.2 Produce owner-only considerations: the written page's own carriers after a committed write, and a bounded vault ranking in explicit review, with a fingerprint over the carrier, the registry hashes and a bucketed unfiled count.

## 4. Onboarding and skill

- [x] 4.1 Name the carrier review in adoption's next actions and advertise the route in bootstrap.
- [x] 4.2 Teach the workflow in the generic skill scaffold, and keep the scaffold generic.

## 5. Proof and rollout

- [x] 5.1 Add the fixture end-to-end check: undeclared carriers, then inferred declarations, then topics on the map and domain maps returning members.
- [ ] 5.2 Run the real-model check on dogfood vaults, including the hostile cases.
- [ ] 5.3 After release, run the workflow on the personal vault through the governed route, coordinated with its configuration-hold owner, and report the declared topics to the owner for review.
- [ ] 5.4 Once `compile-context-maps-and-profiles` is archived, carry a MODIFIED block of its requirement "Topic membership is declared" against the canonical spec, naming carriers and homes as routes.
