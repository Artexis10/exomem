# Design

## Context

The product name is Evermatter and the product domain is `evermatter.ai`. The company site stays `substratesystems.io`. The name `exomem` appears about 48,000 times in 5,100 files of this repository. About 1,300 of those files read `EXOMEM_*` settings. Users' vaults store `exomem_id` in frontmatter. Agent clients store the MCP server key `exomem`, and so they expose tools as `mcp__exomem__*`. Hooks, tests and users' own permission rules match that prefix. About fifteen sessions have open pull requests against `src/exomem`.

The hosted service binds each OAuth token to one MCP resource URL by exact equality. On 2026-11-13 a disconnect cost a user ten days of saves. A host move that strands tokens is therefore not acceptable.

The design follows the rule of thumb in "Writing effective tools for agents" on namespacing: a tool prefix is an interface that agents and their configuration depend on, so it changes only with a migration path.

## Goals / Non-Goals

**Goals**
- A person who reads or types the product name sees Evermatter everywhere.
- Every existing install, vault, agent configuration and hosted connection keeps working with no action from the user.
- The rename lands without conflicting with the open pull requests beyond docs and user-facing strings.

**Non-Goals**
- Renaming the Python package, settings, state paths, vault keys, database identifiers or wire names.
- Renaming the MCP server key or the plugin and skill identifiers.

## Decisions

### D1. Rename the external surfaces only

The external name changes and the internal name stays. This follows the existing `exomem-identity` contract, which already names `exomem` as the internal name.

A full module rename would touch every open pull request. It would also force a settings and state-path migration that protects no user. The internal name is invisible to users except through `EXOMEM_*` settings. Those stay documented as the supported prefix. A later change may add `EVERMATTER_*` aliases if users ask for them.

### D2. The MCP server key and the plugin and skill identifiers stay `exomem`

Clients store the key the installer wrote. Renaming it for new installs would split the user base into two tool prefixes. Hooks and permission rules match `mcp__exomem__*`, so a split breaks them silently. The server's displayed title and instructions say Evermatter instead.

Plugin and skill identifiers are what clients use to update an installed plugin. Renaming them would leave existing installs on a dead identifier. Their display names and descriptions say Evermatter.

### D3. Compatibility through maintained mechanisms

| Old surface | Mechanism | Earliest removal |
|---|---|---|
| `pip install exomem`, `uvx exomem`, `uv tool upgrade exomem` | The release publishes the same build under both distribution names, with identical content | Reviewed on or after 2027-11-23 |
| `exomem` and `kb` commands | Console scripts in `evermatter` | Reviewed on or after 2027-11-23 |
| `ghcr.io/artexis10/exomem*` images | Every tag already published stays pullable; new releases publish under `ghcr.io/substrate-systems/evermatter*` (see D7) | None; published tags are never deleted |
| `github.com/Artexis10/exomem` | GitHub's redirect after the transfer and rename | None; GitHub keeps it while the old name stays free |
| MCP registry `io.github.Artexis10/exomem` | Marked deprecated, pointing at `io.github.substrate-systems/evermatter` | 2027-06-01 |
| Old Cloud host | Keeps serving the gateway (see the control-plane change) | When no live token family references it, and not before 2027-11-23 |

Removal needs evidence that nothing uses the old surface, for example PyPI download counts for the old name or gateway traffic per host.

A metapackage named `exomem` that depends on `evermatter` was rejected. Both distributions would own the same `exomem/` files. An in-place upgrade installs `evermatter` and then uninstalls the old `exomem`, which deletes the files that `evermatter` just wrote. Publishing one build under two names avoids the overlap: each install upgrades within its own name.

### D4. Distribution name and install lines

The PyPI distribution is `evermatter`. Shipped install lines write `evermatter==<exomem-release>` and `uvx evermatter@<exomem-release>`. The placeholder name stays, because it is an internal token that the packaging code fills.

PyPI publishes through a trusted publisher bound to the repository and workflow. The new project needs a pending publisher, created by the PyPI account owner. The existing `exomem` publisher needs its repository name updated after the rename.

### D5. One release carries the rename and the licence cut

The rename release is the first release licensed FSL-1.1-ALv2 (`adopt-fair-source-licence`, section 3). Both change the repository's public identity, so one statement announces both. The release owner and the licence change owner agree the release.

### D6. Hosted service hosts

Specified in the control-plane repository. In outline:
- `evermatter.ai` serves the product website and, later, the authorization server.
- `mcp.evermatter.ai` serves the MCP gateway.
- The gateway accepts a configured set of resource URLs; a token is valid when its stored resource is in the set. Old tokens keep working. New connections get the new host.
- The OAuth issuer stays where it is in the first step. Moving the issuer is a second step with its own review.

### D7. Repository, images and registries

The repository moves from the personal account to the `substrate-systems` organisation and takes the name `evermatter` in one move (approved on 2026-11-20, owned by the licence change). GitHub redirects the old URL for web, API and git traffic.

Image names, the MCP registry entry and PyPI do not follow a move, so the release publishes the new names explicitly:
- Images publish as `ghcr.io/substrate-systems/evermatter` and `ghcr.io/substrate-systems/evermatter-cellctl`. After the move, the repository's CI token cannot write to the personal account's registry namespace, and a personal token in a company pipeline is the wrong shape. So the old image names keep the tags they already have and receive no new ones.
- The Cloud deploy verifies each image's build attestation against the repository that built it. It derives that repository from the image name: `ghcr.io/artexis10/exomem` from `Artexis10/exomem`, and `ghcr.io/substrate-systems/evermatter` from `substrate-systems/evermatter`. A rollback to a release cut before the move still verifies.
- The MCP registry entry moves to `io.github.substrate-systems/evermatter`, the namespace that GitHub sign-in proves for the moved repository.

## Risks / Trade-offs

- **Two names in one codebase.** Contributors read `exomem` in code and Evermatter in docs. The `exomem-identity` spec states the split, so a reviewer can check it.
- **Directory listings.** The ChatGPT app and the Claude directory entry carry the display name. Changing it may need the directory's re-review. The rename does not wait for it; the listing updates when the directory accepts it.
- **Search engines.** The website's old product paths redirect permanently, so links keep working and rankings transfer.

## Migration Plan

1. Merge this change with the external-surface edits and the dual-name build.
2. Move and rename the repository; create the PyPI pending publisher for `evermatter` and point the `exomem` publisher at the moved repository.
3. Cut the rename release together with the licence cut. Publish both distribution names, both image names and the new registry entry.
4. Announce the rename once, through the README, the changelog, the website and the hosted service's notices.

Rollback: the rename is additive. The old package, commands, published images, URLs and host keep working, so rolling back means pointing docs back at the old names.
