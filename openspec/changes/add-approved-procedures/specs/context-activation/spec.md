## ADDED Requirements

### Requirement: Method items carry the approval verdict

Each item of `task_context.methods` SHALL carry an `approval` field with the value `approved`, `stale` or `unapproved`. The value SHALL come from the verdict function of the `procedure-approval` capability, for the caller's view. A caller that is not the vault owner SHALL receive `approved` or `unapproved` only. An `approved` item SHALL add one prose line, "Approved procedure: <title>. Read it, then follow it where it fits.", and the shared prose counter SHALL charge that line once like any other line. The line SHALL carry no step and no page text other than the sanitized title. An item that does not fit with its line SHALL follow the existing overflow rules. Approval SHALL NOT change which methods the packet selects or their order. Each `method_versions` entry of the continuity token SHALL carry the item's `approval`, and an entry without the field SHALL read as `unapproved`. Revalidation SHALL compare each carried entry's `approval` with the caller's current verdict. When they differ, revalidation SHALL reset the held methods with the existing generic reset, even when the page bytes are unchanged.

#### Scenario: An approved method carries one line
- **WHEN** the owner approved a release procedure and a client activates a matching software task
- **THEN** the method item carries `approval` set to `approved`
- **AND** the packet adds one line that names the title and says to read the page and follow it where it fits
- **AND** the packet carries none of the page's steps

#### Scenario: A revoke with unchanged bytes resets the next activation
- **WHEN** a client holds an approved method and the owner then revokes it, with the page bytes unchanged
- **THEN** the client's next activation that carries its continuity token resets the held methods
- **AND** the method item, if still relevant, reports `approval` set to `unapproved` and the new token carries that value

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
