# Tasks

## 1. Evidence and guarded merge

- [x] 1.1 Run historical Full coverage on ready PRs with one scope policy; prove correct draft, ready, release, and docs behavior.
- [x] 1.2 Add the guarded merge command; reproduce failed current-main preflight and prove merge never runs.
- [x] 1.3 Bind PR merge-tree receipts and inherited gate attempts; prove identical-tree reuse and partial/mismatched proof rejection.
- [x] 1.4 Balance K3s shards with measured case durations; prove complete disjoint collection and record actual CI job times.
- [x] 1.5 Partition paired recall sampling without changing its cases or statistics; prove complete provenance-bound aggregation and record actual CI job times.
- [x] 1.6 Separate bounded package downloads from one installation; prove failure propagation and caller budgets, then observe hosted setup reaching pytest.
- [x] 1.7 Run Full only on schedule and dispatch; ready PRs return to the partial tier on py3.11 (operator, 2026-10-30; reverses 1.1).

CI run 40366767047 passed all six K3s shards and all 24 recall samplers. The longest jobs took 681 seconds and 570 seconds, respectively.
The longest K3s job included 338 seconds of image building and 311 seconds of tests.
Core shard 1 on Python 3.13 passed runtime setup and its tests. The whole run failed on inherited Python 3.11 imports.
These observations verify scheduling and setup; they do not qualify the later source tree.

## 2. Python qualification

- [x] 2.1 Extend maintained recovery tools with scoped qualification; verify official bytes and explicit producer identity.
- [x] 2.2 Wire qualification after Python publication and document independent image status; verify the publication job graph.
- [x] 2.3 Remove the optional CUDA cache export; verify image publication and retain its actual execution timeline.

## 3. Integrated delivery

- [x] 3.1 Obtain independent exact-content review and green source full CI; retain the review and tested-tree receipt.
- [x] 3.2 Synchronize and archive the change after implementation; pass strict validation and archive discipline checks.
- [x] 3.3 Merge and release the reviewed batch; retain ready-to-green, ready-to-qualified, Python-prefix, and separate consumer evidence with unknown states explicit.

## Delivery evidence

[Source PR #2125](https://github.com/Artexis10/exomem/pull/2125) merged as `143af29374314860076727af25469d579f9a4535` on 2026-10-28.
[Independent approval](https://github.com/Artexis10/exomem/pull/2125#issuecomment-6472513151) covers head `70a99db1a3313dd41a42e9a2a6d65facafd94403` and all 30 changed paths.
[Source Full](https://github.com/Artexis10/exomem/actions/runs/40695485693) passed 148 jobs with four skips.
Its artifact `12826666045` binds tested tree `4ea1adfb5adcd759870bfebc3587de897b83a8af`.
Concurrent source work changed the merged tree, so publication used the required Full fallback rather than claiming identical-tree reuse.

[Release Full](https://github.com/Artexis10/exomem/actions/runs/40847081586) qualified source `fb0872dba15925dff660001077d09ce2d4ee2a8c`.
Artifact `12887339249` binds tree `8f5ba45e88da98cc8ea81bf35af9387ad2af0bd5`.
The run succeeded with 150 successful jobs, eight advisory measurement failures and one skip.
Those failures do not establish a latency result.
[Release PR #2180](https://github.com/Artexis10/exomem/pull/2180) produced tag [v0.149.0](https://github.com/Artexis10/exomem/releases/tag/v0.149.0) at `9afb6ab681f3127b3ba18b3290289cd195d15854`.
The tag contains the source merge.

[Publisher 40857280151](https://github.com/Artexis10/exomem/actions/runs/40857280151), attempt 1, completed successfully on 2026-10-29 at 10:48:00Z.
Its Python qualification artifact `12889324142` retains original distribution artifact `12889489460` and verifies matching GitHub and PyPI bytes.

| Distribution | SHA256 |
| --- | --- |
| Wheel | `3b69e0ed5201058575d28328e08056f4169504f8ab5e5f9795a96bb1c40c5b96` |
| Source distribution | `a8ea8cc8410c2128132353882867b91248fecaff5ddd961a79cf7ebbffa0637a` |

[Image job 123395809036](https://github.com/Artexis10/exomem/actions/runs/40857280151/job/123395809036) retained signed evidence for all five images.
Its final verification step checked signed producer identity, build targets and discovery tags, and completed at 10:47:01Z.
The release assets retain the original signed bundles for these digests.

| Image | SHA256 digest |
| --- | --- |
| lean | `fb90978768eabeeee555d80c5157851bce81db069f0a4686b6d33d2739e3210c` |
| ml | `fabf16b9fd65629451da1a80a16dbc86df7c8de3e502997c732ad74e34a11b75` |
| cuda | `e087b8bcf5df2e0b3794dd33d48faef8aa0e3857e80a3d40204ebd2eeffc9287` |
| cloud | `90e74b652622edc129418e42d46423fd76d4d9f04c904666fb5a8b00af81e05d` |
| cellctl | `cd3f9e4cf19e8fa18cfe49a36ab464b6e31482d03f03ff063c954703db9c9a98` |

The CUDA build and push ran from 10:41:12Z to 10:45:44Z: 272 seconds.
Its attestation ended at 10:45:52Z; its post-build step took one second.
The tag workflow retains cache reads and has no CUDA cache export.
This observation proves publication without that export; it does not quantify a cache or pipeline gain.

### Timing

The first-ready boundary remains 2026-10-26 at 13:57:38Z, from [run 40335211788](https://github.com/Artexis10/exomem/actions/runs/40335211788).
Every interval below uses that same boundary and includes intervening failures, queues, retries and source work.

| Boundary | UTC timestamp | Elapsed seconds | Source |
| --- | --- | ---: | --- |
| Reviewed source Full green | 2026-10-28 14:58:30 | 176452 | Run 40695485693 completion |
| Containing source Full accepted | 2026-10-29 10:19:33 | 246115 | Run 40847081586 completion |
| Official Python qualified | 2026-10-29 10:26:45.451530 | 246547.451530 | Python qualification receipt |
| All image evidence and tags verified | 2026-10-29 10:47:01 | 247763 | Publisher final image verification step completion |
| Consumer verification | Unverified | Unknown | Separate deployment evidence required |

The final source-head run took 5898 seconds, including 1279 seconds before its first runner started.
Earlier candidates remain [failed run 40335211788](https://github.com/Artexis10/exomem/actions/runs/40335211788), [successful older tree 40357419102](https://github.com/Artexis10/exomem/actions/runs/40357419102), and [failed run 40366767047](https://github.com/Artexis10/exomem/actions/runs/40366767047).
The later success does not replace their evidence or reset the clock.
The 15–20 minute delivery target was not met; no measured gain is claimed.
Publication does not close installation, rollout, model QA or performance acceptance.

[CI policy PR #2230](https://github.com/Artexis10/exomem/pull/2230) later replaced Full-on-ready-PR behavior with partial PR checks and scheduled or dispatched Full.
The proposal, design and delta already carry that later contract; synchronization preserves it.

The archive command synchronized five new requirements and one modified requirement.
Strict validation passed all 253 current items before and after the archive, with no failures.
