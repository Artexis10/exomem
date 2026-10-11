# Design

## The guard being relaxed, and why the relaxation is narrow

`_read_json` refuses any config that is not a regular file. That guard exists because a hook config names commands the agent executes; a symlink is a redirect, and following an arbitrary one would let anything that can create a link in the config directory choose what the agent runs.

yadm's alternate mechanism produces exactly one shape of link, and it is not attacker-shaped: the deployed name points at a sibling in the same directory whose name is the deployed name plus a `##`-prefixed suffix. `_alternate_sources()` already trusts that shape when it prunes, so the module has an existing, shipped judgement that a `##` sibling is the same trust domain as the deployed file.

The relaxation therefore admits a link only when every one of these holds, and refuses otherwise:

- the link value contains no path separator, so the target cannot escape the directory
- the target name is exactly `{deployed name}##{suffix}` with a non-empty suffix
- the target resolves inside the already-trusted config directory
- the target is itself a regular file passing the unchanged ownership and `0o022` writability checks

An attacker who can already create files in that directory can write the config directly; the relaxation grants no reach that was not already there. A link to an absolute path, a parent directory, a non-`##` name, or another link stays refused.

## Why merge writes every alternate source rather than only the link target

Two yadm deployment styles must both survive:

- **Link** (Unix): the deployed name is a symlink to one source. Writing the resolved target is sufficient and the deployed view updates for free.
- **Copy** (Windows/Msys, where links are impractical): the deployed name is a real file, and `yadm alt` later overwrites it from whichever `##` source matches the machine.

Writing only the resolved target fixes the first and leaves the second exactly as broken as it is today. Writing only the deployed file fixes neither durably. So the merge writes the deployed config *and* every parseable alternate sibling, which is the shape `uninstall_hook` already uses — a source list, each written through the same mode-preserving backup and same-directory atomic replacement as the deployed file.

Sources that do not parse as JSON are skipped rather than failing the run: yadm alternates may be templates, and uninstall already decides parseability by trying. A skipped source is reported, never silently dropped.

## Unevaluated is a third state

The current report models each condition as pass or fail, so a condition that never ran has nowhere to go and lands on pass. The fix gives it its own state and makes it fail the run:

- a condition whose input could not be read reports `unevaluated`, naming the path and the read error
- `overall` fails when any condition is unevaluated
- an unevaluated condition is never rendered with the vocabulary of a pass

This is the narrow correction. It does not change what any evaluable condition decides, so a machine whose config reads cleanly sees an identical report.

## Alternatives rejected

**Teach yadm about exomem instead.** Moves the coupling into the user's dotfiles and has to be repeated per machine and per profile; the tool that owns the entries should own their durability.

**Refuse alternate-managed configs with a clear error and instruct the user to edit sources by hand.** Honest, and strictly better than today's silent revert, but it leaves the install/uninstall asymmetry permanently and pushes hand-editing onto exactly the configs where hand-editing has already drifted.

**Follow any symlink.** Removes a real guard for a case that does not need it.
