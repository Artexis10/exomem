import { spawn, spawnSync } from "node:child_process"
import { randomBytes } from "node:crypto"
import { constants as fsConstants } from "node:fs"
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join, relative, resolve } from "node:path"
import { fileURLToPath } from "node:url"

export const GUEST_TIMEOUTS_MS = {
  startup: 30_000,
  ingest: 180_000,
  search: 70_000,
  cleanup: 120_000,
} as const

export const EXOMEM_READINESS_TIMEOUT_MS = 60_000
export const exomem_authored_transport = true
export const latency_publishable = false
export const EXOMEM_LME_PROFILE_PROVENANCE = "benchmarks/lme/adapter.py::lme_profile"
export const EXOMEM_LME_ENV = {
  EXOMEM_DISABLE_EMBEDDINGS: "",
  EXOMEM_DISABLE_WARMUP: "1",
  EXOMEM_DISABLE_FILE_WATCHER: "1",
  EXOMEM_DISABLE_MODE_WATCH: "1",
  EXOMEM_DISABLE_CORPUS_CACHE: "1",
  EXOMEM_VEC_BACKEND: "numpy",
  EXOMEM_LEXICAL_BACKEND: "python",
  HF_HUB_OFFLINE: "1",
  TRANSFORMERS_OFFLINE: "1",
} as const

export function buildExomemChildEnvironment(
  ambient: Record<string, string | undefined>,
  owned: Record<string, string>
): Record<string, string> {
  const result: Record<string, string> = {}
  for (const [key, value] of Object.entries({ ...ambient, ...EXOMEM_LME_ENV, ...owned })) {
    if (typeof value === "string") result[key] = value
  }
  return result
}

export type GuestProvider = "basic-memory" | "exomem"

export interface ServiceDescriptor {
  protocol_version: 1
  provider: GuestProvider
  base_url: string
  bearer_token: string
  pid: number
  process_start_identity: string
  checkout_pin: string
  work_root: string
  evidence_root: string
  checkout_root?: string
  container_tag?: string
  vault_root?: string
  instance_id?: string
}

export interface DescriptorExpectation {
  protocol_version?: 1
  provider: GuestProvider
  checkout_pin: string
  work_root: string
  evidence_root: string
  checkout_root?: string
  container_tag?: string
  vault_root?: string
  expected_command?: string[]
  expected_environment?: Record<string, string>
  expected_instance_id?: string
  require_process_group_leader?: boolean
  process_start_identity?: string
}

export interface GuestEnvelope {
  protocol_version: 1
  request_id: string
  [key: string]: unknown
}

type Fetch = typeof fetch

function requireAbsoluteRoot(name: string, value: string | undefined): string {
  if (!value) throw new Error(`${name} is required`)
  const normalized = resolve(value)
  if (normalized !== value) throw new Error(`${name} must be an absolute normalized path`)
  return normalized
}

export async function sha256Hex(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : new Uint8Array(value)
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")
}

export function descriptorPath(workRoot: string, provider: GuestProvider): string {
  return join(resolve(workRoot), "services", provider, "service.v1.json")
}

export async function processStartIdentity(pid: number): Promise<string> {
  if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error("invalid process identity")
  try {
    const contents = await readFile(`/proc/${pid}/stat`, "utf8")
    const close = contents.lastIndexOf(")")
    const fields = contents.slice(close + 2).split(" ")
    const startTicks = fields[19]
    if (!startTicks) throw new Error("missing process start identity")
    return `linux-proc-v1:${pid}:${startTicks}`
  } catch {
    try {
      process.kill(pid, 0)
    } catch {
      throw new Error("process is not live")
    }
    return `process-v1:${pid}`
  }
}

function stableDescriptor(value: ServiceDescriptor): string {
  return `${JSON.stringify(value, null, 2)}\n`
}

export async function writeSecureDescriptor(path: string, descriptor: ServiceDescriptor): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.tmp-${process.pid}-${crypto.randomUUID()}`
  const handle = await open(temporary, "wx", 0o600)
  try {
    await handle.writeFile(stableDescriptor(descriptor), "utf8")
    await handle.sync()
  } finally {
    await handle.close()
  }
  await chmod(temporary, 0o600)
  await rename(temporary, path)
}

function exactBound(actual: ServiceDescriptor, expected: DescriptorExpectation): void {
  for (const key of ["provider", "checkout_pin", "work_root", "evidence_root"] as const) {
    if (actual[key] !== expected[key]) throw new Error(`${key.replaceAll("_", " ")} mismatch`)
  }
  if (expected.container_tag !== undefined && actual.container_tag !== expected.container_tag) {
    throw new Error("container tag mismatch")
  }
  if (expected.vault_root !== undefined && actual.vault_root !== expected.vault_root) {
    throw new Error("vault root mismatch")
  }
  if (expected.checkout_root !== undefined && actual.checkout_root !== expected.checkout_root) {
    throw new Error("checkout root mismatch")
  }
  if (expected.process_start_identity !== undefined &&
      actual.process_start_identity !== expected.process_start_identity) {
    throw new Error("process start identity mismatch")
  }
}

function requireExactDescriptorSchema(
  actual: Record<string, unknown>
): asserts actual is Record<string, unknown> & ServiceDescriptor {
  const baseKeys = [
    "protocol_version", "provider", "base_url", "bearer_token", "pid",
    "process_start_identity", "checkout_pin", "work_root", "evidence_root",
  ]
  const optionalKeys = ["checkout_root", "instance_id"]
  if (actual.provider === "exomem") optionalKeys.push("container_tag", "vault_root")
  const allowed = new Set([...baseKeys, ...optionalKeys])
  if (Object.keys(actual).some((key) => !allowed.has(key)) || baseKeys.some((key) => !(key in actual))) {
    throw new Error("descriptor schema has an unknown or missing field")
  }
  if (actual.protocol_version !== 1 || !["basic-memory", "exomem"].includes(String(actual.provider)) ||
      typeof actual.base_url !== "string" || typeof actual.bearer_token !== "string" || !actual.bearer_token ||
      !Number.isSafeInteger(actual.pid) || Number(actual.pid) <= 0 ||
      typeof actual.process_start_identity !== "string" || !actual.process_start_identity ||
      typeof actual.checkout_pin !== "string" || !actual.checkout_pin ||
      typeof actual.work_root !== "string" || typeof actual.evidence_root !== "string") {
    throw new Error("descriptor schema is invalid")
  }
  for (const key of optionalKeys) {
    if (key in actual && typeof actual[key] !== "string") throw new Error("descriptor schema is invalid")
  }
  let parsed: URL
  try { parsed = new URL(actual.base_url) } catch { throw new Error("descriptor base URL is invalid") }
  if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || !parsed.port ||
      parsed.username || parsed.password || (parsed.pathname !== "/" && parsed.pathname !== "")) {
    throw new Error("descriptor base URL must be loopback HTTP")
  }
}

async function processGroupIdentity(pid: number): Promise<number> {
  const contents = await readFile(`/proc/${pid}/stat`, "utf8")
  const close = contents.lastIndexOf(")")
  const fields = contents.slice(close + 2).split(" ")
  const group = Number(fields[2])
  if (!Number.isSafeInteger(group) || group <= 0) throw new Error("process group identity is invalid")
  return group
}

async function verifyProcessBinding(actual: ServiceDescriptor, expected: DescriptorExpectation): Promise<void> {
  const liveIdentity = await processStartIdentity(actual.pid)
  if (liveIdentity !== actual.process_start_identity) throw new Error("stale process identity mismatch")
  if (expected.require_process_group_leader && await processGroupIdentity(actual.pid) !== actual.pid) {
    throw new Error("descriptor process group ownership mismatch")
  }
  const expectedCommand = expected.expected_command ??
    (actual.provider === "exomem" && expected.checkout_root
      ? exomemServiceCommand(expected.checkout_root, Number(new URL(actual.base_url).port))
      : undefined)
  if (expectedCommand) {
    const command = (await readFile(`/proc/${actual.pid}/cmdline`))
      .toString("utf8").split("\0").filter(Boolean)
    if (JSON.stringify(command) !== JSON.stringify(expectedCommand)) {
      throw new Error("descriptor process command mismatch")
    }
  }
  if (expected.expected_environment || expected.expected_instance_id) {
    const values = (await readFile(`/proc/${actual.pid}/environ`))
      .toString("utf8").split("\0").filter(Boolean)
    const environment = new Map(values.map((entry) => {
      const separator = entry.indexOf("=")
      return [entry.slice(0, separator), entry.slice(separator + 1)]
    }))
    for (const [key, value] of Object.entries(expected.expected_environment ?? {})) {
      if (environment.get(key) !== value) throw new Error("descriptor process environment mismatch")
    }
    if (expected.expected_instance_id !== undefined &&
        (actual.instance_id !== expected.expected_instance_id ||
         environment.get("MEMORYBENCH_GUEST_INSTANCE_ID") !== expected.expected_instance_id)) {
      throw new Error("descriptor process instance mismatch")
    }
    const tokenKey = actual.provider === "exomem" ? "EXOMEM_REST_API_KEY" : "MEMORYBENCH_GUEST_BEARER_TOKEN"
    if (environment.get(tokenKey) !== actual.bearer_token) throw new Error("descriptor process authentication mismatch")
  }
}

export async function readSecureDescriptor(
  path: string,
  expected: DescriptorExpectation
): Promise<ServiceDescriptor> {
  let handle
  try {
    handle = await open(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ELOOP") throw new Error("descriptor must be opened no-follow")
    throw error
  }
  let raw: string
  try {
    const metadata = await handle.stat()
    if (!metadata.isFile()) throw new Error("descriptor must be a no-follow regular file")
    if ((metadata.mode & 0o777) !== 0o600) throw new Error("descriptor mode must be 0600")
    if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
      throw new Error("descriptor owner mismatch")
    }
    raw = await handle.readFile("utf8")
  } finally {
    await handle.close()
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new Error("descriptor JSON is invalid")
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("descriptor schema is invalid")
  requireExactDescriptorSchema(parsed as Record<string, unknown>)
  const actual = parsed as ServiceDescriptor
  exactBound(actual, expected)
  await verifyProcessBinding(actual, expected)
  return actual
}

function retryableNetworkError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  const code = (error as Error & { code?: string; cause?: { code?: string } }).code ??
    (error as Error & { cause?: { code?: string } }).cause?.code
  return code === "ECONNRESET" || code === "ECONNREFUSED"
}

function exactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  return Object.keys(value).sort().join("\0") === [...expected].sort().join("\0")
}

function safeRemoteError(): Error {
  return new Error("guest request refused")
}

function validateGuestResponseEnvelope(
  parsed: unknown,
  requestId: string
): { ok: true; data: unknown } | {
  ok: false
  retryable: boolean
  retryAfterMs: number | null
} {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("guest response envelope is invalid")
  }
  const envelope = parsed as Record<string, unknown>
  if (envelope.protocol_version !== 1 || envelope.request_id !== requestId || typeof envelope.ok !== "boolean") {
    throw new Error("guest response envelope is invalid")
  }
  if (envelope.ok === true) {
    if (!exactKeys(envelope, ["protocol_version", "request_id", "ok", "data"])) {
      throw new Error("guest response envelope is invalid")
    }
    return { ok: true, data: envelope.data }
  }
  if (!exactKeys(envelope, ["protocol_version", "request_id", "ok", "error"]) ||
      !envelope.error || typeof envelope.error !== "object" || Array.isArray(envelope.error)) {
    throw new Error("guest response envelope is invalid")
  }
  const error = envelope.error as Record<string, unknown>
  if (!exactKeys(error, ["code", "message", "retryable", "retry_after_ms", "evidence_ref"]) ||
      typeof error.code !== "string" || !error.code || typeof error.message !== "string" ||
      typeof error.retryable !== "boolean" ||
      !(error.retry_after_ms === null ||
        (Number.isSafeInteger(error.retry_after_ms) && Number(error.retry_after_ms) >= 0 && Number(error.retry_after_ms) <= 2_000)) ||
      !(error.evidence_ref === null || typeof error.evidence_ref === "string")) {
    throw new Error("guest response envelope is invalid")
  }
  return {
    ok: false,
    retryable: error.retryable,
    retryAfterMs: error.retry_after_ms as number | null,
  }
}

export async function postJsonWithRetry<T>({
  url,
  token,
  envelope,
  timeoutMs,
  fetcher = fetch,
  sleep = (milliseconds) => new Promise<void>((resolveSleep) => setTimeout(resolveSleep, milliseconds)),
}: {
  url: string
  token: string
  envelope: GuestEnvelope | Record<string, unknown>
  timeoutMs: number
  fetcher?: Fetch
  sleep?: (milliseconds: number) => Promise<void>
}): Promise<T> {
  if (!url.startsWith("http://127.0.0.1:") && !url.startsWith("http://localhost:")) {
    throw new Error("guest transport requires loopback HTTP")
  }
  const body = JSON.stringify(envelope)
  const requestId = envelope.request_id
  if (typeof requestId !== "string") throw new Error("guest request envelope is invalid")
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const controller = new AbortController()
    const deadline = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await fetcher(url, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body,
        signal: controller.signal,
      })
      const contentType = response.headers.get("content-type") ?? ""
      if (!contentType.toLowerCase().startsWith("application/json")) {
        throw new Error("guest response is not JSON")
      }
      let parsed: unknown
      try {
        parsed = await response.json()
      } catch {
        throw new Error("guest response is invalid JSON")
      }
      const responseEnvelope = validateGuestResponseEnvelope(parsed, requestId)
      if (!responseEnvelope.ok && response.status >= 500 && responseEnvelope.retryable && attempt === 0) {
        await sleep(responseEnvelope.retryAfterMs ?? 250)
        continue
      }
      if (!response.ok || !responseEnvelope.ok) throw safeRemoteError()
      return responseEnvelope.data as T
    } catch (error) {
      if (attempt === 0 && retryableNetworkError(error)) {
        await sleep(250)
        continue
      }
      throw error
    } finally {
      clearTimeout(deadline)
    }
  }
  throw new Error("guest request retry budget exhausted")
}

export async function postSidecar<T>(
  service: ServiceDescriptor,
  route: string,
  body: Record<string, unknown>
): Promise<T> {
  const deadline = route === "/v1/ingest" ? GUEST_TIMEOUTS_MS.ingest :
    route === "/v1/search" ? GUEST_TIMEOUTS_MS.search : GUEST_TIMEOUTS_MS.cleanup
  try {
    return await postJsonWithRetry<T>({
      url: `${service.base_url}${route}`,
      token: service.bearer_token,
      envelope: body,
      timeoutMs: deadline,
    })
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      let expected: DescriptorExpectation | undefined
      if (service.provider === "basic-memory" && service.instance_id && service.checkout_root) {
        expected = await basicDescriptorExpectation(
          service.checkout_root, service.checkout_pin, service.work_root, service.evidence_root
        )
        if (expected.expected_instance_id !== service.instance_id) {
          throw new Error("Basic Memory instance mismatch")
        }
      }
      await retireOwnedProcessGroup(service, "operation_deadline", expected)
    }
    throw error
  }
}

export async function postExomem<T>(
  service: ServiceDescriptor,
  path: string,
  body: Record<string, unknown>
): Promise<T> {
  if (!path.startsWith("/api/")) throw new Error("Exomem guest route is not an API route")
  const requestId = body.request_id
  const idempotencyKey = body.idempotency_key
  if (path === "/api/capture_source" &&
      (typeof requestId !== "string" || typeof idempotencyKey !== "string" || requestId !== idempotencyKey)) {
    throw new Error("Exomem mutation identity is missing or unstable")
  }
  const payload = { ...body }
  delete payload.request_id
  delete payload.idempotency_key
  const bytes = JSON.stringify(payload)
  const timeoutMs = path === "/api/capture_source" ? GUEST_TIMEOUTS_MS.ingest : GUEST_TIMEOUTS_MS.search
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const controller = new AbortController()
    const deadline = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await fetch(`${service.base_url}${path}`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${service.bearer_token}`,
          "content-type": "application/json",
          ...(typeof requestId === "string" ? { "x-exomem-request-id": requestId } : {}),
          ...(typeof idempotencyKey === "string" ? { "idempotency-key": idempotencyKey } : {}),
        },
        body: bytes,
        signal: controller.signal,
      })
      if (!(response.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
        throw new Error("Exomem response is not JSON")
      }
      let envelope: unknown
      try { envelope = await response.json() } catch { throw new Error("Exomem response is invalid JSON") }
      if (!envelope || typeof envelope !== "object") throw new Error("Exomem response envelope is invalid")
      const result = envelope as {
        success?: unknown
        data?: unknown
        error?: { retryable?: unknown; retry_after_ms?: unknown }
      }
      if (response.status >= 500 && result.error?.retryable === true && attempt === 0) {
        const requested = result.error.retry_after_ms
        const delay = Number.isSafeInteger(requested) && Number(requested) >= 0 && Number(requested) <= 2_000
          ? Number(requested) : 250
        await new Promise((resolveSleep) => setTimeout(resolveSleep, delay))
        continue
      }
      if (!response.ok || result.success !== true || !("data" in result)) {
        throw new Error("Exomem request refused")
      }
      return result.data as T
    } catch (error) {
      if (attempt === 0 && retryableNetworkError(error)) {
        await new Promise((resolveSleep) => setTimeout(resolveSleep, 250))
        continue
      }
      throw error
    } finally {
      clearTimeout(deadline)
    }
  }
  throw new Error("Exomem request retry budget exhausted")
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue)
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalValue(item)])
    )
  }
  return value
}

function scrubEvidence(value: unknown, secrets: string[]): unknown {
  if (Array.isArray(value)) return value.map((item) => scrubEvidence(item, secrets))
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !/(token|api.?key|authorization|environment|payload)/i.test(key))
        .map(([key, item]) => [key, scrubEvidence(item, secrets)])
    )
  }
  if (typeof value === "string") {
    let safe = value
    const home = homedir()
    const sensitive = new Set(secrets.filter(Boolean))
    if (home) sensitive.add(home)
    for (const match of safe.matchAll(/\/home\/[^/\s"']+(?:\/[^\s"']*)?/g)) sensitive.add(match[0])
    for (const secret of [...sensitive].sort((left, right) => right.length - left.length)) {
      const bytes = Buffer.from(secret, "utf8")
      const variants = new Set([
        secret,
        bytes.toString("base64"),
        bytes.toString("base64url"),
        bytes.toString("hex"),
      ])
      for (const variant of [...variants].sort((left, right) => right.length - left.length)) {
        safe = safe.replaceAll(variant, "<redacted>")
      }
    }
    if (home) safe = safe.replaceAll(home, "<home>")
    return safe
  }
  return value
}

export async function appendGuestEvidence(
  service: ServiceDescriptor,
  event: string,
  data: Record<string, unknown>
): Promise<{ path: string; sha256: string }> {
  await mkdir(service.evidence_root, { recursive: true, mode: 0o700 })
  await chmod(service.evidence_root, 0o700)
  const entry = canonicalValue({
    protocol_version: 1,
    event,
    recorded_at_utc: new Date().toISOString(),
    data: scrubEvidence(data, [service.bearer_token]),
  })
  const bytes = `${JSON.stringify(entry)}\n`
  const digest = await sha256Hex(bytes)
  let sequence = 0
  let reservation = ""
  let handle: Awaited<ReturnType<typeof open>> | null = null
  for (;;) {
    const existing = await readdir(service.evidence_root)
    const maximum = existing.reduce((current, name) => {
      const match = name.match(/^(?:operation-|\.operation-sequence-)(\d{6})(?:-|\.)/)
      return match ? Math.max(current, Number(match[1])) : current
    }, 0)
    sequence = maximum + 1
    reservation = join(service.evidence_root, `.operation-sequence-${String(sequence).padStart(6, "0")}.lock`)
    try {
      const candidate = await open(reservation, "wx", 0o600)
      const published = (await readdir(service.evidence_root)).some((name) =>
        name.startsWith(`operation-${String(sequence).padStart(6, "0")}-`)
      )
      if (published) {
        await candidate.close()
        await rm(reservation, { force: true })
        continue
      }
      handle = candidate
      break
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") continue
      throw error
    }
  }
  if (handle === null) throw new Error("evidence sequence allocation failed")
  const relative = `operation-${String(sequence).padStart(6, "0")}-${digest.slice(0, 12)}.json`
  try {
    await handle.writeFile(bytes, "utf8")
    await handle.sync()
  } finally {
    await handle.close()
  }
  try {
    await rename(reservation, join(service.evidence_root, relative))
  } catch (error) {
    await rm(reservation, { force: true })
    throw error
  }
  return { path: relative, sha256: digest }
}

async function acquireLaunchLock(path: string): Promise<() => Promise<void>> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  let handle
  try {
    handle = await open(path, "wx", 0o600)
  } catch {
    throw new Error("guest service launch is already in progress")
  }
  await handle.writeFile(`${process.pid}\n`, "utf8")
  return async () => {
    await handle.close()
    await rm(path, { force: true })
  }
}

function configuredRoots(provider: GuestProvider, containerTag?: string) {
  const programmeWork = requireAbsoluteRoot("MEMORYBENCH_GUEST_WORK_ROOT", process.env.MEMORYBENCH_GUEST_WORK_ROOT)
  const programmeEvidence = requireAbsoluteRoot(
    "MEMORYBENCH_GUEST_EVIDENCE_ROOT",
    process.env.MEMORYBENCH_GUEST_EVIDENCE_ROOT
  )
  if (provider === "basic-memory") {
    return {
      work: join(programmeWork, "services", provider),
      evidence: join(programmeEvidence, provider),
      descriptor: descriptorPath(programmeWork, provider),
    }
  }
  if (!containerTag) throw new Error("container tag is required")
  return sha256Hex(containerTag).then((digest) => ({
    work: join(programmeWork, "services", provider, digest.slice(0, 24)),
    evidence: join(programmeEvidence, provider, digest.slice(0, 24)),
    descriptor: join(programmeWork, "services", provider, digest.slice(0, 24), "service.v1.json"),
  }))
}

function basicMemoryCommand(checkout: string): string[] {
  return [
    "uv", "run", "--project", join(checkout, "benchmarks"), "--no-sync", "python",
    join(dirname(fileURLToPath(import.meta.url)), "basic-memory", "sidecar.py"),
  ]
}

function exomemServiceCommand(exomemHome: string, port: number): string[] {
  return [
    "uv", "run", "--project", exomemHome, "--no-sync", "exomem",
    "--transport", "http", "--host", "127.0.0.1", "--port", String(port),
  ]
}

async function serviceInstanceId(values: Record<string, string>): Promise<string> {
  return `memorybench-v1-${(await sha256Hex(JSON.stringify(canonicalValue(values)))).slice(0, 32)}`
}

async function basicDescriptorExpectation(
  checkout: string,
  pin: string,
  work: string,
  evidence: string
): Promise<DescriptorExpectation> {
  const instance = await serviceInstanceId({ provider: "basic-memory", checkout, pin, work, evidence })
  return {
    protocol_version: 1,
    provider: "basic-memory",
    checkout_pin: pin,
    checkout_root: checkout,
    work_root: work,
    evidence_root: evidence,
    expected_command: basicMemoryCommand(checkout),
    expected_environment: {
      BASIC_MEMORY_HOME: checkout,
      MEMORYBENCH_GUEST_WORK_ROOT: work,
      MEMORYBENCH_GUEST_EVIDENCE_ROOT: evidence,
      MEMORYBENCH_GUEST_PROVIDER: "basic-memory",
      UV_NO_SYNC: "1",
      UV_OFFLINE: "1",
    },
    expected_instance_id: instance,
    require_process_group_leader: true,
  }
}

async function exomemDescriptorExpectation(
  containerTag: string,
  exomemHome: string,
  pin: string,
  work: string,
  evidence: string,
  vault: string,
  port?: number
): Promise<DescriptorExpectation> {
  const instance = await serviceInstanceId({
    provider: "exomem", container_tag: containerTag, checkout: exomemHome, pin, work, evidence, vault,
  })
  return {
    protocol_version: 1,
    provider: "exomem",
    checkout_pin: pin,
    checkout_root: exomemHome,
    work_root: work,
    evidence_root: evidence,
    container_tag: containerTag,
    vault_root: vault,
    ...(port === undefined ? {} : { expected_command: exomemServiceCommand(exomemHome, port) }),
    expected_environment: {
      ...EXOMEM_LME_ENV,
      EXOMEM_VAULT_PATH: vault,
      MEMORYBENCH_GUEST_PROVIDER: "exomem",
    },
    expected_instance_id: instance,
    require_process_group_leader: true,
  }
}

async function waitForReady(
  child: ReturnType<typeof spawn>,
  timeoutMs: number
): Promise<{ base_url: string }> {
  return new Promise((resolveReady, rejectReady) => {
    let bytes = ""
    const timer = setTimeout(() => rejectReady(new Error("guest service startup deadline exceeded")), timeoutMs)
    child.once("exit", (code) => {
      clearTimeout(timer)
      rejectReady(new Error(`guest service exited during startup (${code ?? "signal"})`))
    })
    child.stdout?.on("data", (chunk: Buffer) => {
      bytes += chunk.toString("utf8")
      const lines = bytes.split("\n")
      bytes = lines.pop() ?? ""
      for (const line of lines) {
        try {
          const event = JSON.parse(line) as { protocol_version?: number; event?: string; base_url?: string }
          if (event.protocol_version === 1 && event.event === "ready" &&
              typeof event.base_url === "string" && event.base_url.startsWith("http://127.0.0.1:")) {
            clearTimeout(timer)
            resolveReady({ base_url: event.base_url })
          }
        } catch {
          // Startup stdout is protocol-only; non-events are ignored and never evidenced.
        }
      }
    })
  })
}

async function serviceProjection(service: ServiceDescriptor): Promise<void> {
  await mkdir(service.evidence_root, { recursive: true, mode: 0o700 })
  const { bearer_token: secret, ...publicService } = service
  const projection = scrubEvidence(publicService, [secret])
  const path = join(service.evidence_root, "service.json")
  await writeFile(path, `${JSON.stringify(projection, null, 2)}\n`, { mode: 0o600 })
}

async function currentDescriptor(path: string, expected: DescriptorExpectation): Promise<ServiceDescriptor | null> {
  try {
    return await readSecureDescriptor(path, expected)
  } catch (error) {
    try {
      await lstat(path)
    } catch {
      return null
    }
    throw error
  }
}

export interface CleanupAttachBinding {
  provider: GuestProvider
  provider_checkout: { root: string; commit: string }
  guest_work_root: string
  guest_evidence_root: string
}

export async function attachBasicMemoryService(
  binding: CleanupAttachBinding
): Promise<ServiceDescriptor> {
  if (binding.provider !== "basic-memory") throw new Error("Basic Memory cleanup binding mismatch")
  const checkout = requireAbsoluteRoot("provider checkout", binding.provider_checkout.root)
  const programmeWork = requireAbsoluteRoot("guest work root", binding.guest_work_root)
  const programmeEvidence = requireAbsoluteRoot("guest evidence root", binding.guest_evidence_root)
  const work = join(programmeWork, "services", "basic-memory")
  const evidence = join(programmeEvidence, "basic-memory")
  const expected: DescriptorExpectation = await basicDescriptorExpectation(
    checkout, binding.provider_checkout.commit, work, evidence
  )
  return readSecureDescriptor(descriptorPath(programmeWork, "basic-memory"), expected)
}

export async function attachExomemService(
  containerTag: string,
  binding: CleanupAttachBinding
): Promise<ServiceDescriptor> {
  if (binding.provider !== "exomem") throw new Error("Exomem cleanup binding mismatch")
  const checkout = requireAbsoluteRoot("provider checkout", binding.provider_checkout.root)
  const programmeWork = requireAbsoluteRoot("guest work root", binding.guest_work_root)
  const programmeEvidence = requireAbsoluteRoot("guest evidence root", binding.guest_evidence_root)
  const digest = await sha256Hex(containerTag)
  const work = join(programmeWork, "services", "exomem", digest.slice(0, 24))
  const evidence = join(programmeEvidence, "exomem", digest.slice(0, 24))
  const vault = join(work, "vault")
  const expected: DescriptorExpectation = await exomemDescriptorExpectation(
    containerTag, checkout, binding.provider_checkout.commit, work, evidence, vault
  )
  return readSecureDescriptor(join(work, "service.v1.json"), expected)
}

export async function ensureBasicMemoryService(): Promise<ServiceDescriptor> {
  const roots = await configuredRoots("basic-memory")
  const checkout = requireAbsoluteRoot("BASIC_MEMORY_HOME", process.env.BASIC_MEMORY_HOME)
  const pin = process.env.BASIC_MEMORY_COMMIT ?? "816accaa9befe8281668ba8819eaf74d11ce2385"
  await mkdir(roots.work, { recursive: true, mode: 0o700 })
  await mkdir(roots.evidence, { recursive: true, mode: 0o700 })
  const expected = await basicDescriptorExpectation(checkout, pin, roots.work, roots.evidence)
  const attached = await currentDescriptor(roots.descriptor, expected)
  if (attached) return attached
  const release = await acquireLaunchLock(join(roots.work, "launch.lock"))
  let child: ReturnType<typeof spawn> | null = null
  let startIdentity: string | null = null
  const token = randomBytes(32).toString("base64url")
  try {
    child = spawn(
      basicMemoryCommand(checkout)[0],
      basicMemoryCommand(checkout).slice(1),
      {
        cwd: checkout,
        detached: true,
        env: {
          ...process.env,
          BASIC_MEMORY_HOME: checkout,
          MEMORYBENCH_GUEST_WORK_ROOT: roots.work,
          MEMORYBENCH_GUEST_EVIDENCE_ROOT: roots.evidence,
          MEMORYBENCH_GUEST_BEARER_TOKEN: token,
          MEMORYBENCH_GUEST_PROVIDER: "basic-memory",
          MEMORYBENCH_GUEST_INSTANCE_ID: expected.expected_instance_id!,
          UV_NO_SYNC: "1",
          UV_OFFLINE: "1",
        },
        stdio: ["ignore", "pipe", "ignore"],
      }
    )
    if (!child.pid) throw new Error("Basic Memory sidecar did not expose a pid")
    startIdentity = await processStartIdentity(child.pid)
    const ready = await waitForReady(child, GUEST_TIMEOUTS_MS.startup)
    const descriptor: ServiceDescriptor = {
      protocol_version: 1,
      provider: "basic-memory",
      base_url: ready.base_url,
      bearer_token: token,
      pid: child.pid,
      process_start_identity: startIdentity,
      checkout_pin: pin,
      checkout_root: checkout,
      work_root: roots.work,
      evidence_root: roots.evidence,
      instance_id: expected.expected_instance_id,
    }
    await serviceProjection(descriptor)
    await writeSecureDescriptor(roots.descriptor, descriptor)
    return descriptor
  } catch (error) {
    if (child?.pid && startIdentity) {
      await retireOwnedProcessGroup({
        protocol_version: 1,
        provider: "basic-memory",
        base_url: "http://127.0.0.1:1",
        bearer_token: token,
        pid: child.pid,
        process_start_identity: startIdentity,
        checkout_pin: pin,
        checkout_root: checkout,
        work_root: roots.work,
        evidence_root: roots.evidence,
        instance_id: expected.expected_instance_id,
      }, "startup_failure", expected)
    }
    throw error
  } finally {
    await release()
  }
}

async function reservePort(): Promise<number> {
  const script = "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()"
  const result = spawnSync(process.env.PYTHON ?? "python3", ["-c", script], { encoding: "utf8" })
  const port = Number(result.stdout.trim())
  if (result.status !== 0 || !Number.isSafeInteger(port) || port <= 0) throw new Error("cannot reserve loopback port")
  return port
}

async function waitForExomem(baseUrl: string, token: string, child: ReturnType<typeof spawn>): Promise<void> {
  const started = Date.now()
  while (Date.now() - started < EXOMEM_READINESS_TIMEOUT_MS) {
    if (child.exitCode !== null) throw new Error("Exomem service exited during startup")
    try {
      const response = await fetch(`${baseUrl}/api/openapi.json`, {
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(1_000),
      })
      if (response.ok) return
    } catch {
      // Refusal during the bounded readiness window is expected.
    }
    await new Promise((resolveSleep) => setTimeout(resolveSleep, 250))
  }
  throw new Error("Exomem readiness deadline exceeded")
}

export async function ensureExomemService(containerTag: string): Promise<ServiceDescriptor> {
  const roots = await configuredRoots("exomem", containerTag)
  const exomemHome = requireAbsoluteRoot("EXOMEM_HOME", process.env.EXOMEM_HOME)
  const pin = process.env.EXOMEM_COMMIT ?? "owned-checkout"
  const vault = join(roots.work, "vault")
  await mkdir(roots.work, { recursive: true, mode: 0o700 })
  await mkdir(roots.evidence, { recursive: true, mode: 0o700 })
  const expected = await exomemDescriptorExpectation(
    containerTag, exomemHome, pin, roots.work, roots.evidence, vault
  )
  const attached = await currentDescriptor(roots.descriptor, expected)
  if (attached) return attached
  const release = await acquireLaunchLock(join(roots.work, "launch.lock"))
  try {
    const init = spawnSync("uv", ["run", "--project", exomemHome, "--no-sync", "exomem", "init", "--vault", vault], {
      cwd: roots.work,
      env: buildExomemChildEnvironment(process.env, {
        EXOMEM_VAULT_PATH: vault,
        MEMORYBENCH_GUEST_PROVIDER: "exomem",
        MEMORYBENCH_GUEST_INSTANCE_ID: expected.expected_instance_id!,
      }),
      encoding: "utf8",
      timeout: GUEST_TIMEOUTS_MS.startup,
    })
    if (init.status !== 0) throw new Error("Exomem vault initialization failed")
    const token = randomBytes(32).toString("base64url")
    let lastError: unknown
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const port = await reservePort()
      const baseUrl = `http://127.0.0.1:${port}`
      const attemptExpected = await exomemDescriptorExpectation(
        containerTag, exomemHome, pin, roots.work, roots.evidence, vault, port
      )
      const child = spawn(
        exomemServiceCommand(exomemHome, port)[0],
        exomemServiceCommand(exomemHome, port).slice(1),
        {
          cwd: roots.work,
          detached: true,
          env: buildExomemChildEnvironment(process.env, {
            EXOMEM_VAULT_PATH: vault,
            EXOMEM_REST_API_KEY: token,
            MEMORYBENCH_GUEST_PROVIDER: "exomem",
            MEMORYBENCH_GUEST_INSTANCE_ID: expected.expected_instance_id!,
          }),
          stdio: ["ignore", "ignore", "ignore"],
        }
      )
      let startIdentity: string | null = null
      try {
        if (!child.pid) throw new Error("Exomem service did not expose a pid")
        startIdentity = await processStartIdentity(child.pid)
        await waitForExomem(baseUrl, token, child)
        const descriptor: ServiceDescriptor = {
          protocol_version: 1,
          provider: "exomem",
          base_url: baseUrl,
          bearer_token: token,
          pid: child.pid,
          process_start_identity: startIdentity,
          checkout_pin: pin,
          checkout_root: exomemHome,
          work_root: roots.work,
          evidence_root: roots.evidence,
          container_tag: containerTag,
          vault_root: vault,
          instance_id: expected.expected_instance_id,
        }
        await serviceProjection(descriptor)
        await writeSecureDescriptor(roots.descriptor, descriptor)
        return descriptor
      } catch (error) {
        lastError = error
        if (child.pid && startIdentity) {
          await retireOwnedProcessGroup({
            protocol_version: 1,
            provider: "exomem",
            base_url: baseUrl,
            bearer_token: token,
            pid: child.pid,
            process_start_identity: startIdentity,
            checkout_pin: pin,
            checkout_root: exomemHome,
            work_root: roots.work,
            evidence_root: roots.evidence,
            container_tag: containerTag,
            vault_root: vault,
            instance_id: expected.expected_instance_id,
          }, "startup_failure", attemptExpected)
        }
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Exomem launch attempts exhausted")
  } finally {
    await release()
  }
}

export async function runExomemDoctor(service: ServiceDescriptor): Promise<unknown> {
  if (!service.vault_root) throw new Error("Exomem vault binding missing")
  const exomemHome = requireAbsoluteRoot("EXOMEM_HOME", process.env.EXOMEM_HOME)
  const result = spawnSync(
    "uv",
    ["run", "--project", exomemHome, "--no-sync", "exomem", "doctor", "--vault", service.vault_root, "--profile", "hybrid", "--json"],
    {
      cwd: service.work_root,
      env: buildExomemChildEnvironment(process.env, {
        EXOMEM_VAULT_PATH: service.vault_root,
        MEMORYBENCH_GUEST_PROVIDER: "exomem",
        MEMORYBENCH_GUEST_INSTANCE_ID: service.instance_id ?? "invalid-missing-instance",
      }),
      encoding: "utf8",
      timeout: GUEST_TIMEOUTS_MS.search,
    }
  )
  if (result.status !== 0) throw new Error("Exomem doctor failed")
  try {
    return JSON.parse(result.stdout)
  } catch {
    throw new Error("Exomem doctor returned non-JSON output")
  }
}

async function processIsLive(pid: number): Promise<boolean> {
  try {
    const contents = await readFile(`/proc/${pid}/stat`, "utf8")
    const close = contents.lastIndexOf(")")
    return contents.slice(close + 2).split(" ")[0] !== "Z"
  } catch {
    try { process.kill(pid, 0); return true } catch { return false }
  }
}

async function liveProcessGroupMembers(group: number): Promise<number[]> {
  const members: number[] = []
  for (const entry of await readdir("/proc", { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
    try {
      const contents = await readFile(`/proc/${entry.name}/stat`, "utf8")
      const close = contents.lastIndexOf(")")
      const fields = contents.slice(close + 2).split(" ")
      if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
    } catch {
      // Processes may exit between the directory scan and stat read.
    }
  }
  return members
}

async function strictlyObservedLiveProcessGroupMembers(group: number): Promise<number[]> {
  const members: number[] = []
  for (const entry of await readdir("/proc", { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
    try {
      const contents = await readFile(`/proc/${entry.name}/stat`, "utf8")
      const close = contents.lastIndexOf(")")
      if (close < 0) throw new Error("process stat is malformed")
      const fields = contents.slice(close + 2).split(" ")
      if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
    } catch (error) {
      if (["ENOENT", "ESRCH"].includes((error as NodeJS.ErrnoException).code ?? "")) continue
      throw error
    }
  }
  return members
}

/** Fail closed unless the complete owned process group is observed absent twice. */
export async function ownedProcessGroupAbsent(service: ServiceDescriptor | null): Promise<boolean> {
  if (service === null) return true
  try {
    if ((await strictlyObservedLiveProcessGroupMembers(service.pid)).length !== 0) return false
    await new Promise((resolveTick) => setTimeout(resolveTick, 0))
    return (await strictlyObservedLiveProcessGroupMembers(service.pid)).length === 0
  } catch {
    return false
  }
}

export interface GuestProcessScanBinding {
  provider: GuestProvider
  provider_checkout_root: string
  memorybench_home: string
  guest_work_root: string
}

function strictChild(path: string, root: string): boolean {
  const suffix = relative(root, path)
  return suffix !== "" && suffix !== ".." &&
    !suffix.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`) &&
    resolve(root, suffix) === path
}

function parseProcCommandLine(bytes: Uint8Array): string[] | null {
  if (bytes.length === 0 || bytes[bytes.length - 1] !== 0) return null
  let decoded: string
  try {
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
  } catch {
    return null
  }
  const command = decoded.split("\0")
  if (command.pop() !== "" || command.length === 0 || command.some((argument) => !argument)) return null
  return command
}

function parseProcStat(pid: string, line: string): { state: string; startTime: string } | null {
  const close = line.lastIndexOf(")")
  if (!line.startsWith(`${pid} (`) || close < pid.length + 3) return null
  const fields = line.slice(close + 2).split(" ")
  if (fields.length < 20 || !/^[A-Z]$/.test(fields[0]) || !/^\d+$/.test(fields[19])) return null
  return { state: fields[0], startTime: fields[19] }
}

function exactGuestCommand(command: string[], binding: GuestProcessScanBinding): boolean {
  if (binding.provider === "basic-memory") {
    const expected = [
      "uv", "run", "--project", join(binding.provider_checkout_root, "benchmarks"),
      "--no-sync", "python",
      join(binding.memorybench_home, "src", "providers", "basic-memory", "sidecar.py"),
    ]
    return command.length === expected.length && command.every((argument, index) => argument === expected[index])
  }
  const expectedPrefix = [
    "uv", "run", "--project", binding.provider_checkout_root, "--no-sync", "exomem",
    "--transport", "http", "--host", "127.0.0.1", "--port",
  ]
  return command.length === expectedPrefix.length + 1 &&
    expectedPrefix.every((argument, index) => command[index] === argument) &&
    /^[1-9]\d{0,4}$/.test(command[expectedPrefix.length]) &&
    Number(command[expectedPrefix.length]) <= 65_535
}

/** Ignore unrelated same-UID processes; fail closed after exact plan-bound command classification. */
export async function ownedGuestProcessesAbsent(binding: GuestProcessScanBinding): Promise<boolean> {
  try {
    for (let observation = 0; observation < 2; observation += 1) {
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        const processRoot = `/proc/${entry.name}`
        let classified = false
        try {
          const metadata = await stat(processRoot)
          if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) continue
          const command = parseProcCommandLine(await readFile(join(processRoot, "cmdline")))
          if (!command || !exactGuestCommand(command, binding)) continue
          classified = true
          const firstStat = parseProcStat(entry.name, await readFile(join(processRoot, "stat"), "utf8"))
          if (!firstStat) return false
          if (firstStat.state === "Z") continue
          const environment = (await readFile(join(processRoot, "environ"), "utf8"))
            .split("\0").filter(Boolean)
          const secondStat = parseProcStat(entry.name, await readFile(join(processRoot, "stat"), "utf8"))
          const secondCommand = parseProcCommandLine(await readFile(join(processRoot, "cmdline")))
          if (!secondStat || secondStat.startTime !== firstStat.startTime ||
              !secondCommand || !exactGuestCommand(secondCommand, binding)) return false
          if (secondStat.state === "Z") continue
          const workBinding = environment.find((value) =>
            value.startsWith("MEMORYBENCH_GUEST_WORK_ROOT="))?.slice("MEMORYBENCH_GUEST_WORK_ROOT=".length)
          if (typeof workBinding === "string" &&
              (workBinding === binding.guest_work_root || strictChild(workBinding, binding.guest_work_root)) &&
              environment.includes(`MEMORYBENCH_GUEST_PROVIDER=${binding.provider}`)) return false
        } catch {
          if (classified) return false
          continue
        }
      }
      if (observation === 0) await new Promise((resolveTick) => setTimeout(resolveTick, 0))
    }
    return true
  } catch {
    return false
  }
}

interface LiveProcessGroupMember {
  pid: number
  start_identity: string
}

async function readLiveProcessGroupMember(pid: number, group: number): Promise<LiveProcessGroupMember | null> {
  try {
    const contents = await readFile(`/proc/${pid}/stat`, "utf8")
    const close = contents.lastIndexOf(")")
    const fields = contents.slice(close + 2).split(" ")
    if (fields[0] === "Z" || Number(fields[2]) !== group || !fields[19]) return null
    return { pid, start_identity: `linux-proc-v1:${pid}:${fields[19]}` }
  } catch (error) {
    if (["ENOENT", "ESRCH"].includes((error as NodeJS.ErrnoException).code ?? "")) return null
    throw error
  }
}

async function liveProcessGroupSnapshot(group: number): Promise<LiveProcessGroupMember[]> {
  const snapshot: LiveProcessGroupMember[] = []
  for (const pid of await liveProcessGroupMembers(group)) {
    const member = await readLiveProcessGroupMember(pid, group)
    if (member) snapshot.push(member)
  }
  return snapshot.sort((left, right) => left.pid - right.pid)
}

function sameProcessGroupSnapshot(
  left: LiveProcessGroupMember[],
  right: LiveProcessGroupMember[]
): boolean {
  return left.length === right.length && left.every((member, index) =>
    member.pid === right[index].pid && member.start_identity === right[index].start_identity
  )
}

async function verifyOrphanProcessGroupBinding(
  service: ServiceDescriptor,
  expected?: DescriptorExpectation
): Promise<LiveProcessGroupMember[]> {
  if ((await liveProcessGroupSnapshot(service.pid)).length === 0) return []
  if (!service.instance_id) throw new Error("orphaned process group instance binding is missing")
  if (expected?.expected_instance_id !== undefined && expected.expected_instance_id !== service.instance_id) {
    throw new Error("orphaned process group instance binding mismatch")
  }
  const tokenKey = service.provider === "exomem" ? "EXOMEM_REST_API_KEY" : "MEMORYBENCH_GUEST_BEARER_TOKEN"
  const bindings: Record<string, string> = {
    ...(expected?.expected_environment ?? {}),
    MEMORYBENCH_GUEST_PROVIDER: service.provider,
    MEMORYBENCH_GUEST_INSTANCE_ID: service.instance_id,
    [tokenKey]: service.bearer_token,
  }
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const before = await liveProcessGroupSnapshot(service.pid)
    if (before.length === 0) return []
    const verified: LiveProcessGroupMember[] = []
    for (const member of before) {
      let values: string[]
      try {
        values = (await readFile(`/proc/${member.pid}/environ`, "utf8")).split("\0").filter(Boolean)
      } catch (error) {
        if (["ENOENT", "ESRCH"].includes((error as NodeJS.ErrnoException).code ?? "")) continue
        throw error
      }
      const after = await readLiveProcessGroupMember(member.pid, service.pid)
      if (!after || after.start_identity !== member.start_identity) continue
      const environment = new Map(values.map((entry) => {
        const separator = entry.indexOf("=")
        return [entry.slice(0, separator), entry.slice(separator + 1)]
      }))
      for (const [key, value] of Object.entries(bindings)) {
        if (environment.get(key) !== value) throw new Error("orphaned process group environment mismatch")
      }
      verified.push(after)
    }
    const final = await liveProcessGroupSnapshot(service.pid)
    if (sameProcessGroupSnapshot(verified, final)) return final
  }
  throw new Error("orphaned process group membership did not stabilize")
}

async function waitForProcessGroupAbsence(group: number, milliseconds: number): Promise<boolean> {
  const deadline = Date.now() + milliseconds
  while (Date.now() <= deadline) {
    if ((await liveProcessGroupMembers(group)).length === 0) return true
    await new Promise((resolveSleep) => setTimeout(resolveSleep, 50))
  }
  return (await liveProcessGroupMembers(group)).length === 0
}

export async function retireOwnedProcessGroup(
  service: ServiceDescriptor,
  _reason: string,
  expected?: DescriptorExpectation
): Promise<void> {
  if (await processIsLive(service.pid)) {
    if (expected) {
      await verifyProcessBinding(service, expected)
    } else if (await processStartIdentity(service.pid) !== service.process_start_identity ||
               await processGroupIdentity(service.pid) !== service.pid) {
      throw new Error("owned process identity changed")
    }
  } else {
    const survivors = await verifyOrphanProcessGroupBinding(service, expected)
    if (survivors.length === 0) return
  }
  try { process.kill(-service.pid, "SIGTERM") } catch {
    if ((await liveProcessGroupMembers(service.pid)).length === 0) return
    throw new Error("owned process TERM failed")
  }
  if (await waitForProcessGroupAbsence(service.pid, 5_000)) return
  if (await processIsLive(service.pid)) {
    if (expected) {
      await verifyProcessBinding(service, expected)
    } else if (await processStartIdentity(service.pid) !== service.process_start_identity ||
               await processGroupIdentity(service.pid) !== service.pid) {
      throw new Error("owned process identity changed before KILL")
    }
  } else {
    const survivors = await verifyOrphanProcessGroupBinding(service, expected)
    if (survivors.length === 0) return
  }
  try { process.kill(-service.pid, "SIGKILL") } catch {
    if ((await liveProcessGroupMembers(service.pid)).length === 0) return
    throw new Error("owned process KILL failed")
  }
  if (!await waitForProcessGroupAbsence(service.pid, 5_000)) {
    throw new Error("owned process group is still live after KILL")
  }
}

export async function clearExomemService(containerTag: string): Promise<void> {
  const roots = await configuredRoots("exomem", containerTag)
  const exomemHome = requireAbsoluteRoot("EXOMEM_HOME", process.env.EXOMEM_HOME)
  const pin = process.env.EXOMEM_COMMIT ?? "owned-checkout"
  const vault = join(roots.work, "vault")
  const expected = await exomemDescriptorExpectation(
    containerTag, exomemHome, pin, roots.work, roots.evidence, vault
  )
  const service = await readSecureDescriptor(roots.descriptor, expected)
  await retireOwnedProcessGroup(service, "cleanup", expected)
  await rm(roots.work, { recursive: true, force: true })
  try { await stat(roots.work); throw new Error("owned Exomem root still exists") } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
}

export async function finalizeBasicMemoryService(service: ServiceDescriptor): Promise<void> {
  if (service.provider !== "basic-memory") throw new Error("Basic Memory cleanup descriptor mismatch")
  let expected: DescriptorExpectation | undefined
  if (service.instance_id && service.checkout_root) {
    expected = await basicDescriptorExpectation(
      service.checkout_root, service.checkout_pin, service.work_root, service.evidence_root
    )
    if (expected.expected_instance_id !== service.instance_id) throw new Error("Basic Memory instance mismatch")
  }
  await retireOwnedProcessGroup(service, "cleanup", expected)
  await rm(service.work_root, { recursive: true, force: true })
  try {
    await stat(service.work_root)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return
    throw error
  }
  throw new Error("owned Basic Memory work root still exists")
}
