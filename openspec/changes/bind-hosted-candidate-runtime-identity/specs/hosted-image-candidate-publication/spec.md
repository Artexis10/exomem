## ADDED Requirements

### Requirement: Candidate composition proves component source closure

A consumer SHALL compose an image candidate only when the candidate source commit is equal to or an ancestor of the composition commit and a fixed no-shell Git diff proves no relevant build input changed. The runtime closure SHALL include `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, and `src/**`. The provisioner closure SHALL include `infra/provisioner/Dockerfile`, `infra/provisioner/pyproject.toml`, `infra/provisioner/uv.lock`, `infra/provisioner/README.md`, `infra/provisioner/alembic.ini`, `infra/provisioner/src/**`, `infra/provisioner/alembic/**`, `infra/helm/cell/**`, and `.dockerignore`. Missing or shallow commits, non-ancestry, Git errors, additions, deletions, renames, or content changes within the closure MUST fail closed. A valid candidate or image attestation MUST NOT waive source drift.

#### Scenario: Only unrelated composition files changed

- **WHEN** a candidate source is an ancestor and every change since that source is outside its component closure
- **THEN** the candidate remains eligible for composition after its signatures and exact bytes verify

#### Scenario: Runtime input changed after candidate publication

- **WHEN** any file under the runtime closure changes between the runtime candidate source and composition commit
- **THEN** composition rejects the candidate and requires a newly published runtime candidate

#### Scenario: Provisioner migration changed after candidate publication

- **WHEN** a provisioner Alembic migration/configuration, README, cell Helm input, provisioner source, package lock, Dockerfile, or `.dockerignore` changes after the provisioner candidate source
- **THEN** composition rejects that provisioner candidate and requires a new independently attested candidate

#### Scenario: Candidate commit is unavailable or unrelated

- **WHEN** the candidate source is missing from the repository, cannot be proven as an ancestor, or the checkout is too shallow to perform the proof
- **THEN** the guard fails closed instead of assuming source closure
