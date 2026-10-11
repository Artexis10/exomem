## REMOVED Requirements

### Requirement: Image candidates are digest-authoritative and strict
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.

### Requirement: Provenance is bound to certificate-controlled source and signer identity
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.

### Requirement: Attestation publication is durable and immediately verified
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.

### Requirement: Release checkout and attested source identities remain truthful
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.

### Requirement: Runtime and provisioner producers are independent
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.

### Requirement: Published images pass immutable identity smoke
**Reason**: The Hosted runtime and provisioner images are no longer built or published; the release workflow publishes only the lean, ml, cuda, cloud and cellctl images.
**Migration**: None. The Cloud cell and cellctl images keep their own digest and attestation steps.
