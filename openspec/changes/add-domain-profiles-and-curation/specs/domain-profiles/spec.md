## Purpose

Give the agent the person's durable preferences for the domains that a turn touches, without spending context on unrelated domains.

## ADDED Requirements

### Requirement: Profiles are a registered note type in one area

The shipped note-type registry SHALL define a compiled `profile` type in the folder `Notes/Profiles`, with no partition.
A live profile page whose `topics` resolve to exactly one registered domain SHALL be that domain's profile.
A live profile page with no `topics` SHALL be a global profile.
A governed write SHALL reject a profile page with more than one topic. The error SHALL name the topics and say that each domain takes its own profile page.
A profile page whose topics resolve to no registered domain SHALL supply no lines, and the response SHALL report a validation finding. Such a page SHALL NOT become a global profile.
The product SHALL hold no list of domains or profiles in code.
The product SHALL set no limit on how many profiles a vault holds or how many levels its domain hierarchy has.

#### Scenario: A music profile lives beside the others

- **WHEN** the owner's agent creates a profile page with `topics: [music]`
- **THEN** the typed writer places it in `Notes/Profiles`, and it is the music profile

#### Scenario: A profile cannot cover two domains

- **WHEN** a governed write creates a profile page with `topics: [music, games]`
- **THEN** validation and commit reject it with the same remediation

#### Scenario: An unregistered domain does not widen a profile

- **WHEN** a profile page names a topic that no registered domain resolves
- **THEN** it supplies no lines, not as a domain profile and not as a global profile, and the response reports the finding

### Requirement: A domain may inherit its ancestors' profiles

A domain that declares a parent SHALL inherit the profiles of its ancestors.
When a turn touches a domain, the response SHALL serve that domain's profile first, then its ancestors' profiles, nearest ancestor first.
A domain without a profile page SHALL still pass inheritance on to its descendants.

#### Scenario: A narrow profile carries its parent's through-line

- **WHEN** `games` declares the parent `media`, both have profiles, and a turn touches `games`
- **THEN** the response serves the games profile, then the media profile

### Requirement: A profile page with no topics is a global profile

A global profile page SHALL supply stance lines exactly as a compiled page marked `stance: true`. The same admission, egress, currency, budget and held-generation rules SHALL apply, under the existing `stance` key.
A page marked `stance: true` SHALL keep supplying stance.

#### Scenario: A retyped global profile serves the same lines

- **WHEN** a page marked `stance: true` is retyped as a profile page with no topics, and the mark is removed
- **THEN** the `stance` block serves the same lines

### Requirement: Maps responses serve the profiles that a turn touches

Unless `EXOMEM_ACTIVATION_PROFILES=off`, each maps response SHALL carry `domain_profiles` with `state`, `profiles`, `omitted` and `selection`.
Each profile entry SHALL carry `domain`, `label`, `generation`, `state`, `lines`, `omitted` and `expired`. Each line SHALL be an admitted `{ref, text}` with `review_due` when applicable.

Selection:

- The product SHALL decide which domains a turn touches by retrieval restricted to the caller's admitted profile pages. A page that is not a profile never touches a domain.
- A semantic reading SHALL score each profile page's stored chunks against the turn's vector and take the page's best chunk score. The vector is the one the activation pass already encoded: the turn slice's, or the task-context dense lane's when the slice encodes nothing, as on a small catalogue. The response SHALL NOT encode the turn again.
- The semantic reading SHALL cost in proportion to the number of profile pages, not the size of the vault.
- A lexical reading SHALL run one bounded keyword query over the same pages. It SHALL read the catalogue at the lexical checkpoint the activation request already holds, and SHALL NOT take a new freshness snapshot or walk the vault. A catalogue that is not ready at that checkpoint leaves the reading unrun.
- A page SHALL be selected when its semantic score reaches `budgets.profile_semantic_floor` or its keyword score reaches `budgets.profile_lexical_floor`.
- Selected pages SHALL rank by the sum of their scores, and the response SHALL keep at most `budgets.profile_domains_max` touched domains. Ancestors follow after that cap, so they do not count against it.
- The three limits are vault-owned activation conventions with shipped defaults of 0.50, 8.5 and 2. The floors accept a number in 0..1 and 0..100, and the cap accepts a whole number in 1..8.
- `selection` SHALL be `semantic` when the vector reading ran, `lexical` when it could not run and only the keyword reading did, and `none` when neither reading could run.
- A caller who sees only part of the vault SHALL receive the keyword reading alone, with statistics taken over the profile pages that caller can see, so that no hidden page moves a score.
- It SHALL make no model call.
- It SHALL hold no list of domains or words, and no limit, in code.
- A turn that touches no domain with a profile SHALL receive no domain profile.

Budgets:

- Each profile SHALL fit whole lines within the vault's `budgets.domain_profile_max_chars`.
- All domain profiles together SHALL fit within `budgets.profiles_max_chars`.
- Both are vault-owned activation conventions, with shipped defaults of 1200 and 2400. Each accepts a whole number in 0..8000.
- The block SHALL have its own bound. It SHALL neither reduce the request's `max_chars` nor count in `budget.used_chars`.

Order and omission:

- Profiles SHALL follow the order of the domains the turn touches, most strongly touched first, each followed by its ancestors.
- A profile that does not fit SHALL be listed in `omitted` with its `domain`, `label` and `ref`, so the agent can read it on demand.
- Lines SHALL follow page path and unit order.
- When several profile pages exist for one domain, their lines SHALL be joined in page path order.

Egress and state:

- Every line SHALL pass the same unit and terminal egress checks as stance lines.
- A caller-withheld page or unit SHALL affect no count and no generation.
- State SHALL be `ready` when at least one profile is served or `unchanged`, `empty` when the turn touches no domain with a profile, and `unavailable` when compilation fails or no reading could run while profile pages exist.
- A compilation failure SHALL leave the map, stance, skills and turn context available.

#### Scenario: A gaming question carries the gaming profile

- **WHEN** a turn asks for a game recommendation, and the vault holds games, music and media profiles with `media` as the parent of both
- **THEN** the response carries the games profile, then the media profile, and no music profile

#### Scenario: Two domains are active together

- **WHEN** a turn asks about the music to play while gaming, and both profiles fit the total budget
- **THEN** both domain profiles and their shared ancestor are served once each

#### Scenario: A turn that shares no word with a profile still reaches it by meaning

- **WHEN** a turn names none of the words in the games profile, its vector lies near that profile's page, and the turn vector is available
- **THEN** `selection` is `semantic` and the response serves the games profile and its ancestor

#### Scenario: A small catalogue selects by meaning at the shipped floors

- **WHEN** the catalogue is too small for the turn slice to encode the turn, an encoder is resident, and the turn's vector lies near the games profile's page
- **THEN** `selection` is `semantic`, the games profile is served, and the vault has not lowered any floor

#### Scenario: Without a turn vector the block reports a keyword-only read

- **WHEN** no turn vector exists and the turn shares no word with any profile page
- **THEN** `selection` is `lexical` and no profile is served

#### Scenario: A general-knowledge turn carries no domain profile

- **WHEN** a turn asks a general-knowledge question that touches no profiled domain
- **THEN** `domain_profiles.state` is `empty` and no profile line is served

#### Scenario: Over budget keeps the strongest domains

- **WHEN** three touched domains each have a profile, and the total budget fits only two of them
- **THEN** the response serves the two most strongly touched profiles whole and lists the third profile in `omitted` with its ref

#### Scenario: A profile line cannot expose a hidden reference

- **WHEN** a visible profile line names a page that this caller cannot see
- **THEN** the line receives the existing unit egress decision, and the block exposes no hidden reference or hidden-material count

#### Scenario: Profiles leave the turn's allowance whole

- **WHEN** the same turn is compiled once with profiles on and once with them off
- **THEN** the map, the turn slice, the task candidates and `budget.used_chars` are the same

### Requirement: Profiles are session context

A response SHALL send a profile's lines only when the caller's continuity token does not record that profile's current generation.
Otherwise the profile entry SHALL be `unchanged`, with its generation and no lines, and it SHALL spend no profile budget.
The continuity token SHALL carry the generations of at most sixteen most recently served profiles; an older held profile is sent again when a later turn touches it. A malformed or oversized entry SHALL be ignored, and the token SHALL be decoded without error.

#### Scenario: A later turn in the same domain repeats nothing

- **WHEN** the caller already received the current games profile
- **THEN** the next gaming turn marks it `unchanged` and spends no profile budget

#### Scenario: An edited profile is sent again

- **WHEN** a games profile line changes after the caller received the profile
- **THEN** the next gaming turn sends the games profile `ready` with its new generation

### Requirement: The start-of-turn hook renders the profiles

The hook SHALL render served domain profiles outside its render ceiling, as it renders the stance and skills blocks.
The hook's own cap SHALL equal the largest `profiles_max_chars` that a vault may set.
The hook SHALL keep held profiles through the continuity token and SHALL keep no separate state file for them.

#### Scenario: A real service packet renders whole profiles

- **WHEN** the hook renders the maps response of a served vault with a games profile under a tight render ceiling
- **THEN** the games profile lines appear whole, and the map and turn context keep their own ceiling
