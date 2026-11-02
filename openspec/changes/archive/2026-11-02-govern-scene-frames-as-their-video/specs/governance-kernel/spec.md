## ADDED Requirements

### Requirement: A scene-frame page is governed as its video

A scene-frame page SHALL take the scope membership, and with it the audiences, of its video's Source page. A page SHALL count as a scene frame for this rule only when its `parent_media` field names the video and the page sits in that video's canonical `<parent-media>.frames/` directory. A page that names a parent without that path SHALL be governed as itself. When the video's Source page cannot be read, membership SHALL be unresolved and fail closed.

#### Scenario: A frame belongs to the scopes of its video
- **WHEN** a governance scope selects a video's Source page by its stable identifier
- **THEN** each scene-frame page of that video is a member of the scope

#### Scenario: A claimed parent does not move a page into another scope
- **WHEN** a page outside a video's frames directory names that video in `parent_media`
- **THEN** its membership is decided by its own selectors
