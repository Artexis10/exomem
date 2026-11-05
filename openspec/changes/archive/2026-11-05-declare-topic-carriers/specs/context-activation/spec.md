## MODIFIED Requirements

### Requirement: Topic membership is declared
A topic SHALL be a domain that the vocabulary registry declares. An entry SHALL belong to a topic only through one of these:
- its page's `topics` field, a value or a list, names the domain;
- one of its page's project keys (its `project` and `projects` fields) maps to the domain through that key's hand-written `domain` field or a `project` carrier in the `topic-carriers` overlay registry; a project anchor belongs to its own key's domains;
- a `folder` carrier declares the domain and its folder contains the entry;
- a `tag` carrier declares the domain and matches one of the entry's frontmatter tags;
- a home declares the domain and its folder contains the entry;
- an authored relation from the entry to a page is of a relation kind whose registry family is `membership`, and that page declares the domain through its own topics, project keys, carriers or home.

A declared value SHALL resolve when the domains resolver returns it as a built-in, registered or alias key; a deprecated key SHALL resolve to its replacement. A value that does not resolve SHALL give no membership, and the catalogue SHALL count it as an unresolved declaration. A page's `domain` field SHALL NOT declare membership, because it names a source's subject or an experiment's folder. Membership SHALL run from the entry to the topic and SHALL NOT be transitive: an entry SHALL NOT join a topic that its relation's target reaches only through its own relations. A mention of an anchor SHALL NOT give the mentioning page that anchor's topics. Notes that are not anchors SHALL be catalogue entries in the topics they declare.

A caller denied the `topic-carriers` or `homes` overlay SHALL receive no membership from that overlay. A relation SHALL add membership only from a target admitted to the caller. Membership SHALL use the current registries at read time, so a carrier save changes the next map without a page write or catalogue refold.

#### Scenario: A vault-added domain gets a domain map
- **WHEN** the vault overlay adds a domain and pages declare it
- **THEN** the context catalogue holds a topic and a domain map for it without a code change

#### Scenario: A project key gives its pages a topic
- **WHEN** the project-key registry maps a key to a domain and a note names that key in its `project` field
- **THEN** the note is an entry of that domain's topic

#### Scenario: An unresolved topic is counted, not guessed
- **WHEN** a page edited outside Exomem names a value in its `topics` field that the domains registry does not hold
- **THEN** the page joins no topic through that value and the catalogue counts one unresolved declaration

#### Scenario: An experiment's folder domain is not a topic
- **WHEN** an experiment page carries `domain: ops` and no `topics` field
- **THEN** it joins no topic through its `domain` field

#### Scenario: A mention does not spread membership
- **WHEN** a note mentions an anchor of another topic and has no relation of the membership family to it
- **THEN** the note does not join that topic

#### Scenario: A project carrier gives its pages a topic
- **WHEN** a project carrier declares a topic and a note names its key in `project` or `projects`
- **THEN** the note joins that topic without a change to the project-key file

#### Scenario: A folder carrier gives contained entries a topic
- **WHEN** a folder carrier declares a topic and its folder contains a catalogue entry
- **THEN** the entry joins that topic

#### Scenario: A tag carrier files a note in a type folder
- **WHEN** a note in a type folder has a tag whose carrier declares a topic
- **THEN** the note joins that topic without a page write

#### Scenario: A home gives membership
- **WHEN** a home declares a topic and its folder contains a catalogue entry
- **THEN** the entry joins that topic

#### Scenario: A denied overlay gives no membership
- **WHEN** a caller may not read the carrier or home overlay that declares a topic
- **THEN** that overlay gives no topic membership to the caller's entries

#### Scenario: A membership target declares topics through a carrier
- **WHEN** an admitted membership-relation target declares a topic through a carrier
- **THEN** the source entry joins that topic without following the target's relations
