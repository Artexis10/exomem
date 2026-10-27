## ADDED Requirements

### Requirement: Topics are declared on carriers

A topic declaration SHALL be carried by a project key, a folder or a tag. Every declaration that Exomem writes SHALL live in the `topic-carriers` overlay registry, with a `kind` of `project`, `folder` or `tag`, a `match`, `topics`, an `origin` per topic and a `rejected` list. A project key's hand-written `domain` field in `project-keys.yaml` SHALL stay a read input, and Exomem SHALL NOT write it. The registry SHALL refuse two carriers with the same kind and match, a topic that the domains resolver does not resolve, a topic in the carrier's rejected list, a `project` carrier for an unknown key, and a `project` carrier topic that the key already declares by hand. It SHALL refuse a folder that holds no catalogue entries, a folder that is or contains a note-type or entity-type folder as the effective type registries declare it, a folder that is or contains a home's folder, and a sub-folder of a type folder partitioned by project. A tag SHALL match in its written form, compared casefolded, without `#`, and a nested tag SHALL match only itself.

#### Scenario: A type folder is refused

- **WHEN** an agent saves a folder carrier whose folder is a note type's folder
- **THEN** the save is refused and names the type that owns the folder

#### Scenario: An ancestor of type folders is refused

- **WHEN** an agent saves a folder carrier for the folder that holds the note-type folders
- **THEN** the save is refused because the folder contains type folders

#### Scenario: A folder without entries is refused

- **WHEN** an agent saves a folder carrier for a folder that holds no catalogue entries
- **THEN** the save is refused

### Requirement: Entries join the topics of their carriers

An entry SHALL join the topics of its `topics` field, of its project keys' domains from the hand-written field and from `project` carriers, of every folder carrier whose folder contains it, of the home whose folder contains it, of every tag carrier that matches one of its frontmatter tags, and the topics that its membership-relation targets declare through those routes. Membership SHALL NOT be transitive. A mention, a source citation or an episode's `about` ref SHALL NOT give membership. Membership SHALL be computed at read time, so a carrier save changes the next map, including the owner's cached map, without a page write or a catalogue refold. A home's topics SHALL resolve through the domains resolver, and an unresolved home topic SHALL be counted. A caller denied the `topic-carriers` or `homes` overlay SHALL get no membership from it. A tag-only edit to a page SHALL change the catalogue generation.

#### Scenario: A tag carrier files a note in a type folder

- **WHEN** a note in a note-type folder has no project key and carries a tag whose carrier declares a topic
- **THEN** the note is a member of that topic on the next map

#### Scenario: A home gives membership

- **WHEN** a home declares a topic and a note sits in the home's folder
- **THEN** the note is a member of that topic

#### Scenario: A carrier save changes the cached map

- **WHEN** the owner's map is cached and the agent saves a carrier topic
- **THEN** the next map shows the carrier's entries under that topic without any page write

#### Scenario: A denied overlay gives no membership

- **WHEN** a caller may not read the `topic-carriers` overlay
- **THEN** that caller's map files no entry through a carrier

#### Scenario: A citation gives no membership

- **WHEN** a note cites a source page whose subject domain is a topic
- **THEN** the note does not join that topic through the citation

### Requirement: Declarations are written through governed saves

`schema_memory` SHALL inspect, propose, save, list the history of and restore carriers under subject `topic-carriers`, through the generic registry contract. A save SHALL require the current hash and an audit reason, and SHALL refuse a stale hash before any write. The hash SHALL cover origins, so a stale save cannot downgrade a reviewed declaration. Each declared topic SHALL carry an origin, `inferred` or `reviewed`. A declaration without a recorded origin SHALL read as `reviewed`. A save SHALL be refused for a principal that may not save a vocabulary registry.

#### Scenario: A stale save is refused

- **WHEN** an agent saves with a hash older than the current one
- **THEN** the save is refused and no file changes

#### Scenario: A hand-written declaration counts as reviewed

- **WHEN** a person wrote `domain: ops` for a project key by hand
- **THEN** `inspect` reports that declaration with origin `reviewed`

#### Scenario: Exomem never edits the project-key file

- **WHEN** the agent declares a topic for a project key
- **THEN** the declaration is a `project` carrier and `project-keys.yaml` is unchanged

### Requirement: The agent infers carrier topics and the server only supplies evidence

Ordinary work SHALL expose bounded considerations in the `topic-carrier/v1` vocabulary family, to the owner only, for carriers whose declaration would file unfiled entries. After a committed write, the producer SHALL consider only the written page's own carriers and SHALL offer at most one item. Explicit review SHALL rank carriers across the vault by the number of unfiled entries they would file. A carrier below a minimum entry count SHALL NOT be offered. A consideration SHALL carry the carrier, its entry and unfiled counts, a deterministic bounded sample of entry titles with their catalogue descriptions, the tags most frequent among its entries, the topics already held by its entries' relation targets, the subject domains of the sources its entries cite, whether a tag is also a key of another registry, and the existing domains with their descriptions and aliases. The active agent SHALL choose the topic. The server SHALL NOT call a model and SHALL NOT propose or refuse a topic from word lists or text patterns. The family's only workflow decision SHALL be `defer`. An unchanged or deferred consideration SHALL NOT be repeated, and a material change to its bucketed evidence SHALL allow it again.

#### Scenario: An undeclared carrier is considered once

- **WHEN** a carrier would file unfiled entries and its evidence does not change
- **THEN** the consideration appears once and is not repeated in later turns

#### Scenario: The agent declares an inferred topic

- **WHEN** the agent reads the consideration and saves a topic for the carrier
- **THEN** the declaration carries origin `inferred` and the carrier's entries join that topic at once

### Requirement: A person can accept, replace or reject a declaration

`inspect` SHALL list every declaration with its origin. A person's accept SHALL set origin `reviewed`. A reject SHALL remove the topic and record it in the carrier's rejected list. A replace SHALL change one carrier's topic: the old topic SHALL be recorded as rejected and the new topic saved with origin `reviewed`. A replace SHALL NOT change any other carrier, page or domain. The agent SHALL NOT be offered a rejected topic for that carrier again.

#### Scenario: A rejected domain is not offered again

- **WHEN** a person rejects an inferred domain for a carrier
- **THEN** the declaration is removed and a save that declares it again for that carrier is refused

#### Scenario: A replace changes one row only

- **WHEN** a person replaces the inferred topic of one carrier
- **THEN** that carrier's entries join the new topic, and every other carrier that declares the old topic is unchanged

### Requirement: Onboarding shows inferred declarations for review

Vault adoption SHALL name the carrier review in its next actions when entries are unfiled, and bootstrap SHALL advertise the route. The review SHALL show the person one list with each carrier, its topic, the declaration's origin and a few entry titles. The person SHALL be able to accept all, change single rows, or skip. Skipping SHALL leave the declarations `inferred` and in effect.

#### Scenario: A skipped review keeps the map working

- **WHEN** the person skips review during onboarding
- **THEN** the inferred declarations stay in effect and remain listed for later review
