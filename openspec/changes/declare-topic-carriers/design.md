# Design

## Context

`compile-context-maps-and-profiles` D3 makes topic membership a declared contract. An entry joins a topic through:
- its `topics` field;
- the `domain` field of its project key;
- an authored membership-family relation.

A declared value that the domains resolver does not resolve gives no membership and is counted.

The catalogue holds map entries: notes, anchors, collections and episode threads. Most pages under the append-only and structured trees are not entries, so a carrier matters only for the entries it reaches.

Two writers touch the relevant files today:
- `register_project_key` appends new keys on use;
- `schema_memory` saves the `domains` registry with `expected_hash` and `why`.

No governed writer declares a topic for a project key, a folder or a tag, and nothing records whether a person has reviewed a declaration.

## Decisions

### D1. The agent infers; the server supplies evidence and validates

Inferring a topic is a judgement about meaning, so the active agent makes it. Per carrier, the server gives bounded evidence:
- what the carrier is: the project key with its folder and category, the folder path, or the tag;
- its entry count and its unfiled entry count;
- a deterministic sample of its entries' titles with their one-line catalogue descriptions;
- the tags most frequent among its entries;
- the topics that the targets of its entries' relations already hold;
- the subject domains declared by the sources its entries cite;
- for a tag, whether the tag is also a key in the status, note-type, entity-type or source-kind registry;
- the existing domains, with their descriptions and aliases.

The server validates every save. It calls no model, and no word list or text pattern proposes or refuses a topic. A tag that equals a registry key is flagged in the evidence, and the agent judges whether it names a subject.

### D2. One carrier registry

A carrier is a project key, a folder or a tag whose entries share a subject. Every agent-written declaration lives in one overlay registry, `topic-carriers` (`_Schema/topic-carriers.yaml`, with an empty shipped pack). It uses the generic registry contract: `inspect`, `propose`, `save`, `history` and `restore`, with `expected_hash` and `why`. Its authority is the same as for every vocabulary registry save: the owner writes, and a restricted principal is refused. No new authority action exists.

An entry carries these attributes:
- `kind`: `project`, `folder` or `tag`;
- `match`: the project key, the folder path relative to the knowledge base, or the tag in its written form (`tag_variants.written_form`, compared casefolded, without `#`; a nested tag `a/b` matches only itself);
- `topics`: the canonical domain keys its entries join, stored as the resolver returned them at save time;
- `origin`: a map from each topic to `inferred` or `reviewed`;
- `rejected`: domain keys a person rejected for this carrier.

`kind` and `match` are fixed once the entry exists. `kind`, `inferred` and `reviewed` are closed values that this schema declares. The registry cap is sized for a vault's frequent tags plus its folders and keys.

A project key's hand-written `domain` field in `project-keys.yaml` stays a read input with origin `reviewed`, so D3's existing contract keeps working. Exomem never writes that field. The registry refuses a `project` carrier topic that the key already declares by hand, and a person removes a hand-written value by editing the file.

The validator refuses:
- two entries with the same `kind` and `match`;
- a topic that the domains resolver does not resolve. The agent registers a new domain through the domains registry first;
- a topic that the carrier's `rejected` list holds;
- a `project` carrier for a key the project-key registry does not hold;
- a `folder` carrier whose folder holds no catalogue entries, counted with the catalogue's own entry rule;
- a `folder` carrier whose folder is, or contains, a type folder or a home's folder. The note-type and entity-type registries, shipped plus overlay, declare the type folders. A type folder groups by type, not by subject, and an ancestor folder would put every type into one topic;
- a `folder` carrier inside a type folder that the type partitions by project, because project carriers serve those folders.

A home stays a placement rule: the folder where typed creation writes for a topic or project. Its `topics` resolve through the domains resolver like any declaration. Unresolved home topics are counted, and resolved ones give membership to the entries in the home's folder.

### D3. Membership through carriers

An entry joins the union of these topics:
- the topics its `topics` field resolves to;
- the domains of its project keys, from the hand-written `domain` field and from `project` carriers;
- the topics of every `folder` carrier whose folder contains the entry, and of the home whose folder contains it;
- the topics of every `tag` carrier that matches one of its frontmatter tags;
- the topics that its membership-relation targets declare through any of the routes above.

Membership is not transitive. A mention, a source citation or an episode's `about` ref gives no membership; they stay inference evidence (D1).

The catalogue stores each entry's frontmatter tags in a new `entry_tags` table. The tags join the entry digest, so a tag-only edit moves the catalogue generation. The table is added in place. Old rows keep serving, marked stale, while one background full fold fills the tags. The schema-version wipe is not used, because a wipe leaves every map warming until the fold finishes.

Membership is computed at read time from the stored rows and the current registries, as D11 of the maps change requires, so a carrier save needs no refold. The owner's cached map is keyed on the carrier and homes registry hashes as well as the domains and project registries, so a carrier save changes the next map.

Caller view (maps D7): a caller denied the `topic-carriers` or `homes` overlay gets no membership from it, the same way a caller denied the project registry gets none from project keys. A counterfactual twin proves it.

### D4. Origin and rejection

A declaration without a recorded origin, such as a hand-written project-key `domain`, reads as `reviewed`. Origin never changes membership. The registry's save hash covers the whole overlay, origin included, so a stale save can never downgrade a `reviewed` declaration to `inferred`.

### D5. Review operations

Each review action is an ordinary carrier save:
- **Accept** sets a topic's origin to `reviewed`.
- **Reject** removes the topic and adds it to `rejected`.
- **Replace** changes one carrier's topic: the old topic goes to `rejected`, and the new one is saved with origin `reviewed`. This is what "change a row" means in review.

A global rename of a domain is a different operation. It retires one domain in favour of another for every carrier, page and capture. It stays the domains registry's own operation: deprecate with a replacement, or alias an unregistered spelling. The review offers it only as an explicit, previewed step.

### D6. One family, automatic, then reviewable

`topic-carrier/v1` is a vocabulary family under the family contract of `activate-agent-led-vocabulary-evolution`:
- **Evidence:** D1.
- **Validator:** the `topic-carriers` registry validator.
- **Persistence owner:** the `topic-carriers` registry.
- **Decisions:** `defer` only. A declaration is a registry save, not a workflow decision. Its item disappears when the carrier no longer files unfiled entries.
- **Authority:** D2.

The family needs the workflow's choice contract, review-family mapping, freshness check and bootstrap family listing.

The producer runs for the owner only. Its evidence aggregates the whole vault, and only the owner may save. It offers carriers whose declaration would file unfiled entries. A minimum entry count keeps a one-page folder or tag out; it is a size, not vocabulary.

Each item's fingerprint covers:
- the carrier identity;
- the registry hashes;
- a bucketed unfiled count.

The deterministic sample is evidence only, because any sample over a growing set changes when a page lands. So a single new page does not re-offer deferred advice, and a material change does.

The consideration reaches ordinary work in two places:
- **After a committed write.** The producer first asks whether a notice could be taken at all: the family disposition and the reservation state. It then reads the written page's own folder chain, tags and project keys, skips carriers already declared, and reads each remaining carrier's count from a briefly cached recount. It offers at most one item in the single advisory slot, and only when the tag-variant advisory left the slot free. A whole-vault ranking never runs on the write path.
- **In explicit vocabulary review.** `review_memory(mode="vocabulary")` and `review_item_context` rank carriers across the vault by the unfiled entries they would file, and the review continuation pages through them. A parent folder whose unfiled entries equal those of one offered folder below it is not offered. `inspect` stays a pure registry read: it lists declarations and the hand-written project-key topics, and it ranks and records nothing.

The agent may declare with origin `inferred` without a per-carrier approval, and the map uses the declaration at once. Inferred declarations stay listed for review until a person accepts, replaces or rejects them.

### D7. Onboarding

Bootstrap reads no vault content. So:
- adoption's next actions name the carrier review when entries are unfiled;
- bootstrap advertises the route next to the vocabulary workflow;
- the skill tells the agent to run inference and then show the person one list.

The list has the carrier, its topic, the origin and a few entry titles. The person can accept all, change single rows, or skip. Skipping keeps the declarations `inferred` and in effect.

### D8. Coverage

The owner coverage counts:
- unfiled entries;
- carriers that would file them;
- unresolved topics on carriers, homes and project keys;
- carriers that no longer match anything, such as a moved folder.

The carrier counts partition the declared carriers: filing, matching but with no resolved topic, dangling, and tag carriers still unknown before the tag fill.

## Risks

- **Wrong inference.** A wrong topic groups entries under the wrong topic. The cost is a worse map, not lost data. Review, replace and reject are always available, and rejections are remembered.
- **Noisy tags.** Vaults hold thousands of distinct tags. Only tags that clear the minimum entry count reach the consideration. Tags that equal a registry key are flagged, and the agent may decline any tag.
- **Write contention.** Saves use `expected_hash`, so a stale save is refused, not merged.
- **Tag fill after upgrade.** Until the background fold fills `entry_tags`, tag carriers file nothing and the map marks itself stale. Nothing is wiped.
- **Debt this change meets but does not fix:**
  - the `Entities` root literal repeated across entity modules;
  - family-identifier branches in the vocabulary workflow;
  - the hard-coded family list in bootstrap;
  - a conventions-hash change that folds the whole vault on the request thread.

## Proof plan

- **Highest-level check:** start from a fixture vault with an undeclared project key, a topic folder and a tagged note in a type folder. The agent workflow saves inferred carriers through `schema_memory`. The next maps packet then shows those topics without a page write, and `read_memory` on each topic's domain map returns its members.
- **Tests:**
  - a stale `expected_hash` is refused;
  - a topic that does not resolve is refused;
  - a rejected topic is refused;
  - a hand-written project-key `domain` reads as `reviewed`;
  - a carrier on a type folder, on an ancestor of one, or on a folder without entries is refused;
  - a `project`, a `folder` and a `tag` carrier and a home each give membership;
  - a carrier save changes the owner's cached map;
  - a caller denied the carriers overlay gets no membership from it;
  - a tag-only edit moves the catalogue generation;
  - an upgrade fills tags without wiping rows;
  - a consideration is suppressed when unchanged and after a defer, and returns when its bucketed evidence changes;
  - the write-path producer reads only the written page's carriers.
- **Real-model check:** on dogfood vaults, an agent given only the considerations declares carriers that a blind judge rates as fitting their entries. Include hostile cases:
  - a key with one page;
  - a folder that mixes two topics;
  - a tag that is a status, not a subject;
  - a carrier in another language.
