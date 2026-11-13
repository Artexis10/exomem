## ADDED Requirements

### Requirement: Method items carry the approval verdict

An item of `task_context.methods` SHALL carry an `approval` field only when the caller's verdict is `approved` or `stale`, and an item without the field SHALL read `unapproved`. The value SHALL come from the verdict function of the `procedure-approval` capability, for the caller's view. A caller that is not the vault owner SHALL receive `approved` or no field. An `approved` item's prose line, 'Approved procedure: "<title>". Read it, then follow it where it fits.', SHALL replace its method line, and the shared prose counter SHALL charge it in place of that line. The title SHALL come from the approved bytes: the frontmatter title, else the first H1, else the page's ref, and never the file name. An `approved` item's `read_ref` SHALL be the page's stable ref, never its path. Every method line SHALL quote and escape its title, so that no title reads as part of the fixed instruction. The line SHALL carry no step and no page text other than that title. An item that does not fit with its line SHALL follow the existing overflow rules. Approval SHALL NOT change which methods the packet selects or their order. A `method_versions` entry of the continuity token SHALL carry the item's `approval` only when the item carries it, and an entry without the field SHALL read as `unapproved`. Revalidation SHALL compare each carried entry's `approval` with the caller's current verdict. When they differ, revalidation SHALL reset the held methods with the existing generic reset, even when the page bytes are unchanged. When the approvals ledger cannot be read for a method's ref, the item SHALL read `unapproved` and the packet SHALL report the task-context lane as failed.

#### Scenario: An approved method carries one line
- **WHEN** the owner approved a release procedure and a client activates a matching software task
- **THEN** the method item carries `approval` set to `approved`
- **AND** the item's line names the quoted title and says to read the page and follow it where it fits, in place of the method line
- **AND** the packet carries none of the page's steps

#### Scenario: A revoke with unchanged bytes resets the next activation
- **WHEN** a client holds an approved method and the owner then revokes it, with the page bytes unchanged
- **THEN** the client's next activation that carries its continuity token resets the held methods
- **AND** the method item, if still relevant, carries no `approval` field, and neither does its entry in the new token

#### Scenario: Approval does not rank
- **WHEN** two methods match a task equally and the owner has approved only one of them
- **THEN** the packet selects and orders them as it does when neither is approved

#### Scenario: An older token carries no approval field
- **WHEN** an activation receives a valid token whose method entries have no `approval`
- **THEN** each entry reads as `unapproved`
- **AND** a held method that is now `approved` resets, and a held method that is still `unapproved` does not

#### Scenario: A crowded packet charges the line
- **WHEN** the approved line and the other task lines together exceed the shared budget
- **THEN** the packet applies the existing overflow rules and `used_chars` does not exceed `limit_chars`

#### Scenario: A renamed approved page keeps its own title
- **WHEN** an approved page with no frontmatter title and no H1 moves to a new file name with unchanged bytes
- **THEN** the method item still reads `approved`
- **AND** its line names the page's ref and never the new file name
- **AND** its `read_ref` is the page's ref, which `read_memory` opens

#### Scenario: An unreadable ledger marks the packet incomplete
- **WHEN** the approval key or the approvals ledger cannot be read while a client activates a task with an approved method
- **THEN** the method item carries no `approval` field
- **AND** the packet reports the task-context lane as failed
