<!-- authority:non-specification -->

# Prominence — how much Exomem speaks up

Exomem has one knob for how much it participates in a conversation. It is separate
from `exomem mode`, which governs how much of your *machine* Exomem may use. A laptop
on battery can still want maximal recall; a workstation can still want Exomem to stay
out of the way.

| Level | Recall | Capture | Narration |
|---|---|---|---|
| `off` | never on its own | never on its own | silent |
| `light` | only when you ask, or the turn is unmistakably on-topic | only when you ask | silent |
| `balanced` | on topic match | durable conclusions | quiet; mentions the KB only on a hit |
| `maximal` | before every substantive turn | every stepping stone | says what it recalled and saved |

## Which level you get by default, and why

**Assistants with hooks — Claude Code, Codex — default to `balanced`.** Those clients
run a capture/retrieve nudge that re-arms the check every turn, so moderate
instructions are enough.

**Assistants without hooks — claude.ai, ChatGPT, the hosted service — default to
`maximal`.** There is nothing there to re-arm the check, and instruction text decays
over a long conversation: the model reads it at turn 1, then gradually stops acting on
it. `maximal` on a hookless client produces roughly the behaviour `balanced` produces
on a hooked one. If Exomem seems to "forget to remember" in a web chat, this is why —
raise the level rather than concluding recall is broken.

Nothing stops you overriding either default. The intent is that you **tune down** if
it is too chatty, not that you have to tune up to get it working.

## Setting it

On a local install, where the setting reaches both the server and the CLI:

```
exomem prominence              # show the active level and its full contract
exomem prominence maximal      # set it
exomem prominence --hook-env   # print the nudge tunables this level implies
```

Precedence is `EXOMEM_PROMINENCE` (env) → the config file → the surface default. The
level is stored beside `mode` in the same config file, so setting one never clears the
other. `bootstrap()` reports the active level under `engagement`.

After changing the level, re-run `exomem install-hook` so the nudge cadence matches.

On a web client there is no filesystem, so the level lives in your assistant's custom
instructions. Paste one of the blocks below.

---

## Copy-paste blocks

Each block is self-contained. Paste it into:

- **claude.ai** — Settings → Profile → *"What personal preferences should Claude
  consider in responses?"*
- **ChatGPT** — Settings → Personalization → Custom instructions → *"Anything else
  ChatGPT should know?"* (each field caps around 1500 characters; these fit)

Replace the connector name if you named yours something other than Exomem. Otherwise
paste the block as it stands rather than writing your own shorter version of it.

**Each item in the capture list is a separate switch, not an example of a general
idea.** An assistant treats that list as the definition of what is worth saving, so
dropping an item deletes that whole class of capture — silently. Nothing errors, no
warning appears, and the symptom is only that some kind of thing never gets written,
which is close to impossible to notice from inside a conversation. A shortened list
has already cost a real user months of uncaptured hands-on results: it kept the four
knowledge-work items and dropped the rest, so every method actually carried out fell
through. If a level is nearly right, append a tuning line from the section below
instead of trimming the block — that keeps the classes intact and changes only the
threshold.

Watch the recall and capture lines separately, too. A clause such as "stay quiet on
chit-chat" belongs to recall; moved or generalised to capture, it suppresses exactly
the casual-looking conversations where a real result tends to arrive.

### Maximal — recommended for web and hosted

```
I keep a personal Knowledge Base served by the Exomem MCP connector. If no Exomem skill is loaded, call bootstrap(profile="compact") once per chat and follow it.

Exomem prominence: MAXIMAL.
- Recall: search Exomem before answering any substantive turn, not only ones that obviously reference past work. Assume it may hold something relevant until a search says otherwise. Skip only pure chit-chat and short control messages. Cite what you use. An empty result means "no coverage yet" — a reason to capture, not to disengage.
- Capture: save at every stepping stone and keep the bar low — a decision, a solved problem, a diagnosed failure, a reusable pattern, a durable fact about a recurring person, project, or organisation, or a method I actually carried out and told you how it went (worked, failed, or bounded a parameter). Do not wait to be asked. When torn between saving and letting it pass, save. Write a short compiled note, never a transcript.
- Narration: say what you did. Name what you recalled, and report one line after each write: "Saved -> <path>".
- Treat the final mutation result as authoritative: if it reports committed, the write succeeded, whatever warnings appear beside it. Never infer a failure code that the server did not return.
```

### Balanced — the default where hooks exist

```
I keep a personal Knowledge Base served by the Exomem MCP connector. If no Exomem skill is loaded, call bootstrap(profile="compact") once per chat and follow it.

Exomem prominence: BALANCED.
- Recall: search Exomem when a turn references one of my projects, domains, named entities, or asks what I concluded, tried, or decided. Skip chit-chat, control messages, and follow-ups the current conversation already answers. Cite what you use.
- Capture: save when the conversation reaches a stepping stone — a durable conclusion lands, a recurring entity gains reusable facts, or a method I carried out reached a result worth repeating or avoiding. Not mid-thought exploration, tangents, or open questions. Short compiled note, not a transcript.
- Narration: stay quiet. Mention the KB only when a search returned something you used, and report one line after a write: "Saved -> <path>".
- Treat the final mutation result as authoritative: if it reports committed, the write succeeded, whatever warnings appear beside it.
```

### Light — when it is getting in the way

```
I keep a personal Knowledge Base served by the Exomem MCP connector.

Exomem prominence: LIGHT.
- Recall: search Exomem only when I ask a recall question outright, or when the turn is unmistakably about a topic it covers. When in doubt, don't search.
- Capture: write to Exomem only when I ask. Do not save on your own judgment, however durable the conclusion looks.
- Narration: never mention searching. Fold anything you retrieve into the answer with a citation and nothing more.
```

### Off — explicit invocation only

```
I keep a personal Knowledge Base served by the Exomem MCP connector. Use it only when I explicitly ask you to search it or save to it. Never search or write on your own judgment, and don't mention it otherwise.
```

---

## Tuning without changing level

If a level is nearly right, these are the usual adjustments, each a line you can
append to the block:

- Too much saving, recall is fine — *"Capture only decisions and diagnosed failures;
  skip patterns and entity facts."*
- Right amount of work, too much talking — *"Do not narrate recall; keep the one-line
  save report."*
- Should stay out of one area — *"Never search or save anything about <topic>."*
- Recall keeps missing — check coverage first with a direct question before raising
  the level. An empty result on a topic you never captured is honest, not a failure.

## What the level does not change

The level governs *when* Exomem acts, never what it is allowed to touch. Governed
writes stay inside the Knowledge Base folder at every level, `Sources/` and `Evidence/`
stay append-only, and access policy is enforced independently. `maximal` is more
eager, not more privileged.
