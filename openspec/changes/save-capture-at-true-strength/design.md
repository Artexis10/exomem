## Context

The hook renders the activation `capture_policy` block as `say` only, and cuts it to its first sentence above 1,600 characters. Every hook revision since the block began reads only `say`. The longest rendering, at `silent` with `confirm-shortcut`, is 1,587 characters including the header, and the populated bootstrap core has 24 to 59 bytes of headroom at `maximal`.

MCP-only clients receive the block as raw JSON with no cap.

## Decisions

### One principle, not two lists

The old text named material that stays quiet ("tentative events", "unresolved questions"). Agents read each list as a general rule and saved nothing unsettled. A real-model probe also saw an excluded class echoed into a write as a disclaimer that kept the excluded detail. The new rule states what to do with a claim, at its true strength, and gives one reason to write nothing: nothing later needs it. The rule names no excluded material, and it scopes the silence to writes.

### The full rule rides its own key

`rules` is a new key, not a longer `say`. A longer `say` would pass the hook's cap and silence every capture rule. A hook installed before `rules` existed ignores a new key and renders `say`, so `say` keeps `_EPISTEMIC_CAPTURE`, which is a one-sentence form of the rule. The hook that knows `rules` renders it after `say` under `_CAPTURE_RULES_MAX_CHARS`, a budget 25% above the measured text. A longer `rules` is omitted whole. The hook never cuts `say` to make room.

The bootstrap core stays at its byte ceiling and carries no new text. The baseline tail loses "tentative events", which frees 19 bytes. The first sentence of that text already requires stability or recurrence and reuse value, which keeps a tentative claim from becoming a baseline.

### Tentative stays out of the baseline, and in the notes

A tentative claim fails the baseline test, so it gets no facet and no affiliation. When later work could use it, the agent saves it as one compiled observation that states it is unconfirmed. This reverses the earlier rule that an unsure report of an event stays unwritten unless a collection has a note field. A tentative claim is still never written as an event that happened.

### Cost of the block

The block grows by about 810 characters, and the server reserves them from the map's room. The block is sent once per conversation. At the default hook budget of 6,000 characters the map keeps about 3,600. A request with less than about 3,150 characters leaves the block owed, as the existing "request too small" rule states.

## Risks

- A model may read "true strength" as licence to save every uncertain remark. The trivia clause and the reuse test limit it. The real-model run covers a trivia control.
- The core headroom stays small. The change adds no core text.
