# Tasks

## 1. Write side

- [ ] 1.1 Add a one-line `summary` and current-state fields with an as-of date to pages; accept an agent-supplied summary on the write tools; store a marked derived summary otherwise.
- [ ] 1.2 Add a per-thread head index that exposes the newest episode of each thread.
- [ ] 1.3 Queue profile and map refresh for the anchors and topics a write touches, through the derived-sync components; mark unrefreshed lines stale.
- [ ] 1.4 Backfill summaries for existing pages, with the method chosen from the open question in the design.

## 2. Compiled views

- [ ] 2.1 Compile anchor profiles from semantic units by role, currency, typed relations, related pages and recent changes.
- [ ] 2.2 Compile topic profiles and domain maps from registry domains.
- [ ] 2.3 Compile the map with its budget and tiering.
- [ ] 2.4 Apply the reader's view to every map, domain map and profile.

## 3. Read side

- [ ] 3.1 Select the turn slice by meaning over map lines and profile summaries, with resolver evidence as a boost and a calibrated threshold.
- [ ] 3.2 Accept agent `queries` on `activate_context` and run them through hybrid recall over the caller's view.
- [ ] 3.3 Serve the map packet behind a setting, with `anchors` as the default.

## 4. Client contract

- [ ] 4.1 Update the server instructions, the `activate_context` description, the skill scaffold and the retrieve hook for the map packet.

## 5. Evaluation and switch

- [ ] 5.1 Settle the scoring rule for newer pages written after a reported failure, before any held-out run.
- [ ] 5.2 Iterate on the development split: map plus slice from the turn alone, and with agent pulls; report served, reachable, off-target share, harm on cases without relevant memory, tokens, calls and latency.
- [ ] 5.3 Run the held-out split once, blind, against the current packet and the activation-audit twins.
- [ ] 5.4 If it passes, switch the default through the release line, then replay field reports on the installed release.
