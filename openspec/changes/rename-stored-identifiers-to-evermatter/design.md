# Design

## Context

`rename-agent-surfaces-to-evermatter` renames what agents and users see. This change renames the internal and stored names. Some of those names are bytes that stored data depends on, so the change separates the names that move from the constants that never change.

| Family | Examples | Decision |
|---|---|---|
| Note identity | `exomem_id` in every governed note and evidence sidecar (about 330 uses in 57 modules) | Moves (D2, D3) |
| Reference scheme | `exomem://` in references and MCP resource URIs (about 150 uses in 49 modules) | New scheme emitted, both resolve forever (D4) |
| Import package | `exomem`, `python -m exomem`, installed units that run `-m exomem maintain` and `-m exomem.recall_canary` | Moves with an alias package (D5) |
| Machine-local state | `<state home>/exomem`, the mode config (`~/.exomem/config.json`, `ProgramData\exomem`), import journals under the user state directory | Moves by rename (D6) |
| Protocol and derivation constants | HKDF salts such as `exomem-session-storage-v1`, the cell bearer tag `exomem-cloud-cell-token-v1:`, token audiences and the issuer, hash domains behind derived ids and backups, the `exomem-operation-v1` fence in log notes, schema ids | Keep their bytes (D0) |
| In-vault directory | `.exomem` inside the vault, shared through sync by machines on different versions | Keeps its name (D0) |
| Cloud infrastructure | Helm release, `exomem.io/*` labels and annotations, metric names, systemd unit names, images, namespaces, secrets, cellctl tables | Readers first per family; stateful resources keep their names (D7) |

The design follows the rule of the agent-facing rename, which cites "Writing effective tools for agents" on namespacing: every reader accepts both names before any writer emits the new one, and stored data moves last, on an explicit request.

## Goals / Non-Goals

**Goals**
- After this change, new notes, references, paths, imports and new Cloud resources carry the `evermatter` name.
- No note, reference, sign-in, derived id or stored state is lost, and each step can be undone until the next one starts.

**Non-Goals**
- Rewriting references that users or agents wrote into note text. A reference is content, and the old scheme resolves forever instead.
- Changing any protocol or derivation constant (D0).
- Renaming the `exomem` names on PyPI and the container registry. `rename-to-evermatter` already publishes under both names.

## Decisions

### D0. Protocol and derivation constants keep their bytes

A salt, token tag, audience, issuer, hash domain, stored fence or schema id is part of the data it protects or identifies. Changing one makes every stored session record unreadable, every cell token invalid, every derived id different or every old backup unverifiable. Such a constant therefore keeps its bytes, whatever name it carries. Each one gets a one-line comment that says so, and the package rename leaves it unchanged.

The in-vault `.exomem` directory keeps its name for a related reason. Machines that share a vault through sync run different versions, and an older machine would recreate the old directory beside the new one.

### D1. Readers first, writers second, data last

Each moving name changes in three steps, and each step ships in its own release:
1. Every reader accepts both names.
2. Every writer emits the new name.
3. Stored data moves on an explicit request.

A client, vault or cell then never holds a name that the running code cannot read.

### D2. One accessor for note identity

One function reads a note's identity, and one function writes it. The writer writes `evermatter_id`. The reader returns the value when the note holds one key, or both keys with the same value. When the note holds both keys with different values, the reader returns an integrity error, never one of the two values. The index then keeps that note under its path without an identity, the resolver resolves neither value, and the audit names the note and the fix: keep one value. Every module calls these two functions, and none reads the key directly.

### D3. The rewrite is a governed maintenance job, and its rollback reverses the rename

The rewrite follows the shape of the existing `backfill-ids` job and commits through `maintenance_jobs.commit_pages`:
1. A preview lists the notes that will change, and the notes whose text names `exomem_id`, such as Obsidian queries.
2. A start with the preview's plan ID renames the key in each note. `commit_pages` re-reads each page and skips any page that changed since the plan.
3. The job is resumable and idempotent, and it writes a receipt.
4. A rollback is the same job in reverse: it renames `evermatter_id` back to `exomem_id` through the same path. It keeps no copies, and it skips and lists any note edited since the rewrite, so no edit is lost.

A personal vault runs the job when its owner asks. Cloud vaults run it after the Cloud host move, one cell at a time, under a production grant. The order gives users one disruption at a time.

### D4. The reference scheme: emit the new one, resolve both forever

New references use `evermatter://`. The resolver accepts both schemes with no end date, because references live in places we never rewrite: users' notes, other agents' memories and ledgers. Tool results that echo a stored reference keep the scheme it was stored with. The MCP server registers its resources under both schemes, including the adoption run resources and the artifact-recovery prefix.

### D5. The package moves with an alias

The import package becomes `evermatter`. An `exomem` alias package follows the existing `kb_mcp` contract:
- `exomem.<submodule>` is the same module object as `evermatter.<submodule>`;
- `python -m exomem` and `python -m exomem.<submodule>` run like the new names, so installed service units keep working;
- the deprecation line goes to the log, never to a hook's stdout.

`kb_mcp` re-targets to the new package. The rename is one commit of moved files and rewritten imports. It lands in a freeze window that the Session manager announces to open branches, and it changes no constant (D0).

### D6. Machine-local state moves by rename

An explicit command stops the service and renames `<state home>/exomem` to `<state home>/evermatter` on the same filesystem. The rename is atomic and takes no extra disk. The same command moves the mode config and the import journals. Until the move, every reader falls back to the old path, so a machine that never runs the command keeps working. The undo is the reverse rename. A running service never moves its own state.

### D7. Cloud infrastructure: readers first per family; stateful resources keep their names

No user sees these names, and several hold state or enforce isolation. So each family moves only where it is safe:
- **Labels and annotations.** Readers, including the tenant isolation policy, accept both `exomem.io/*` and `evermatter.ai/*` before any writer emits the new one. The isolation label change gets a second review pass.
- **Metric names, systemd units, the Helm release and images.** Each moves readers first: alerts read both metric names, and the new unit replaces the old one in one converge run.
- **Namespaces on local volumes, secrets and cellctl tables.** They keep their names. New resources take the new name. Renaming a live one is a migration with data risk and no visible gain.

Each moving family gets a runbook and a production grant.

## Risks / Trade-offs

- **Users' own queries.** Obsidian queries, templates and plugins that a user wrote against `exomem_id` stop matching after the rewrite. The preview lists the notes that contain them, and the release notes say so.
- **Sync churn.** A vault under git or a sync service sees one change to every note. The job runs only on request, so the owner chooses the moment.
- **Branch conflicts.** The package rename conflicts with every open branch, so it lands in an announced freeze window.

## Removal

On or after 2027-11-23, review the use of the `exomem` alias package and the old state-path fallback, and remove each only with evidence that nothing uses it. The `exomem_id` reader and the `exomem://` resolver stay, because the rewrite runs only on request.
