## ADDED Requirements

### Requirement: Folder listings are bounded and resumable

`browse_memory` in `list` mode SHALL return at most `limit` entries. `limit` SHALL default to 200, and values outside 1 to 1,000 SHALL be clamped to that range. Entries SHALL keep the existing order: directories first, then files, each ordered by path without regard to case. When more visible entries follow the page, the response SHALL carry `truncated: true` and a `next_cursor`. Otherwise it SHALL carry `truncated: false` and no cursor. Passing `next_cursor` back as `cursor`, with the same `path` and `recursive`, SHALL return the entries that follow the last returned entry. A cursor that does not decode, or that was issued for another `path` or `recursive` value, SHALL fail with `INVALID_CURSOR`. `limit` and `cursor` apply to `list` mode only: `overview` with either SHALL fail with `INVALID_MODE`.

#### Scenario: A large folder returns one bounded page

- **WHEN** a caller lists a folder of 1,500 visible files without `limit`
- **THEN** the response holds the first 200 entries in the existing order
- **AND** it carries `truncated: true` and a `next_cursor`

#### Scenario: Pages continue without a gap or a duplicate

- **WHEN** a caller follows `next_cursor` until `truncated` is false, on a folder that does not change meanwhile
- **THEN** the concatenated pages equal the folder's complete listing, in order, with each entry once

#### Scenario: A last page that ends exactly at the limit

- **WHEN** a folder holds exactly `limit` visible entries
- **THEN** the response holds them all with `truncated: false` and no cursor

#### Scenario: A cursor from another listing is refused

- **WHEN** a caller passes a cursor issued for another `path`
- **THEN** the call fails with `INVALID_CURSOR` and returns no entries

### Requirement: A page reads only what it returns and discloses no withheld entry

A `list` call SHALL read file bytes and frontmatter only for the entries it returns, and SHALL enumerate names for the rest. A page SHALL hold only entries the caller may see, the owner included: an entry that the response's egress filter would drop, such as a page whose deletion is pending, SHALL NOT take a place on the page. A page SHALL be filled to `limit` from visible entries when enough exist. A cursor SHALL encode only an entry the caller received. A response SHALL NOT report a total count of the folder's entries.

#### Scenario: Withheld entries do not shorten a page or appear in a cursor

- **WHEN** a non-owner caller lists a folder in which some entries are withheld from it
- **THEN** each page holds `limit` visible entries while enough remain
- **AND** no page and no cursor names a withheld entry

#### Scenario: Unreturned files are not read

- **WHEN** a caller lists one page of a folder of 1,500 Markdown files
- **THEN** the leaf reads the bytes of at most `limit` files
