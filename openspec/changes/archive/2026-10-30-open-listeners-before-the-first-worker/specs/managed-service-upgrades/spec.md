## ADDED Requirements

### Requirement: The listeners open before the first worker is ready

The supervisor SHALL open the public listener, the local listener and the control channel before it starts its first worker. Until that worker reports ready, the supervisor SHALL report a distinct starting phase, and ingress SHALL refuse every call at once as not dispatched, without executing it and without holding it in the admission queue. When the first worker reports ready, ingress SHALL serve calls. A first worker that fails to start SHALL still stop the supervisor with a failed exit status.

#### Scenario: A call arrives during a cold warm
- **WHEN** a call reaches the public endpoint while the first worker is still warming
- **THEN** the endpoint answers at once that the call was not dispatched
- **AND** the call is not executed

#### Scenario: The first worker becomes ready
- **WHEN** the first worker reports ready
- **THEN** the phase becomes ready and calls are served

#### Scenario: The first worker fails to start
- **WHEN** the first worker exits before it reports ready, or does not report ready inside the cold-start window
- **THEN** the supervisor stops with a failed exit status
