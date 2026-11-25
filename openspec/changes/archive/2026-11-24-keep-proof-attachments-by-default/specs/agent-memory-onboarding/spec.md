## ADDED Requirements

### Requirement: Proof files go to Evidence in the turn they arrive

The learning instruction, the bootstrap profile's `binary_upload` entry, the `save_evidence` description and the skill scaffold SHALL ask the agent to keep a file the user shares to prove something as Evidence in the same turn, and to cite it in what it writes. The bootstrap profile SHALL name `upload_files` with `scope` and `category` for a client without file handles. Saving stays subject to `proactive_capture`.

#### Scenario: A serial label is kept in its turn

- **WHEN** a user shares a photo of a product's serial label and asks the agent to register its warranty
- **THEN** the agent keeps the photo as Evidence in that turn, or gives the user the page link when it cannot send the bytes
- **AND** any observation it writes about the serial carries the returned citation

### Requirement: Agents never keep a shared secret

The same texts SHALL ask the agent never to keep a card number, security code or password that a shared file shows, and to ask the user for a copy with the secret covered.

#### Scenario: A card photo is not kept

- **WHEN** a user shares a photo that shows a full card number
- **THEN** the agent does not keep it and asks for a copy with the number covered
