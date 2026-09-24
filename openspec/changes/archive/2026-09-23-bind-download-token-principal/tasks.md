## 1. Binding

- [x] 1.1 Pin the contract as failing tests on main: a non-owner OAuth and a Cloudflare Access minter are refused a withheld path and served a released one, the owner's mint and the raw secret stay owner, an unresolved or unbound minter binds the floor, a claimless capability is refused, and withheld and missing refuse identically, including under a case-folding resolver.
- [x] 1.2 Add the bound capability wire form (`mint_bound`, `bound_audience`) with a version-tagged, NUL-separated signed message, one claim spelling per audience, and a byte comparison of the signature.
- [x] 1.3 Bind the effective principal's audience in `op_transfer_artifact`, falling back to the floor when it is unresolved, and default `mint_for_endpoint`'s download audience to the floor.
- [x] 1.4 Resolve a bound capability to its audience in `download_principal`, keep the raw secret as the owner, and accept only the raw secret or a bound capability on the download branch of `_authorized`.
- [x] 1.5 Render every download `NOT_FOUND` once, from the request's normalized spelling.
- [x] 1.6 Keep the claim single-case so the handoff survives the dispatcher's terminal credential scrubber, and pin a Cloudflare Access mint-then-download round trip through the REST dispatcher.

## 2. Hardening from review

- [x] 2.1 Refuse an expiry longer than twelve ASCII digits in both token parsers, and compare the v1 signature as bytes.
- [x] 2.2 Refuse a folder on `/download` with the same request-spelled 404 as a missing path.
- [x] 2.3 Answer other `/download` path refusals with a fixed `INVALID_PATH` reason that names no server path.
- [x] 2.4 Compare every presented bearer as bytes on the transfer routes, the REST facade and the lease coordinator.
- [x] 2.5 Decide release by path before a direct page read reports undecodable or unparseable bytes, and report them with a fixed reason.

## 3. Unit reads and graph seeds

- [x] 3.1 Take the page release decision before an exact unit read resolves any reference, serve a unit only from a page released in full, and build its citation from the released frontmatter.
- [x] 3.2 Resolve a graph-context unit seed whose parent is withheld as a unit of an absent page, on `connect_memory` context and the `graph_context` leaf, and answer a withheld context page seed as an absent page.
- [x] 3.4 Decide a unit seed by every page the resolver can consult, including a parent named by path, and never substitute the owner's seed.
- [x] 3.3 Record the withheld receipt for a withheld undecodable read, as a decodable read does.

## 4. Verification

- [x] 4.1 Move the tests that minted claimless download tokens onto owner-bound capabilities, without weakening an assertion.
- [x] 4.2 Scoped suites green for transfer, upload tokens, the transfer routes, egress and principal resolution, plus ruff, the privacy gate, OpenSpec strict, capabilities, the hosted check and the harness module list.
