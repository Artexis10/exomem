## Why

An outcome evaluation found that connected agents lost work at capture in two ways. They saved a pointer to a working file instead of the values in it, and the file was gone later. They also never saved an open question or a held view.

The cause is scope. The rule "tentative claims stay quiet" belongs to personal baseline capture, which covers casual statements such as "I might switch gyms". Exomem serves it unscoped, in the bootstrap core and in the per-turn capture policy. Agents read it as "never save anything tentative".

## What Changes

- Scope the rule to what it governs. The served text says "tentative events stay quiet", as the scaffold already does. The baseline requirement says "tentative personal claims" and states that it governs personal baseline context only.
- Add three classes to the stepping-stone capture predicate:
  - a held view, such as an interpretation or working hypothesis, attributed to its holder with its uncertainty, never as an established fact;
  - a question the work leaves open, saved as an open question;
  - the concrete values a task settled (times, prices, terms, contacts, limits), stated in full, never as a pointer to a working file.
- Serve the three classes in the per-turn activation `capture_policy` block, which every client receives, and in the skill scaffold. The `balanced` bootstrap capture text gets one short clause for the open question.
- Casual tentative personal claims, and mid-thought exploration and tangents, stay quiet.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `durable-personal-baseline-capture`: the quiet rule names personal claims and states its scope.
- `agent-bootstrap-contract`: the capture predicate covers held views, open questions and established values.

## Impact

- `src/exomem/prominence.py`: one wording fix, one new constant, one clause in the `balanced` capture text.
- The skill scaffold (`operations.md`, `engagement.md`, the `exomem-capture` workflow skill) and the packages generated from it.
- The copyable custom-instruction blocks in `docs/prominence.md`, `docs/ai-assistant-guide.md` and `QUICKSTART.md`: a same-length swap to "tentative events stay quiet", and the `balanced` block drops its "or open questions" exclusion.
- The activation `capture_policy` block grows by one sentence. Its longest rendering stays under the hook's 1,600-character cap. The bootstrap core grows by 39 bytes at `balanced` and by none at `maximal`.
- No tool schema, write path or stored data changes. The behaviour depends on a model's judgement, so a real-model probe on the three classes and the control decides the release.
