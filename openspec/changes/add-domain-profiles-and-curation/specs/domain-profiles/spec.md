## Purpose

Give the agent the person's durable preferences for the domains a turn touches, without spending context on unrelated domains.

## ADDED Requirements

### Requirement: Profiles are a registered note type in one area

The shipped note-type registry SHALL define a compiled `profile` type whose folder holds every profile and whose partition is the domain registry.
A profile page with no domain SHALL be the global profile. A profile page whose `topics` name one registered domain SHALL be that domain's profile.
A vault MAY nest a narrower profile under a domain folder when its domain registry declares the narrower domain.
The product SHALL hold no list of domains or profiles in code.

#### Scenario: A music profile lives beside the others

- **WHEN** the owner's agent creates a profile page with `topics: [music]`
- **THEN** the typed writer places it in the profile area's music folder

### Requirement: Maps responses serve the profiles a turn touches

Each maps response SHALL serve the global profile and every domain profile whose domain the turn resolves to through existing topic and anchor resolution.
Each profile SHALL fit whole lines under the vault's profile budgets: `budgets.stance_max_chars` for the global profile and `budgets.domain_profile_max_chars` for each domain profile, with `budgets.profiles_max_chars` capping all domain profiles together.
When domain profiles exceed the total budget, the response SHALL keep the profiles of the most strongly resolved domains and report the rest as omitted.
Every line SHALL pass the same admission and terminal egress checks as other served units.

#### Scenario: A gaming question carries the gaming profile

- **WHEN** a turn asks for a game recommendation and resolves to the gaming domain
- **THEN** the response carries the global profile and the gaming profile, and no music profile

#### Scenario: Two domains are active together

- **WHEN** a turn resolves to both music and audio equipment
- **THEN** both domain profiles are served within the total budget

### Requirement: Profiles are session context

A response SHALL send a profile's lines only when the caller's continuity token does not already record that profile's current generation.
Otherwise the profile SHALL appear as `unchanged` with its generation and no lines.

#### Scenario: A later turn in the same domain repeats nothing

- **WHEN** the caller already received the current gaming profile
- **THEN** the next gaming turn marks it `unchanged` and spends no profile budget
