# Tasks

## 1. Protocol constants (D0)

- [ ] 1.1 Mark each protocol and derivation constant with a one-line comment that says it keeps its bytes: salts, token tags, audiences, the issuer, hash domains, stored fences and schema ids.

## 2. Note identity: readers and writers (D1, D2)

- [ ] 2.1 Add one accessor that reads note identity and returns an integrity error for two different values, and one writer that writes `evermatter_id`. Route every read and write through them.
- [ ] 2.2 Keep a conflicting note in the index without an identity, resolve neither value, and report it in the audit.
- [ ] 2.3 Update the scaffold's frontmatter reference and skill text to name `evermatter_id`.

## 3. Note identity: the rewrite job (D3)

- [ ] 3.1 Add the governed rewrite job on `commit_pages`: preview with a plan ID, skip pages changed since the plan, resumable, receipt, and a reverse job for rollback.
- [ ] 3.2 List in the preview the notes whose text names `exomem_id`.
- [ ] 3.3 Run the job and its rollback on a copy of a production-sized vault, and record the counts and the duration.
- [ ] 3.4 After `move-cloud-to-evermatter-hosts` completes, run the job on Cloud vaults one cell at a time under a production grant.

## 4. Reference scheme (D4)

- [ ] 4.1 Emit `evermatter://` for new references from one emitter. Resolve both schemes, and register MCP resources under both.

## 5. Package and state (D5, D6)

- [ ] 5.1 Rename the import package to `evermatter` in an announced freeze window. Ship the `exomem` alias package with the same module objects and `python -m` entry points, and re-target `kb_mcp`.
- [ ] 5.2 Add the explicit command that stops the service and renames the state directory, the mode config and the import journals. Make readers fall back to the old paths.

## 6. Cloud infrastructure (D7)

- [ ] 6.1 Move labels and annotations readers first, including the tenant isolation policy, with a second review pass.
- [ ] 6.2 Move metric names, systemd units, the Helm release and images readers first, each under a runbook and a production grant.
- [ ] 6.3 Give new namespaces, secrets and cellctl tables the new name, and leave existing ones unchanged.

## 7. Compatibility window (optional for archive)

Archive this change after `rename-agent-surfaces-to-evermatter`, because both modify the same requirement.

- [ ] 7.1 On or after 2027-11-23, review use of the alias package and the old state-path fallback, and remove each only with evidence.
