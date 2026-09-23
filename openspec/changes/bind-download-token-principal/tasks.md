## 1. Binding

- [x] 1.1 Pin the contract as failing tests on main: a non-owner OAuth and a Cloudflare Access minter are refused a withheld path and served a released one, the owner's mint and the raw secret stay owner, an unresolved or unbound minter binds the floor, a claimless capability is refused, and withheld and missing refuse identically, including under a case-folding resolver.
- [x] 1.2 Add the bound capability wire form (`mint_bound`, `bound_audience`) with a version-tagged, NUL-separated signed message, one claim spelling per audience, and a byte comparison of the signature.
- [x] 1.3 Bind the effective principal's audience in `op_transfer_artifact`, falling back to the floor when it is unresolved, and default `mint_for_endpoint`'s download audience to the floor.
- [x] 1.4 Resolve a bound capability to its audience in `download_principal`, keep the raw secret as the owner, and accept only the raw secret or a bound capability on the download branch of `_authorized`.
- [x] 1.5 Render every download `NOT_FOUND` once, from the request's normalized spelling.
- [x] 1.6 Keep the claim single-case so the handoff survives the dispatcher's terminal credential scrubber, and pin a Cloudflare Access mint-then-download round trip through the REST dispatcher.

## 2. Verification

- [x] 2.1 Move the tests that minted claimless download tokens onto owner-bound capabilities, without weakening an assertion.
- [x] 2.2 Scoped suites green for transfer, upload tokens, the transfer routes, egress and principal resolution, plus ruff, the privacy gate, OpenSpec strict, capabilities, the hosted check and the harness module list.
