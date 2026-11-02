## ADDED Requirements

### Requirement: A scene frame is also governed by its video

A scene-frame page SHALL belong to its own scopes and to the scopes of its video's Source page. A page SHALL take its video's scopes only when its `parent_media` field names the parent and the page sits in that parent's canonical `<parent-media>.frames/` directory. When the video's Source page cannot be read, the frame's membership SHALL be unresolved and fail closed. Live evaluation SHALL read the video's current membership. A governed projection catalog row of a frame SHALL hold the video's scopes as of the frame's publication.

#### Scenario: A frame belongs to the scopes of its video
- **WHEN** a governance scope selects a video's Source page by its stable identifier
- **THEN** each scene-frame page of that video is a member of the scope

#### Scenario: A frame keeps its own scopes
- **WHEN** a governance scope selects scene frames by their tag, path or identifier
- **THEN** each frame stays a member of that scope as well as of its video's scopes

#### Scenario: A claimed parent cannot widen a page
- **WHEN** a page names a parent in `parent_media`
- **THEN** it keeps every scope its own selectors give it
