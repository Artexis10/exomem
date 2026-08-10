import { afterAll, describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { chmod, copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

const fixtureRoot = await mkdtemp(join(tmpdir(), "competitive-ingest-test-"))
const commandDirectory = join(fixtureRoot, "src/cli/commands")
await mkdir(commandDirectory, { recursive: true })
await mkdir(join(fixtureRoot, "src/orchestrator"), { recursive: true })
await mkdir(join(fixtureRoot, "src/types"), { recursive: true })
await copyFile(
  new URL("../competitive-ingest.ts", import.meta.url),
  join(commandDirectory, "competitive-ingest.ts"),
)
await writeFile(
  join(fixtureRoot, "src/orchestrator/index.ts"),
  "export const orchestrator = { ingest: async (_options: unknown): Promise<void> => {} }\n",
)
await writeFile(
  join(fixtureRoot, "src/types/benchmark.ts"),
  'export type BenchmarkName = "longmemeval"\n',
)
await writeFile(
  join(fixtureRoot, "src/types/provider.ts"),
  'export type ProviderName = "basic-memory" | "exomem"\n',
)
const { ingestOptions, main, parseCompetitivePlan } = await import(
  `${pathToFileURL(join(commandDirectory, "competitive-ingest.ts")).href}?fixture=${Date.now()}`
)

afterAll(async () => { await rm(fixtureRoot, { recursive: true, force: true }) })

function plan(selection: Record<string, unknown>): Record<string, unknown> {
  const root = process.cwd()
  return {
    protocol_version: "1.0.0", schema_version: 1,
    artifact_type: "memorybench-run-plan.v1", run_id: "public-run",
    upstream_run_id: "native-run", provider: "exomem",
    provider_variant: "exomem-source-only", benchmark: "longmemeval",
    harness: {}, dataset: {}, dataset_path: `${root}/dataset.json`, selection,
    provider_checkout: {}, memorybench_home: root, output_root: `${root}/output`,
    guest_work_root: `${root}/output/work`, guest_evidence_root: `${root}/output/evidence`,
    preregistration_sha256: "a".repeat(64), privacy_hmac_key_hex: "b".repeat(64),
  }
}

describe("competitive ingest plan binding", () => {
  test("passes exact explicit order without limit or sampling", () => {
    const parsed = parseCompetitivePlan(Buffer.from(JSON.stringify(plan({
      mode: "explicit", target_question_ids: ["q-02", "q-01"],
    }))))
    const options = ingestOptions(parsed)
    expect(options).toEqual({
      provider: "exomem", benchmark: "longmemeval", runId: "native-run",
      questionIds: ["q-02", "q-01"],
    })
    expect("limit" in options).toBe(false)
    expect("sampling" in options).toBe(false)
  })

  test("full selection invokes the native all-question path", () => {
    const parsed = parseCompetitivePlan(Buffer.from(JSON.stringify(plan({
      mode: "full", target_question_ids: null,
    }))))
    expect(ingestOptions(parsed)).toEqual({
      provider: "exomem", benchmark: "longmemeval", runId: "native-run",
    })
  })

  test("main binds secure plan bytes and digest before entering the injected ingest seam", async () => {
    const bytes = Buffer.from(JSON.stringify(plan({
      mode: "explicit", target_question_ids: ["q-02", "q-01"],
    })))
    const path = join(fixtureRoot, "plan.json")
    await writeFile(path, bytes)
    await chmod(path, 0o600)
    const digest = createHash("sha256").update(bytes).digest("hex")
    const observed: unknown[] = []
    await main(["--plan", path, "--plan-sha256", digest], async (options: unknown) => {
      observed.push(options)
    })
    expect(observed).toEqual([{
      provider: "exomem", benchmark: "longmemeval", runId: "native-run",
      questionIds: ["q-02", "q-01"],
    }])
    await expect(main(
      ["--plan", path, "--plan-sha256", "0".repeat(64)], async () => {},
    )).rejects.toThrow(/digest/)
  })

  test("rejects duplicate members and every invalid selection shape", () => {
    expect(() => parseCompetitivePlan(Buffer.from(
      '{"artifact_type":"memorybench-run-plan.v1","artifact_type":"memorybench-run-plan.v1"}',
    ))).toThrow(/duplicate/)
    for (const selection of [
      { mode: "full", target_question_ids: [] },
      { mode: "explicit", target_question_ids: null },
      { mode: "explicit", target_question_ids: [] },
      { mode: "explicit", target_question_ids: ["q-01", "q-01"] },
    ]) {
      expect(() => parseCompetitivePlan(Buffer.from(JSON.stringify(plan(selection))))).toThrow(/selection/)
    }
  })
})
