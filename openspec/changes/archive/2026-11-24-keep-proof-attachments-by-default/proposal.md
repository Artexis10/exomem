# Proposal

## Why

A user shares a photo of a serial label, a receipt or a warranty card so that an agent can act on it. The agent reads it, writes a text observation and says "Saved", but it does not keep the file. The user has to check, ask again, and sometimes find out weeks later that the photo was never stored. Incidents on 2026-09-26, 2026-10-08, 2026-10-11, 2026-10-25, 2026-10-28 and 2026-11-24 show the same pattern. Each time, the storage tools worked when they were called. The miss is in the path:

- A client without file handles needs three steps: `upload_files`, a shell `curl` send, then `save_evidence` with the held handle. A client with no shell has no path at all. A held file that nobody saves expires after an hour.
- `save_evidence` refuses a caption on files, so what the file shows stays in the chat.
- The bootstrap profile and the learning instruction say to keep handed files, but they do not say that proof belongs in Evidence in the same turn.
- No write path removes a card number or a password from text that a capture keeps. Evidence is append-only, so such a value would stay.

## What Changes

- **A caption on file evidence.** `save_evidence` takes `description` with `files`. One caption applies to every file in the call, and it is written on each newly stored original's page. Each kept file's result carries `cite`, a wikilink that text about the file includes to cite it.
- **One call from attachment to Evidence.** `upload_files(lane="evidence", scope, category, description)` returns a ticket that saves on arrival. The send stores the file in Evidence and returns its receipt, and no second call follows. Such a ticket may omit `expected_sha256`. On a personal server the reply also gives a `page` link: a person opens it on the device that holds the file and picks the file. The ticket travels in the URL fragment, which no request carries.
- **Kept text never holds a card number or a labelled credential.** Every capture that keeps caller text removes a payment card number (scheme prefix, length and Luhn check) and the labelled credential shapes that the egress scrubber names. The kept text shows where the value was, and the result's `warnings` say so.
- **Agents are told to keep proof in the turn it arrives.** The bootstrap profile's `binary_upload` entry, the learning instruction, the `save_evidence` description and the skill scaffold say: keep a proof file the user shares as Evidence in that turn, cite it, and never keep a card number, code or password. The server instructions are unchanged, because their 900-character bound is fixed by `conversation-aware-activation`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `client-artifact-preservation`: a caption on file evidence, a citation on each kept file, evidence tickets that save on arrival, the send page, and write-time redaction.
- `agent-memory-onboarding`: the learning instruction asks for proof in Evidence in the same turn and for covered copies of secrets.

## Impact

- Public tools: `save_evidence` accepts `description` with `files`. `upload_files` gains `scope`, `category` and `description`. Results gain `cite` and `caption`. The tool-surface fingerprint changes, so the ChatGPT connector contract records a pending digest.
- HTTP: `GET /upload/send` on personal servers. The ticket send route saves on arrival for a ticket with a destination.
- The `exomem attach` client accepts the stored receipt of a destination ticket.
- Hosted-client users must paste the new learning instruction into their custom instructions to get its wording.
