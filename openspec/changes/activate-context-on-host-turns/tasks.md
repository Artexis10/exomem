# Tasks: activate-context-on-host-turns

Depends on `add-context-activation` (PR #1282) merged; rebase before starting.

## 1. Continuity token and anchor override (server)

- [ ] 1.1 Red: `tests/test_working_set_continuity.py` — token round-trip; continuity
      is a qualifier (one contact kind + continuity → resolved; continuity alone →
      nothing); stale token ignored and reported; unresolved stays unresolved; token
      from another vault ignored.
- [ ] 1.2 Red: `anchor` override — ambiguous → resolved with `[agent_choice]`;
      unknown ref refused; withheld ref refused without naming it.
- [ ] 1.3 Implement in `working_set_resolve.py`, `working_set_runtime.py`,
      `commands.py` (two optional arguments; docstring contract); regenerate the
      schema fixture, digests, capabilities, pending connector digest.

## 2. Decision ledger

- [ ] 2.1 Red: `tests/test_working_set_review_family.py` — family registered; triage
      records decision with fingerprint, refs, evidence kinds, closed reason; no
      due-state counter; identical packet after a decision.
- [ ] 2.2 Implement the `working_set` family in `review_state.py` and the triage route.

## 3. Hook working-set mode

- [ ] 3.1 Red: `tests/test_retrieve_nudge_working_set.py` — mode gate; data header;
      bound; abstained → reminder only; failure → reminder within budget; prominence
      and cooldown gates unchanged; token persisted beside the checkpoint and cleared
      on SessionStart/PreCompact/SessionEnd; `src/exomem/_hooks` copy byte-identical.
- [ ] 3.2 Implement in `exomem_retrieve_nudge.py` (+ `_hooks` copy) and
      `exomem_continuation_checkpoint.py`; document the mode in `install_hook.py`.

## 4. Hosted carrier line

- [ ] 4.1 Red: bootstrap guidance contains the line at balanced/maximal, absent at
      light/off; compact ceiling holds; scaffold recall loop byte-identical to the
      served projection.
- [ ] 4.2 Implement in `commands.py` bootstrap guidance and the scaffold; run
      `refresh-skill-contract` and `package-skills`.

## 5. Delivery

- [ ] 5.1 Hook journey test (f27 shape) with the packet under a data header on a real
      `claude -p` run — one session, opt-in, reported as a finding.
- [ ] 5.2 `openspec validate --all --strict`; scoped suites; privacy gate; PR.
