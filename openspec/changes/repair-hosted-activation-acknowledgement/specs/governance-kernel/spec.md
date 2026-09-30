## ADDED Requirements

### Requirement: Hosted publication has external successor acknowledgement

Hosted catalog and policy publication SHALL acknowledge their exact committed successor through authenticated external control-plane authority. The runtime SHALL NOT make its local projected custody writable, silently accept mismatched authority, or infer acknowledgement from canonical bytes alone. A private recovery proof path SHALL remain usable during mismatch while content paths remain closed.

#### Scenario: First write after enrollment
- **WHEN** an ordinary write advances the first enrolled hosted catalog
- **THEN** canonical publication and external successor acknowledgement complete under their respective authorities
- **AND** subsequent content reads verify their exact parity

#### Scenario: Publication commits before external acknowledgement
- **WHEN** the process or transport fails between committed publication and external acknowledgement
- **THEN** recovery accepts only that exact receipt-proven successor
- **AND** unrelated content serving remains blocked until parity is restored
