import { createHash } from "node:crypto"
import { constants as fsConstants } from "node:fs"
import { open, stat } from "node:fs/promises"
import { dirname, isAbsolute, normalize } from "node:path"

import { orchestrator } from "../../orchestrator"
import type { BenchmarkName } from "../../types/benchmark"
import type { ProviderName } from "../../types/provider"

interface CompetitivePlan {
  protocol_version: "1.0.0"
  schema_version: 1
  artifact_type: "memorybench-run-plan.v1"
  run_id: string
  upstream_run_id: string
  provider: ProviderName
  provider_variant: string
  benchmark: BenchmarkName
  selection: {
    mode: "full" | "explicit"
    target_question_ids: string[] | null
  }
  memorybench_home: string
  [key: string]: unknown
}

type IngestOptions = {
  provider: ProviderName
  benchmark: BenchmarkName
  runId: string
  questionIds?: string[]
}

const PLAN_KEYS = [
  "protocol_version", "schema_version", "artifact_type", "run_id", "upstream_run_id",
  "provider", "provider_variant", "benchmark", "harness", "dataset", "dataset_path",
  "selection", "provider_checkout", "memorybench_home", "output_root", "guest_work_root",
  "guest_evidence_root", "preregistration_sha256", "privacy_hmac_key_hex",
] as const
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const SHA256 = /^[0-9a-f]{64}$/

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("expected object")
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): void {
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new Error("run plan has unknown or missing fields")
  }
}

function rejectDuplicateMembers(text: string): void {
  let offset = 0
  const whitespace = () => { while (/\s/.test(text[offset] ?? "")) offset += 1 }
  const string = (): string => {
    const start = offset
    if (text[offset++] !== '"') throw new Error("invalid JSON string")
    while (offset < text.length) {
      if (text[offset] === "\\") { offset += 2; continue }
      if (text[offset++] === '"') return JSON.parse(text.slice(start, offset))
    }
    throw new Error("unterminated JSON string")
  }
  const value = (): void => {
    whitespace()
    if (text[offset] === "{") {
      offset += 1
      whitespace()
      const keys = new Set<string>()
      if (text[offset] === "}") { offset += 1; return }
      for (;;) {
        whitespace()
        const key = string()
        if (keys.has(key)) throw new Error("duplicate object member")
        keys.add(key)
        whitespace()
        if (text[offset++] !== ":") throw new Error("invalid JSON object")
        value()
        whitespace()
        const delimiter = text[offset++]
        if (delimiter === "}") return
        if (delimiter !== ",") throw new Error("invalid JSON object")
      }
    }
    if (text[offset] === "[") {
      offset += 1
      whitespace()
      if (text[offset] === "]") { offset += 1; return }
      for (;;) {
        value()
        whitespace()
        const delimiter = text[offset++]
        if (delimiter === "]") return
        if (delimiter !== ",") throw new Error("invalid JSON array")
      }
    }
    if (text[offset] === '"') { string(); return }
    const start = offset
    while (offset < text.length && !/[\s,}\]]/.test(text[offset])) offset += 1
    if (start === offset) throw new Error("invalid JSON value")
  }
  value()
  whitespace()
  if (offset !== text.length) throw new Error("trailing JSON data")
}

async function secureRead(path: string): Promise<Uint8Array> {
  if (!isAbsolute(path) || normalize(path) !== path) throw new Error("run plan path must be absolute")
  const parent = await stat(dirname(path))
  if ((parent.mode & 0o022) !== 0) throw new Error("run plan parent is group/world writable")
  const handle = await open(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW)
  try {
    const metadata = await handle.stat()
    if (!metadata.isFile() || (metadata.mode & 0o777) !== 0o600) {
      throw new Error("run plan must be a mode-0600 no-follow regular file")
    }
    if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
      throw new Error("run plan owner mismatch")
    }
    return new Uint8Array(await handle.readFile())
  } finally {
    await handle.close()
  }
}

export function parseCompetitivePlan(bytes: Uint8Array): CompetitivePlan {
  const text = new TextDecoder().decode(bytes)
  rejectDuplicateMembers(text)
  const raw = record(JSON.parse(text))
  exactKeys(raw, PLAN_KEYS)
  if (raw.protocol_version !== "1.0.0" || raw.schema_version !== 1 ||
      raw.artifact_type !== "memorybench-run-plan.v1" ||
      !RUN_ID.test(String(raw.run_id)) || !RUN_ID.test(String(raw.upstream_run_id)) ||
      !["basic-memory", "exomem"].includes(String(raw.provider)) ||
      raw.benchmark !== "longmemeval" || typeof raw.memorybench_home !== "string" ||
      !isAbsolute(raw.memorybench_home) || normalize(raw.memorybench_home) !== raw.memorybench_home) {
    throw new Error("run plan identity is invalid")
  }
  const provider = raw.provider as ProviderName
  const registered = provider === "basic-memory" ? "basic-memory-controlled" : "exomem-source-only"
  if (raw.provider_variant !== registered) throw new Error("run plan provider variant is invalid")
  const selection = record(raw.selection)
  exactKeys(selection, ["mode", "target_question_ids"])
  if (selection.mode === "full") {
    if (selection.target_question_ids !== null) throw new Error("full selection is invalid")
  } else if (selection.mode !== "explicit" || !Array.isArray(selection.target_question_ids) ||
      selection.target_question_ids.length === 0 ||
      selection.target_question_ids.some((id) => typeof id !== "string" || !id) ||
      new Set(selection.target_question_ids).size !== selection.target_question_ids.length) {
    throw new Error("explicit selection is invalid")
  }
  return raw as unknown as CompetitivePlan
}

export function ingestOptions(plan: CompetitivePlan): IngestOptions {
  return {
    provider: plan.provider,
    benchmark: plan.benchmark,
    runId: plan.upstream_run_id,
    ...(plan.selection.mode === "explicit"
      ? { questionIds: [...plan.selection.target_question_ids!] }
      : {}),
  }
}

export async function main(
  argv: string[] = process.argv.slice(2),
  ingest: (options: IngestOptions) => Promise<void> = (options) => orchestrator.ingest(options),
): Promise<void> {
  if (argv.length !== 4 || argv[0] !== "--plan" || argv[2] !== "--plan-sha256" ||
      !SHA256.test(argv[3])) throw new Error("competitive ingest accepts only bound plan arguments")
  const bytes = await secureRead(argv[1])
  if (createHash("sha256").update(bytes).digest("hex") !== argv[3]) {
    throw new Error("run plan digest mismatch")
  }
  const plan = parseCompetitivePlan(bytes)
  if (process.cwd() !== plan.memorybench_home) throw new Error("MemoryBench home binding mismatch")
  await ingest(ingestOptions(plan))
}

if (import.meta.main) await main()
