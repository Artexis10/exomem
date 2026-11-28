# vault-skills Specification

## Purpose

Vault skills are procedures that the owner keeps as pages in their own vault. Exomem offers them to the agent in the skills catalogue and as method items. This capability covers what a vault skill may carry, starting with pick-by-problem routing tables.

## Requirements

### Requirement: Routing tables travel as vault skills

A pick-by-problem routing table SHALL be carried as an ordinary vault skill, with no separate carrier and no table parsing.
The skills catalogue SHALL list a routing skill, and skill method items SHALL serve it, exactly as for any other vault skill.
The shipped scaffold SHALL teach the routing-skill convention:

- the skill's description names the problems that the table covers;
- the skill's body holds the pick-by-problem table, one row per problem, each row naming the sources or sections to read first;
- the skill's body tells the agent to name the matched row and point to that row's sources first, and to answer without the table when no row fits.

#### Scenario: A row-shaped turn reaches the routing skill

- **WHEN** a vault holds a routing skill whose table has a row for slow builds, and a turn asks why the build has become slow
- **THEN** the skills catalogue lists the routing skill, and the agent can load it by its reference and answer from the matching row

#### Scenario: An unrelated turn does not use the table

- **WHEN** a vault holds the same routing skill, and a turn asks for a dinner recipe
- **THEN** the skill's own text tells an agent that has it loaded to answer without the table

#### Scenario: The scaffold teaches the convention

- **WHEN** a person reads the shipped skill guidance on vault skills
- **THEN** it describes how to write a routing skill: the description, the table and the instruction to name the matched row
