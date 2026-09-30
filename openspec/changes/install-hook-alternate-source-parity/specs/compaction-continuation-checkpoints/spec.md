## ADDED Requirements

### Requirement: Installation survives alternate-source regeneration

`exomem install-hook` SHALL merge its entries into the deployed config and into every parseable alternate source for that config, so a subsequent alternate regeneration cannot revert the install. An alternate source SHALL be a same-directory sibling whose name is the deployed config's name followed by `##` and a non-empty suffix, and SHALL NOT be a symlink. Each source SHALL be written through the same mode-preserving backup and same-directory atomic replacement as the deployed config, and a normalized no-op on a source SHALL create neither backup nor rewrite. A source that does not parse as a JSON object SHALL be skipped and reported, never treated as a failure and never silently dropped. Where no alternate source exists, behavior SHALL be byte-identical to a single-file install.

Where the deployed config is itself an alternate link, the regular-file requirement SHALL be relaxed only for a link whose value contains no path separator, whose target name is the deployed name followed by `##` and a non-empty suffix, and whose target is a regular file inside the same trusted directory satisfying the unchanged ownership and group/other-writability checks. Every other symlink SHALL remain refused, including a link to an absolute path, to a parent directory, to a non-`##` name, or to another link.

#### Scenario: A copied alternate deployment survives regeneration

- **WHEN** `install-hook` runs against a deployed config that has a matching `##`-suffixed sibling source
- **THEN** both the deployed config and the sibling source contain the current entries
- **AND** regenerating the deployed config from that source leaves the entries present

#### Scenario: A linked alternate deployment installs through the link

- **WHEN** `install-hook` runs against a deployed config that is a symlink to a same-directory `##`-suffixed sibling
- **THEN** the merge succeeds and the sibling source contains the current entries
- **AND** the deployed name remains a symlink to that sibling

#### Scenario: A symlink outside the alternate shape is still refused

- **WHEN** the deployed config is a symlink whose target is an absolute path, a parent-directory path, a name without a `##` suffix, or another symlink
- **THEN** the run fails with the unsafe-config error and writes nothing

#### Scenario: An unparseable alternate source is reported, not fatal

- **WHEN** one alternate sibling is a template that does not parse as a JSON object
- **THEN** that source is reported as skipped
- **AND** the deployed config and every parseable sibling are still merged

#### Scenario: A plain install is unchanged

- **WHEN** `install-hook` runs against a deployed config with no `##`-suffixed sibling
- **THEN** exactly the deployed config is written, with the existing backup and replacement behavior

### Requirement: Uninstall reports truthfully on an alternate-link deployment

`exomem install-hook --uninstall` SHALL resolve an alternate-link deployed config through the same bounded relaxation as install, prune the resolved target, and report that removal against the deployed config rather than raising the regular-file refusal. Where the resolved target is also reached as an alternate sibling, the second visit SHALL be a normalized no-op creating neither backup nor rewrite, so exactly one backup per config exists on disk. `success` SHALL reflect what is on disk: a run that removed entries SHALL NOT report failure, and `uninstall_all_hooks` SHALL aggregate those verdicts unchanged. Removal semantics SHALL be unchanged — this requirement governs only what the run reports about work it performed.

#### Scenario: A linked deployment reports the removal it performed

- **WHEN** `--uninstall` runs against a deployed config that is a symlink to a same-directory `##`-suffixed sibling holding current entries
- **THEN** the entries are removed from the resolved target
- **AND** the run reports the removal against the deployed config with no config error
- **AND** the run does not report failure

#### Scenario: The resolved target is not backed up twice

- **WHEN** the resolved target is visited again as an alternate sibling in the same run
- **THEN** that visit reports no change and no removal
- **AND** exactly one backup file exists for that config on disk

### Requirement: An unevaluated condition is never reported as a pass

`install-hook --check` SHALL report a condition it could not evaluate as `unevaluated`, distinct from both pass and fail, naming the path and the underlying error. `overall` SHALL fail when any condition is unevaluated. An unevaluated condition SHALL NOT be rendered with the vocabulary of a pass, and the absence of a finding SHALL NOT be reported as the absence of the thing sought. Conditions whose inputs were read successfully SHALL decide exactly as before.

#### Scenario: An unreadable config does not yield a clean legacy report

- **WHEN** `--check` cannot read the hook config and that config in fact holds legacy entries
- **THEN** the legacy condition is reported as unevaluated, naming the config path and the read error
- **AND** the legacy condition is not reported as a pass
- **AND** `overall` fails

#### Scenario: A readable config reports exactly as before

- **WHEN** `--check` reads the hook config successfully
- **THEN** every condition reports the same verdict it reported before this change
