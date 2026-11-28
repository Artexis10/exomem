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

Every authored write SHALL call one pre-plan step that adds to `inherited_sensitivity` the effective classes of every derivation ref the planned page holds after the write, a row's source refs, an episode's `about` refs and the page it edits. The plan function SHALL refuse an authored write whose floor lacks a class that the step computes.

#### Scenario: An observation links a classified page

- **WHEN** an agent adds an observation to an entity page with a `derived_from` link to a page of class `identity`
- **THEN** the entity page's `inherited_sensitivity` includes `identity`

#### Scenario: A surface skips the step

- **WHEN** an authored write reaches the plan function with a floor that lacks a class of a page it names in `sources`
- **THEN** the plan function refuses the write and names the missing class

### Requirement: Derivation refs are read from the planned page

A derivation ref SHALL be a `sources` value, a frontmatter field mapped to an `inherits_sensitivity` relation, or a typed link with such a relation, whether it arrived through `sources`, `links`, `set_fields` or body text.

#### Scenario: Sources set through a field edit

- **WHEN** an agent sets a note's `sources` field through `edit_note` field edits to name a `health` page
- **THEN** the note's floor includes `health`

### Requirement: Carry-over never reveals an unreadable page

The carry-over step SHALL resolve each ref under the writer's visibility and SHALL treat a ref the writer cannot read exactly as a ref to no page.

#### Scenario: A compiled source citation the writer cannot read

- **WHEN** a restricted writer adds a withheld source to a compiled note's `sources`
- **THEN** the existing compiled source closure refuses the write as it does for a missing source, and exposes no class

#### Scenario: A permissive link to a withheld page

- **WHEN** a restricted writer adds a typed body link to a withheld page, and the global graph later resolves the link
- **THEN** the write and the later restricted view behave as if the target were missing, because the text has no authorized lineage binding

### Requirement: Only authorized derivation bindings carry sensitivity classes

The store SHALL inherit classes only from a ref resolved under writer authority and bound to the written page or row version. The writer SHALL recheck the binding under writer authority before publication. Mechanical rewrites SHALL preserve it. Graph resolution alone SHALL NOT establish a binding. A legacy derivation claim without binding evidence SHALL remain unresolved for class-policy reads until the owner verifies it, regardless of target existence.

#### Scenario: A legacy claim has no binding evidence

- **WHEN** a class-restricted read reaches a legacy note with a derivation claim that has no binding evidence
- **THEN** the note answers as missing whether its claimed target exists or is absent

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

Classify SHALL set or clear `sensitivity_mask` on a page, with a required reason and a revert. Setting a mask and clearing a floor SHALL require the owner principal. A mask SHALL name only a class the page holds now by floor or inheritance, and SHALL apply to those contributions until the owner clears it. Dependents SHALL inherit the page's resulting effective classes.

#### Scenario: Masking a class on a note

- **WHEN** a note inherits `health` from a cited Source and the owner masks `health` on the note
- **THEN** the note's effective classes exclude `health`, and a page that cites only the note does not inherit `health`

#### Scenario: A writer masks a class in advance

- **WHEN** a non-owner writer, or the owner before the class arrives, asks to mask `health` on a note that does not hold `health`
- **THEN** classify refuses and the note's bytes are unchanged

#### Scenario: An inherited class is also an own class

- **WHEN** the owner masks a class the note holds both as its own class and by inheritance
- **THEN** the note and its dependents still receive the class through the note's own contribution

### Requirement: Inheritance follows derivation edges and stays current

The vault store SHALL hold a derived `page_sensitivity` object with each page's effective and inherited classes and a generation. Inheritance SHALL follow authorized graph bindings whose relation carries `inherits_sensitivity`, transitively. A commit SHALL mark its affected lineage pending before readers can see it. The generation SHALL increase whenever the effective classes change.

#### Scenario: A Source is classified after a note cites it

- **WHEN** a note cites a Source, and later the owner classifies the Source as `identity`
- **THEN** after derivation the note's effective classes include `identity`, and the note's bytes are unchanged

#### Scenario: A cycle of derivation edges

- **WHEN** two pages cite each other and one gains a class
- **THEN** both carry the class and the recompute terminates

### Requirement: Governance caches key on the inheritance revision

Every cache entry for a scope set or governance decision SHALL key on its page's generation and its scope parents' generations as well as its existing inputs. The store SHALL read generations in batches. A change to one page's classes SHALL NOT invalidate entries for pages whose generations did not move.

#### Scenario: A cached decision after a raise

- **WHEN** a restricted caller's decision for a note is cached and the note's Source then gains a class
- **THEN** the next read decides the note under the new effective classes

### Requirement: Projected retrieval uses the current release decision

Under a policy with a `sensitivity` scope, projected retrieval SHALL check the scope against current effective classes. It SHALL exclude an item before ranking or reduction when the exact authorized representation is unavailable, while continuing to decide the remaining items.

#### Scenario: A current class requires a variant that has not published

- **WHEN** the current classes require a lower-level variant that the catalog does not yet hold
- **THEN** the item contributes nothing to matches, counts or ranks, and other authorized items can still be returned

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

Class-policy reads by non-owners SHALL release a page only with proof of current effective classes at the pinned snapshot. Pending pages SHALL answer as missing in shape and timing and be excluded before ranking. Readers SHALL reuse the snapshot proof without waiting for derivation or walking the graph again.

#### Scenario: A page was classified a moment ago

- **WHEN** the owner classifies a note `health` and a restricted caller reads it before the classify's intent derives
- **THEN** the note answers as missing

#### Scenario: A raise is still deriving

- **WHEN** a hub Source gains `health` and a restricted caller searches while some dependents are still pending
- **THEN** the pending dependents answer as missing, and the answer cannot be told apart from one where those pages do not exist

### Requirement: A commit marks affected lineage pending

The store SHALL maintain pending pages and their dependents, including changed binding resolutions found through indexed keys before and after the commit. Newly resolved authored placeholders without bindings SHALL NOT seed pending work.

#### Scenario: A commit changes citation resolution before derivation

- **WHEN** a committed title or path changes the resolution of an authorized binding before its graph rows refresh
- **THEN** the citer and its dependents answer as missing until their current lineage is proven

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
