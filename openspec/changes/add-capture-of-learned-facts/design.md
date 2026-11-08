## Context

Development rounds on 2026-11-08 compared the same instruction text in two channels on the same CL-Bench questions. Run data lives outside the repository. The paired comparisons use one run per arm, so they guide the design and decide nothing.

| Channel | Writes | Questions with a recall | Score |
| --- | --- | --- | --- |
| `balanced` block plus a save sentence, system prompt | 0 of 10 questions | 1 of 10 | 0.560 |
| `balanced` block in system prompt, save sentence in workspace `CLAUDE.md` | 13, on 5 of 10 questions | 1 of 10 | 0.573 |
| `balanced` block in system prompt, recall-and-save sentences in workspace `CLAUDE.md` | 62, on 19 of 20 questions | 20 of 20 | 0.773 |

The last row scored +0.18 [+0.11, +0.27] over the shipped arm and −0.00 [−0.02, +0.02] against a plain memory folder, on paired questions. Recall was the second missing half: a save-only sentence saved the right facts, but later questions never read them.

## Decisions

### The instruction is the user's, not the server's

The server already states its capture policy through bootstrap, the activation block and hooks. Agents treat that text as background. The learning instruction is a request that the user makes once, so it lives in the user's own instruction file and in the user's voice. Exomem supplies the text and the placement. It does not inject the text through any server or hook channel. Hook cues were tested and did not change behaviour.

### One source, three placements

The text lives once in the package, beside the prominence constants. Setup, the documentation check and the benchmark read that source. The docs show the same text, and a check fails when the doc copy differs from the package text.

### Setup asks, and marks what it writes

The instruction file belongs to the user. Setup shows the text and asks before it writes. It writes between named begin and end markers and replaces only that span on a re-run. `install-hook --uninstall` and a setup flag remove only that span. A non-interactive run writes it only with an explicit flag, so a scripted setup never edits a user file silently. While the effective prominence is `off` or `light`, setup does not offer the step: those levels mean "save only when I ask".

Alternatives considered: a forced save turn from the Stop hook, which reaches only Claude Code and Codex and needs no user file, and a user-set learning mode. The forced turn stays a possible later backstop if a measured gap remains after this change.

### The benchmark lays what setup lays

The CL-Bench P arm stands for Exomem as shipped. After this change, the shipped setup includes the learning instruction, so P lays it. The harness reads project settings only, so P writes the text to the workspace `CLAUDE.md`. Claude Code delivers that file through the same user-context channel as `~/.claude/CLAUDE.md`. The manifest records the text's digest and where it was laid.

## Risks

- **Write friction:** more saves mean more write refusals (58 refusal mentions in the 20-question run). The write-path work owns that fix, and this change reports the count.
- **Over-capture:** a user may find more notes than they expected. The text limits saves to reusable facts and asks to update an existing note first. The curation flow reviews the rest.
- **Hosted clients:** no harness proves the paste-in route.
