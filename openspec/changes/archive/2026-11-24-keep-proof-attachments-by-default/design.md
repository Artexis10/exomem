# Design

## Decisions

### Save on arrival, through the same command

A destination ticket records `scope`, `category` and the caption when it is issued. After the send holds the bytes, the route runs the same `preserve_artifacts` command that `save_evidence` runs, under the ticket's principal, which owns the hold. Duplicate detection, family listing, media scheduling and receipts therefore behave exactly as for `save_evidence`. When that save fails, the hold stays: the reply carries the held handle and the refusal, and the agent can still redeem it with `save_evidence` within the hold's hour. Form fields cannot change the destination, because the ticket protocol's fixed field set is unchanged.

### A digest is optional only for a destination ticket

A held ticket binds an expected SHA-256 so that a sandbox proves it sent the file it meant to send. A person who picks a file on a page has no digest to give. A destination ticket may therefore omit it, and the receipt reports the stored hash. Every other ticket still requires a digest.

### The page holds no secret

`GET /upload/send` returns the same static bytes for every ticket. The ticket stays in the URL fragment, which browsers do not send. The page reads the fragment, then removes it from the address bar. A content security policy allows only the page's own script, by hash, and connections to the same origin. The response sets `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. A Cloud cell's byte route sits behind the gateway, which serves no page, so only a personal server offers one.

### A citation, not a typed relation

A compact observation cannot carry a typed relation: the observation command refuses one without a governed rich kind. The vault already cites a Source by an inline wikilink in the observation's text. Each kept file's result therefore carries `cite`, a wikilink to its page, which any note or observation can include.

### What redaction removes

Redaction runs where a capture keeps caller text: a text original, a caption and an extracted text. It removes two shapes:

- **A payment card number.** It matches a scheme's issuer prefix and length, and its Luhn check digit holds. The scheme table is a closed set that the card schemes publish, so it lives in code.
- **The labelled credential shapes that the egress scrubber already names.** The scrubber's high-entropy heuristic is left out, because evidence quotes hashes and serial numbers that the heuristic would mangle.

The removed value becomes a marker in the kept text, and the result's `warnings` name it so that the agent tells the person.

Image pixels are not inspected at write time, because OCR runs later in the media worker. Instructions tell the agent, which sees the image, not to keep a picture that shows a secret, and to ask for a covered copy.

### Where the agent-facing rule lives

The server instructions sit at their 900-character bound, which `conversation-aware-activation` forbids raising, and measured runs show that agents treat server text as background. The rule therefore lives in four places that agents act on:
- the learning instruction, in the user's voice;
- the bootstrap profile's `binary_upload` entry, which the server instructions send every agent to read;
- the `save_evidence` description;
- the skill scaffold.

The session-close real-model replay measures whether this placement changes behaviour.

## Risks

- **Card-shaped identifiers.** A 16-digit identifier that matches a scheme prefix and passes Luhn is removed, apart from its last four digits. IMEIs (15 digits, prefixes outside the 15-digit schemes) and hashes pass unchanged. The marker keeps the last four digits.
- **Short labelled secrets.** The scrubber's labelled-credential pattern needs 16 or more characters, so a short password in text is kept. The agent-side rule covers it.
- **An already-stored image that shows a secret** is unaffected. Removing an original from append-only Evidence needs its own design.
