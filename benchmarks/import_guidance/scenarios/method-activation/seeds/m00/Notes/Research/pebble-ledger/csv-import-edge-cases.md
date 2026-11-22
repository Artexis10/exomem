---
exomem_id: 07bc45f7-7ef1-54bf-a577-141134dacad2
type: research-note
project: pebble-ledger
status: active
created: 2026-04-02
updated: 2026-09-11
sources: []
tags: [software, import, csv]
---

# CSV import edge cases

## Question

Which quirks in bank CSV exports break the Pebble Ledger importer, and how should each be handled?

## Findings

- One bank writes dates as day/month/year and another as year-month-day. The importer needs a per-account date format, not a guess from the data.
- Amounts sometimes carry a trailing minus sign or sit in separate debit and credit columns.
- A few exports start with two or three lines of preamble before the header row.
- Merchant names arrive in capitals with trailing reference numbers; stripping the numbers makes the category rules far more reliable.
- Duplicate rows across overlapping exports are common. A hash of date, amount and cleaned merchant text catches nearly all of them.

## Open threads

- Import speed on the full history is about forty seconds on the old laptop. Nobody has profiled where it goes.

## Relations

- about_entity [[Entities/Concepts/Pebble Ledger]]
