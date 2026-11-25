# Design

## Context

`rename-agent-surfaces-to-evermatter` renames what agents and users see. This change renames what is stored:

| Identifier | Where it lives | Uses in `src/exomem` |
|---|---|---|
| `exomem_id` | every governed note's frontmatter and evidence sidecar | about 330, in 57 modules |
| `exomem://` | references in notes, ledgers, agents' memories and tool results | about 150, in 49 modules |
| package `exomem` | every import, plugin hook and test | all modules |
| state paths | `<state home>/exomem/state` and configuration directories | `state_paths.py` |
| Cloud names | Helm release, namespaces, secrets, images, cellctl | the Helm chart, cellctl and the secret contracts |

The design follows the same rule as the agent-facing rename (it cites "Writing effective tools for agents" on namespacing): every reader accepts both names before any writer emits the new one, and stored data moves last, on an explicit request.

## Goals / Non-Goals

**Goals**
- After this change, new notes, references, paths, imports and Cloud resources carry the `evermatter` name.
- No note, reference, sign-in or stored state is lost, and each step can be undone until the next one starts.

**Non-Goals**
- Rewriting references that users or agents wrote into note text. A reference is content; the old scheme resolves forever instead.
- Renaming the `exomem` names on PyPI and the container registry. `rename-to-evermatter` already publishes under both names.

## Decisions

### D1. Readers first, writers second, data last

Each identifier moves in three steps, and each step ships in its own release:
1. Every reader accepts both names.
2. Every writer emits the new name.
3. Stored data moves on an explicit request.

A client, vault or cell then never holds a name that the running code cannot read.

### D2. One accessor for note identity

One function reads a note's identity: `evermatter_id` first, then `exomem_id`. One function writes it, and it writes `evermatter_id`. Every module calls these two functions; none reads the key directly.

A note that holds both keys with different values is an integrity error. The audit reports it, and no reader picks one silently. A note that holds both with the same value is a half-finished rewrite, and the job completes it.

### D3. The rewrite is a governed maintenance job

The rewrite follows the shape of the existing `backfill-ids` job:
1. A preview lists the notes that would change, and the notes whose text names `exomem_id` (such as Obsidian queries).
2. A start with the preview's plan ID copies each note before it changes it.
3. Each note changes atomically. The job is resumable and idempotent, and it writes a receipt.
4. Rollback restores the copies from the receipt.

A personal vault runs the job when its owner asks. A Cloud vault runs it after the Cloud host move, one cell at a time, under a production grant.

### D4. The reference scheme: emit the new one, resolve both forever

New references use `evermatter://`. The resolver accepts both schemes with no end date, because references live in places that we never rewrite: users' notes, other agents' memories and ledgers. Tool results that echo a stored reference keep the scheme that the reference was stored with.

### D5. The package moves with an alias

The import package becomes `evermatter`. A small `exomem` package re-exports it and logs one deprecation line, so plugin hooks and scripts installed against the old name keep running through the compatibility window. Console scripts already use `evermatter`.

### D6. State paths move through the existing relocation

The state root moves from `exomem` to `evermatter` directories through the crash-safe relocation in `state_migration.py`: copy, fsync, verify, publish, and then delete the source. It runs only from an explicit command that stops the service first. A running service never moves its own state. Until the move happens, the service reads the old root.

### D7. Cloud infrastructure names move one family at a time

Renaming a live Kubernetes namespace, secret or Helm release is a migration, not an edit. Each family gets its own runbook with a reversible order of steps:
1. Create the new resource.
2. Point the consumers at it.
3. Prove the result.
4. Retire the old resource.

Each family needs a production grant. Secrets get a second review pass. This step comes last, because nothing a user sees depends on it.

## Risks / Trade-offs

- **Users' own queries.** Obsidian queries, templates and plugins that a user wrote against `exomem_id` stop matching after the rewrite. The preview lists the notes that contain them, and the release notes say so.
- **Sync churn.** A vault under git or a sync service sees one change to every note. The job runs only on request, so the owner chooses the moment.
- **Duration.** The package and infrastructure steps are large mechanical sweeps. Each ships in its own release, with one review, behind the readers-first rule.

## Removal

On or after 2027-11-23, review the use of `exomem_id` readers, the `exomem` alias package and the old state paths. Remove each only with evidence that nothing uses it. The `exomem://` resolver stays.
