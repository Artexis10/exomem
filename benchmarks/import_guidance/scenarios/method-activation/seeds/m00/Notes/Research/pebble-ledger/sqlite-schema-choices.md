---
exomem_id: 7c8afc1a-b433-573d-8cd6-d89ea8ffb809
type: research-note
project: pebble-ledger
status: active
created: 2026-03-08
updated: 2026-08-19
sources: []
tags: [software, sqlite, schema]
---

# Pebble Ledger schema choices

## Question

How should Pebble Ledger lay out its tables so that five years of bank statements stay quick to query and easy to migrate?

## Findings

- One `transactions` table with an integer amount in minor units avoids rounding surprises that floats produced in the first prototype.
- Categories live in their own table and the transaction row holds only the category id. Renaming a category then touches one row.
- A composite index on `(account_id, booked_on)` serves almost every report the tool prints. A second index on the merchant text was never used and was dropped.
- The raw import row is kept in a `source_rows` table so a bad parse can be repaired without asking the bank for the file again.

## Open threads

- Whether tags deserve a join table or a simple text column.
- How to version the schema without a heavyweight migration library.

## Relations

- about_entity [[Entities/Concepts/Pebble Ledger]]
- relates_to [[Entities/Decisions/Keep Pebble Ledger data in one SQLite file]]
