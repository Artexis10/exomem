## ADDED Requirements

### Requirement: A Custody-Only Audit Reads Only Custody-Marked Sources

An audit that selects only `custody_source` SHALL choose the pages it checks from page frontmatter before it authorizes or parses any page. It SHALL authorize and parse only pages whose frontmatter records `custody_only: true` and pages whose frontmatter it cannot decide. It SHALL read a page that such a Source names in `ingested_into` or `supersedes` only when the check needs it, through the same admission as the corpus parse. Its findings SHALL equal the findings of the same check over the full corpus. Any other category selection SHALL keep its existing corpus read.

#### Scenario: A large vault with few custody Sources

- **WHEN** a vault holds many pages and a few Sources that record `custody_only: true`
- **AND** an audit selects only `custody_source` under the served request allowance
- **THEN** the audit reports complete `custody_source` coverage
- **AND** it authorizes and parses no page outside the custody-marked Sources and the pages they name

#### Scenario: A compiled custody Source stays off the list

- **WHEN** a custody-marked Source names in `ingested_into` a live page that cites it
- **THEN** the custody-only audit reports no finding for that Source, as the full audit does

#### Scenario: A named page the caller may not see

- **WHEN** a custody-marked Source names a derivative that the caller may not read
- **THEN** the custody-only audit reports that derivative as missing, as the full audit does
- **AND** the response names nothing from the hidden page

#### Scenario: A mixed selection keeps the full read

- **WHEN** an audit selects `custody_source` together with another category
- **THEN** it reads the corpus as it did before this change
