# Design

## Context

Exomem carries capture guidance in four channels:

- The learning instruction, in the user's voice. Setup writes it to `CLAUDE.md` and `AGENTS.md`, and hosted users paste it.
- The level blocks in `docs/prominence.md`, which users paste or setup appends to the system prompt.
- Server text: the served contract, the `capture_policy` block in `recall_context` and the server instructions.
- The Stop-hook capture check.

The `capture_policy` block reaches a session once. After that, the continuity token marks it as held. A file handed over later in a session gets no custody text from it.

Primary sources: the roster rows "Context is wrong, bloated, stale, or missing" (Effective context engineering) and "I can't tell whether a change actually improved the agent" (Demystifying evals for AI agents). The first says where an instruction sits decides its weight. The second says a behaviour change needs repeated real-model trials with positive and negative cases.

## Goals and non-goals

Goals:

- An agent keeps a file the user hands over, without being asked, on every client that loads the learning instruction.
- An agent does not keep files it works on in the user's repository, or files the user only mentions.

Non-goals:

- A hook or server that uploads or saves on its own. Hooks trigger; agents author. Saving without the agent would skip taking identity from the document and would copy workspace files.
- A server-side detector of handed files. The server cannot see client files, and a word list that guesses what a user meant is unsound under C6.

## Decisions

**Custody lives in the learning instruction.** The instruction already asks for recall and saves in the user's voice, and the 2026-11-08 measurement showed that channel works. One sentence asks for custody. One more scopes it away from repository files. The custody rule stays under `proactive_capture`: handing a file over makes it eligible, and the disposition still decides the write, as for generated-file adoption.

**Remove the contradictions rather than add more guidance.** "Never save transcripts" means chat transcripts. "Tentative claims stay quiet" means notes. Both read as reasons to drop a handed file. Each gets the narrower wording in the level blocks, the served baseline doctrine and the Stop-hook check. The Balanced and Maximal blocks stay under 1,500 characters, the ChatGPT field limit.

**No per-turn custody line.** The 2026-11-17 rounds showed it does not change behaviour, and the per-turn carrier is at its size ceiling.

**A text copy is labelled.** A `content` capture of a handed file returned the same "Saved" line as an exact-bytes capture, and an agent told the user it had kept the file. The reply says when Exomem kept text rather than the original.

**A kept-for-custody Source waits in its own quiet count.** The agent decides, when it saves a handed file, whether the file only needs keeping. It says so with `save_source` `options.custody_only: true`. The Source records `custody_only: true`, and the reply's outcome line says "kept for custody only", so the user can overrule the judgement at once. The `unprocessed_source` family skips such a Source. A new audit family, `custody_source`, lists it instead: always `info`, oldest first, never escalated by age, and outside the default attention set and the due-state projection, so a client lists it by name and nothing pushes it. A wrong mark loses nothing: the original stays stored and findable, a note that cites the Source takes it off both lists through the existing coverage rule, and a correction with `options.custody_only: false` removes the mark. The operator chose this on 2026-11-18 over a capture-time Inbox dismissal (dismissals train the queue to go quiet for a whole family) and over leaving custody Sources to age into warnings.

**Contingency: a Stop-hook custody check.** Build it only if the real-model run misses its bar. It would fire when the agent read a file that the human turn names, outside a git work tree, and no Source write succeeded. It reaches Claude Code and Codex only.

## Risks

- An agent can mark a file custody-only that the user wanted compiled. The reply names the mark, and the quiet count keeps it listed.
- The behaviour stays a model judgement. Hosted clients have no harness proof.
- Existing users keep the old instruction until they run setup again or paste the new text.

## Proof

One incident-harness run on the subscription. The arms are the base branch head and the changed text. The harness asserts that the learning-instruction and level-block digests differ between arms.

Cases, three repeats each:

- Positives: the transcript case, the screenshot case, and one held-out positive written outside the session that iterates on the wording.
- Negatives: a git-tracked file the user asks to fix, and a file the user names but says to ignore. Each expects no Source.
- Regressions: the re-shared attachment and the misleading repository match.

Ship bar: at least 5 of 6 exact-bytes saves on the two positives in the changed arm, no Source in the negatives, and the screenshot judge never treats a kept claim as verified.
