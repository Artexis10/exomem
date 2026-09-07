<!-- authority:non-specification -->

# Memory permissions for a local server

The local owner page lets you review exact proposed changes, approve or deny a
request, and grant or revoke bounded additive permissions. It uses the server's
existing pinned GitHub identity. Signing in does not approve a change or create
any grants.

This workflow serves a standalone vault managed by a Linux systemd user service
or a macOS LaunchAgent. Hosted accounts use Substrate's Exomem Home and account
session; this page does not administer them. The native maintenance command does
not support Windows yet. Existing unconfigured installations retain v1 behavior.

## Configure the installation

Install a release containing the native owner controls into the existing
service environment. Preserve that environment's optional extras and use its
normal upgrade procedure. The server must already have a working GitHub OAuth
configuration, including the pinned numeric `EXOMEM_GITHUB_USER_ID`, durable
OAuth storage and an HTTPS `EXOMEM_BASE_URL`.

Create a private directory owned by the service user, with mode `0700`, outside
both the vault and its runtime state directory. Add these two settings to the
existing service environment:

```text
EXOMEM_VOCABULARY_AUTHORITY_DIR=<absolute private directory>
EXOMEM_OWNER_SERVICE_UNIT=<absolute systemd unit or LaunchAgent plist>
```

The unit must identify the exact vault, state root and service interpreter.
After restarting that service through its normal manager, open `/owner` on its
existing HTTPS address. The page uses the same GitHub application and callback
as the connector. Keep `EXOMEM_OWNER_ALLOW_LOOPBACK` unset in a remote service;
its explicit HTTP loopback exception exists for local testing only.

Do not prefill the four authorization-custody environment settings when the
installation has never enrolled custody. The reviewed migration prepares and
publishes them together. Existing custody must be complete and consistent;
moving or deleting authority files is not a recovery procedure.

## Review and activate

The page presents the next applicable step:

1. **Initial policy**, only if absent: inspect the canonical `Existing access`
   policy. It preserves the existing access scope, creates no rules or grants,
   and commits only after explicit acceptance.
2. **Governance storage upgrade**, if needed: inspect the migration and backup
   plan, accept it, then run the exact maintenance command displayed by the
   page in a terminal on the service host.
3. **Activation**: inspect and accept the deployment change and ongoing renewal
   of this installation's serving proof, then run its displayed maintenance
   command. Activation creates no grants.

Migration and activation require separate reviews. Each review expires after
at most five minutes. The maintenance command uses the reviewed service's
installed interpreter, stops only that service, verifies its worker and
listener have stopped, executes the accepted transition, and verifies the same
service after restart. It does not upgrade packages or run an index drain.

After activation, connect the agent and open its authorization session. The
page lists current audiences, pending requests and active grants. Review the
actual before/after content for exact approvals. For standing permissions,
choose the supported actions, scope and expiry, then review and accept the
result. All action boxes start unchecked. Permissions bind the displayed
audience and issuer; they do not automatically isolate individual models or
clients that share that identity.

Revocation prevents subsequent covered writes. It does not undo an already
committed change. A request whose content or authority has changed must be
prepared and reviewed again. Legacy requests without complete write previews
can be inspected or denied, but require a fresh proposal for exact approval.

## Interrupted maintenance

A failed transition retains its receipt and leaves the service stopped when
the runner can prove that state. Inspect the reported result and retain the
receipt. Re-run the same displayed command with `--resume`; recovery requires
the exact service, review and stopped-transition proof.

An expired review cannot start new maintenance. Recovery after expiry can
complete only a transition proven to have started while the review was valid.
If deployment publication outlasts the review, the page requests fresh approval
before activating permissions. Do not restart an uncertain partial migration
by hand or remove its state to force the older behavior.

The approved renewal worker preserves the same identity, keys, authority floor
and grants while refreshing serving proofs, including after an overnight
restart. It performs no semantic memory work. Changing the configured owner,
vault, custody files or keyring invalidates that renewal permission. This page
does not yet provide an ownership-transfer or key-replacement workflow for an
activated vault. Compatible package upgrades and unrelated environment changes
do not themselves widen authority.

Back up the external authority directory and custody along with the matching
vault using the existing governed backup procedure. Restoring only the vault
does not restore its permissions. An activated vault cannot be downgraded by
removing configuration or starting an older writer.
