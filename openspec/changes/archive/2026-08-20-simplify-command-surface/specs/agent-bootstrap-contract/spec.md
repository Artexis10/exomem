## ADDED Requirements

### Requirement: Bootstrap Presents Simple Actions First
The bootstrap contract SHALL present simple product actions before the full technical tool catalog, so generic agents can route common user intents without learning every command.

#### Scenario: Compact bootstrap includes action routing
- **WHEN** `bootstrap(profile="compact")` is called
- **THEN** the response includes a simple action catalog with action names, intent descriptions, default canonical routes, and safety notes
- **AND** the response still avoids note bodies, private vault paths, and private project names

#### Scenario: Bootstrap distinguishes simple and advanced tools
- **WHEN** an agent reads the bootstrap response
- **THEN** it can identify the normal route for ask, remember, capture, review, connect, adopt, and maintain
- **AND** it can identify when to fall through to advanced canonical commands

### Requirement: Scaffold Guidance Uses Intent Language
The installed Exomem skill and operation references SHALL teach agents simple user-intent language before listing canonical tools.

#### Scenario: Agent maps user words to actions
- **WHEN** a generic agent reads the scaffolded skill or operation reference
- **THEN** it sees examples such as "ask what I know", "remember this conclusion", "capture this source", "review stale knowledge", "connect this note", and "adopt this vault"
- **AND** each example names the canonical operation route to use
