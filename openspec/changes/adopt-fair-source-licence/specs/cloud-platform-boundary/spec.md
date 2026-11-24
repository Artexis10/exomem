## ADDED Requirements

### Requirement: The trust set stays public

The code that enforces the hosted service's published security claims SHALL stay public in this repository. This holds even when the rest of the platform develops in private. The trust set is:
- the image admission, cell deletion and operator access policies;
- the deletion-grant contract and its verifier;
- the key custodian, as its own package and image;
- the cell-runtime conformance suite.

Each trust-set image SHALL be built by public CI with a public build attestation.

#### Scenario: A customer checks the custodian

- **WHEN** a customer takes a published custodian image digest
- **THEN** they can verify, without our help, that public CI built it from public source
- **AND** they can read that source in this repository

#### Scenario: A claim no longer links private code

- **WHEN** the security model document states a security claim
- **THEN** each code link it gives resolves to public code in the trust set or the product
- **AND** a claim about private controller behaviour says it is our statement and names the public policy or custodian that bounds it

### Requirement: The public security model states what is provable

The public security model document SHALL separate three kinds of statement:
1. What anyone can read: the trust-set source.
2. What anyone can prove: the build provenance of a published trust-set image digest.
3. What rests on the operator's statement: which digests run, which policies are installed, and how the private controller behaves.

It SHALL NOT present an operator statement as independently verifiable.

#### Scenario: Reading the security model after the cut

- **WHEN** a reader looks for evidence of a claim about the private controller
- **THEN** the document labels it as the operator's statement
- **AND** it names the trust-set component that limits what the controller can do

### Requirement: Public code never depends on private code

The product and the trust set SHALL build, test and release without reading, importing or fetching any private repository. The private platform MAY depend on released product images, the published cell-runtime contract and the trust set. A self-host installation SHALL never need private code.

#### Scenario: Building the product without private access

- **WHEN** the product CI runs on a fork or without private-repository credentials
- **THEN** every product job and trust-set job completes without reaching a private repository

#### Scenario: The conformance suite ships with each release

- **WHEN** a product version is made available
- **THEN** the cell-runtime conformance suite for that version is public in this repository at the same tag

### Requirement: Published platform code stays reachable

Platform code and images published before the cut SHALL stay reachable at their published locations, for as long as any image built from them remains published. The move to private repositories SHALL NOT rewrite or delete published history.

#### Scenario: An old image's source

- **WHEN** someone pulls a fleet image published before the cut
- **THEN** the source tag it names still resolves in a public repository
