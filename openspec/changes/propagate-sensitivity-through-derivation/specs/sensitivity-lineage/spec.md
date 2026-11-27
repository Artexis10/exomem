## ADDED Requirements

### Requirement: Effective classes combine own, floor and inherited classes, minus a mask

A page's effective classes SHALL be its own `sensitivity` classes together with its `inherited_sensitivity` floor and its inherited classes, minus the classes in its `sensitivity_mask`. A mask SHALL NOT remove an own class. Its inherited classes SHALL be the union of the effective classes of the pages it derives from. Every governance decision that reads a class SHALL read the effective classes.

#### Scenario: A note cites a classified Source

- **WHEN** a Source carries `sensitivity: [health]` and a note names it in `sources`
- **THEN** the note's effective classes include `health`

### Requirement: A row's effective classes include its sources

A row's effective classes SHALL be the union of its own classes, its collection floor and the effective classes of the pages its current row version names as sources.

#### Scenario: A row names a classified Source

- **WHEN** a row's current version names a Source with own class `financial`
- **THEN** the row's effective classes include `financial`

### Requirement: An authored write carries the classes of what it drew on

Every authored write SHALL call one pre-plan step that adds to `inherited_sensitivity` the effective classes of every derivation ref the planned page holds after the write, a row's source refs, an episode's `about` refs and the page it edits. A derivation ref is a `sources` value, a frontmatter field mapped to an `inherits_sensitivity` relation, or a typed link with such a relation, however it arrived. The plan function SHALL refuse an authored write whose floor lacks a class that the step computes.

#### Scenario: An observation links a classified page

- **WHEN** an agent adds an observation to an entity page with a `derived_from` link to a page of class `identity`
- **THEN** the entity page's `inherited_sensitivity` includes `identity`

#### Scenario: A surface skips the step

- **WHEN** an authored write reaches the plan function with a floor that lacks a class of a page it names in `sources`
- **THEN** the plan function refuses the write and names the missing class

### Requirement: Carry-over never reveals an unreadable page

The carry-over step SHALL resolve each ref under the writer's visibility and SHALL treat a ref the writer cannot read exactly as a ref to no page.

#### Scenario: A source the writer cannot read

- **WHEN** a restricted writer names in `sources` a page withheld from it
- **THEN** the write keeps the ref unresolved with the warning it gives for a page that does not exist, and takes and reports no class from it

### Requirement: Record writes accept sources

`add_record` and `edit_record` SHALL accept an optional `sources` list, stored as the row's source refs.

#### Scenario: A row names its source

- **WHEN** an agent adds a row with `sources` naming a page
- **THEN** the row's current version names that page as a source

### Requirement: Mechanical rewrites and classify keep the floor

Classify and mechanical rewrites, such as link repair after a rename, folder merges and relocations, SHALL NOT run the carry-over step and SHALL keep the floor they find. No edit SHALL remove a class from `inherited_sensitivity`; only classify SHALL clear it, with a reason record that allows a revert.

#### Scenario: A rename rewrites links

- **WHEN** a page is renamed and the store rewrites the links on 300 pages that cite it
- **THEN** none of those pages' floors change

### Requirement: The owner masks an inherited class on one page

Classify SHALL set or clear `sensitivity_mask` on a page, with a required reason and a revert. Setting a mask and clearing a floor SHALL require the owner principal. A mask SHALL name only a class the page holds now by floor or inheritance. A masked class SHALL leave the page's effective classes and SHALL NOT reach its dependents through it.

#### Scenario: Masking a class on a note

- **WHEN** a note inherits `health` from a cited Source and the owner masks `health` on the note
- **THEN** the note's effective classes exclude `health`, and a page that cites only the note does not inherit `health`

#### Scenario: A writer masks a class in advance

- **WHEN** a non-owner writer, or the owner before the class arrives, asks to mask `health` on a note that does not hold `health`
- **THEN** classify refuses and the note's bytes are unchanged

### Requirement: Inheritance follows derivation edges and stays current

The vault store SHALL hold a derived `page_sensitivity` object with each page's effective and inherited classes and a generation. Inheritance SHALL follow the graph edges whose relation carries `inherits_sensitivity`, transitively. A derivation that changes a page's own classes, floor, mask or derivation edges SHALL mark its dependents pending in the same transaction. The generation SHALL increase whenever the effective classes change.

#### Scenario: A Source is classified after a note cites it

- **WHEN** a note cites a Source, and later the owner classifies the Source as `identity`
- **THEN** after derivation the note's effective classes include `identity`, and the note's bytes are unchanged

#### Scenario: A cycle of derivation edges

- **WHEN** two pages cite each other and one gains a class
- **THEN** both carry the class and the recompute terminates

### Requirement: Governance caches key on the inheritance revision

Every cache entry for a scope set or a governance decision SHALL key on its page's generation and its scope parents' generations as well as its existing inputs. A change to one page's classes SHALL NOT invalidate entries for pages whose generations did not move. Projected retrieval under a policy with a `sensitivity` scope SHALL check that scope against the current effective classes, not against scope ids stored at publish.

#### Scenario: A cached decision after a raise

- **WHEN** a restricted caller's decision for a note is cached and the note's Source then gains a class
- **THEN** the next read decides the note under the new effective classes

### Requirement: Removals flow down only to pages written before the class

Clearing a class on a page SHALL remove it from dependents that hold it only by inheritance. A dependent whose floor holds the class SHALL keep it.

#### Scenario: A class is cleared on a Source

- **WHEN** note A cited a Source before the Source was classified `health`, note B's authored write cited it after, and the owner then clears `health` on the Source
- **THEN** note A no longer carries `health` and note B still does

### Requirement: A class needs a page the graph indexes

Capture, classify and a plain edit SHALL refuse a class on a page the graph does not index, with `SENSITIVITY_PAGE_UNTRACKED` and a reason that names the rule.

#### Scenario: Classifying a page in an excluded folder

- **WHEN** the owner classifies a Markdown page in a scan-excluded folder
- **THEN** the classify refuses with `SENSITIVITY_PAGE_UNTRACKED` and the page's bytes are unchanged

### Requirement: Uncurrent inheritance answers as missing

When the active policy has a `sensitivity` scope and the caller is not the owner, a page SHALL be released only when its effective classes are current at the read's pinned snapshot. The pending set SHALL hold pages with an underived intent, pages marked for inheritance work and the dependents of both. A page in it SHALL answer as a page that does not exist, in response shape and in timing, and projected retrieval SHALL remove it before ranking.

#### Scenario: A page was classified a moment ago

- **WHEN** the owner classifies a note `health` and a restricted caller reads it before the classify's intent derives
- **THEN** the note answers as missing

#### Scenario: A raise is still deriving

- **WHEN** a hub Source gains `health` and a restricted caller searches while some dependents are still pending
- **THEN** the pending dependents answer as missing, and the answer cannot be told apart from one where those pages do not exist

### Requirement: Owner reads do not pay for inheritance

Owner reads SHALL NOT wait on inheritance work or pay the currency check.

#### Scenario: The owner reads during a hub raise

- **WHEN** a hub Source gains a class and its dependents are still pending
- **THEN** the owner's reads of those dependents return them without waiting on the recompute

### Requirement: A classified read asks the agent to name its sources

A read result that releases a page with effective classes SHALL tell the agent, in one line that names no class, to name the page in `sources` if a write uses it.

#### Scenario: Reading a health page

- **WHEN** an agent reads a page with effective class `health`
- **THEN** the result asks it to name the page in `sources` when a write uses it
