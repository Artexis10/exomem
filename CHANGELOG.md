# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.25.0](https://github.com/Artexis10/exomem/compare/v0.24.2...v0.25.0) (2026-07-19)


### Features

* harden writes and proactive entity capture ([#258](https://github.com/Artexis10/exomem/issues/258)) ([9ef69ef](https://github.com/Artexis10/exomem/commit/9ef69ef0b5d2b7b2c6609b291cd03003a12d0850))

## [0.24.2](https://github.com/Artexis10/exomem/compare/v0.24.1...v0.24.2) (2026-07-18)


### Features

* add durable OAuth refresh tokens ([#255](https://github.com/Artexis10/exomem/issues/255)) ([4298567](https://github.com/Artexis10/exomem/commit/4298567fceee1dc10d235cce487b2c8b97d6b953))

## [0.24.1](https://github.com/Artexis10/exomem/compare/v0.24.0...v0.24.1) (2026-07-18)


### Bug Fixes

* prevent stale connector rollouts ([#253](https://github.com/Artexis10/exomem/issues/253)) ([87953b0](https://github.com/Artexis10/exomem/commit/87953b056713e9cbf840aefda2843b486a815c19))

## [0.24.0](https://github.com/Artexis10/exomem/compare/v0.23.0...v0.24.0) (2026-07-16)


### Features

* Adoption Studio v1 — governed adoption runs, agent proposals, Studio UI, hosted staging ([#234](https://github.com/Artexis10/exomem/issues/234)) ([74341fa](https://github.com/Artexis10/exomem/commit/74341fa4bc7f1dfe203ebc2c597846032b8d759e))
* complete the semantic language product contract ([#245](https://github.com/Artexis10/exomem/issues/245)) ([7e89a3f](https://github.com/Artexis10/exomem/commit/7e89a3f7bff78d7dac8155dfd194624a33193796))


### Bug Fixes

* bound reconcile maintenance work ([#248](https://github.com/Artexis10/exomem/issues/248)) ([46b61ab](https://github.com/Artexis10/exomem/commit/46b61ab30482773be81e29b084042cd33eca01f3))
* exclude nested dot-trash from corpus scans ([#249](https://github.com/Artexis10/exomem/issues/249)) ([c32ec26](https://github.com/Artexis10/exomem/commit/c32ec26c306b86358778452ca69d641b243b6cf3))
* keep doctor SQLite checks filesystem read-only ([#246](https://github.com/Artexis10/exomem/issues/246)) ([62db3a0](https://github.com/Artexis10/exomem/commit/62db3a0d7f5486a62bbfbf7e6d656d2f8b4f446c))
* make deferred embedding replay durable ([#247](https://github.com/Artexis10/exomem/issues/247)) ([567647a](https://github.com/Artexis10/exomem/commit/567647ae82b8c2fe6e14a9ae9d6a11c51d06df7f))
* preserve exact bytes and review access on Windows ([#244](https://github.com/Artexis10/exomem/issues/244)) ([dc55888](https://github.com/Artexis10/exomem/commit/dc5588814de2527f466c6aea4019ba40dfcb18bf))

## [0.23.0](https://github.com/Artexis10/exomem/compare/v0.22.0...v0.23.0) (2026-07-16)


### Features

* add durable continuation checkpoints ([#231](https://github.com/Artexis10/exomem/issues/231)) ([dcf27c5](https://github.com/Artexis10/exomem/commit/dcf27c5cb7cdb3f6dd8a26d4a74be3722fee7328))
* add first-class semantic language ([#232](https://github.com/Artexis10/exomem/issues/232)) ([87db724](https://github.com/Artexis10/exomem/commit/87db72488a54330185e5651f43192bfaea760e09))
* add governed hosted secret handoff ([0b26187](https://github.com/Artexis10/exomem/commit/0b261870e6c24264c72b77e7ca361454ddaa45cd))
* add guarded semantic unit mutation ([#241](https://github.com/Artexis10/exomem/issues/241)) ([34bd057](https://github.com/Artexis10/exomem/commit/34bd057e71437f6038efbc3eb45f753164f2de55))
* add hardened K3s bootstrap ([ebecf2e](https://github.com/Artexis10/exomem/commit/ebecf2e9ccff4b174c3db8a3253b94650fa4e5fa))
* add hosted credential security authority ([7194bae](https://github.com/Artexis10/exomem/commit/7194bae79540555f8d1ef36bdb08c697b7eadf7c))
* add hosted platform and cell charts ([ab525a9](https://github.com/Artexis10/exomem/commit/ab525a934f87ab289a8903dc78d43ce103d6e751))
* add hosted platform operations gates ([68bc982](https://github.com/Artexis10/exomem/commit/68bc982c83e425b72281b8acda78007b9e979f8b))
* add human-readable memory citation guidance ([cf67623](https://github.com/Artexis10/exomem/commit/cf676234a5a44ffdd7cb7ca800599f02d02e14aa))
* add semantic unit context packs ([#242](https://github.com/Artexis10/exomem/issues/242)) ([e889196](https://github.com/Artexis10/exomem/commit/e889196442c34970d7182bc7ef05e8a5f217475a))
* complete hosted runtime packaging contract ([1b91db4](https://github.com/Artexis10/exomem/commit/1b91db4f20b77d8fe3d7c46f7e22f8dc269d7d45))
* complete isolated hosted tenant runtime ([f451d8a](https://github.com/Artexis10/exomem/commit/f451d8a64d093b0aeedb3fc9b669d4e4316449f8))
* complete the hosted private multi-tenant beta ([def4d4b](https://github.com/Artexis10/exomem/commit/def4d4bca9fb29d3046a4541cf3e2060722c4560))
* compose hosted runtime security ([73a4875](https://github.com/Artexis10/exomem/commit/73a4875216460d65fc62623db3456b1d7ab7d24b))
* expose process media product action ([eaea4b7](https://github.com/Artexis10/exomem/commit/eaea4b7af712302aa2b2d868e9e3749446d00851))
* expose semantic recall and creation review ([#240](https://github.com/Artexis10/exomem/issues/240)) ([818bb6f](https://github.com/Artexis10/exomem/commit/818bb6fd37e5a01bcd1e6fbd5481097cb39c2ab3))
* freeze immutable hosted release unit ([89c97b9](https://github.com/Artexis10/exomem/commit/89c97b979a83852e13edd0c9df455edbc459466b))
* **hosted:** complete integrated durability plane ([dbabc6e](https://github.com/Artexis10/exomem/commit/dbabc6e14829457c3128c18ce7c699bce20206c1))
* **hosted:** complete platform operations composition ([d304997](https://github.com/Artexis10/exomem/commit/d304997dd3d4d68de61859966eb501af947b23e6))
* **hosted:** complete private multi-tenant beta infrastructure ([61eec3f](https://github.com/Artexis10/exomem/commit/61eec3fdf62ecfa00d8cb91ba3ea73573eec9242))
* **hosted:** harden the owner-canary deployment path ([#235](https://github.com/Artexis10/exomem/issues/235)) ([5700254](https://github.com/Artexis10/exomem/commit/5700254db0cc4e47f9754c1c4d02811a1ff0eae8))
* **hosted:** implement live provider lifecycle ([48dd56b](https://github.com/Artexis10/exomem/commit/48dd56be2467f9128d868e99a0ea28de1a1c4ad6))
* implement durable hosted provisioner core ([8dd1b08](https://github.com/Artexis10/exomem/commit/8dd1b0818d10ba8c15f52d28677d7cd0dfe79df5))
* implement hosted runtime operator and restore ([719ba29](https://github.com/Artexis10/exomem/commit/719ba29d28bceb8f4439aff6ef0d728efde69939))
* implement hosted transfer v2 runtime ([5b39c7d](https://github.com/Artexis10/exomem/commit/5b39c7dd5ef04e72bea6c7886684ae6d9715c60e))
* implement split-state hosted foundation ([c24420a](https://github.com/Artexis10/exomem/commit/c24420ae2ec1df5919ac69ab0bbbb4926143fa55))
* process media with timestamped transcripts ([1e44642](https://github.com/Artexis10/exomem/commit/1e446424eefba5f60092dfc3dfb5654ed7e1b846))
* reconcile governed media artifacts ([f228434](https://github.com/Artexis10/exomem/commit/f2284343b5d64a076ffcd1149dfec81081c1d434))
* retain actionable media job state ([32369ba](https://github.com/Artexis10/exomem/commit/32369bad6252bd42004dc6d80996124ff5f72c4d))
* scaffold hosted infrastructure delivery ([2d4c450](https://github.com/Artexis10/exomem/commit/2d4c450e3a3a25d153e429a063fd6e09538dd3ed))
* wire automatic media reconciliation ([014fbf1](https://github.com/Artexis10/exomem/commit/014fbf1168a4547d12a73529dc3346a32e7f02c9))


### Bug Fixes

* accept browser safelisted upload preflights ([c255ffb](https://github.com/Artexis10/exomem/commit/c255ffb2dfcd7bc470372d4efa0e8a11b00f0640))
* align memory citation link guidance ([ac2a692](https://github.com/Artexis10/exomem/commit/ac2a692b66681ec4b09eac8195864d10de4eb99e))
* bind Windows batch timestamps to file handles ([5377ce6](https://github.com/Artexis10/exomem/commit/5377ce65d89dac3254584d9e9711cc2cbb5ecc12))
* bound rotating media discovery ([82aa88f](https://github.com/Artexis10/exomem/commit/82aa88f7be4af3ba2293f8183c70391a1304ede0))
* **ci:** canonicalize Terraform validator path ([1fe3cb9](https://github.com/Artexis10/exomem/commit/1fe3cb93418d317701b33d89353bb702bb35351e))
* **ci:** create validator install directory ([c985145](https://github.com/Artexis10/exomem/commit/c985145e385e1efa143fe54b66a3c4597eeb32d2))
* **ci:** install pinned Trivy release artifact ([657ef1c](https://github.com/Artexis10/exomem/commit/657ef1c502de501ce1395e950e0bcc05119f6e62))
* classify hosted command admission per invocation ([d925642](https://github.com/Artexis10/exomem/commit/d925642361026089a9e4ff758e3e9dadf5a8e3ba))
* clean Windows batch stages after failed replace ([6031060](https://github.com/Artexis10/exomem/commit/603106003c4149c2c28a233c2468edc1cba2dc8c))
* close automatic media recovery gaps ([4116aa9](https://github.com/Artexis10/exomem/commit/4116aa921e2a0a91a733e53efb136212b2afe51c))
* close hosted admission escape hatches ([6898181](https://github.com/Artexis10/exomem/commit/689818156e3690442ea7e28498c5e7d2bebbd4b0))
* close hosted platform review blockers ([f9a3cca](https://github.com/Artexis10/exomem/commit/f9a3cca98428a12a44b76d551724a53aaf8e7fc6))
* close hosted runtime admission escapes ([14ecbbf](https://github.com/Artexis10/exomem/commit/14ecbbff0010a21634f1bda4f6c15e0b4a038891))
* close provisioner PostgreSQL safety gaps ([a8fa7ff](https://github.com/Artexis10/exomem/commit/a8fa7ff68851305634bcc1294b68724ea14643bb))
* close supersession staging cleanup gaps ([78ef2c7](https://github.com/Artexis10/exomem/commit/78ef2c7251841fd0fddfe8a245d723a1359895ce))
* compare transcript sidecars by raw bytes ([8bd4532](https://github.com/Artexis10/exomem/commit/8bd453232e41552e11db0760e9a69974c6bf1d04))
* constrain provisioner trusted proxies ([3a78353](https://github.com/Artexis10/exomem/commit/3a783536493f55bdfd6d4a5065f8d6b785743345))
* derive lease selector defaults from leaves ([cafee99](https://github.com/Artexis10/exomem/commit/cafee99f31671dc02d141ed2afad1cae17cc8c04))
* emit scheduler failures on transport errors ([f4edfa2](https://github.com/Artexis10/exomem/commit/f4edfa2e8bcf9b50c4064b39a675df96eb3d1672))
* freeze pod spec during job finalization ([1b4ed16](https://github.com/Artexis10/exomem/commit/1b4ed162071f32493e281b24531e81999aaa6646))
* guard explicit media retries ([d679b29](https://github.com/Artexis10/exomem/commit/d679b291c24643c178cf1fc7e7ed81d7033faf5c))
* harden automatic media reconciliation ([ff1b7fb](https://github.com/Artexis10/exomem/commit/ff1b7fbcfcbb1e0194a9115e5f697d262a370323))
* harden hosted Helm deployment boundary ([42d4bf9](https://github.com/Artexis10/exomem/commit/42d4bf9454ddc4b1ff793c22932c5e5214435217))
* harden hosted platform operations ([32c56ab](https://github.com/Artexis10/exomem/commit/32c56abcb6cfea5c10cf34a1cdbf37669aa77f48))
* harden hosted restore lifetime ownership ([596a138](https://github.com/Artexis10/exomem/commit/596a1385320299f9ca3c7033eaa19510ea2c9f5b))
* harden hosted secret handoff ([6f719a0](https://github.com/Artexis10/exomem/commit/6f719a093c716b4a8eb33c96a60b9d07093ee17b))
* harden media processing outcomes ([13a796b](https://github.com/Artexis10/exomem/commit/13a796b609bb30be4db95bb92fe0b907212c6773))
* harden media reconciliation convergence ([bb9a512](https://github.com/Artexis10/exomem/commit/bb9a512427e8939b8bbf58284aa7fa2cd7a19bbb))
* harden provisioner fencing and leases ([2ea5603](https://github.com/Artexis10/exomem/commit/2ea560364fd29d29f01a3cdcbfe94fa4f1b0ca06))
* harden writer fencing and supersession atomicity ([4c21edf](https://github.com/Artexis10/exomem/commit/4c21edf8d4cfc0ebcd9bcfb1e64afc0760581cb8))
* heal orphan lexical index rows ([bc56ff0](https://github.com/Artexis10/exomem/commit/bc56ff0da91c8972164fa35cd25d841692f7e4c9))
* **hosted:** distinguish expired export requests ([9e27b4c](https://github.com/Artexis10/exomem/commit/9e27b4cba8a1f546d7bf51c4c84abe9aa9ca6324))
* **hosted:** harden provider lifecycle boundaries ([0c5c29e](https://github.com/Artexis10/exomem/commit/0c5c29e845b7474b810291b73fe11306ddba461e))
* **hosted:** tighten integrated release validation ([0915135](https://github.com/Artexis10/exomem/commit/0915135835c066f2e4c753df5fdfed033048ceb5))
* ignore private batch workspaces during retrieval ([7e81131](https://github.com/Artexis10/exomem/commit/7e811318b204edea62189a89572b273277cbf6bf))
* include project registration in supersession batch ([7274ad1](https://github.com/Artexis10/exomem/commit/7274ad10bc304afcc66a72c482c2f08f1124cc99))
* keep frontmatter cache content-fresh ([80eb668](https://github.com/Artexis10/exomem/commit/80eb668c0923777d1aa173c6405aa5aa4df0b2b1))
* keep frontmatter cache content-fresh ([c0efb69](https://github.com/Artexis10/exomem/commit/c0efb6935cc41d4b97a58e991a0fe4e249f07016))
* keep hosted runtime import-safe on Windows ([b085814](https://github.com/Artexis10/exomem/commit/b0858148c9dfe503d79f1f4ee9f7e862d1ce8bbf))
* keep pending sidecar CAS in text hash domain ([cb3167a](https://github.com/Artexis10/exomem/commit/cb3167a894f0db9a71284f4749f8d076515b14b4))
* lock hosted platform admission policy ([95c3c24](https://github.com/Artexis10/exomem/commit/95c3c248ca123e749027025fa94d98c2eaa551e8))
* make export release cleanup replay-safe ([54618b9](https://github.com/Artexis10/exomem/commit/54618b931dec8f0ad053dce48dd80cc36c95c549))
* make supersession atomic ([a423fc0](https://github.com/Artexis10/exomem/commit/a423fc0be46a382ab84da4e67f7632a85dd971d2))
* persist actionable stale transcription failures ([4614964](https://github.com/Artexis10/exomem/commit/46149642c59be93b6b5496a4836ed4a9d2bd7384))
* preserve and govern automatic media processing ([0fbf6de](https://github.com/Artexis10/exomem/commit/0fbf6dedb13dcc5e0f5049108d59af7bd9096d00))
* preserve completed transcripts on media failure ([2c96a66](https://github.com/Artexis10/exomem/commit/2c96a66e971eb807550984dfddf3e54f878b5ab9))
* preserve concurrent transcripts during stale recovery ([3b81a29](https://github.com/Artexis10/exomem/commit/3b81a29e808520539ac163af5e4037c5439be5f3))
* preserve full index retry ledger ([ba69f9c](https://github.com/Artexis10/exomem/commit/ba69f9c7c517bdbcf8b9702691b1535e8f722d06))
* preserve legacy completed media ([48a4534](https://github.com/Artexis10/exomem/commit/48a453497eb3a6c1910ddf7e438332ba47b06f13))
* preserve user ACL inheritance for Windows service writes ([a7cd43a](https://github.com/Artexis10/exomem/commit/a7cd43a951a75512195900b47bd8aabce45001ef))
* prevent uv lockfile drift ([#239](https://github.com/Artexis10/exomem/issues/239)) ([b16e8d4](https://github.com/Artexis10/exomem/commit/b16e8d4854a130d7bbfddde8cd23fdd01ebf1d85))
* probe selected hosted protocol ([6472367](https://github.com/Artexis10/exomem/commit/6472367af041ff38bf0b343a02609c1362b6b9a5))
* reconcile aggregate media retries ([c7c8911](https://github.com/Artexis10/exomem/commit/c7c89113049bccf82cc965b79a6e39a7e2af88a4))
* reconcile malformed completed provenance ([59b8037](https://github.com/Artexis10/exomem/commit/59b80378ee54a1170c4cf1990c45c61d95222958))
* reject abbreviated Ansible secret overrides ([4b83b74](https://github.com/Artexis10/exomem/commit/4b83b74bc3e46d04338454bb1a203ce9986dddcd))
* reject legacy credentials in v2 cells ([278dae6](https://github.com/Artexis10/exomem/commit/278dae67ffbd677a5f5a7d05d39924db464b1de9))
* retry stable transcript commits and surface sidecar conflicts ([4b2cc59](https://github.com/Artexis10/exomem/commit/4b2cc59735a4871a5fd9653bf0dc70e5ef46d7c5))
* scope writer lease to write operations ([7454be5](https://github.com/Artexis10/exomem/commit/7454be574ff226824d7e823bcaa0fef0f5fef1de))
* support guarded batch writes on Windows ([f5fad5a](https://github.com/Artexis10/exomem/commit/f5fad5ae3a5eff3268b9075bdb7afa4f648b2d59))
* verify Windows media identity consistently ([026b552](https://github.com/Artexis10/exomem/commit/026b5522a9ebf4391ca70d8dab57bf821f7f6f41))


### Performance

* index media discovery lookups ([9d463fa](https://github.com/Artexis10/exomem/commit/9d463fad46fa1906d56373ef8aa757abbc767be3))

## [0.22.0](https://github.com/Artexis10/exomem/compare/v0.21.0...v0.22.0) (2026-07-13)


### Features

* **auth:** add durable local session authority ([256160f](https://github.com/Artexis10/exomem/commit/256160fa12ad66ff79538ec2ae1e2c843cc82ff8))
* **auth:** add durable session operator controls ([0c1ee62](https://github.com/Artexis10/exomem/commit/0c1ee627360afd17b12c16e9a7890b2f063f020c))
* **auth:** issue durable local OAuth sessions ([2b0ef8e](https://github.com/Artexis10/exomem/commit/2b0ef8ef3f38562c91afb33875cc944d7cbd09c9))


### Bug Fixes

* **auth:** close durable-session rollout gaps ([18e56be](https://github.com/Artexis10/exomem/commit/18e56be8ba59987c6b4f1deba1671b8465261a56))
* **auth:** close legacy OAuth escape paths ([4f7fec3](https://github.com/Artexis10/exomem/commit/4f7fec35b3c8639ed6ba51df2851ff12ebc38ef8))
* **auth:** close rollout harness verifier gaps ([ae4faa8](https://github.com/Artexis10/exomem/commit/ae4faa8d5cfbecdf8b2b0bd6cf7a069de69c892a))
* **auth:** harden durable session rollout controls ([f95fe0a](https://github.com/Artexis10/exomem/commit/f95fe0a665d56972b25d370504a9fb2e0356dc89))
* **auth:** harden session authority concurrency ([f2a633b](https://github.com/Artexis10/exomem/commit/f2a633b93c25c4f6725ce13e3210dbe5f21d7ef2))
* **auth:** issue durable local MCP sessions ([230a1c5](https://github.com/Artexis10/exomem/commit/230a1c53bbbd92f5ed9c903015a9a278d7e0d6f7))
* **auth:** preserve FastMCP DCR grant compatibility ([f92a35b](https://github.com/Artexis10/exomem/commit/f92a35b661894ee62636783727320199a045e038))
* **config:** load cli dotenv from working directory ([e6cb1bc](https://github.com/Artexis10/exomem/commit/e6cb1bc9f4e839205036247726f4bdc965737ff2))
* **config:** load packaged service dotenv from working directory ([e27766e](https://github.com/Artexis10/exomem/commit/e27766eaeb2394fab999c38698c5be052bc037bd))
* **config:** load service dotenv from working directory ([0404da7](https://github.com/Artexis10/exomem/commit/0404da750da5805da1ed991c5a76e289463f15d6))
* **ha:** complete durable state coordinator contract ([b2f109e](https://github.com/Artexis10/exomem/commit/b2f109e68549526a473608c847bac4ff86df16c7))
* **ha:** reject non-object state bodies ([fd9bbba](https://github.com/Artexis10/exomem/commit/fd9bbbae301544fa85ef8e9275161c08378bcbd4))

## [0.21.0](https://github.com/Artexis10/exomem/compare/v0.20.2...v0.21.0) (2026-07-12)


### Features

* gate HA failover on runtime readiness ([#221](https://github.com/Artexis10/exomem/issues/221)) ([f5aeb8c](https://github.com/Artexis10/exomem/commit/f5aeb8c812d65690e322f5b37a27f54a88de8e2c))

## [0.20.2](https://github.com/Artexis10/exomem/compare/v0.20.1...v0.20.2) (2026-07-12)


### Bug Fixes

* prevent HA replay of MCP tool calls ([#219](https://github.com/Artexis10/exomem/issues/219)) ([4edd81b](https://github.com/Artexis10/exomem/commit/4edd81b07e8324dd6f95a7bcf6384079abe571e1))

## [0.20.1](https://github.com/Artexis10/exomem/compare/v0.20.0...v0.20.1) (2026-07-12)


### Bug Fixes

* make remote MCP sessions restart-safe ([#217](https://github.com/Artexis10/exomem/issues/217)) ([daf8ea3](https://github.com/Artexis10/exomem/commit/daf8ea3700d947476fb13f7aeb11c6361ad7f811))

## [0.20.0](https://github.com/Artexis10/exomem/compare/v0.19.1...v0.20.0) (2026-07-12)


### Features

* add an unauthenticated /health liveness endpoint ([a2573d3](https://github.com/Artexis10/exomem/commit/a2573d370ef95ed814c32faf5646d3cb77da4159))


### Bug Fixes

* accept-relation creates the ## Relations section when a note lacks one ([3fd89f7](https://github.com/Artexis10/exomem/commit/3fd89f765f233ec2a2610e4ae44b4847e1bcdb68))
* enforce append-only immutability regardless of path casing ([3e20a1c](https://github.com/Artexis10/exomem/commit/3e20a1cd2d1ac94ca7a7ab528759bf4a7c28a212))
* enforce the no-confidence-floats / no-decay stance in the writers ([7492c01](https://github.com/Artexis10/exomem/commit/7492c015b46806b2f34a9fc9e73fc70d2fb13a76))
* exclude out-of-KB, readonly, and excluded targets from relation suggestions ([2fa0279](https://github.com/Artexis10/exomem/commit/2fa02793973e50d8f82d5e16fe7633660cc543e8))
* heal reconcile count drift by default via maintain_memory ([009170f](https://github.com/Artexis10/exomem/commit/009170ffb7307513d416c5bb88fa34db36113e6e))
* keep governed writes inside Knowledge Base/ and fail the backstop closed ([6f7245e](https://github.com/Artexis10/exomem/commit/6f7245e62e6bafb29d37b26b638412a629b8558b))
* make MCP mutations retry-safe ([22ab936](https://github.com/Artexis10/exomem/commit/22ab936666d82e395b507444cfcf66abc3ac9f53))
* make MCP mutations retry-safe ([cb0c47a](https://github.com/Artexis10/exomem/commit/cb0c47af757308d429e5167dfac48b60f6e32de1))
* write-governance & lifecycle hardening from the promise audit ([9e057b4](https://github.com/Artexis10/exomem/commit/9e057b45d5cb386c4a6a0f9f6f315ff976708fb9))

## [0.19.1](https://github.com/Artexis10/exomem/compare/v0.19.0...v0.19.1) (2026-07-12)


### Bug Fixes

* preserve Unicode titles and vault integrity ([#211](https://github.com/Artexis10/exomem/issues/211)) ([7a2ae4d](https://github.com/Artexis10/exomem/commit/7a2ae4ded475bbb7255bcd4a25adf264c4e63e13))
* stop forcing OAuth session expiry ([#212](https://github.com/Artexis10/exomem/issues/212)) ([3187fc8](https://github.com/Artexis10/exomem/commit/3187fc84578b220536a895cf6cf3439bd671f8e6))

## [0.19.0](https://github.com/Artexis10/exomem/compare/v0.18.0...v0.19.0) (2026-07-11)


### Features

* **benchmark:** add recall-visibility Exomem-only dimension ([9edff7b](https://github.com/Artexis10/exomem/commit/9edff7b26d534febcdf05be85a13d0650a6b51e4))
* **find:** graph-provenance annotation on typed-lane hits ([36ac75c](https://github.com/Artexis10/exomem/commit/36ac75c80e805f631cc1bee2b4717ddd58211f2b))
* **find:** join typed-graph sidecar token to hot-cache freshness key ([9bddfd5](https://github.com/Artexis10/exomem/commit/9bddfd5ce6fd0a7712a3b48cbcbf3dcd1847675d))
* **find:** typed-graph lane expansion with byte-identical fallback ([3817f00](https://github.com/Artexis10/exomem/commit/3817f002aea65d23996040cf9bfda7c2cc7aac5c))
* **graph:** batch neighbour read API + freshness generation token ([0f7df31](https://github.com/Artexis10/exomem/commit/0f7df31d39aee9e7ec0292decd7b44b775842f36))
* make replicated Exomem one failover-safe connector ([#207](https://github.com/Artexis10/exomem/issues/207)) ([67d7337](https://github.com/Artexis10/exomem/commit/67d7337df8cd63788066131dc44f55fcbaf5dab8))
* **review:** relation-acceptance queue assembly and filtering ([cb4dd07](https://github.com/Artexis10/exomem/commit/cb4dd071e110f82906b930fb0a4ba850e793e4ff))
* **review:** relation-queue command surface (review/accept/triage) ([b8c07d0](https://github.com/Artexis10/exomem/commit/b8c07d029f4ea150e46e8653973cc77ab489036c))
* **studio:** batched relation-acceptance queue panel ([31b058a](https://github.com/Artexis10/exomem/commit/31b058af798421b4ddf6eeb9e5a89d02a39cf7db))


### Bug Fixes

* **find:** expose graph provenance in compact hit serialization ([1111a8e](https://github.com/Artexis10/exomem/commit/1111a8e681a8be0b569bbb2f252080762cb84d52))
* **find:** family precedence before target dedup + vault-scope hybrid expansion ([a61ac8b](https://github.com/Artexis10/exomem/commit/a61ac8b61b765d2ac0cf6d238bc5084e04a09a14))
* **graph:** resolve semantic-block edges + deterministic same-family order ([c8d903a](https://github.com/Artexis10/exomem/commit/c8d903a22bb4a6ca803d1dae7be817ede5e8c7d7))
* **graph:** stop opening a write transaction on read connections ([190c8f7](https://github.com/Artexis10/exomem/commit/190c8f7abcaa1588c288ed0d4da688d4ac3d322f))
* identify coordinator requests through Cloudflare ([#208](https://github.com/Artexis10/exomem/issues/208)) ([7c93fb8](https://github.com/Artexis10/exomem/commit/7c93fb835b316adba630bb31a2472a8b364d4dac))
* normalize piped Worker secrets ([#209](https://github.com/Artexis10/exomem/issues/209)) ([143595f](https://github.com/Artexis10/exomem/commit/143595f7cef2cf21123dda6b10607663eff5e74b))
* **review:** fold candidate evidence into the relation fingerprint ([f814fe4](https://github.com/Artexis10/exomem/commit/f814fe4178083de2bf267e545cf59eaca5a3f036))
* **review:** require fingerprint on accept, re-validate live eligibility ([37f8d04](https://github.com/Artexis10/exomem/commit/37f8d0444143e4a01cfd35e021bb795ca33382de))
* **review:** stop relation-queue generation once limit_pages is reached ([b373fff](https://github.com/Artexis10/exomem/commit/b373ffff86f97592cae4b265bf424aaf4b22cfa7))
* **studio:** hide/disable Inbox+Activation filters in relation-queue mode ([4a193bf](https://github.com/Artexis10/exomem/commit/4a193bf62bba81ba0f15013ccbd3d9e4c257095c))

## [0.18.0](https://github.com/Artexis10/exomem/compare/v0.17.0...v0.18.0) (2026-07-11)


### Features

* add multi-host writer lease ([#201](https://github.com/Artexis10/exomem/issues/201)) ([5b96122](https://github.com/Artexis10/exomem/commit/5b9612282f24922bd1d7a627492b27081847f5d4))


### Bug Fixes

* flush MCP SSE sessions immediately ([#205](https://github.com/Artexis10/exomem/issues/205)) ([4c3a843](https://github.com/Artexis10/exomem/commit/4c3a843afc04301285e7cdf07917cc96db222ad2))
* restore fast reference enrichment ([#204](https://github.com/Artexis10/exomem/issues/204)) ([3c65563](https://github.com/Artexis10/exomem/commit/3c655636558c4ba33a806be3e22352713e50d2f6))

## [0.17.0](https://github.com/Artexis10/exomem/compare/v0.16.2...v0.17.0) (2026-07-11)


### Features

* activate and prove the governed graph ([#198](https://github.com/Artexis10/exomem/issues/198)) ([79fb147](https://github.com/Artexis10/exomem/commit/79fb1476270743fbfe0c41647e7fb362c41f30d9))
* add Epistemic Review Studio ([#200](https://github.com/Artexis10/exomem/issues/200)) ([9ec3805](https://github.com/Artexis10/exomem/commit/9ec3805d49e8fd478a95d6c5fe22561f407849b8))

## [0.16.2](https://github.com/Artexis10/exomem/compare/v0.16.1...v0.16.2) (2026-07-10)


### Bug Fixes

* preserve protected trees during link migration ([#195](https://github.com/Artexis10/exomem/issues/195)) ([f17d139](https://github.com/Artexis10/exomem/commit/f17d139b16a1e1424f8e15723c6082b7f4e00e9e))

## [0.16.1](https://github.com/Artexis10/exomem/compare/v0.16.0...v0.16.1) (2026-07-10)


### Bug Fixes

* render wikilinks for Obsidian vault root ([#193](https://github.com/Artexis10/exomem/issues/193)) ([7becced](https://github.com/Artexis10/exomem/commit/7becced6e9470d5853b922d4f47b007c0cc31906))

## [0.16.0](https://github.com/Artexis10/exomem/compare/v0.15.0...v0.16.0) (2026-07-10)


### Features

* add Epistemic Inbox and typed relations ([#190](https://github.com/Artexis10/exomem/issues/190)) ([751aed1](https://github.com/Artexis10/exomem/commit/751aed1258c0473e6b5ec54cf18083a3cecac45a))
* add governed epistemic relation registry ([#191](https://github.com/Artexis10/exomem/issues/191)) ([cd2163d](https://github.com/Artexis10/exomem/commit/cd2163d7636e9729c1c20afdf698b893eb56c6cc))

## [0.15.0](https://github.com/Artexis10/exomem/compare/v0.14.0...v0.15.0) (2026-07-10)


### Features

* close technical memory gaps ([#182](https://github.com/Artexis10/exomem/issues/182)) ([3a5cb55](https://github.com/Artexis10/exomem/commit/3a5cb55f8f5fa33852437827da794dd8416788c1))
* make multimodal capability resource-bounded by default ([#180](https://github.com/Artexis10/exomem/issues/180)) ([cb388bb](https://github.com/Artexis10/exomem/commit/cb388bbcec36eac64e12a31339dbb8b67ab006c0))

## [0.14.0](https://github.com/Artexis10/exomem/compare/v0.13.0...v0.14.0) (2026-07-09)


### Features

* add one-command native release service bootstrap ([#179](https://github.com/Artexis10/exomem/issues/179)) ([9c663ed](https://github.com/Artexis10/exomem/commit/9c663edb0ff701921ffab393098bc4fc0133ea54))


### Bug Fixes

* validate product command adoption flow ([#176](https://github.com/Artexis10/exomem/issues/176)) ([ff22274](https://github.com/Artexis10/exomem/commit/ff22274b24eaf6a377264ec61380ca32278d7584))

## [0.13.0](https://github.com/Artexis10/exomem/compare/v0.12.0...v0.13.0) (2026-07-09)


### Features

* add epistemic graph sidecar ([e508eac](https://github.com/Artexis10/exomem/commit/e508eac6e40a435e824f99d3aa879c03552a4cbd))
* add epistemic graph sidecar ([021dab9](https://github.com/Artexis10/exomem/commit/021dab9179fdd96187c3548b9976f169df8e9f10))
* add simple command surface ([8b7fd8c](https://github.com/Artexis10/exomem/commit/8b7fd8cc2fc9dbf3c18023872f1b8f28932dbe40))
* redesign product command surface ([#174](https://github.com/Artexis10/exomem/issues/174)) ([c549c50](https://github.com/Artexis10/exomem/commit/c549c50ad34bb58ca2d230b09d24c7499bb84514))
* simplify command surface ([59818dc](https://github.com/Artexis10/exomem/commit/59818dc0236da19747364ca5c7c82e66487d32cc))


### Bug Fixes

* harden mac imports and adoption ([#172](https://github.com/Artexis10/exomem/issues/172)) ([c25f7ab](https://github.com/Artexis10/exomem/commit/c25f7ab0a96779b9f0a1a0a05355f83cc36026f1))

## [0.12.0](https://github.com/Artexis10/exomem/compare/v0.11.0...v0.12.0) (2026-07-07)


### Features

* **deploy:** add CUDA container setup path ([e533570](https://github.com/Artexis10/exomem/commit/e5335707edcd796faeeae735b5744a7f11ac5096))
* **hooks:** add install health check ([ccb6f17](https://github.com/Artexis10/exomem/commit/ccb6f1790263751a678d4fec23cc8d4d21636d0e))
* **hooks:** support Codex install target ([#148](https://github.com/Artexis10/exomem/issues/148)) ([f3d1e41](https://github.com/Artexis10/exomem/commit/f3d1e4166f967d02b0af3300489f9e9e284200f5))
* **resource:** complete low-interrupt quiet mode ([#140](https://github.com/Artexis10/exomem/issues/140)) ([7153f10](https://github.com/Artexis10/exomem/commit/7153f101cbeb0fdf8ed992074a36521a07cdc821))


### Bug Fixes

* **hooks:** suppress retrieval nudge on control prompts ([#142](https://github.com/Artexis10/exomem/issues/142)) ([d93d856](https://github.com/Artexis10/exomem/commit/d93d856aacc51814d4b5309abc8a421f853e5071))

## [0.11.0](https://github.com/Artexis10/exomem/compare/v0.10.0...v0.11.0) (2026-07-06)


### Features

* **agent:** add portable bootstrap contract ([baea674](https://github.com/Artexis10/exomem/commit/baea674a16f2919bf276b485e13464725ab93713))

## [0.10.0](https://github.com/Artexis10/exomem/compare/v0.9.0...v0.10.0) (2026-07-06)


### Features

* **compute:** CPU-default device policy + quiet/normal/performance modes (PR1: idle-VRAM kill) ([#130](https://github.com/Artexis10/exomem/issues/130)) ([452a558](https://github.com/Artexis10/exomem/commit/452a5582ccdf7cd8152aa7b435f18cfd87d0eec2))
* **compute:** exomem mode CLI + live switch + GPU-detection prompt (PR4) ([#134](https://github.com/Artexis10/exomem/issues/134)) ([e97ae7c](https://github.com/Artexis10/exomem/commit/e97ae7c95a26bb71e87fc90f42c5b2b3d8104a7d))
* **compute:** extend CPU-default device policy to ASR + diarizer + bulk-index (PR2) ([#132](https://github.com/Artexis10/exomem/issues/132)) ([e611610](https://github.com/Artexis10/exomem/commit/e611610dba445b2f90346e8e4437811ee45f9a3b))
* **compute:** idle-unload subsystem — reclaim resident models after N idle minutes (PR3) ([#133](https://github.com/Artexis10/exomem/issues/133)) ([0a49d45](https://github.com/Artexis10/exomem/commit/0a49d45370f15f610419349810ae6d6406aa50c3))
* **compute:** reranker off/configurable + lite profile + compute-knob docs (PR5) ([#135](https://github.com/Artexis10/exomem/issues/135)) ([e6c1fe2](https://github.com/Artexis10/exomem/commit/e6c1fe2df20f7bc10b390c493816e8709fcc9150))


### Bug Fixes

* **compute:** machine-wide config path so the service + CLI share it — cross-user live-switch (PR6) ([#136](https://github.com/Artexis10/exomem/issues/136)) ([b89deb8](https://github.com/Artexis10/exomem/commit/b89deb8c50849d3495e4a21374ab6d911fc7b534))

## [0.9.0](https://github.com/Artexis10/exomem/compare/v0.8.0...v0.9.0) (2026-07-05)


### Features

* **log:** size-triggered log.md rotation into _archive/logs/ ([a20caa0](https://github.com/Artexis10/exomem/commit/a20caa0dc573745f2d5833a7f6631e14e53e29a2))
* **vecstore:** numpy is the default vector backend; sqlite-vec becomes explicit opt-in (OpenSpec: make-sqlite-vec-opt-in) ([#128](https://github.com/Artexis10/exomem/issues/128)) ([ee5e744](https://github.com/Artexis10/exomem/commit/ee5e7446fee122f0de1b5dc93546d5eef0f91641))


### Bug Fixes

* **ci:** regenerate stale capabilities doc; generator writes LF ([ade69c6](https://github.com/Artexis10/exomem/commit/ade69c694b6dade896d85268a1ebdf6e7d9ae7f0))
* **freshness:** canonicalize event-path registry keys — Windows 8.3 aliases dropped live notes from event-maintained indexes ([#129](https://github.com/Artexis10/exomem/issues/129)) ([5f29525](https://github.com/Artexis10/exomem/commit/5f29525656fae928af914c62298b3bef577ee706))
* **indexes:** excluded scan dirs stay excluded on the incremental paths ([dca70d5](https://github.com/Artexis10/exomem/commit/dca70d56b05bda040f5bf33c11d14bdb2aa3b9ea))
* PyPI SETUP-LOCAL link + hooks recognise the renamed `exomem` tools ([#118](https://github.com/Artexis10/exomem/issues/118)) ([00c00d2](https://github.com/Artexis10/exomem/commit/00c00d2cccdd4714a8f689d9d62164c0a3fffb44))
* **scripts:** install-service no-UAC grant failed silently while claiming success ([b672efa](https://github.com/Artexis10/exomem/commit/b672efaae592c11b7b04e872fbde64c4655cb06a))
* **writers:** reuse the shared freshness-checked WikilinkResolver instead of rebuilding per write ([8459def](https://github.com/Artexis10/exomem/commit/8459defc8a06e538b5f4187ca873d4c1197332ff))


### Performance

* **claims:** key the claim cache on the shared write-generation token ([#127](https://github.com/Artexis10/exomem/issues/127)) ([a04bcf7](https://github.com/Artexis10/exomem/commit/a04bcf7f2ad667275ac617bfb87291c7d29c78c3))
* **embeddings:** key the matrix caches on a write generation, not the WAL-checkpoint mtime ([#125](https://github.com/Artexis10/exomem/issues/125)) ([07e23cf](https://github.com/Artexis10/exomem/commit/07e23cf9645867bef0be9fdcf9a8540a9c5f2219))
* **embeddings:** numpy-lite — the matrix cache holds no chunk text ([557dcf9](https://github.com/Artexis10/exomem/commit/557dcf9b13cb1b1f8bdf6b54f7677c0e115abd82))
* **freshness:** reconcile dispatches the drift delta through the event fan-out ([#124](https://github.com/Artexis10/exomem/issues/124)) ([ecca095](https://github.com/Artexis10/exomem/commit/ecca09516d59ff1a3d6cba6ee2b2409eaa60ac98))
* **lexstore:** heal from the freshness registry, not a filesystem walk ([#122](https://github.com/Artexis10/exomem/issues/122)) ([5b887e5](https://github.com/Artexis10/exomem/commit/5b887e580f9e4664dd4a67344ce5161f6bcf0acf))
* **lexstore:** incremental heal — patch only drifted rows, not a full O(corpus) rebuild ([#121](https://github.com/Artexis10/exomem/issues/121)) ([5415c3d](https://github.com/Artexis10/exomem/commit/5415c3daf326970d9330291575e909611832d327))
* **lexstore:** route the _walk_matches_rows verify path through the registry too ([#123](https://github.com/Artexis10/exomem/issues/123)) ([d579df0](https://github.com/Artexis10/exomem/commit/d579df00c0ea3d29f09376356ca0d63348ff2f47))
* **note:** overlap the two corpus-aware passes; add suggestions= knob (default ON) ([eef4523](https://github.com/Artexis10/exomem/commit/eef4523a3b08232e77925b8251376ae575f97067))
* **yaml:** parse frontmatter via libyaml CSafeLoader (measured 6.9x) ([e3909ab](https://github.com/Artexis10/exomem/commit/e3909ab2e53377ae1e0e488538456b10966deb67))

## [0.8.0](https://github.com/Artexis10/exomem/compare/v0.7.0...v0.8.0) (2026-07-04)


### Features

* configurable governed-folder name via EXOMEM_KB_DIRNAME ([#116](https://github.com/Artexis10/exomem/issues/116)) ([9fade24](https://github.com/Artexis10/exomem/commit/9fade241a896ae2837ccb4dc389ae0a99d69c11d))

## [0.7.0](https://github.com/Artexis10/exomem/compare/v0.6.0...v0.7.0) (2026-07-04)


### Features

* **skill:** exomem rename + self-personalizing, markdown-first, single-sourced skill ([#114](https://github.com/Artexis10/exomem/issues/114)) ([91e3c66](https://github.com/Artexis10/exomem/commit/91e3c66eb4a9b30c988807c2650fa54a8607c7c9))

## [0.6.0](https://github.com/Artexis10/exomem/compare/v0.5.0...v0.6.0) (2026-07-04)


### Features

* **bench:** per-lane latency-vs-scale curve + regression gate + golden set 9→26 ([e3f77f6](https://github.com/Artexis10/exomem/commit/e3f77f6dbda0272d4df901f59f3a1c85d9524392))
* FTS5 lexical backend — indexed bm25/keyword lanes + graph-lane scaling fix (OpenSpec: add-fts5-lexical-backend) ([#113](https://github.com/Artexis10/exomem/issues/113)) ([ba846a6](https://github.com/Artexis10/exomem/commit/ba846a60b37a913daa397c1801346be8d26c5c9c))
* sqlite-vec vec0 vector backend inside the embedding sidecars (OpenSpec: add-sqlite-vec-backend) ([#111](https://github.com/Artexis10/exomem/issues/111)) ([40fc0dd](https://github.com/Artexis10/exomem/commit/40fc0dd1b0b87bc31abd776350ef3fbd41281227))

## [0.5.0](https://github.com/Artexis10/exomem/compare/v0.4.1...v0.5.0) (2026-07-03)


### Features

* add-tunnel-hostname.ps1 — alias a second hostname onto a live tunnel ([c27c74e](https://github.com/Artexis10/exomem/commit/c27c74e5d7107f331fca1a6e199963d10729a721))
* claim-level contradiction hygiene (proximity -&gt; polarity), off by default ([681ed16](https://github.com/Artexis10/exomem/commit/681ed16a7a3d17e3fc072e01b430b6f48ae38b66))
* event-maintained freshness + inbound index — kill the per-request vault walk ([#96](https://github.com/Artexis10/exomem/issues/96)) ([6dc883c](https://github.com/Artexis10/exomem/commit/6dc883c24040542062258fa6149ad071bc0c5e96))
* exomem E monogram — brand the MCP icon + serve a domain favicon ([88e11f5](https://github.com/Artexis10/exomem/commit/88e11f5fdb7c99ad50896a8ac590f5eabe10a07a))
* exomem setup --remote guided remote-connector wizard ([e161b50](https://github.com/Artexis10/exomem/commit/e161b50edba578c7df90c8c64676bc27bdca729c))
* first-class macOS GPU acceleration — MPS embeddings + mlx-whisper ASR ([#95](https://github.com/Artexis10/exomem/issues/95)) ([f5e69ab](https://github.com/Artexis10/exomem/commit/f5e69ab2c18582baec6b60e9063c014442ed13e3))
* golden retrieval regression gate + silent-degradation alarm ([55e61fd](https://github.com/Artexis10/exomem/commit/55e61fd99dbd4c7f5b0d9519b33c04cb88314a58))
* one-line macOS/Linux bootstrap script (scripts/install.sh) ([bd9e2c3](https://github.com/Artexis10/exomem/commit/bd9e2c34fb14d00bc215638ba3a40fd81bef9648))
* opt-in retrieve-and-inject hook (KB_RETRIEVE_INJECT) ([#99](https://github.com/Artexis10/exomem/issues/99)) ([7dad3ae](https://github.com/Artexis10/exomem/commit/7dad3ae8761f84cd36fa50434c2479a2e33904ac))
* opt-in whole-vault semantic index (EXOMEM_INDEX_SCOPE) ([8b9e89e](https://github.com/Artexis10/exomem/commit/8b9e89e649ffdc06af212891c5bdfa552a824d68))
* publish reproducible retrieval benchmark report ([#100](https://github.com/Artexis10/exomem/issues/100)) ([d783a87](https://github.com/Artexis10/exomem/commit/d783a87ae7409601ee7d818b4caa0bc7c9fabdad))


### Bug Fixes

* **find:** a missing embeddings extra is a deployment shape, not a degradation ([cd10117](https://github.com/Artexis10/exomem/commit/cd101176ffb851dc6d3605b161aaa46a6f9c7d58))
* harden backfill --retime — warm bge before diarization, never strip speaker labels ([#97](https://github.com/Artexis10/exomem/issues/97)) ([c4950b8](https://github.com/Artexis10/exomem/commit/c4950b83a968984ed1ae19b96ecd53c50ddd84ef))


### Performance

* event-maintain the wikilink resolver so the graph lane stays warm ([20b4a31](https://github.com/Artexis10/exomem/commit/20b4a3122c50652245675419b53a5f37e4367000))
* **find:** decouple vector-lane latency from sidecar write churn ([#93](https://github.com/Artexis10/exomem/issues/93)) ([bbe9ec6](https://github.com/Artexis10/exomem/commit/bbe9ec68b8568f8e303128b7fd477b8ffe749742))
* run bge/CLIP in fp16 on Apple Silicon (MPS) ([#104](https://github.com/Artexis10/exomem/issues/104)) ([8552835](https://github.com/Artexis10/exomem/commit/85528357506a952b94527a9358db3a4306116fac))
* warm an encode at boot so the first query doesn't pay kernel compile ([#101](https://github.com/Artexis10/exomem/issues/101)) ([420e1c4](https://github.com/Artexis10/exomem/commit/420e1c4a74dfb47cf385cd57e6b34dfcb4f4b9a7))

## [0.4.1](https://github.com/Artexis10/exomem/compare/v0.4.0...v0.4.1) (2026-07-02)


### Bug Fixes

* diarization soft-fail boundary guard + thread vault_root to named attribution ([c5bc82c](https://github.com/Artexis10/exomem/commit/c5bc82c3667b317799c5a66fe93a68a88ab8f0a7))

## [0.4.0](https://github.com/Artexis10/exomem/compare/v0.3.0...v0.4.0) (2026-07-02)


### Features

* Docker distribution — lean/ml images, compose with tunnel profiles, gated GHCR publish ([#90](https://github.com/Artexis10/exomem/issues/90)) ([88286b2](https://github.com/Artexis10/exomem/commit/88286b2d91c80914def999db894aa7d56bfdd5cb))
* lexical-first instant start — non-blocking boot, background warm, readiness defer gates ([#86](https://github.com/Artexis10/exomem/issues/86)) ([3e42418](https://github.com/Artexis10/exomem/commit/3e424183c539de851684bb7dd373963e35e7ed89))
* packaged `exomem demo` + wheel-path onboarding gate — prove value in 30 seconds ([#87](https://github.com/Artexis10/exomem/issues/87)) ([8056308](https://github.com/Artexis10/exomem/commit/8056308bcf7e533f98a19c5cbb3a76c7135edffa))
* remote connector quickstart — doctor --probe, ngrok no-domain path, ingress docs rework ([#89](https://github.com/Artexis10/exomem/issues/89)) ([33084b0](https://github.com/Artexis10/exomem/commit/33084b07f354458be0d9e472a786494053c76417))
* semantic video segments — timed transcripts, fused topic segmentation, transcript_match_at ([#88](https://github.com/Artexis10/exomem/issues/88)) ([5561ec1](https://github.com/Artexis10/exomem/commit/5561ec159929e4301ca57bb09b6b3913d344232c))


### Bug Fixes

* **cli:** first-run polish — entry points target exomem, warm names the missing extra ([#91](https://github.com/Artexis10/exomem/issues/91)) ([c7971f1](https://github.com/Artexis10/exomem/commit/c7971f1d421eb993281bdba2a05fa70b1fb1db8f))

## [0.3.0](https://github.com/Artexis10/exomem/compare/v0.2.1...v0.3.0) (2026-07-02)


### ⚠ BREAKING CHANGES

* canonical import name is exomem and canonical env prefix is EXOMEM_*. kb_mcp imports and KB_MCP_* env vars remain supported aliases.
* `get` no longer returns `content` by default; pass include_raw=true for the raw file text. `body`, `frontmatter`, `content_hash`, and `mtime` are unchanged.

### Features

* `exomem setup` — one-command guided local onboarding ([9a679e4](https://github.com/Artexis10/exomem/commit/9a679e4c209fed914006535e7824c079df909499))
* complete the exomem rename — package, env vars, docs, with permanent kb_mcp compatibility ([#81](https://github.com/Artexis10/exomem/issues/81)) ([9f30990](https://github.com/Artexis10/exomem/commit/9f30990e2201f3cdad27002195a73ce0ef6b8ea2))
* find perf overhaul, opt-in usage-aware ranking, get payload dedup ([2e9f753](https://github.com/Artexis10/exomem/commit/2e9f75374f9cbd7e66bfa22b9e73aaa5077114aa))
* find timing diagnostics, compact detail, hot cache, watcher echo suppression ([4d3d51a](https://github.com/Artexis10/exomem/commit/4d3d51af0999c5e6b6be5364802708efffb26dbf))
* get_video_frames — on-demand inline video keyframes over MCP ([1c0294e](https://github.com/Artexis10/exomem/commit/1c0294e6ca9eac58675e5e31910e289f3eb1ffeb))
* make diarization first-class — rediarize backfill, boot readiness line, truthy env gate ([6f6978b](https://github.com/Artexis10/exomem/commit/6f6978bd53e90f489a1c451064276c7ae878c758))
* read-only vault `overview` op — bounded structure report ([34373aa](https://github.com/Artexis10/exomem/commit/34373aaf63ceaef270a49201ae78c73175d9242a))
* video scene detection + persisted, OCR'd scene frames ([#80](https://github.com/Artexis10/exomem/issues/80)) ([4a009db](https://github.com/Artexis10/exomem/commit/4a009dbf19d988dbefe30f330e81f724010845b8))


### Bug Fixes

* re-promote legacy KB_MCP_* env vars after server-side load_dotenv ([#82](https://github.com/Artexis10/exomem/issues/82)) ([473cef7](https://github.com/Artexis10/exomem/commit/473cef799960e9c28cd4d6fb57b413eb8b802caa))

## [0.2.1](https://github.com/Artexis10/exomem/compare/v0.2.0...v0.2.1) (2026-07-01)


### Bug Fixes

* sync package version for release artifacts ([5a3b75b](https://github.com/Artexis10/exomem/commit/5a3b75b0e67c9d244a8fecff6c7960d898a08a89))

## [0.2.0](https://github.com/Artexis10/exomem/compare/v0.1.0...v0.2.0) (2026-07-01)


### Features

* rename project to exomem ([74cb3a0](https://github.com/Artexis10/exomem/commit/74cb3a035a7b009c4b720cc53b3e7c72feda2a5f))

## 0.1.0 (2026-07-01)

### Features

* initial public source release baseline
