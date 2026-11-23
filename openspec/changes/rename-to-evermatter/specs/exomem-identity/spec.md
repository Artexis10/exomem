## MODIFIED Requirements

### Requirement: Exomem Is the Canonical Internal Name

The Python import package SHALL be `exomem`, environment variables SHALL use the `EXOMEM_`
prefix, and the CLI SHALL be reachable as `python -m exomem`. Identifiers in code, the skill
scaffold, tests and scripts SHALL use the exomem name. Prose that a person reads SHALL use the
product name, Evermatter.

#### Scenario: Package imports under the new name

- **WHEN** `import exomem` (or any submodule) is executed in an installed environment
- **THEN** the package loads and all functionality is available under the `exomem` name

#### Scenario: Configuration documented under the new prefix

- **WHEN** a user consults the README/deployment docs for configuration
- **THEN** every variable is documented as `EXOMEM_*`, with a note that legacy `KB_MCP_*`
  names remain honored

## ADDED Requirements

### Requirement: Stored and Configured Identifiers Keep the Internal Name

State and config paths, vault frontmatter keys, the MCP server key that the installer writes,
and plugin and skill identifiers SHALL keep the `exomem` name.

#### Scenario: Agent tool names stay stable

- **WHEN** a new install connects an agent client
- **THEN** the installer writes the server key `exomem`
- **AND** the client exposes the tools under the same prefix as an install made before the rename

### Requirement: Evermatter Is the Product Name on Every External Surface

Every surface that a person reads or types to find, install, run or connect the product SHALL
name it Evermatter. A shipped install line SHALL name the `evermatter` distribution with a
pinned release.

#### Scenario: A new user installs from the README

- **WHEN** a user follows the README install line
- **THEN** it installs the pinned `evermatter` distribution
- **AND** the `evermatter` command starts the product

#### Scenario: The agent sees the product name

- **WHEN** an agent client lists the connected MCP server
- **THEN** the server title is Evermatter

### Requirement: Old Package and Command Names Keep Working

Each release SHALL be published under both the `evermatter` and the `exomem` PyPI names with
identical content. The `exomem` and `kb` commands SHALL run the same CLI as `evermatter`.

#### Scenario: An existing unpinned install upgrades

- **WHEN** a user runs `pip install --upgrade exomem` or `uv tool upgrade exomem` after the rename release
- **THEN** the rename release is installed under the `exomem` name with every module file intact
- **AND** the `exomem` command still starts the product with the user's existing state and settings

### Requirement: Old Image Names Keep Working Until Their Removal Date

Each published image SHALL also be published under its old name from the same digest until the
removal date that the design records. A removal SHALL wait for evidence that the old name is
unused.

#### Scenario: A pinned image reference keeps pulling

- **WHEN** a deployment pulls an image by its old name and a release tag cut after the rename
- **THEN** the pull succeeds with the same digest as the image under the new name
