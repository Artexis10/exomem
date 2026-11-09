## ADDED Requirements

### Requirement: An intended release lands when its checks pass

Release automation SHALL merge a release PR only when a release intent dispatched on main named that PR, its exact source commit and its exact reviewed head. It SHALL wait until every required check on that head passes, then apply the same source, head, managed-file and Full-evidence preflight as the supported release merge command, and merge that head with an identity whose push starts the release workflow. A Full run that no intent named, an intent without a head, a newer head, a moved main, a failed or unfinished required check, or the repository switch turned off SHALL leave the release PR unmerged.

#### Scenario: A nightly Full run passes while a release PR is open
- **WHEN** a scheduled Full run on main completes and no release intent names the open release PR's current source and head
- **THEN** the release checks are refreshed and the release PR stays unmerged

#### Scenario: The release head changes after the intent
- **WHEN** the release intent named an earlier head of the release PR
- **THEN** automation does not merge the newer head

#### Scenario: Required checks are not all reported yet
- **WHEN** a CI run on the release head is still queued or running, or no required check is reported yet
- **THEN** automation keeps waiting and does not treat the partial list as final

#### Scenario: A required check fails
- **WHEN** an intent names the release PR's current source and head and a required check fails
- **THEN** automation does not merge, and its run fails naming the check

#### Scenario: The intent named this head and every check passes
- **WHEN** an intent names the release PR's current source and head, evidence is complete and every required check passes
- **THEN** automation merges exactly that head and the release workflow creates the tag

#### Scenario: Landing is switched off
- **WHEN** the repository variable `RELEASE_AUTO_MERGE` is `off` as an automation run starts
- **THEN** that run merges no release PR, and the release session merges with the supported command
