## Decision

Replace the reply-length gate with a landing gate at every level except
`maximal`. A landing is the point where work becomes durable and shareable, which
is where a stepping-stone worth capturing most often appears; a long reply is
not evidence of one.

## Measurement

Over 14 days on the owner's machine the length gate fired 1,994 times (about 76%
of prompts). Only 20% of those turns wrote to Exomem. Each fire adds a full
context re-read (median about 420k tokens in long sessions).

## Shape

- Landing commands are a small module constant: `git` commit/push/merge/tag,
  `yadm` commit/push, `gh pr create`, `gh pr merge`, `gh release create`.
- The command line is tokenised, not substring-matched, and a command word counts
  only at the start of a `&&`/`;`/`|`/newline segment after env assignments and
  a few wrappers, including `bash -c`. Prose inside quotes (a commit message,
  an `echo`) never counts.
- A command counts unless it fails: the existing per-tool signal for Claude
  (`tool_result.is_error`), or an explicit non-zero exit code for Codex, whether
  it runs `exec_command` as a function call or inside an `exec` cell. Missing
  exit information counts as landed: on real Codex sessions a quarter of landing
  cells return while the command is still running, and a false positive costs
  one reminder on a rare event.
- Dry runs, `git merge --abort/--quit`, `git push --delete`, and a `git tag` that
  lists, verifies or deletes are not landings. `git commit -n` is `--no-verify`
  and still commits. `timeout`, `env -u` and `if`/`then`/`!` are command prefixes.
- Because a landing replaces the length test, a terse "Pushed." after a push
  still gets the check. No new environment knob is added; the existing
  `EXOMEM_CAPTURE_NUDGE_MIN_CHARS` now sets the `maximal` gate and the episode
  ask's notion of a substantive turn.
- Conversation-level decisions with no landing are not lost: the episode ask
  (every K substantive turns) is unchanged.

## Risk

A decision made in a turn with no landing now waits for the episode ask instead
of a per-turn reminder. The cost of that is a later, batched prompt; the cost of
the old gate was a full-context turn on four of five fires that wrote nothing.
