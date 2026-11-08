## ADDED Requirements

### Requirement: Learning instruction for every client
Exomem SHALL ship one learning instruction in the user's voice. It SHALL ask the agent to recall what Exomem holds about the work before it starts, and to save each reusable fact it establishes as one concise observation, updating an existing note when one covers it. The text SHALL have one source in the package. The assistant guide, the remote quickstart and the prominence guide SHALL give the same text as paste-in custom instructions for hosted clients, and a documentation check SHALL fail when a doc copy differs from the package text. No server, bootstrap or hook channel SHALL inject the learning instruction.

#### Scenario: A hosted-client user finds the paste-in text
- **WHEN** a user follows the remote quickstart to connect ChatGPT or claude.ai
- **THEN** the guide gives the learning instruction as text to paste into account-level or project-level custom instructions, ahead of the copyable policy block

#### Scenario: The doc copy cannot drift
- **WHEN** the packaged learning instruction changes and a guide still shows the old text
- **THEN** the documentation check fails and names the guide

#### Scenario: The benchmark measures what users get
- **WHEN** the CL-Bench P arm prepares a question
- **THEN** it lays the packaged learning instruction where setup would place it for that client
- **AND** the run manifest records the text's digest and the place it was laid
