import { createHmac, createHash } from "node:crypto"
import { constants as fsConstants } from "node:fs"
import { chmod, lstat, mkdir, open, readFile, readdir, rename, stat, writeFile } from "node:fs/promises"
import { dirname, join, relative, resolve } from "node:path"
import BasicMemoryProvider from "./providers/basic-memory/index"
import ExomemProvider from "./providers/exomem/index"
import {
  attachBasicMemoryService,
  attachExomemService,
  finalizeBasicMemoryService,
  type CleanupAttachBinding,
  type ServiceDescriptor,
  ownedGuestProcessesAbsent,
  ownedProcessGroupAbsent,
} from "./providers/_guest_transport"

type ProviderName = "basic-memory" | "exomem"
type DiscoverySource = "checkpoint" | "guest_evidence" | "secure_descriptor"
type CleanupFailureCode =
  | "descriptor_missing" | "descriptor_insecure" | "descriptor_stale"
  | "descriptor_binding_mismatch" | "clear_failed" | "namespace_absence_unproved"
  | "corpus_absence_unproved" | "config_absence_unproved"
  | "process_group_absence_unproved" | "work_root_absence_unproved"
  | "cleanup_proof_write_failed"

interface CleanupTarget {
  container_tag: string
  container_tag_hmac_sha256: string
  discovery_sources: DiscoverySource[]
  namespace_expected: boolean
}

interface CleanupPlan {
  protocol_version: "1.0.0"
  schema_version: 1
  artifact_type: "guest-cleanup-plan.v1"
  run_id: string
  provider: ProviderName
  provider_variant: string
  guest_work_root: string
  guest_evidence_root: string
  run_plan_path: string
  run_plan_sha256: string
  targets: CleanupTarget[]
}

interface ArtifactReference {
  root: "memorybench_run" | "output"
  path: string | null
  path_hmac_sha256: string | null
  sha256: string
}

interface TargetAbsence {
  namespace: boolean | null
  corpus: boolean | null
  config: boolean | null
  descriptor: boolean | null
  process_group: boolean | null
  work_root: boolean | null
}

interface AbsenceWithArtifacts extends TargetAbsence {
  artifacts: ArtifactReference[]
}

interface FinalAbsence {
  config: boolean
  descriptor: boolean
  process_group: boolean
  work_root: boolean
  artifacts: ArtifactReference[]
}

type SurfaceObservation = "absent" | "not_absent" | null
type ObservationOperation = "clear_succeeded" | "clear_failed" | "attach_failed" | "final_probe"

interface PrivateCleanupObservation {
  protocol_version: "1.0.0"
  artifact_type: "guest-cleanup-observation.v1"
  run_id: string
  provider: ProviderName
  provider_variant: string
  scope: "target" | "final"
  container_tag_hmac_sha256: string | null
  operation_result: ObservationOperation
  operation_failure_code: CleanupFailureCode | null
  basic_finalization_calls: number
  surfaces: Record<keyof TargetAbsence, SurfaceObservation>
  process_binding: { pid: number; process_start_identity: string } | null
}

interface PendingTargetObservation {
  target: CleanupTarget
  operation_result: Exclude<ObservationOperation, "final_probe">
  operation_failure_code: CleanupFailureCode | null
  absence: TargetAbsence
  service: ServiceDescriptor | null
}

interface CleanupTargetProof {
  container_tag_hmac_sha256: string
  discovery_sources: DiscoverySource[]
  outcome: "cleared" | "already_absent" | "clear_failed" | "absence_unproved"
  failure_code: CleanupFailureCode | null
  artifacts: ArtifactReference[]
  absence: TargetAbsence
}

interface CleanupProof {
  protocol_version: "1.0.0"
  schema_version: 1
  artifact_type: "guest-cleanup.v1"
  run_id: string
  provider: ProviderName
  provider_variant: string
  trigger: "success" | "stage_failure" | "export_failure" | "SIGINT" | "SIGTERM"
  targets: CleanupTargetProof[]
  basic_public_cleanup_calls: number
  failure_codes: CleanupFailureCode[]
  final_absence: FinalAbsence
  all_absent: boolean
}

interface RunPlan {
  protocol_version: "1.0.0"
  schema_version: 1
  artifact_type: "memorybench-run-plan.v1"
  run_id: string
  provider: ProviderName
  provider_variant: string
  provider_checkout: { root: string; repository: string; commit: string; tree: string; lock_sha256: string }
  memorybench_home: string
  output_root: string
  guest_work_root: string
  guest_evidence_root: string
  contract_revision: string
  privacy_hmac_key_hex: string
  selection: { mode: "full" | "explicit"; target_question_ids: string[] | null }
  [key: string]: unknown
}

interface CleanupDependencies {
  attachBasicMemoryService?: (binding: CleanupAttachBinding) => Promise<ServiceDescriptor>
  attachExomemService?: (containerTag: string, binding: CleanupAttachBinding) => Promise<ServiceDescriptor>
  providerFactory?: (
    provider: ProviderName,
    service: ServiceDescriptor,
  ) => CleanupProvider
  targetAbsence?: (target: CleanupTarget) => Promise<AbsenceWithArtifacts>
  finalAbsence?: () => Promise<FinalAbsence>
  stdout?: (line: string) => void
}

interface CleanupProvider {
  clear(containerTag: string): Promise<void>
  finalizationCalls?: number
}

const RUN_PLAN_KEYS = [
  "protocol_version", "schema_version", "artifact_type", "run_id", "upstream_run_id",
  "provider", "provider_variant", "benchmark", "harness", "dataset", "dataset_path",
  "selection",
  "provider_checkout", "memorybench_home", "output_root", "guest_work_root",
  "guest_evidence_root", "contract_revision", "preregistration_sha256", "privacy_hmac_key_hex",
]
const CLEANUP_PLAN_KEYS = [
  "protocol_version", "schema_version", "artifact_type", "run_id", "provider",
  "provider_variant", "guest_work_root", "guest_evidence_root", "run_plan_path",
  "run_plan_sha256", "targets",
]
const TARGET_KEYS = [
  "container_tag", "container_tag_hmac_sha256", "discovery_sources", "namespace_expected",
]
const SOURCES = ["checkpoint", "guest_evidence", "secure_descriptor"] as const
const FAILURE_CODES = new Set<CleanupFailureCode>([
  "descriptor_missing", "descriptor_insecure", "descriptor_stale", "descriptor_binding_mismatch",
  "clear_failed", "namespace_absence_unproved", "corpus_absence_unproved",
  "config_absence_unproved", "process_group_absence_unproved", "work_root_absence_unproved",
  "cleanup_proof_write_failed",
])
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/
const PUBLIC_SOURCE = /^(?:https:\/\/[^\\\s]+|[A-Za-z0-9][A-Za-z0-9._+-]*(?::[A-Za-z0-9][A-Za-z0-9._/@+-]*)?|[A-Za-z0-9][A-Za-z0-9._+-]*(?:\/[A-Za-z0-9][A-Za-z0-9._+-]*)+)$/
const HARNESS = {
  repository: "https://github.com/supermemoryai/memorybench",
  commit: "118209a746d97d0d85e5a7234267f0b6962857e9",
  tree: "2ee25bdbcb6bfaaecb32f917920c53775a299b37",
  bun_lock_sha256: "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da",
} as const

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("object is invalid")
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): void {
  if (Object.keys(value).sort().join("\0") !== [...expected].sort().join("\0")) {
    throw new Error("object has an unknown or missing field")
  }
}

function isHex64(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
}

function absoluteNormalized(value: unknown, name: string): string {
  if (typeof value !== "string" || resolve(value) !== value) {
    throw new Error(`${name} must be an absolute normalized path`)
  }
  return value
}

function strictChild(path: string, parent: string): boolean {
  const suffix = relative(parent, path)
  return suffix !== "" && suffix !== ".." && !suffix.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) && resolve(parent, suffix) === path
}

function sortedUniqueStrings(values: unknown[], allowed?: readonly string[]): boolean {
  return values.every((value) => typeof value === "string" && (!allowed || allowed.includes(value))) &&
    values.join("\0") === [...new Set(values as string[])].sort().join("\0")
}

export function privacyHmacSha256(keyHex: string, domain: string, rawValue: string): string {
  if (!/^[0-9a-f]{64}$/.test(keyHex) ||
      !["case-id", "container-tag", "artifact-path"].includes(domain)) {
    throw new Error("privacy HMAC input is invalid")
  }
  return createHmac("sha256", Buffer.from(keyHex, "hex"))
    .update(Buffer.from(domain, "utf8"))
    .update(Buffer.from([0]))
    .update(Buffer.from(rawValue, "utf8"))
    .digest("hex")
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex")
}

async function secureRead(
  path: string,
  options: { currentUid?: () => number } = {},
): Promise<Uint8Array> {
  absoluteNormalized(path, "private path")
  const parent = await stat(dirname(path))
  if ((parent.mode & 0o022) !== 0) throw new Error("private file parent is group/world writable")
  let handle
  try {
    handle = await open(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ELOOP") throw new Error("private file must be opened no-follow")
    throw error
  }
  try {
    const metadata = await handle.stat()
    if (!metadata.isFile()) throw new Error("private file must be a no-follow regular file")
    if ((metadata.mode & 0o777) !== 0o600) throw new Error("private file mode must be 0600")
    const currentUid = options.currentUid ?? (() => typeof process.getuid === "function" ? process.getuid() : metadata.uid)
    if (metadata.uid !== currentUid()) throw new Error("private file owner mismatch")
    return new Uint8Array(await handle.readFile())
  } finally {
    await handle.close()
  }
}

function parseJson(bytes: Uint8Array, description: string): Record<string, unknown> {
  try {
    const text = new TextDecoder().decode(bytes)
    rejectDuplicateMembers(text)
    return record(JSON.parse(text))
  } catch {
    throw new Error(`${description} JSON is invalid`)
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

function validateRunPlan(raw: Record<string, unknown>): RunPlan {
  exactKeys(raw, RUN_PLAN_KEYS)
  if (raw.protocol_version !== "1.0.0" || raw.schema_version !== 1 ||
      raw.artifact_type !== "memorybench-run-plan.v1" ||
      !["basic-memory", "exomem"].includes(String(raw.provider)) ||
      !RUN_ID.test(String(raw.run_id)) || !RUN_ID.test(String(raw.upstream_run_id)) ||
      raw.benchmark !== "longmemeval" || !/^[0-9a-f]{40}$/.test(String(raw.contract_revision)) ||
      !isHex64(raw.preregistration_sha256) ||
      !isHex64(raw.privacy_hmac_key_hex)) throw new Error("run plan identity is invalid")
  const provider = raw.provider as ProviderName
  const registered = provider === "basic-memory" ? "basic-memory-controlled" : "exomem-source-only"
  if (raw.provider_variant !== registered) throw new Error("run plan provider variant is invalid")

  const harness = record(raw.harness)
  exactKeys(harness, Object.keys(HARNESS))
  if (Object.entries(HARNESS).some(([key, expected]) => harness[key] !== expected)) {
    throw new Error("run plan harness identity is invalid")
  }
  const dataset = record(raw.dataset)
  exactKeys(dataset, ["id", "variant", "source", "revision", "sha256", "case_count"])
  if ([dataset.id, dataset.variant, dataset.revision].some((value) => typeof value !== "string") ||
      typeof dataset.source !== "string" || !PUBLIC_SOURCE.test(dataset.source) || dataset.source.startsWith("file:") ||
      dataset.source.split("/").some((part) => part === "." || part === "..") ||
      !isHex64(dataset.sha256) || !Number.isSafeInteger(dataset.case_count) ||
      Number(dataset.case_count) < 0) throw new Error("run plan dataset identity is invalid")

  const selection = record(raw.selection)
  exactKeys(selection, ["mode", "target_question_ids"])
  if (selection.mode === "full") {
    if (selection.target_question_ids !== null) throw new Error("full selection is invalid")
  } else if (selection.mode !== "explicit" || !Array.isArray(selection.target_question_ids) ||
      selection.target_question_ids.length === 0 ||
      selection.target_question_ids.some((value) => typeof value !== "string" || !value) ||
      new Set(selection.target_question_ids).size !== selection.target_question_ids.length) {
    throw new Error("explicit selection is invalid")
  }

  const checkout = record(raw.provider_checkout)
  exactKeys(checkout, ["root", "repository", "commit", "tree", "lock_sha256"])
  absoluteNormalized(checkout.root, "provider checkout root")
  if (typeof checkout.repository !== "string" || !PUBLIC_SOURCE.test(checkout.repository) ||
      !/^[0-9a-f]{40}$/.test(String(checkout.commit)) ||
      !/^[0-9a-f]{40}$/.test(String(checkout.tree)) || !isHex64(checkout.lock_sha256)) {
    throw new Error("run plan provider checkout identity is invalid")
  }
  for (const name of ["dataset_path", "memorybench_home", "output_root", "guest_work_root", "guest_evidence_root"] as const) {
    absoluteNormalized(raw[name], name)
  }
  const output = raw.output_root as string
  const work = raw.guest_work_root as string
  const evidence = raw.guest_evidence_root as string
  if (work === evidence || !strictChild(work, output) || !strictChild(evidence, output)) {
    throw new Error("run plan guest roots are invalid")
  }
  return raw as unknown as RunPlan
}

function validateTarget(raw: Record<string, unknown>): CleanupTarget {
  exactKeys(raw, TARGET_KEYS)
  if (typeof raw.container_tag !== "string" || raw.container_tag.length === 0 ||
      !isHex64(raw.container_tag_hmac_sha256) || typeof raw.namespace_expected !== "boolean" ||
      !Array.isArray(raw.discovery_sources) || raw.discovery_sources.length === 0 ||
      !sortedUniqueStrings(raw.discovery_sources, SOURCES)) {
    throw new Error("cleanup target is invalid")
  }
  return raw as unknown as CleanupTarget
}

function validatePlanShape(raw: Record<string, unknown>): CleanupPlan {
  exactKeys(raw, CLEANUP_PLAN_KEYS)
  if (raw.protocol_version !== "1.0.0" || raw.schema_version !== 1 ||
      raw.artifact_type !== "guest-cleanup-plan.v1" ||
      !["basic-memory", "exomem"].includes(String(raw.provider)) ||
      !RUN_ID.test(String(raw.run_id)) || typeof raw.provider_variant !== "string" ||
      !isHex64(raw.run_plan_sha256) || !Array.isArray(raw.targets)) {
    throw new Error("cleanup plan is invalid")
  }
  for (const name of ["guest_work_root", "guest_evidence_root", "run_plan_path"] as const) {
    absoluteNormalized(raw[name], name)
  }
  return { ...raw, targets: raw.targets.map((target) => validateTarget(record(target))) } as CleanupPlan
}

async function boundRunPlan(plan: CleanupPlan): Promise<RunPlan> {
  const bytes = await secureRead(plan.run_plan_path)
  if (sha256(bytes) !== plan.run_plan_sha256) throw new Error("run-plan digest mismatch")
  const runPlan = validateRunPlan(parseJson(bytes, "run plan"))
  if (runPlan.run_id !== plan.run_id || runPlan.provider !== plan.provider ||
      runPlan.provider_variant !== plan.provider_variant ||
      runPlan.guest_work_root !== plan.guest_work_root ||
      runPlan.guest_evidence_root !== plan.guest_evidence_root) {
    throw new Error("cleanup/run-plan root or provider binding mismatch")
  }
  return runPlan
}

export async function parseCleanupPlan(
  path: string,
  options: { currentUid?: () => number } = {},
): Promise<CleanupPlan> {
  const plan = validatePlanShape(parseJson(await secureRead(path, options), "cleanup plan"))
  const runPlan = await boundRunPlan(plan)
  const normalized = await normalizedTargets(plan, runPlan)
  if (JSON.stringify(normalized) !== JSON.stringify(plan.targets)) {
    throw new Error("cleanup targets are not HMAC-sorted, unique, and canonical")
  }
  return plan
}

function sortedUnique<T>(values: T[]): T[] {
  return [...new Set(values)].sort()
}

async function normalizedTargets(plan: CleanupPlan, runPlan: RunPlan): Promise<CleanupTarget[]> {
  const byHmac = new Map<string, CleanupTarget>()
  for (const target of plan.targets) {
    const existing = byHmac.get(target.container_tag_hmac_sha256)
    if (existing && (
      existing.container_tag !== target.container_tag ||
      existing.namespace_expected !== target.namespace_expected
    )) throw new Error("duplicate cleanup target HMAC conflict")
    const expected = privacyHmacSha256(
      runPlan.privacy_hmac_key_hex, "container-tag", target.container_tag
    )
    if (expected !== target.container_tag_hmac_sha256) throw new Error("cleanup target HMAC mismatch")
    const prior = byHmac.get(expected)
    if (prior) {
      if (prior.container_tag !== target.container_tag ||
          prior.namespace_expected !== target.namespace_expected) {
        throw new Error("duplicate cleanup target digest conflict")
      }
      prior.discovery_sources = sortedUnique([
        ...prior.discovery_sources, ...target.discovery_sources,
      ]) as DiscoverySource[]
    } else {
      byHmac.set(expected, { ...target, discovery_sources: sortedUnique(target.discovery_sources) as DiscoverySource[] })
    }
  }
  return [...byHmac.values()].sort(
    (left, right) => left.container_tag_hmac_sha256.localeCompare(right.container_tag_hmac_sha256)
  )
}

function binding(runPlan: RunPlan): CleanupAttachBinding {
  return {
    provider: runPlan.provider,
    provider_checkout: {
      root: runPlan.provider_checkout.root,
      commit: runPlan.provider_checkout.commit,
    },
    guest_work_root: runPlan.guest_work_root,
    guest_evidence_root: runPlan.guest_evidence_root,
  }
}

function defaultProviderFactory(
  provider: ProviderName,
  service: ServiceDescriptor,
): CleanupProvider {
  if (provider === "basic-memory") {
    return new BasicMemoryProvider({
      ensureService: async () => service,
      clearService: finalizeBasicMemoryService,
    })
  }
  return new ExomemProvider({ ensureService: async () => service })
}

async function pathAbsent(path: string): Promise<boolean> {
  try { await lstat(path); return false } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return true
    return false
  }
}

async function exactProcessAbsent(service: ServiceDescriptor | null): Promise<boolean> {
  return ownedProcessGroupAbsent(service)
}

async function atomicEvidence(outputRoot: string, relative: string, payload: unknown): Promise<ArtifactReference> {
  const path = join(outputRoot, relative)
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8")
  const temporary = `${path}.tmp-${process.pid}-${crypto.randomUUID()}`
  await writeFile(temporary, bytes, { mode: 0o600, flag: "wx" })
  await chmod(temporary, 0o600)
  await rename(temporary, path)
  return { root: "output", path: relative, path_hmac_sha256: null, sha256: sha256(bytes) }
}

const SURFACE_KEYS = ["namespace", "corpus", "config", "descriptor", "process_group", "work_root"] as const

function surfaceObservations(absence: TargetAbsence): PrivateCleanupObservation["surfaces"] {
  return Object.fromEntries(SURFACE_KEYS.map((surface) => [
    surface,
    absence[surface] === null ? null : absence[surface] ? "absent" : "not_absent",
  ])) as PrivateCleanupObservation["surfaces"]
}

function observationAbsence(observation: PrivateCleanupObservation): TargetAbsence {
  return Object.fromEntries(SURFACE_KEYS.map((surface) => [
    surface,
    observation.surfaces[surface] === null ? null : observation.surfaces[surface] === "absent",
  ])) as unknown as TargetAbsence
}

function privateProcessBinding(service: ServiceDescriptor | null): PrivateCleanupObservation["process_binding"] {
  return service && Number.isSafeInteger(service.pid) && service.pid > 0 &&
    typeof service.process_start_identity === "string" && service.process_start_identity
    ? { pid: service.pid, process_start_identity: service.process_start_identity }
    : null
}

function observationPayload(
  runPlan: RunPlan,
  scope: "target" | "final",
  targetHmac: string | null,
  operationResult: ObservationOperation,
  operationFailureCode: CleanupFailureCode | null,
  basicFinalizationCalls: number,
  absence: TargetAbsence,
  service: ServiceDescriptor | null,
): PrivateCleanupObservation {
  return {
    protocol_version: "1.0.0",
    artifact_type: "guest-cleanup-observation.v1",
    run_id: runPlan.run_id,
    provider: runPlan.provider,
    provider_variant: runPlan.provider_variant,
    scope,
    container_tag_hmac_sha256: targetHmac,
    operation_result: operationResult,
    operation_failure_code: operationFailureCode,
    basic_finalization_calls: basicFinalizationCalls,
    surfaces: surfaceObservations(absence),
    process_binding: privateProcessBinding(service),
  }
}

function exomemTargetWork(runPlan: RunPlan, target: CleanupTarget): string {
  const rawDigest = createHash("sha256").update(target.container_tag, "utf8").digest("hex")
  return join(runPlan.guest_work_root, "services", "exomem", rawDigest.slice(0, 24))
}

async function defaultTargetAbsence(
  runPlan: RunPlan,
  target: CleanupTarget,
  clearSucceeded: boolean,
  service: ServiceDescriptor | null,
): Promise<AbsenceWithArtifacts> {
  const exomem = runPlan.provider === "exomem"
  const work = exomem
    ? exomemTargetWork(runPlan, target)
    : join(runPlan.guest_work_root, "services", "basic-memory")
  const workMissing = await pathAbsent(work)
  const descriptorMissing = await pathAbsent(join(work, "service.v1.json"))
  const processMissing = await exactProcessAbsent(service)
  const absence: TargetAbsence = exomem ? {
    namespace: clearSucceeded,
    corpus: null,
    config: null,
    descriptor: descriptorMissing,
    process_group: processMissing,
    work_root: workMissing,
  } : {
    namespace: clearSucceeded,
    corpus: clearSucceeded,
    config: null,
    descriptor: null,
    process_group: null,
    work_root: null,
  }
  return { ...absence, artifacts: [] }
}

async function defaultFinalAbsence(
  runPlan: RunPlan, retainedServices: ServiceDescriptor[],
): Promise<FinalAbsence> {
  const serviceRoot = join(runPlan.guest_work_root, "services", runPlan.provider)
  const workRoot = await pathAbsent(serviceRoot)
  const descriptor = await Promise.all(retainedServices.map((service) =>
    pathAbsent(join(service.work_root, "service.v1.json"))))
  const processes = await Promise.all(retainedServices.map(exactProcessAbsent))
  const final = {
    config: await pathAbsent(join(serviceRoot, "config.json")),
    descriptor: descriptor.every(Boolean),
    process_group: processes.every(Boolean),
    work_root: workRoot,
  }
  return { ...final, artifacts: [] }
}

function descriptorFailure(error: unknown): CleanupFailureCode {
  const text = error instanceof Error ? error.message.toLowerCase() : ""
  if (text.includes("missing") || text.includes("enoent")) return "descriptor_missing"
  if (text.includes("mode") || text.includes("owner") || text.includes("nofollow") || text.includes("secure")) {
    return "descriptor_insecure"
  }
  if (text.includes("stale") || text.includes("process is not live")) return "descriptor_stale"
  return "descriptor_binding_mismatch"
}

function absenceFailure(absence: TargetAbsence): CleanupFailureCode | null {
  if (absence.namespace !== true) return "namespace_absence_unproved"
  if (absence.corpus === false) return "corpus_absence_unproved"
  if (absence.config === false) return "config_absence_unproved"
  if (absence.descriptor === false) return "descriptor_insecure"
  if (absence.process_group === false) return "process_group_absence_unproved"
  if (absence.work_root === false) return "work_root_absence_unproved"
  return null
}

function computeAllAbsent(proof: Omit<CleanupProof, "all_absent">): boolean {
  const basicCount = proof.provider === "exomem"
    ? proof.basic_public_cleanup_calls === 0
    : proof.basic_public_cleanup_calls === (
        proof.targets.every((target) => target.outcome === "already_absent") ? 0 : 1
      )
  const targetAbsent = proof.targets.every((target) => {
    const absence = target.absence
    return !["clear_failed", "absence_unproved"].includes(target.outcome) &&
      absence.namespace === true &&
      (absence.corpus === true || absence.corpus === null) &&
      (absence.config === true || absence.config === null) &&
      (absence.descriptor === true || absence.descriptor === null) &&
      (absence.process_group === true || absence.process_group === null) &&
      (absence.work_root === true || absence.work_root === null)
  })
  return basicCount && targetAbsent && proof.failure_codes.length === 0 &&
    proof.final_absence.config && proof.final_absence.descriptor &&
    proof.final_absence.process_group && proof.final_absence.work_root
}

export async function executeCleanup(
  planInput: CleanupPlan,
  dependencies: CleanupDependencies = {},
  trigger: CleanupProof["trigger"] = "success",
): Promise<CleanupProof> {
  const plan = validatePlanShape(record(planInput))
  const runPlan = await boundRunPlan(plan)
  const targets = await normalizedTargets(plan, runPlan)
  const attachBasic = dependencies.attachBasicMemoryService ?? attachBasicMemoryService
  const attachExomem = dependencies.attachExomemService ?? attachExomemService
  const makeProvider = dependencies.providerFactory ?? defaultProviderFactory
  const proofs: CleanupTargetProof[] = []
  const failures = new Set<CleanupFailureCode>()
  let basicService: ServiceDescriptor | null = null
  let basicProvider: CleanupProvider | null = null
  const retainedServices: ServiceDescriptor[] = []

  if (!dependencies.providerFactory && runPlan.provider === "exomem") {
    process.env.MEMORYBENCH_GUEST_WORK_ROOT = runPlan.guest_work_root
    process.env.MEMORYBENCH_GUEST_EVIDENCE_ROOT = runPlan.guest_evidence_root
    process.env.EXOMEM_HOME = runPlan.provider_checkout.root
    process.env.EXOMEM_COMMIT = runPlan.provider_checkout.commit
  }

  for (const target of targets) {
    let clearSucceeded = false
    let attached = false
    let attachFailure: CleanupFailureCode | null = null
    let clearFailure = false
    let attachedService: ServiceDescriptor | null = null
    try {
      let provider: CleanupProvider
      if (plan.provider === "basic-memory") {
        if (!basicService) basicService = await attachBasic(binding(runPlan))
        attachedService = basicService
        attached = true
        basicProvider ??= makeProvider("basic-memory", basicService)
        provider = basicProvider
      } else {
        const service = await attachExomem(target.container_tag, binding(runPlan))
        attachedService = service
        attached = true
        provider = makeProvider("exomem", service)
      }
      await provider.clear(target.container_tag)
      clearSucceeded = true
    } catch (error) {
      if (!attached) {
        attachFailure = descriptorFailure(error)
      } else {
        clearFailure = true
      }
    }
    if (attachedService && !retainedServices.some((service) =>
      service.pid === attachedService!.pid && service.process_start_identity === attachedService!.process_start_identity)) {
      retainedServices.push(attachedService)
    }

    let observed: AbsenceWithArtifacts
    try {
      observed = dependencies.targetAbsence
        ? await dependencies.targetAbsence(target)
        : await defaultTargetAbsence(runPlan, target, clearSucceeded, attachedService)
    } catch {
      observed = {
        namespace: false,
        corpus: plan.provider === "basic-memory" ? false : null,
        config: null,
        descriptor: plan.provider === "exomem" ? false : null,
        process_group: plan.provider === "exomem" ? false : null,
        work_root: plan.provider === "exomem" ? false : null,
        artifacts: [],
      }
    }
    const { artifacts: _untrustedArtifacts, ...rawAbsence } = observed
    const operationResult: PendingTargetObservation["operation_result"] = clearFailure
      ? "clear_failed" : attachFailure ? "attach_failed" : "clear_succeeded"
    const operationFailure = clearFailure ? "clear_failed" : attachFailure
    let artifacts: ArtifactReference[] = []
    let absence = rawAbsence
    try {
      const observation = observationPayload(
        runPlan,
        "target",
        target.container_tag_hmac_sha256,
        operationResult,
        operationFailure,
        plan.provider === "basic-memory" ? basicProvider?.finalizationCalls ?? 0 : 0,
        rawAbsence,
        attachedService,
      )
      artifacts = [await atomicEvidence(
        runPlan.output_root,
        `cleanup-evidence/target-${target.container_tag_hmac_sha256}.json`,
        observation,
      )]
      absence = observationAbsence(observation)
    } catch {
      failures.add("cleanup_proof_write_failed")
      absence = {
        namespace: false,
        corpus: plan.provider === "basic-memory" ? false : null,
        config: null,
        descriptor: plan.provider === "exomem" ? false : null,
        process_group: plan.provider === "exomem" ? false : null,
        work_root: plan.provider === "exomem" ? false : null,
      }
    }
    let outcome: CleanupTargetProof["outcome"]
    let failureCode: CleanupFailureCode | null
    if (artifacts.length === 0) {
      outcome = "absence_unproved"
      failureCode = "cleanup_proof_write_failed"
    } else if (clearFailure) {
      outcome = "clear_failed"
      failureCode = "clear_failed"
    } else if (attachFailure) {
      const alreadyAbsent = plan.provider === "basic-memory"
        ? absence.namespace === true && absence.corpus === true
        : absence.descriptor === true && absence.work_root === true
      if (alreadyAbsent) {
        outcome = "already_absent"
        failureCode = null
      } else {
        outcome = "absence_unproved"
        failureCode = attachFailure
      }
    } else {
      failureCode = absenceFailure(absence)
      outcome = failureCode === null ? "cleared" : "absence_unproved"
    }
    if (failureCode) failures.add(failureCode)
    proofs.push({
      container_tag_hmac_sha256: target.container_tag_hmac_sha256,
      discovery_sources: target.discovery_sources,
      outcome,
      failure_code: failureCode,
      artifacts: [...artifacts].sort((left, right) =>
        `${left.root}\0${left.path ?? ""}\0${left.path_hmac_sha256 ?? ""}\0${left.sha256}`
          .localeCompare(`${right.root}\0${right.path ?? ""}\0${right.path_hmac_sha256 ?? ""}\0${right.sha256}`)),
      absence,
    })
  }

  let finalAbsence: FinalAbsence
  try {
    finalAbsence = dependencies.finalAbsence
      ? await dependencies.finalAbsence()
      : await defaultFinalAbsence(runPlan, retainedServices)
  } catch {
    finalAbsence = {
      config: false,
      descriptor: false,
      process_group: false,
      work_root: false,
      artifacts: [],
    }
  }
  for (const [surface, code] of [
    ["config", "config_absence_unproved"],
    ["descriptor", "descriptor_insecure"],
    ["process_group", "process_group_absence_unproved"],
    ["work_root", "work_root_absence_unproved"],
  ] as const) if (!finalAbsence[surface]) failures.add(code)
  const basicCalls = plan.provider === "basic-memory" ? basicProvider?.finalizationCalls ?? 0 : 0
  const { artifacts: _untrustedFinalArtifacts, ...rawFinalAbsence } = finalAbsence
  try {
    const finalObservation = observationPayload(
      runPlan,
      "final",
      null,
      "final_probe",
      null,
      basicCalls,
      { namespace: null, corpus: null, ...rawFinalAbsence },
      retainedServices.length === 1 ? retainedServices[0] : null,
    )
    finalAbsence = {
      config: observationAbsence(finalObservation).config === true,
      descriptor: observationAbsence(finalObservation).descriptor === true,
      process_group: observationAbsence(finalObservation).process_group === true,
      work_root: observationAbsence(finalObservation).work_root === true,
      artifacts: [await atomicEvidence(
        runPlan.output_root,
        "cleanup-evidence/final.json",
        finalObservation,
      )],
    }
  } catch {
    failures.add("cleanup_proof_write_failed")
    finalAbsence = { config: false, descriptor: false, process_group: false, work_root: false, artifacts: [] }
  }
  const withoutAggregate: Omit<CleanupProof, "all_absent"> = {
    protocol_version: "1.0.0",
    schema_version: 1,
    artifact_type: "guest-cleanup.v1",
    run_id: plan.run_id,
    provider: plan.provider,
    provider_variant: plan.provider_variant,
    trigger,
    targets: proofs,
    basic_public_cleanup_calls: basicCalls,
    failure_codes: [...failures].sort(),
    final_absence: {
      ...finalAbsence,
      artifacts: [...finalAbsence.artifacts].sort((left, right) =>
        `${left.root}\0${left.path ?? ""}\0${left.path_hmac_sha256 ?? ""}\0${left.sha256}`
          .localeCompare(`${right.root}\0${right.path ?? ""}\0${right.path_hmac_sha256 ?? ""}\0${right.sha256}`)),
    },
  }
  return { ...withoutAggregate, all_absent: computeAllAbsent(withoutAggregate) }
}

function validateArtifact(value: unknown): ArtifactReference {
  const artifact = record(value)
  exactKeys(artifact, ["root", "path", "path_hmac_sha256", "sha256"])
  if (!["output", "memorybench_run"].includes(String(artifact.root)) || !isHex64(artifact.sha256)) {
    throw new Error("cleanup artifact reference is invalid")
  }
  if (artifact.root === "output") {
    if (typeof artifact.path !== "string" || !artifact.path || artifact.path_hmac_sha256 !== null ||
        artifact.path.startsWith("/") || artifact.path.includes("\\") ||
        artifact.path.split("/").some((part) => !part || part === "." || part === "..")) {
      throw new Error("output artifact reference is invalid")
    }
  } else if (artifact.path !== null || !isHex64(artifact.path_hmac_sha256)) {
    throw new Error("MemoryBench artifact reference is invalid")
  }
  return artifact as unknown as ArtifactReference
}

export async function validateCleanupProof(value: unknown): Promise<CleanupProof> {
  const proof = record(value)
  exactKeys(proof, [
    "protocol_version", "schema_version", "artifact_type", "run_id", "provider",
    "provider_variant", "trigger", "targets", "basic_public_cleanup_calls",
    "failure_codes", "final_absence", "all_absent",
  ])
  if (proof.protocol_version !== "1.0.0" || proof.schema_version !== 1 ||
      proof.artifact_type !== "guest-cleanup.v1" ||
      !["basic-memory", "exomem"].includes(String(proof.provider)) ||
      !RUN_ID.test(String(proof.run_id)) ||
      !["success", "stage_failure", "export_failure", "SIGINT", "SIGTERM"].includes(String(proof.trigger)) ||
      !Array.isArray(proof.targets) || !Number.isSafeInteger(proof.basic_public_cleanup_calls) ||
      Number(proof.basic_public_cleanup_calls) < 0 || !Array.isArray(proof.failure_codes) ||
      typeof proof.all_absent !== "boolean") throw new Error("cleanup proof is invalid")
  const provider = proof.provider as ProviderName
  const registered = provider === "basic-memory" ? "basic-memory-controlled" : "exomem-source-only"
  if (proof.provider_variant !== registered) throw new Error("cleanup proof provider variant is invalid")
  const targets = proof.targets.map((raw) => {
    const target = record(raw)
    exactKeys(target, [
      "container_tag_hmac_sha256", "discovery_sources", "outcome", "failure_code",
      "artifacts", "absence",
    ])
    if (!isHex64(target.container_tag_hmac_sha256) || !Array.isArray(target.discovery_sources) ||
        target.discovery_sources.length === 0 || !sortedUniqueStrings(target.discovery_sources, SOURCES) ||
        !Array.isArray(target.artifacts) ||
        !["cleared", "already_absent", "clear_failed", "absence_unproved"].includes(String(target.outcome)) ||
        !(target.failure_code === null || FAILURE_CODES.has(target.failure_code as CleanupFailureCode))) {
      throw new Error("cleanup target proof is invalid")
    }
    const absence = record(target.absence)
    exactKeys(absence, ["namespace", "corpus", "config", "descriptor", "process_group", "work_root"])
    for (const surface of Object.values(absence)) {
      if (!(surface === null || typeof surface === "boolean")) throw new Error("cleanup absence is invalid")
    }
    if (provider === "basic-memory") {
      if (typeof absence.corpus !== "boolean" ||
          [absence.config, absence.descriptor, absence.process_group, absence.work_root]
            .some((surface) => surface !== null)) throw new Error("Basic cleanup applicability is invalid")
    } else if (absence.corpus !== null || absence.config !== null ||
        [absence.descriptor, absence.process_group, absence.work_root]
          .some((surface) => typeof surface !== "boolean")) {
      throw new Error("Exomem cleanup applicability is invalid")
    }
    const failed = ["clear_failed", "absence_unproved"].includes(String(target.outcome))
    if (failed !== (target.failure_code !== null)) throw new Error("cleanup outcome/failure is invalid")
    if (target.outcome === "already_absent") {
      if (provider === "basic-memory" && (absence.namespace !== true || absence.corpus !== true)) {
        throw new Error("Basic already_absent proof is invalid")
      }
      if (provider === "exomem" && (absence.descriptor !== true || absence.work_root !== true)) {
        throw new Error("Exomem already_absent proof is invalid")
      }
    }
    const artifacts = target.artifacts.map(validateArtifact)
    const artifactKeys = artifacts.map((artifact) =>
      `${artifact.root}\0${artifact.path ?? ""}\0${artifact.path_hmac_sha256 ?? ""}\0${artifact.sha256}`)
    if (artifactKeys.join("\0") !== [...new Set(artifactKeys)].sort().join("\0")) {
      throw new Error("cleanup artifacts are not sorted and unique")
    }
    return {
      ...target,
      artifacts,
      absence,
    } as unknown as CleanupTargetProof
  })
  const hmacs = targets.map((target) => target.container_tag_hmac_sha256)
  if (hmacs.join("\0") !== [...new Set(hmacs)].sort().join("\0")) {
    throw new Error("cleanup targets are not HMAC-sorted and unique")
  }
  const final = record(proof.final_absence)
  exactKeys(final, ["config", "descriptor", "process_group", "work_root", "artifacts"])
  if ([final.config, final.descriptor, final.process_group, final.work_root]
      .some((surface) => typeof surface !== "boolean") || !Array.isArray(final.artifacts)) {
    throw new Error("final cleanup absence is invalid")
  }
  const finalArtifacts = final.artifacts.map(validateArtifact)
  const finalArtifactKeys = finalArtifacts.map((artifact) =>
    `${artifact.root}\0${artifact.path ?? ""}\0${artifact.path_hmac_sha256 ?? ""}\0${artifact.sha256}`)
  if (finalArtifactKeys.join("\0") !== [...new Set(finalArtifactKeys)].sort().join("\0")) {
    throw new Error("final cleanup artifacts are not sorted and unique")
  }
  const validated: CleanupProof = {
    ...proof,
    provider,
    targets,
    failure_codes: proof.failure_codes as CleanupFailureCode[],
    final_absence: { ...final, artifacts: finalArtifacts } as FinalAbsence,
  } as CleanupProof
  const failed = targets.some((target) =>
    target.outcome === "clear_failed" || target.outcome === "absence_unproved") ||
    validated.failure_codes.length > 0 ||
    [validated.final_absence.config, validated.final_absence.descriptor,
      validated.final_absence.process_group, validated.final_absence.work_root]
      .some((surface) => surface === false)
  const basicCountValid = provider === "exomem"
    ? validated.basic_public_cleanup_calls === 0
    : failed
      ? [0, 1].includes(validated.basic_public_cleanup_calls)
      : validated.basic_public_cleanup_calls ===
        (targets.every((target) => target.outcome === "already_absent") ? 0 : 1)
  if (validated.failure_codes.some((code) => !FAILURE_CODES.has(code)) ||
      validated.failure_codes.join("\0") !== sortedUnique(validated.failure_codes).join("\0") ||
      !basicCountValid ||
      computeAllAbsent(validated) !== validated.all_absent) {
    throw new Error("all_absent or cleanup failures contradict evidence")
  }
  return validated
}

function validateObservation(
  value: unknown,
  runPlan: RunPlan,
  scope: "target" | "final",
  targetHmac: string | null,
): PrivateCleanupObservation {
  const observation = record(value)
  exactKeys(observation, [
    "protocol_version", "artifact_type", "run_id", "provider", "provider_variant",
    "scope", "container_tag_hmac_sha256", "operation_result", "operation_failure_code",
    "basic_finalization_calls", "surfaces", "process_binding",
  ])
  if (observation.protocol_version !== "1.0.0" ||
      observation.artifact_type !== "guest-cleanup-observation.v1" ||
      observation.run_id !== runPlan.run_id || observation.provider !== runPlan.provider ||
      observation.provider_variant !== runPlan.provider_variant || observation.scope !== scope ||
      observation.container_tag_hmac_sha256 !== targetHmac ||
      !Number.isSafeInteger(observation.basic_finalization_calls) ||
      ![0, 1].includes(Number(observation.basic_finalization_calls))) {
    throw new Error("cleanup observation binding is invalid")
  }
  if (runPlan.provider === "exomem" && observation.basic_finalization_calls !== 0) {
    throw new Error("cleanup observation Basic seam is invalid")
  }
  const operation = String(observation.operation_result)
  const failure = observation.operation_failure_code
  if (scope === "final") {
    if (operation !== "final_probe" || failure !== null) {
      throw new Error("final cleanup observation operation is invalid")
    }
  } else if (!isHex64(targetHmac) || !["clear_succeeded", "clear_failed", "attach_failed"].includes(operation) ||
      (operation === "clear_succeeded" && failure !== null) ||
      (operation === "clear_failed" && failure !== "clear_failed") ||
      (operation === "attach_failed" && !(failure !== null && FAILURE_CODES.has(failure as CleanupFailureCode)))) {
    throw new Error("target cleanup observation operation is invalid")
  }
  const surfaces = record(observation.surfaces)
  exactKeys(surfaces, SURFACE_KEYS)
  if (Object.values(surfaces).some((surface) =>
    !["absent", "not_absent", null].includes(surface as SurfaceObservation))) {
    throw new Error("cleanup observation surface is invalid")
  }
  if (scope === "final") {
    if (surfaces.namespace !== null || surfaces.corpus !== null ||
        [surfaces.config, surfaces.descriptor, surfaces.process_group, surfaces.work_root]
          .some((surface) => surface === null)) {
      throw new Error("final cleanup observation applicability is invalid")
    }
  } else if (runPlan.provider === "basic-memory") {
    if ([surfaces.namespace, surfaces.corpus].some((surface) => surface === null) ||
        [surfaces.config, surfaces.descriptor, surfaces.process_group, surfaces.work_root]
          .some((surface) => surface !== null)) {
      throw new Error("Basic cleanup observation applicability is invalid")
    }
  } else if (surfaces.namespace === null || surfaces.corpus !== null || surfaces.config !== null ||
      [surfaces.descriptor, surfaces.process_group, surfaces.work_root]
        .some((surface) => surface === null)) {
    throw new Error("Exomem cleanup observation applicability is invalid")
  }
  if (observation.process_binding !== null) {
    const processBinding = record(observation.process_binding)
    exactKeys(processBinding, ["pid", "process_start_identity"])
    if (!Number.isSafeInteger(processBinding.pid) || Number(processBinding.pid) <= 0 ||
        typeof processBinding.process_start_identity !== "string" || !processBinding.process_start_identity) {
      throw new Error("cleanup observation process binding is invalid")
    }
  }
  return { ...observation, surfaces } as unknown as PrivateCleanupObservation
}

async function artifactObservation(
  reference: ArtifactReference,
  runPlan: RunPlan,
  scope: "target" | "final",
  targetHmac: string | null,
): Promise<PrivateCleanupObservation> {
  if (reference.root !== "output" || reference.path === null) {
    throw new Error("cleanup observation evidence must be private output evidence")
  }
  const expectedPath = scope === "final"
    ? "cleanup-evidence/final.json"
    : `cleanup-evidence/target-${targetHmac}.json`
  if (reference.path !== expectedPath) throw new Error("cleanup observation path is invalid")
  const path = resolve(runPlan.output_root, reference.path)
  if (!strictChild(path, runPlan.output_root)) throw new Error("cleanup evidence escapes output")
  const bytes = await secureRead(path)
  if (sha256(bytes) !== reference.sha256) throw new Error("cleanup evidence digest mismatch")
  return validateObservation(parseJson(bytes, "cleanup observation evidence"), runPlan, scope, targetHmac)
}

function observationTargetClaim(observation: PrivateCleanupObservation): Pick<CleanupTargetProof, "outcome" | "failure_code" | "absence"> {
  const absence = observationAbsence(observation)
  if (observation.operation_result === "clear_failed") {
    return { outcome: "clear_failed", failure_code: "clear_failed", absence }
  }
  if (observation.operation_result === "attach_failed") {
    const alreadyAbsent = observation.provider === "basic-memory"
      ? absence.namespace === true && absence.corpus === true
      : absence.descriptor === true && absence.work_root === true
    return alreadyAbsent
      ? { outcome: "already_absent", failure_code: null, absence }
      : { outcome: "absence_unproved", failure_code: observation.operation_failure_code, absence }
  }
  const failure = absenceFailure(absence)
  return {
    outcome: failure === null ? "cleared" : "absence_unproved",
    failure_code: failure,
    absence,
  }
}

export async function observePersistedCleanup(
  planPath: string,
  proofPath: string,
): Promise<boolean> {
  const plan = await parseCleanupPlan(planPath)
  const runPlan = await boundRunPlan(plan)
  const proof = await validateCleanupProof(parseJson(await secureRead(proofPath), "cleanup proof"))
  if (proof.run_id !== plan.run_id || proof.provider !== plan.provider ||
      proof.provider_variant !== plan.provider_variant) return false
  const expectedTargets = plan.targets.map((target) =>
    `${target.container_tag_hmac_sha256}\0${target.discovery_sources.join("\0")}`)
  const actualTargets = proof.targets.map((target) =>
    `${target.container_tag_hmac_sha256}\0${target.discovery_sources.join("\0")}`)
  if (expectedTargets.join("\n") !== actualTargets.join("\n")) return false
  const sourceFailures = new Set<CleanupFailureCode>()
  const processBindings = new Map<number, PrivateCleanupObservation["process_binding"]>()
  for (const target of proof.targets) {
    if (target.artifacts.length !== 1) return false
    const observation = await artifactObservation(
      target.artifacts[0], runPlan, "target", target.container_tag_hmac_sha256,
    )
    const expected = observationTargetClaim(observation)
    if (target.outcome !== expected.outcome || target.failure_code !== expected.failure_code ||
        SURFACE_KEYS.some((surface) => target.absence[surface] !== expected.absence[surface])) return false
    if (observation.process_binding) processBindings.set(observation.process_binding.pid, observation.process_binding)
    if (expected.failure_code) sourceFailures.add(expected.failure_code)
  }
  if (proof.final_absence.artifacts.length !== 1) return false
  const finalObservation = await artifactObservation(
    proof.final_absence.artifacts[0], runPlan, "final", null,
  )
  const finalObserved = observationAbsence(finalObservation)
  if (proof.basic_public_cleanup_calls !== finalObservation.basic_finalization_calls ||
      proof.final_absence.config !== (finalObserved.config === true) ||
      proof.final_absence.descriptor !== (finalObserved.descriptor === true) ||
      proof.final_absence.process_group !== (finalObserved.process_group === true) ||
      proof.final_absence.work_root !== (finalObserved.work_root === true)) return false
  if (finalObservation.process_binding) {
    processBindings.set(finalObservation.process_binding.pid, finalObservation.process_binding)
  }
  for (const [surface, code] of [
    ["config", "config_absence_unproved"],
    ["descriptor", "descriptor_insecure"],
    ["process_group", "process_group_absence_unproved"],
    ["work_root", "work_root_absence_unproved"],
  ] as const) if (finalObserved[surface] !== true) sourceFailures.add(code)
  if (proof.failure_codes.join("\0") !== [...sourceFailures].sort().join("\0")) return false

  for (const binding of processBindings.values()) {
    if (binding === null || !await ownedProcessGroupAbsent({ pid: binding.pid } as ServiceDescriptor)) {
      return false
    }
  }

  const processAbsent = await ownedGuestProcessesAbsent({
    provider: runPlan.provider,
    provider_checkout_root: runPlan.provider_checkout.root,
    memorybench_home: runPlan.memorybench_home,
    guest_work_root: runPlan.guest_work_root,
  })
  const serviceRoot = join(runPlan.guest_work_root, "services", runPlan.provider)
  const serviceRootAbsent = await pathAbsent(serviceRoot)
  const configAbsent = (await pathAbsent(join(serviceRoot, "config.json"))) &&
    (await pathAbsent(join(serviceRoot, "basic-config", "config.json")))
  let targetsAbsent = true
  for (const target of plan.targets) {
    if (runPlan.provider === "exomem") {
      const work = exomemTargetWork(runPlan, target)
      targetsAbsent = targetsAbsent && await pathAbsent(work) &&
        await pathAbsent(join(work, "service.v1.json"))
    } else {
      const namespace = `mb-${createHash("sha256").update(target.container_tag, "utf8").digest("hex").slice(0, 24)}`
      targetsAbsent = targetsAbsent &&
        await pathAbsent(join(serviceRoot, "corpora", namespace)) && serviceRootAbsent
    }
  }
  return proof.all_absent === true && targetsAbsent && configAbsent &&
    serviceRootAbsent && processAbsent
}

export async function main(
  argv: string[] = process.argv.slice(2),
  dependencies: CleanupDependencies = {},
): Promise<number> {
  if (argv.length === 5 && argv[0] === "--validate-only" && argv[1] === "--plan" && argv[3] === "--proof") {
    const observedAbsent = await observePersistedCleanup(argv[2], argv[4])
    ;(dependencies.stdout ?? ((line: string) => process.stdout.write(line)))(
      `${JSON.stringify({ observed_absent: observedAbsent })}\n`
    )
    return observedAbsent ? 0 : 3
  }
  if (argv.length !== 2 || argv[0] !== "--plan") throw new Error("cleanup accepts only --plan")
  const plan = await parseCleanupPlan(argv[1])
  const rawTrigger = process.env.MEMORYBENCH_CLEANUP_TRIGGER ?? "success"
  if (!["success", "stage_failure", "export_failure", "SIGINT", "SIGTERM"].includes(rawTrigger)) {
    throw new Error("cleanup trigger is invalid")
  }
  const proof = await executeCleanup(plan, dependencies, rawTrigger as CleanupProof["trigger"])
  const validated = await validateCleanupProof(proof)
  ;(dependencies.stdout ?? ((line: string) => process.stdout.write(line)))(`${JSON.stringify(validated)}\n`)
  return validated.all_absent ? 0 : 3
}

if (import.meta.main) {
  try {
    process.exitCode = await main()
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
    process.exitCode = 3
  }
}
