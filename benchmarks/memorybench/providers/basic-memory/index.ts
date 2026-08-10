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
  appendGuestEvidence,
  ensureBasicMemoryService,
  finalizeBasicMemoryService,
  postSidecar,
  type ServiceDescriptor,
} from "../_guest_transport"

interface EvidenceRef {
  path: string
  sha256: string
}

interface ReadinessReceipt {
  protocol_version: 1
  verified: true
  container_tag: string
  document_id: string
  rendered_sha256: string
  fallback_detected: false
  evidence_refs: EvidenceRef[]
}

interface IngestResponse {
  document_id: string
  namespace: string
  readiness: ReadinessReceipt
}

interface SidecarHit {
  text?: string | null
  score?: number | null
  [key: string]: unknown
}

interface SearchResponse {
  namespace: string
  hits: SidecarHit[]
}

interface CleanupResponse {
  namespace: string
  final: boolean
  absence_proved: boolean
}

type EnsureService = () => Promise<ServiceDescriptor>
type Post = (
  service: ServiceDescriptor,
  route: string,
  body: Record<string, unknown>
) => Promise<unknown>
type ClearService = (service: ServiceDescriptor) => Promise<void>

export class BasicMemoryProvider implements Provider {
  name = "basic-memory"
  concurrency = { default: 1, ingest: 1, indexing: 1, search: 1 }
  private service: ServiceDescriptor | null = null
  private receipts = new Map<string, ReadinessReceipt>()
  private readonly ensureService: EnsureService
  private readonly post: Post
  private readonly clearService: ClearService
  private readonly evidenceEnabled: boolean
  private manifestWritten = false
  finalizationCalls = 0

  constructor(dependencies: { ensureService?: EnsureService; post?: Post; clearService?: ClearService } = {}) {
    this.ensureService = dependencies.ensureService ?? ensureBasicMemoryService
    this.post = dependencies.post ?? postSidecar
    this.evidenceEnabled = dependencies.ensureService === undefined && dependencies.post === undefined
    this.clearService = dependencies.clearService ??
      (this.evidenceEnabled ? finalizeBasicMemoryService : async () => {})
  }

  async initialize(_config: ProviderConfig): Promise<void> {
    // The guest owns local isolated credentials; MemoryBench supplies no API key.
  }

  private async getService(): Promise<ServiceDescriptor> {
    this.service ??= await this.ensureService()
    if (this.service.provider !== "basic-memory") throw new Error("Basic Memory service descriptor mismatch")
    if (this.evidenceEnabled && !this.manifestWritten) {
      await appendGuestEvidence(this.service, "provider-manifest", {
        provider: this.name,
        protocol_version: 1,
        concurrency: this.concurrency,
        exomem_authored_transport: true,
        latency_publishable: false,
        checkout_pin: this.service.checkout_pin,
        checkout_root: this.service.checkout_root,
      })
      this.manifestWritten = true
    }
    return this.service
  }

  private async request(
    service: ServiceDescriptor,
    route: string,
    body: Record<string, unknown>
  ): Promise<unknown> {
    if (this.evidenceEnabled) await appendGuestEvidence(service, "request", { route, body })
    const response = await this.post(service, route, body)
    if (this.evidenceEnabled) await appendGuestEvidence(service, "response", { route, response })
    return response
  }

  async ingest(sessions: UnifiedSession[], options: IngestOptions): Promise<IngestResult> {
    const service = await this.getService()
    const documentIds: string[] = []
    for (const [position, session] of sessions.entries()) {
      const response = await this.request(service, "/v1/ingest", {
        protocol_version: 1,
        request_id: crypto.randomUUID(),
        container_tag: options.containerTag,
        session: {
          session_id: session.sessionId,
          position,
          ...(typeof session.metadata?.formattedDate === "string"
            ? { date: session.metadata.formattedDate }
            : typeof session.metadata?.date === "string" ? { date: session.metadata.date } : {}),
          messages: session.messages,
        },
      }) as IngestResponse
      this.acceptReceipt(response, session.sessionId, options.containerTag)
      documentIds.push(response.document_id)
    }
    return { documentIds }
  }

  private acceptReceipt(response: IngestResponse, documentId: string, containerTag: string): void {
    const receipt = response?.readiness
    if (response?.document_id !== documentId || !receipt || receipt.protocol_version !== 1 ||
        receipt.verified !== true || receipt.container_tag !== containerTag ||
        receipt.document_id !== documentId || receipt.fallback_detected !== false ||
        !/^[0-9a-f]{64}$/.test(receipt.rendered_sha256) || !Array.isArray(receipt.evidence_refs) ||
        receipt.evidence_refs.length === 0) {
      throw new Error("Basic Memory readiness receipt is missing, stale, cross-container, or fallback-tainted")
    }
    for (const reference of receipt.evidence_refs) {
      if (!reference || typeof reference.path !== "string" || !/^[0-9a-f]{64}$/.test(reference.sha256)) {
        throw new Error("Basic Memory readiness receipt evidence is invalid")
      }
    }
    this.receipts.set(documentId, receipt)
  }

  async awaitIndexing(
    result: IngestResult,
    containerTag: string,
    onProgress?: IndexingProgressCallback
  ): Promise<void> {
    const completedIds: string[] = []
    for (const documentId of result.documentIds) {
      const receipt = this.receipts.get(documentId)
      if (!receipt) throw new Error(`missing readiness receipt for ${documentId}`)
      if (receipt.container_tag !== containerTag || receipt.document_id !== documentId ||
          receipt.verified !== true || receipt.fallback_detected !== false) {
        throw new Error(`stale or cross-container readiness receipt for ${documentId}`)
      }
      completedIds.push(documentId)
    }
    onProgress?.({ completedIds, failedIds: [], total: result.documentIds.length })
  }

  async search(query: string, options: SearchOptions): Promise<Array<{ content: string; score: number }>> {
    const limit = options.limit
    if (!Number.isSafeInteger(limit) || (limit ?? 0) <= 0) throw new Error("Basic Memory search requires an exact positive limit")
    const service = await this.getService()
    const response = await this.request(service, "/v1/search", {
      protocol_version: 1,
      request_id: crypto.randomUUID(),
      container_tag: options.containerTag,
      query,
      limit,
    }) as SearchResponse
    if (!Array.isArray(response?.hits) || response.hits.length === 0 || response.hits.length > limit!) {
      throw new Error("Basic Memory search response is empty or exceeds the exact limit")
    }
    return response.hits.map((hit) => ({
      content: hit.text ?? "",
      score: hit.score ?? 0,
    }))
  }

  async clear(containerTag: string): Promise<void> {
    const service = await this.getService()
    const response = await this.request(service, "/v1/cleanup", {
      protocol_version: 1,
      request_id: crypto.randomUUID(),
      container_tag: containerTag,
    }) as CleanupResponse
    if (!response || response.absence_proved !== true) throw new Error("Basic Memory cleanup absence was not proved")
    for (const [documentId, receipt] of this.receipts) {
      if (receipt.container_tag === containerTag) this.receipts.delete(documentId)
    }
    if (response.final === true) {
      this.finalizationCalls += 1
      await this.clearService(service)
      this.service = null
      this.manifestWritten = false
    }
  }
}

export default BasicMemoryProvider
