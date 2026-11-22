## Purpose

Let the owner see whether a method that activation served was then used, from evidence the service already keeps.

## ADDED Requirements

### Requirement: The owner can see whether a served method was used

The owner served-context command SHALL report, for each method that retained served-context rows served, how often it was served and what use evidence followed each serving.
Use evidence SHALL be computed when the owner reads it, from the newest `MAX_JOINED_ROWS` (2000) retained rows that served a method and the existing local read and write logs; the report SHALL say when that cap applied. Activation SHALL do no extra work for it.
A committed Records item write SHALL be logged in the write log under its collection's manifest path, as a page write is logged under its page.
For one serving, evidence SHALL be:

- `opened` when the read log holds a read of the method page, or for a skill a file in the skill's folder, after the serving and within `METHOD_USE_WINDOW_MINUTES` (120);
- `followed` when the write log holds a successful write, in the same window, to a page that the method page links to, to an item of a collection whose manifest it links to, or, for a method that is itself a collection manifest, to that collection;
- `unknown` when the log is disabled, absent or rotated past the window start, or when the window has not closed yet and holds no matching event.

`opened` and `followed` SHALL be reported separately, each as observed, not observed or unknown. A method that links to no page or collection SHALL report `followed` as unknown, never as not observed.
The report SHALL say that the logs belong to one host and service and are not tied to one conversation, so concurrent work by the same principal can produce the evidence.

#### Scenario: A served skill followed by a write to its destination

- **WHEN** a skill that links to a collection is served and a write to that collection succeeds 10 minutes later
- **THEN** the report counts one serving of the skill with `followed: observed`

#### Scenario: A served method with no later read or write

- **WHEN** a method that links to a page is served and the logs hold no later read of it and no write to the linked page within the window
- **THEN** the report counts one serving with `opened: not_observed` and `followed: not_observed`

#### Scenario: Disabled logging reads as unknown

- **WHEN** the read and write logs are disabled for the service
- **THEN** every serving reports `opened: unknown` and `followed: unknown`, never `not_observed`
