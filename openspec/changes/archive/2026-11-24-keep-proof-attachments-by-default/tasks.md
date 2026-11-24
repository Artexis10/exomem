# Tasks

## 1. Caption and citation

- [x] 1.1 Accept `description` with `files` in `save_evidence`, write it on each newly stored original's page, and report `caption`.
- [x] 1.2 Return `cite` on each kept file's row and project it through the reply.

## 2. Save on arrival

- [x] 2.1 Let `upload_files` take `scope`, `category` and `description` on the evidence lane, and record the destination on the ticket.
- [x] 2.2 Save a destination ticket's bytes on arrival through `preserve_artifacts`, and keep the hold when the save fails.
- [x] 2.3 Serve the static send page on personal servers and offer its link in the ticket reply.
- [x] 2.4 Accept a destination ticket's stored receipt in `exomem attach`.

## 3. Redaction

- [x] 3.1 Remove card numbers and labelled credentials from every kept caller text, and warn.

## 4. Guidance

- [x] 4.1 Update the learning instruction and its guide copies, the bootstrap `binary_upload` entry, the `save_evidence` description and the skill scaffold.

## 5. Proof

- [x] 5.1 Deterministic tests for the caption, citation, save on arrival, page and redaction.
- [x] 5.2 Session-close real-model replay with image turns (cases c13 and c14) on base and changed code.
