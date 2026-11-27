# records

## ADDED Requirements

### Requirement: A Records collection declares how a page's rows add up

A Records manifest MAY declare `subject`, a closed mapping. `field` SHALL name a `link` field and `measure` an `integer` or `number` field. `state` MAY name an `enum`, `string`, `boolean` or `integer` field. `order` MAY name a `date` or `datetime` field. `show` MAY list up to four fields, each once and none a `link`. `recent` SHALL be 0 to 10, default 3. A field MAY carry a `description`. The server SHALL take each field name and state value from the declaration and never guess one.

#### Scenario: A declaration with only its required keys

- **WHEN** a Records manifest declares `subject` with a link `field` and a number `measure`
- **THEN** the manifest parses with no `state`, no `order`, no `show` and `recent` 3

### Requirement: A malformed subject is refused with what to change

A collection create, `revise` and `validate_revision` SHALL refuse a `subject` with an unknown key, a missing required key, a key that names no declared field or a field of another type, or a `show` or `recent` outside its bound. The refusal SHALL name the key and say what to change, and list the declared fields that a wrong field's key accepts. A Planning or derived collection SHALL NOT declare `subject`. A reader SHALL treat a refused `subject` as none.

#### Scenario: A measure that is not a number is refused

- **WHEN** a revise sets `subject.measure` to a declared `string` field
- **THEN** the revise is refused, and the message names `subject.measure`, the field's type and the declared number fields

#### Scenario: A link field is refused in show

- **WHEN** a revise lists a declared `link` field in `subject.show`
- **THEN** the revise is refused, and the message names `subject.show`, the link field and the fields that `show` accepts

#### Scenario: A Planning collection cannot declare a subject

- **WHEN** a create or revise gives a Planning manifest a `subject`
- **THEN** it is refused, and the message says that only a Records collection declares `subject`

#### Scenario: A manifest that used subject for something else stays readable

- **WHEN** a stored manifest holds a `subject` value that is not a declaration, such as a plain word
- **THEN** the collection reads with no ledger, and a revise that keeps the value is refused with what to change
