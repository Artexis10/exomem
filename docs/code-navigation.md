# Optional code navigation with Graft

Graft provides a local structural map for unfamiliar cross-file questions. Use
it when a caller graph or file API would narrow the next source read. Exact
text searches and small, familiar edits usually need only `rg` or `ast-grep`.

Install the smoke-tested CLI once with Node.js 20 or newer:

```sh
npm install --global @nanonets/graft@0.16.0
graft --version
```

The commands below use Bash (WSL on Windows). From the current task worktree,
build its own index:

```sh
DO_NOT_TRACK=1 graft build --no-gitignore --no-ignore
```

The ignored `graft/` directory is disposable. Each worktree needs its own
index; do not share one across branches. `build` uses local parsers. Avoid
`--deep`, which adds model calls. This CLI setup does not require `graft init`,
MCP registration, prompt hooks or automatic context injection.

Choose one bounded query, then inspect the source it identifies:

```sh
DO_NOT_TRACK=1 graft map --max-dirs 8
DO_NOT_TRACK=1 graft callers skill_contract --in src/exomem --depth 1
DO_NOT_TRACK=1 graft skeleton src/exomem/workflow_skills.py
```

Queries refresh against the working tree by default. Keep that check enabled;
`graft check` diagnoses freshness separately. If a command fails, continue
with source tools rather than spending the task repairing an optional index.

Results are navigation hints. Same-named wrappers, dynamic calls and registered
tool routes can be missed, so verify important callers and governance boundaries
with source and tests. Generated cards stay out of the Knowledge Base: OpenSpec
remains the specification authority, and Exomem holds durable decisions.
Graft's displayed “tokens saved” estimate is not measured provider usage.
Version 0.16.0 also skips paths starting with the index path, such as a root
`graft_helpers.py` beside `graft/`; use source search for those files.

Upstream command reference: [Graft CLI](https://github.com/trailhq/Graft#cli).
