# Tasks: activate-context-on-host-turns

`add-context-activation` is merged (PR #1282, 4836ecde); this branch carries main.

## 1. Continuity token and anchor override (server)

- [x] 1.1 Red: `tests/test_working_set_continuity.py` — token round-trip; minted from
      the served packet (a guard-removed anchor is absent from the decoded token);
      continuity is a qualifier (one contact kind + continuity → resolved; continuity
      alone → nothing); unresolved stays unresolved; a token from another index, another
      registry hash or an undecodable token → `stale`; an older generation of the same
      index stays valid with removed refs dropped; `generation.continuity` reports
      `applied` / `stale` / `absent`; a request with a token or an override is never
      served another request's cached packet.
- [x] 1.2 Red: `anchor` override — ambiguous → resolved with `[agent_choice]` and the
      competitors omitted; unknown ref and withheld ref refused with the same
      structured error, neither naming the page; the operation writes nothing.
- [x] 1.3 Implement in `working_set_resolve.py`, `working_set_runtime.py`,
      `commands.py` (two optional arguments; docstring contract), CLI and REST parity;
      regenerate the schema fixture, digests, capabilities, pending connector digest.

## 2. Decision ledger — deferred

- Moved to `add-consolidation-dreamer`, where its consumer lives. Review families are
  signal families tied to due-state, and a read-only operation must not write; an
  agent's anchor choice is not recorded by this change.

## 3. Hook working-set mode

- [x] 3.1 Red: `tests/test_retrieve_nudge_working_set.py` — mode gate; data header;
      current state, units, pointers as whole items under the render ceiling;
      `ambiguous` → header + competing anchors + the `anchor` instruction; any other
      abstention → reminder only; failure → reminder within budget; prominence and
      cooldown gates unchanged; token persisted beside the checkpoint and cleared on
      the lifecycle events each client delivers; `src/exomem/_hooks` copy
      byte-identical.
- [x] 3.2 Implement in `exomem_retrieve_nudge.py` (+ `_hooks` copy) and
      `exomem_continuation_checkpoint.py`; document the mode in `install_hook.py`.

## 4. Hosted carrier line

- [x] 4.1 Red: bootstrap guidance contains the line at balanced/maximal, absent at
      light/off; the line costs at most 220 bytes; compact stays under its ceiling at
      every level and surface and keeps its 512-byte warning margin at the default
      (balanced) level (measured 2026-09-18 with the line: balanced 557 bytes of
      headroom, maximal 192, maximal having been inside the margin before the line);
      scaffold recall loop byte-identical to the served projection.
- [x] 4.2 Implement in `commands.py` bootstrap guidance and the scaffold; run
      `refresh-skill-contract` and `package-skills`.

## 5. Delivery

- [x] 5.1 Hook journey test with no paid model session: run the shipped hook as a
      subprocess against a local stub of `/api/activate_context` through a three-turn
      session (resolved packet, then an `unresolved` turn with worded candidates, then a
      follow-up carrying the persisted continuity token), asserting the injected block,
      the token round-trip and the lifecycle clear. The owner's own use is the live
      acceptance; a paid `claude -p` run is not wanted.
- [x] 5.2 `openspec validate --all --strict`; scoped suites; privacy gate; PR.
