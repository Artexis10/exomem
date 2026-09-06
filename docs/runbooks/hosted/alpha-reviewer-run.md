<!-- authority:non-specification -->

# Hosted alpha — the reviewer client run

The manual half of a hosted promotion. A bootstrap `run` has produced a live
cell and two canary credentials; this runbook covers what a human does in real
clients before `observe`/`sign`/`import`/`promote` can proceed.

It is unautomatable by design: the seven promotion operations are assertions
that a person drove a real client through a real journey. `observe --rehearse`
refuses to emit signable records precisely so this cannot be faked.

## Before you start

| Input | Where it comes from |
|---|---|
| MCP endpoint | `https://substratesystems.io/api/exomem/mcp/v1` |
| Canary credentials, per platform | `<state-dir>/run-canary-<platform>.response.json` |
| Sibling stage ids | `<state-dir>/sibling-stage-ids.json` |
| Results templates | `<state-dir>/results-<platform>.json` |

Three clocks, and the tightest one governs:

- **Canary credentials** — 24 h from `run`. Bounds this entire runbook.
- **Staged release** — the operator's choice at `prepare`, up to 7 days. Bounds
  the evidence steps that follow.
- **Bootstrap authority** — 28 min, spent inside `run`. Already gone; it never
  constrained this phase.

Both platforms share one tenant and one vault. Seed once, in the first run.

## The seven operations

Each is a boolean you assert per platform, recorded in
`results-<platform>.json`.

| Operation | Satisfied by |
|---|---|
| `native_install` | Adding the connector through the client's own connector UI, not a config file |
| `authorization` | Signing in at `/exomem/sign-in` and consenting at `/exomem/authorize` |
| `tool_discovery` | The client listing the profile's tools — 25 for `hosted-alpha-agent-v4` |
| `content_recall` | Asking a natural question and getting governed content back |
| `citation` | That answer naming its source page |
| `durable_capture` | The assistant capturing a conclusion worth keeping |
| `fresh_chat_recall` | A new chat with no history recalling what was captured |

Only flip what actually happened. A false negative costs a rerun; a false
positive poisons signed evidence and `import` rejects it later, at a point where
the window is mostly spent.

## Seeding: use real, connected material

**Do not seed the marketplace review fixture for a promotion run.**
`promotion_evidence.py` contains no reference to it. The evidence is seven
human-observed booleans, six database counts and HMAC strings; nothing binds it
to fixture content. The scrubbed fixture exists for provider review, where a
reviewer needs a reproducible corpus — a different obligation, later.

Seed notes you actually care about, in one pass, in ordinary language. Five to
eight is plenty. They must be genuinely connected, because the authoring
contract requires it and because a recall test over disconnected filler proves
retrieval plumbing rather than the product.

The rule, stated once so it does not surprise you: **the first compiled page in
an empty vault commits freely; every later one must either carry a qualifying
typed relation or explicitly record that none applied.** Material that cites its
counterpart satisfies this for free. Material that stands alone needs
`relation_disposition="reviewed_none"` with the `relation_review_hash` returned
by validation and a stated reason. Write connected notes and the question does
not arise.

A workable shape: one page establishing context, two or three that cite it or
each other, and one decision page with an explicit provenance link. That gives
`content_recall` something to find, `citation` something to name, and the graph
something real to traverse.

## Running it

Repeat per platform, first Claude then ChatGPT, seeding only in the first.

1. Add the connector through the client's UI. → `native_install`
2. Connect, sign in with that platform's canary pair, consent. → `authorization`
3. Open the tool list and count. → `tool_discovery`
4. *(First platform only.)* Seed the notes, in one pass.
5. Ask a real question whose answer lives in what you seeded. → `content_recall`
6. Confirm the answer names its source. → `citation`
7. Reach a conclusion in conversation and have it captured. → `durable_capture`
8. New chat, no history, ask for that conclusion. → `fresh_chat_recall`
9. Delete the captured page before the next platform, so its `durable_capture`
   starts from the same absent baseline.

Then record both files and hand back to the operator for `observe`/`sign`/
`import` per platform against the **sibling** stage ids — never the bootstrap
stage from `bootstrap-context.json`. They look alike, `observe` signs the wrong
one without complaint, and `import` rejects it afterwards.

## When a write is refused

A refusal is not an outage, and the client will not always tell you which it is.

Substrate's MCP bridge recognises five error codes and maps everything else to
`CELL_UNAVAILABLE` — "your Exomem is temporarily unavailable", `retryable:
false`. Exomem defines eight refusal codes with user-facing messages and
remediations; the other three, including every authoring-contract refusal,
arrive rewritten as a service outage. Until that is fixed, **treat
"temporarily unavailable" on a write as an unknown refusal, not a diagnosis.**

The cell keeps an unredacted per-mutation record that the redacted pod log does
not:

```
kubectl -n <cell-ns> exec <cell-pod> -c exomem -- cat /var/lib/exomem/logs/mutations.jsonl
```

One line per mutation with `tool`, `outcome`, `error_code`, `duration_ms` and
the receipt id. That is the fastest route from a client symptom to a real code.
`kubectl` is not on the node's PATH; use `k3s kubectl` with
`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`.

To tell an outage from a refusal without leaving the client: an outage stops
tool discovery and authorization too. A refusal leaves reads working.

## Cost of a failed attempt

`reset` reclaims the tenant. The invite, the alias, the staged release and an
operator OAuth client slot are spent per attempt and never returned; client
slots are bounded at 96. Nothing in this runbook is irreversible — the
irreversible step was `run` — so a bad step inside the cell costs nothing.
Stop and diagnose rather than re-running the bootstrap.
