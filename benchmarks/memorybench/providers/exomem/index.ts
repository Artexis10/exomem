import type {
  IndexingProgressCallback,
  IngestOptions,
  IngestResult,
  Provider,
  ProviderConfig,
  SearchOptions,
} from "../../types/provider"
import type { UnifiedSession } from "../../types/unified"
import {
  EXOMEM_LME_ENV,
  appendGuestEvidence,
  clearAllExomemServices,
  clearExomemService,
  configuredExomemMaxLiveServices,
  ensureExomemService,
  installExomemProcessCleanupHandlers,
  postExomem,
  retireExomemService,
  runExomemDoctor,
  runExomemReconcile,
  sha256Hex,
  type ServiceDescriptor,
} from "../_guest_transport"

// Launch provenance: EXOMEM_VAULT_PATH binds the owned vault and
// EXOMEM_REST_API_KEY is generated for the isolated loopback service.

type EnsureService = (containerTag: string) => Promise<ServiceDescriptor>
type Post = (
  service: ServiceDescriptor,
  path: string,
  body: Record<string, unknown>
) => Promise<unknown>
type Doctor = (service: ServiceDescriptor) => Promise<unknown>
type PrepareRetirement = (service: ServiceDescriptor) => Promise<void>
type ClearService = (containerTag: string) => Promise<void>
type RetireService = (service: ServiceDescriptor) => Promise<void>
type ClearAllServices = () => Promise<void>
type RetirementReconcile = (
  service: ServiceDescriptor,
  attemptId: string
) => Promise<unknown>

interface DoctorResult {
  success?: unknown
  profile?: unknown
  checks?: unknown
}

interface SearchSelection {
  path?: unknown
  [key: string]: unknown
}

interface ReadResponse {
  body?: unknown
}

const EXOMEM_RETIREMENT_RECONCILE_ATTEMPTS = 3

export async function prepareExomemRetirement(
  service: ServiceDescriptor,
  reconcile: RetirementReconcile = runExomemReconcile
): Promise<void> {
  for (let attempt = 0; attempt < EXOMEM_RETIREMENT_RECONCILE_ATTEMPTS; attempt += 1) {
    const raw = await reconcile(service, crypto.randomUUID())
    if (!raw || typeof raw !== "object") {
      throw new Error("Exomem retirement barrier response is invalid")
    }
    const response = raw as { graph_status?: unknown; graph_sync_code?: unknown }
    if (response.graph_status === "current" || response.graph_status === "refreshed") return
    const retryable = response.graph_status === "unavailable" &&
      response.graph_sync_code === "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    if (!retryable) break
  }
  throw new Error("Exomem retirement barrier did not prove graph-current state")
}

export class ExomemProvider implements Provider {
  name = "exomem"
  concurrency = { default: 1, ingest: 1, indexing: 1, search: 1 }
  private services = new Map<string, ServiceDescriptor>()
  private readonly ensureService: EnsureService
  private readonly post: Post
  private readonly doctor: Doctor
  private readonly prepareRetirement: PrepareRetirement
  private readonly clearService: ClearService
  private readonly retireService: RetireService
  private readonly clearAllServices: ClearAllServices
  private readonly maxLiveServices: number
  private readonly defaultTransport: boolean
  private readonly evidenceEnabled: boolean
  private manifestsWritten = new Set<string>()

  constructor(dependencies: {
    ensureService?: EnsureService
    post?: Post
    doctor?: Doctor
    prepareRetirement?: PrepareRetirement
    clearService?: ClearService
    retireService?: RetireService
    clearAllServices?: ClearAllServices
    maxLiveServices?: number
  } = {}) {
    this.defaultTransport = dependencies.ensureService === undefined &&
      dependencies.post === undefined && dependencies.doctor === undefined &&
      dependencies.prepareRetirement === undefined &&
      dependencies.clearService === undefined && dependencies.retireService === undefined &&
      dependencies.clearAllServices === undefined
    this.ensureService = dependencies.ensureService ?? ensureExomemService
    this.post = dependencies.post ?? postExomem
    this.doctor = dependencies.doctor ?? runExomemDoctor
    this.prepareRetirement = dependencies.prepareRetirement ??
      (this.defaultTransport
        ? prepareExomemRetirement
        : async () => {})
    this.clearService = dependencies.clearService ?? clearExomemService
    this.retireService = dependencies.retireService ??
      (this.defaultTransport ? retireExomemService : async () => {})
    this.clearAllServices = dependencies.clearAllServices ??
      (this.defaultTransport
        ? clearAllExomemServices
        : async () => {
            for (const tag of [...this.services.keys()]) await this.clearService(tag)
          })
    this.maxLiveServices = dependencies.maxLiveServices ?? configuredExomemMaxLiveServices()
    if (!Number.isSafeInteger(this.maxLiveServices) || this.maxLiveServices <= 0) {
      throw new Error("Exomem live-service cap must be a positive integer")
    }
    this.evidenceEnabled = this.defaultTransport
  }

  async initialize(_config: ProviderConfig): Promise<void> {
    // The local service owns its generated REST key; MemoryBench supplies no API key.
    if (this.defaultTransport) installExomemProcessCleanupHandlers()
  }

  private async getService(containerTag: string): Promise<ServiceDescriptor> {
    let service = this.services.get(containerTag)
    if (!service) {
      while (this.services.size >= this.maxLiveServices) {
        const leastRecentlyUsed = this.services.entries().next().value as
          | [string, ServiceDescriptor]
          | undefined
        if (!leastRecentlyUsed) break
        const [retiredTag, retiredService] = leastRecentlyUsed
        await this.prepareRetirement(retiredService)
        await this.retireService(retiredService)
        this.services.delete(retiredTag)
        this.manifestsWritten.delete(retiredTag)
      }
      service = await this.ensureService(containerTag)
      this.services.set(containerTag, service)
    } else {
      this.services.delete(containerTag)
      this.services.set(containerTag, service)
    }
    if (service.provider !== "exomem" ||
        (service.container_tag !== undefined && service.container_tag !== containerTag)) {
      throw new Error("Exomem service descriptor mismatch")
    }
    if (this.evidenceEnabled && !this.manifestsWritten.has(containerTag)) {
      await appendGuestEvidence(service, "provider-manifest", {
        provider: this.name,
        protocol_version: 1,
        concurrency: this.concurrency,
        exomem_authored_transport: true,
        latency_publishable: false,
        checkout_pin: service.checkout_pin,
        checkout_root: service.checkout_root,
        deterministic_profile: {
          provenance: "benchmarks/lme/adapter.py::lme_profile",
          requested_settings: EXOMEM_LME_ENV,
          embeddings_requested: true,
          warmup: "disabled",
          file_watcher: "disabled",
          mode_watch: "disabled",
          corpus_cache: "disabled",
          vector_backend: "numpy",
          lexical_backend: "python",
          model_network: "offline",
        },
        transport: { host: "127.0.0.1", port_policy: "ephemeral", auth: "bearer" },
      })
      this.manifestsWritten.add(containerTag)
    }
    return service
  }

  private async failAfterCleanup(error: unknown): Promise<never> {
    try {
      await this.clearAllServices()
      this.services.clear()
      this.manifestsWritten.clear()
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        `Exomem operation failed and cleanup did not prove absence: ${error instanceof Error ? error.message : String(error)}`,
        { cause: error }
      )
    }
    throw error
  }

  private async request(
    service: ServiceDescriptor,
    path: string,
    body: Record<string, unknown>
  ): Promise<unknown> {
    if (this.evidenceEnabled) await appendGuestEvidence(service, "request", { path, body })
    const response = await this.post(service, path, body)
    if (this.evidenceEnabled) await appendGuestEvidence(service, "response", { path, response })
    return response
  }

  async ingest(sessions: UnifiedSession[], options: IngestOptions): Promise<IngestResult> {
    try {
      const service = await this.getService(options.containerTag)
      const documentIds: string[] = []
      for (const [position, session] of sessions.entries()) {
        const sessionString = JSON.stringify(session.messages).replace(/</g, "&lt;").replace(/>/g, "&gt;")
        const formattedDate = session.metadata?.formattedDate
        const content = typeof formattedDate === "string" && formattedDate.length > 0
          ? `Here is the date the following session took place: ${formattedDate}\n\nHere is the session as a stringified JSON:\n${sessionString}`
          : `Here is the session as a stringified JSON:\n${sessionString}`
        const digest = await sha256Hex(JSON.stringify({ container: options.containerTag, position, content }))
        const neutral = `mb-session-${digest.slice(0, 24)}`
        const requestId = crypto.randomUUID()
        const response = await this.request(service, "/api/capture_source", {
          content,
          title: neutral,
          slug: neutral,
          source_type: "session",
          compile_guidance: false,
          request_id: requestId,
          idempotency_key: requestId,
        })
        if (!response || typeof response !== "object") throw new Error("Exomem capture response is invalid")
        documentIds.push(session.sessionId)
      }
      return { documentIds }
    } catch (error) {
      return this.failAfterCleanup(error)
    }
  }

  async awaitIndexing(
    result: IngestResult,
    containerTag: string,
    onProgress?: IndexingProgressCallback
  ): Promise<void> {
    try {
      const service = await this.getService(containerTag)
      if (this.evidenceEnabled) await appendGuestEvidence(service, "doctor-request", { profile: "hybrid" })
      const raw = await this.doctor(service)
      if (this.evidenceEnabled) await appendGuestEvidence(service, "doctor-response", { response: raw })
      if (!raw || typeof raw !== "object") throw new Error("Exomem doctor response is invalid")
      const report = raw as DoctorResult
      if (report.success !== true) throw new Error("Exomem doctor did not succeed")
      if (report.profile !== "hybrid") throw new Error("Exomem doctor did not verify hybrid profile")
      if (!Array.isArray(report.checks)) throw new Error("Exomem doctor checks are malformed")
      const checks = new Map<string, string>()
      for (const value of report.checks) {
        if (!value || typeof value !== "object") throw new Error("Exomem doctor check is malformed")
        const check = value as { id?: unknown; status?: unknown }
        if (typeof check.id !== "string" || check.id.length === 0 ||
            typeof check.status !== "string" || !["pass", "warn", "fail"].includes(check.status)) {
          throw new Error("Exomem doctor check is malformed")
        }
        if (checks.has(check.id)) throw new Error("Exomem doctor check identifier is duplicated")
        checks.set(check.id, check.status)
      }
      for (const check of [
        "embeddings.enabled",
        "dep.sentence-transformers",
        "dep.torch",
        "dep.pillow",
        "models.cache",
        "embeddings.sidecar",
      ]) {
        if (checks.get(check) !== "pass") throw new Error(`Exomem doctor semantic check missing or failed: ${check}`)
      }
      onProgress?.({ completedIds: [...result.documentIds], failedIds: [], total: result.documentIds.length })
      await this.retireService(service)
      this.services.delete(containerTag)
      this.manifestsWritten.delete(containerTag)
    } catch (error) {
      await this.failAfterCleanup(error)
    }
  }

  async search(query: string, options: SearchOptions): Promise<Array<{ content: string; score: number }>> {
    try {
      const limit = options.limit
      if (!Number.isSafeInteger(limit) || (limit ?? 0) <= 0) throw new Error("Exomem search requires an exact positive limit")
      const service = await this.getService(options.containerTag)
      const raw = await this.request(service, "/api/ask_memory", {
        query,
        limit,
        scope: "kb",
        mode: "hybrid",
        detail: "full",
      })
      if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const state = (raw as { status?: unknown }).status
        if (state === "warming" || state === "degraded") throw new Error(`Exomem search refused ${state} response`)
        throw new Error("Exomem search response is invalid")
      }
      if (!Array.isArray(raw) || raw.length === 0) throw new Error("Exomem search response is empty")
      if (raw.length > limit!) throw new Error("Exomem search response exceeds exact limit")
      const results: Array<{ content: string; score: number }> = []
      for (const selected of raw as SearchSelection[]) {
        if (!selected || typeof selected.path !== "string" || selected.path.length === 0) {
          throw new Error("Exomem search response is missing a selected path")
        }
        const data = await this.request(service, "/api/read_memory", { path: selected.path }) as ReadResponse
        if (!data || typeof data.body !== "string") throw new Error("Exomem read response is invalid")
        results.push({ content: data.body, score: 0.0 })
      }
      await this.clear(options.containerTag)
      return results
    } catch (error) {
      return this.failAfterCleanup(error)
    }
  }

  async clear(containerTag: string): Promise<void> {
    const service = this.services.get(containerTag)
    if (service && this.evidenceEnabled) {
      await appendGuestEvidence(service, "cleanup-request", { container_tag: containerTag })
    }
    await this.clearService(containerTag)
    if (service && this.evidenceEnabled) {
      await appendGuestEvidence(service, "cleanup-response", { absence_proved: true })
    }
    this.services.delete(containerTag)
    this.manifestsWritten.delete(containerTag)
  }
}

export default ExomemProvider
