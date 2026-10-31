# Organisation replay: input and run-file schema

This document describes the files that the organisation replay adapter reads and writes.
It covers f34 `organisation_autonomy_replay` (PREREGISTRATION §7, sequence 8).
It gives field names, types, allowed values and meanings only.
It holds no situation content, source truth or answer keys.

The adapter drives two sessions per situation and arm, beside the switch-evaluation capture route.
The adapter author never reads situation files, keys or arm maps.
Build and test the adapter against the toy example at the end of this document.

## 1. Situation file

One YAML file holds one situation's runner input.
File names are opaque: `hNN.yaml` for the held-out set and `dNN.yaml` for the development pool.
A file name never tells you the situation's content.

Each situation has a separate key file: `kNN.yaml` for the held-out set and `dkNN.yaml` for the development pool.
A key file names its case file and that file's SHA-256.
Only the scorer reads key files.
The runner loads case files only and never opens a key file.

### Top-level fields

| Field | Type | Values | Meaning |
|---|---|---|---|
| `id` | string | `hNN` or `dNN` | Opaque situation id. Equals the file stem. |
| `pool` | string | `heldout`, `development` | The pool that the situation belongs to. |
| `use` | string | free text | The custody rule for the file. The runner ignores it. |
| `category` | string | `relation`, `referent`, `placement`, `freshness` | The category from the public amendment. |
| `variant` | string | see below | The sub-case inside the category. |
| `client_shape` | string | `hooked`, `hookless` | The client configuration for both sessions. |
| `seed` | mapping | see "Seed" | The vault state to build before the capture session. |
| `capture_session.turns` | list of strings | at least one item | User turns, sent in order to one client process. |
| `fresh_session.question` | string | one question | The only user turn of the fresh-recall session. |

These are the allowed `variant` values for each category:

- `relation`: `specific_fits`, `generic_or_none`
- `referent`: `positive`, `durability_twin`
- `placement`: `under_expected_home`, `outside_unrelated_homes`
- `freshness`: `promoted_entity_type`, `promoted_relation`

The runner needs `category` and `variant` only to label the run file.
Neither field changes how a session runs.

A case file has no key fields.
Refuse a case file that carries a top-level field outside the table above.
Never open a case file or a key file while you write or debug the adapter.

### Runtime parameters

A situation file carries one runtime parameter: `client_shape`.
These values come from the protocol and the binding, the same for every situation:

- prominence: the product's shipped default for the client shape, resolved and recorded at binding;
- call guard: at most 30 Exomem calls per user turn, the same in both arms;
- turn timeout and fault retries: the maintained switch-evaluation values;
- model, client version, hook settings and MCP configuration: bound once per run.

### Seed

| Field | Type | Meaning |
|---|---|---|
| `seed.vocabulary` | list of vocabulary entries | Registry entries and keys to create before the session. |
| `seed.homes` | list of home entries | Homes registry entries. |
| `seed.entities` | list of entity entries | Entity pages. |
| `seed.notes` | list of note entries | Compiled notes. |

The current files seed no typed edges between pages.
Edges are what the sessions are measured on.

**Vocabulary entry**

| Field | Type | Values | Meaning |
|---|---|---|---|
| `registry` | string | `entity-types`, `relations`, `domains`, `projects` | The target registry. `projects` means a project key. |
| `key` | string | slug | The entry key. |
| `label` | string | optional | The display label. Only for `entity-types` and `relations`. |
| `guidance` | string | optional | Entity-type guidance text. |
| `description` | string | optional | Relation description. |
| `parent` | string or null | registered key | The parent entry. A relation always has a parent. An entity type may have none. |
| `seeded_as` | string | `established`, `recent_promotion_by_prior_agent_session` | How the entry enters the vault. See "Seed construction". |

An entry with no `seeded_as` field is `established`.
A `domains` or `projects` entry carries only `registry` and `key`.

**Home entry**

| Field | Type | Meaning |
|---|---|---|
| `folder` | string | A folder under the Notes root, `Notes/<Name>`. |
| `topics` | list of strings | Domain keys that the home serves. The list may be empty. |
| `projects` | list of strings | Project keys that the home serves. The list may be empty. |

A home serves at least one topic or one project.

**Entity entry**

| Field | Type | Meaning |
|---|---|---|
| `ref` | string | A local reference: `A`, `B` and so on. A key refers to the entity by this value. |
| `title` | string | The entity name. |
| `type` | string | A core entity type id, or a vault type that this seed declares. |
| `body` | string | A one-line summary. |

**Note entry**

| Field | Type | Meaning |
|---|---|---|
| `title` | string | The note title. |
| `type` | string | A compiled note type id from the shipped note-types registry. |
| `folder` | string | The folder where the note must end up. Check it after the write; never pass it as an argument. |
| `topics` | list of strings | Optional domain keys. |
| `projects` | list of strings | Optional project keys. A project-partition note type has exactly one. |
| `body` | string | The note body. When it has no semantic unit section, the seed builder wraps it verbatim under one unit heading that the note type accepts, chosen from the note-type registry; both arms use the same wrap. |

### Placement rule for seed notes

The product places a compiled note by design decision D1 of `promote-referents-without-nudges`:

1. A note type with `partition: project`, such as `research-note`, goes to the home of the key passed as `project`.
2. Any other compiled type goes to the first of its `topics`, in the given order, that has a home.
3. Otherwise the note goes to its type's default folder.
4. A superseding note keeps its predecessor's folder.

A note of another type that carries `projects` still lands by rule 2 or 3, never in a project home.
A seed note's `folder` follows from these rules; the seed check compares the actual folder with it.

### Seed construction: the doors and the order

Build each seed in the arm's own slot, through that arm's public MCP doors.
Use a model-free scripted client.
Never write vault files directly.
Take tool and operation names from discovery at bind time, not from this table.

| Seed item | Door | Argument shape |
|---|---|---|
| `entity-types` entry | `schema_memory`, subject `entity-types`, the governed save operation | Entry `{label, guidance, parent?}` under `key`, with the registry's `expected_hash`. |
| `relations` entry | `schema_memory`, subject `relations`, the relation proposal and save operations | `requested_label`, `parent`, `description` and `direction`. Direction is `directed` unless the seed states otherwise. |
| `domains` entry | `schema_memory`, subject `domains`, save | One domain key. |
| `projects` entry | No registry door. A project key registers itself the first time a create-note call uses it in `projects`. | See "Known gaps". |
| home | `schema_memory`, subject `homes`, operation `save` | `proposal: {upsert: {<home key>: {attributes: {folder, topics, projects}}}}`, `expected_hash` and a `why`. |
| entity | `connect_memory`, operation `create-entity` | `entity_type`, `name` (the title) and `summary` (the body). |
| note | `create_note` | `title`, `content` (the body), the note `type`, and `topics` when given. For a note type whose registry `partition` is `project`, pass the single project key as the singular `project` argument. The placement resolver chooses the folder. |

Build the seed in this order:
1. Save `domains` entries, then established `entity-types` and `relations` entries.
2. Save homes.
3. Create entities.
4. Create notes. Each note must land in its `folder`; otherwise the seed fails.
5. Settle every promotion entry that steps 1 to 4 created. See "Known gaps".
6. Save each `recent_promotion_by_prior_agent_session` entry from its own scripted MCP session. That session's id differs from the settle session and from both measured sessions.
7. Snapshot the seed: the tree digest, the entity-graph projection and the registry overlays.
8. Check the snapshot against the seed specification, comparing meaning rather than bytes.

If any step fails, the run records a harness fault and never runs the actor.

## 2. Run file

Write one run directory for each pair of situation and arm.
The scorer reads the run directory and the key, and nothing else.

### `run.json`

| Field | Type | Meaning |
|---|---|---|
| `situation_id` | string | The situation id. |
| `situation_sha256` | string | SHA-256 of the situation file bytes that the run used. |
| `arm` | string | An opaque arm label. The arm map lives elsewhere, and the scorer resolves it only after scoring. |
| `client_shape` | string | Copied from the situation file. |
| `binding_ref` | string | The id of the run's binding record (section 3). |
| `seed` | object | `tree_digest`, `projection_digest`, `registry_overlay_digests`, `check: pass or fail`, and `settled_promotion_ids`. |
| `sessions` | list of 2 | One capture session object and one fresh session object, in that order. |
| `end_state` | object | See "End-state readback". |
| `faults` | list | Each fault as `{session, attempt, kind, detail}`. `kind` uses the switch-evaluation classification: `harness_fault`, `actor_failure`, `quota` or `abort`. |
| `status` | string | `ok`, `struck` or `actor_failure`. |

### Session object

| Field | Type | Meaning |
|---|---|---|
| `role` | string | `capture` or `fresh`. |
| `session_id` | string | The client session id from the init event. |
| `attempt` | integer | The attempt number, starting at 1. A retry keeps the same inputs. |
| `init` | object | The init facts that switch evaluation already records: model, client version, tools, MCP servers, permission mode and the API key source. |
| `transcript_path` | string | The raw stream-JSON transcript, relative to the run directory. |
| `tap_log_path` | string | The tap log: discovery, server instructions, every call and every result. |
| `calls` | list | One record per Exomem call. |
| `final_answer` | string | The final `result` text of the last turn. For the fresh session, this is the answer that gets scored. |
| `turn_results` | list | Each turn's terminal result: subtype, `is_error`, duration and usage. |
| `guard_refusals` | integer | Calls that the 30-per-turn guard refused. |

Each item in `calls` has these fields:

| Field | Type | Meaning |
|---|---|---|
| `turn` | integer | The user turn index. |
| `seq` | integer | The order of the call inside the session. |
| `tool` | string | The public tool name, as served. |
| `arguments` | object | The arguments, verbatim. |
| `request_bytes` | integer | UTF-8 length of the canonical JSON of `arguments`. |
| `result` | object | The result, verbatim. |
| `response_bytes` | integer | UTF-8 length of the result text. |
| `is_error` | boolean | True for an MCP error result. |
| `refusal_code` | string or null | The structured refusal code, when there is one. |
| `mutating` | boolean | From the served tool annotations and the operation. |
| `target` | object or null | `{path, title}` that the call wrote or tried to write, read from the arguments and the result. |
| `retry_hash` | string or null | The retry hash that a refusal offered, or the one that the call sent. |

### End-state readback

Read the end state after the capture session's client and server have exited, and before the fresh session starts.
Read the slot vault with the benchmark's file projector: the opt-in entity-graph projection of `benchmarks/epistemic/projectors/exomem_vault.py`.
Do not read the end state through MCP doors.
A call to an MCP door can deliver or settle due-state and recent promotions, which would change what the fresh session sees.
If a read through a door is unavoidable, use a CLI or REST read, which the product treats as non-settling.
Record the read and its door in the run file.

| Field | Content |
|---|---|
| `pages` | Every page under Notes and Entities: path, title, filename stem, role, type, frontmatter topics and projects, and content hash. Include each page's presence in the seed snapshot. |
| `pages_changed` | The paths that the capture session created or modified, compared with the seed snapshot. |
| `edges` | Every typed edge, resolved: subject path, relation key, target path and the page that holds the edge. |
| `entity_types` | The end-state vault entity-type registry: each key with its parent chain, plus the core keys. |
| `relations` | The end-state relation registry: each key with its parent chain, `generic`, `direction` and `inverse`. |
| `registry_history` | The `added` events in the capture session for entity types and relations: key, registry and time. |
| `homes` | The end-state homes registry: each entry with folder, topics, projects and status. |
| `operation_log` | The atomic operation log entries of the capture session, including rotation archives. Each entry keeps any edge-review reason field verbatim. |
| `promotions` | The open and settled recent-promotion entries: id, kind, creator session and subjects. |

The fresh session runs on the same slot vault, unchanged.
It needs no end-state readback, because the scorer reads its transcript, calls and final answer.

## 3. Checks before generation

The adapter must give the measurement lane each of these, and must refuse to start when one fails:

1. Check the digest of every situation, key and protocol file against the custody manifest. A mismatch stops the run before its first session. The adapter computes situation digests; the measurement lane checks key digests outside the adapter.
2. Write one binding record per arm with these fields:
   - `revision`: 40 hex characters;
   - `wheel_sha256` and the product version string;
   - interpreter path and version for the server, the tap and the seed builder;
   - `package_identity`: origin, revision and loaded source path, read from inside the server process;
   - `tools_list_sha256` and `instructions_sha256` for each client shape, captured through the tap;
   - contract and scaffold version;
   - engagement: owner, authority version, proactive-capture setting and the resolved prominence for each client shape;
   - client version, model id, permission mode, `mcp_config_sha256` and `hook_settings_sha256` (hooked only).
3. Check that the base revision of the two arms is equal, and that the candidate adds only the PR B diff.
4. Refuse the run when a session's model id differs from the bound model id.
5. Confirm that the subscription route is in use: no API key variables in the environment, and an `apiKeySource` that shows no key.

## 4. Known gaps in the current public doors

The adapter packet must settle each gap with its owner before generation.

1. **Seed promotions.** Every entity creation, registry save and typed edge between entity pages through a public door records a recent-promotion entry. As a result, established seed state would look like a fresh promotion to the capture session. The adapter needs a public, non-destructive way to settle those entries before the capture session. Two options:
   - a scripted settle session that receives the due-state carrier;
   - review dismissals through `triage_memory`.

   That settlement must leave the freshness promotion open.
2. **Agent attribution for the freshness promotion.** The product attributes a promotion to the MCP session that made it, from `session_id` in the caller identity. It has no field that tells an agent from a scripted client. A scripted MCP session therefore counts as a prior conversation. Confirm with the owner that this counts as an agent promotion for the protocol, and that both arms surface it to the next session in the same way.
3. **Project keys.** No `schema_memory` subject exists for project keys. A key registers itself on its first use in a `create_note` call with `projects`. A project home therefore needs a seed note first, or a home saved with a project key that is not yet registered. The homes validator does not require the key to exist. Agree which order is canonical, and check that the seed note lands in the project home.
4. **Placement of seed notes.** No public door takes a target folder for a compiled note. The `folder` field of a seed note is therefore a check, and the seed fails when the resolver places the note elsewhere.
5. **Edge-review reasons.** The scorer reads reasons from the operation log. Confirm that the log keeps the reason as its own field, apart from a mutation's general rationale, in both arms. Where the field is absent, report it as `unavailable`, never as zero.
6. **Readback without settlement.** The file projector is not a public door. Confirm with the owner that reading the end state from the slot vault after the process exits is the agreed readback.

## 5. Toy example

This example is a format fixture only.
Every value in it is a placeholder.
It describes no situation.

```yaml
id: "x00"
pool: "development"
use: "adapter self-test only"
category: "freshness"
variant: "promoted_entity_type"
client_shape: "hookless"
seed:
  vocabulary:
    - registry: "domains"
      key: "topic-t"
    - registry: "entity-types"
      key: "type-x"
      label: "Type X"
      guidance: "Placeholder guidance."
      parent: null
      seeded_as: "recent_promotion_by_prior_agent_session"
    - registry: "relations"
      key: "relation-r"
      label: "relation-r"
      description: "Placeholder description."
      parent: "relates_to"
  homes:
    - folder: "Notes/Home H"
      topics: ["topic-t"]
      projects: []
  entities:
    - ref: "A"
      title: "Entity A"
      type: "concept"
      body: "Placeholder summary."
  notes:
    - title: "Note N"
      type: "pattern"
      folder: "Notes/Home H"
      topics: ["topic-t"]
      body: "## Decision\n\nPlaceholder decision sentence.\n"
capture_session:
  turns:
    - "Placeholder user turn one."
    - "Placeholder user turn two."
fresh_session:
  question: "Placeholder question?"
```

A real relation entry names a non-generic parent.
The placeholder parent above only exercises the field.

`create_note` refuses a body without a semantic unit section with `missing_semantic_unit`. The toy body already has one (`## Decision` with one sentence); a plain body is wrapped as the note table describes.
A `sources:` field on a seed note is advisory only, and the seed check ignores it.
The key file of this toy situation would be `dkx00.yaml`; the toy has none, because the adapter never reads keys.
