import { describe, expect, test } from "bun:test"
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { EXOMEM_LME_ENV } from "../_guest_transport"
import type { UnifiedSession } from "../../types/unified"
import { ExomemProvider, prepareExomemRetirement } from "../exomem"

const sessions: UnifiedSession[] = [
  {
    sessionId: "answer_session_abs-session-0",
    messages: [{ role: "user", content: "Tea < coffee." }],
    metadata: { date: "2025-01-02", formattedDate: "January 2, 2025" },
  },
  {
    sessionId: "answer_session_abs-session-1",
    messages: [{ role: "assistant", content: "Coffee is available." }],
    metadata: { date: "2025-01-03" },
  },
]

function actualDoctorReport() {
  return {
    success: true,
    profile: "hybrid",
    checks: [
      { id: "embeddings.enabled", status: "pass", message: "enabled", remediation: null },
      { id: "dep.sentence-transformers", status: "pass", message: "available", remediation: null },
      { id: "dep.torch", status: "pass", message: "available", remediation: null },
      { id: "dep.pillow", status: "pass", message: "available", remediation: null },
      { id: "models.cache", status: "pass", message: "cached", remediation: null },
      { id: "embeddings.sidecar", status: "pass", message: "live", remediation: null },
    ],
  }
}

function harness() {
  const service = {
    protocol_version: 1 as const,
    provider: "exomem" as const,
    base_url: "http://127.0.0.1:41234",
    bearer_token: "random-rest-key",
    pid: process.pid,
    process_start_identity: "fixture-start",
    checkout_pin: "fixture-pin",
    work_root: "/fixture/work/services/exomem/digest",
    evidence_root: "/fixture/evidence/exomem/digest",
  }
  const posts: Array<{ path: string; body: Record<string, unknown>; token: string }> = []
  const post = async (actual: typeof service, path: string, body: Record<string, unknown>) => {
    posts.push({ path, body, token: actual.bearer_token })
    if (path === "/api/capture_source") {
      return { path: `Knowledge Base/Sources/Sessions/${body.slug}.md` }
    }
    if (path === "/api/ask_memory") {
      return [
        { path: "Knowledge Base/Sources/Sessions/a.md", title: "a" },
        { path: "Knowledge Base/Sources/Sessions/b.md", title: "b" },
      ]
    }
    if (path === "/api/read_memory") {
      return { body: `body:${body.path}` }
    }
    throw new Error(`unexpected path ${path}`)
  }
  const doctor = async () => actualDoctorReport()
  const cleared: string[] = []
  return { service, posts, post, doctor, cleared }
}

describe("Exomem guest provider", () => {
  test("singleton ingest calls and resumed providers preserve the upstream session ordinal", async () => {
    const h = harness()
    const create = () => new ExomemProvider({ ensureService: async () => h.service, post: h.post, doctor: h.doctor })
    const provider = create()
    await provider.ingest([sessions[0]], { containerTag: "case-run" })
    await provider.ingest([sessions[1]], { containerTag: "case-run" })
    await create().ingest([sessions[1]], { containerTag: "case-run" })
    expect(String(h.posts[0].body.content)).toContain("Session ordinal: 1")
    expect(String(h.posts[1].body.content)).toContain("Session ordinal: 2")
    for (const field of ["content", "title", "slug", "tags", "source_type", "compile_guidance"]) {
      expect(h.posts[2].body[field]).toEqual(h.posts[1].body[field])
    }
  })

  test("provider evidence records its requested CPU environment", async () => {
    const root = await mkdtemp(join(tmpdir(), "exomem-profile-evidence-"))
    try {
      const h = harness()
      const service = { ...h.service, evidence_root: root }
      const provider = new ExomemProvider({
        ensureService: async () => service,
        post: h.post,
        doctor: h.doctor,
        clearService: async () => {},
      })
      // Exercise real evidence persistence while replacing only service I/O.
      ;(provider as unknown as { evidenceEnabled: boolean }).evidenceEnabled = true
      await provider.ingest(sessions.slice(0, 1), { containerTag: "profile-evidence" })
      const entries = await Promise.all((await readdir(root)).filter((name) => name.endsWith(".json"))
        .map(async (name) => JSON.parse(await readFile(join(root, name), "utf8"))))
      const manifest = entries.find((entry) => entry.event === "provider-manifest")
      expect(manifest.data.deterministic_profile.requested_settings).toEqual(EXOMEM_LME_ENV)
      expect(manifest.data.deterministic_profile.requested_settings.EXOMEM_DEVICE).toBe("cpu")
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })

  test("declares sequential concurrency and accepts no API key", async () => {
    const h = harness()
    const provider = new ExomemProvider({
      ensureService: async () => h.service,
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
    })
    expect(provider.concurrency).toEqual({ default: 1, ingest: 1, indexing: 1, search: 1 })
    await provider.initialize({ apiKey: "none" })
  })

  test("ingests sessions sequentially with neutral titles and stable mutation identities", async () => {
    const h = harness()
    const provider = new ExomemProvider({
      ensureService: async () => h.service,
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
    })
    await provider.initialize({ apiKey: "none" })
    const result = await provider.ingest(sessions, { containerTag: "question_answer_abs-run" })

    expect(result.documentIds).toEqual(sessions.map((session) => session.sessionId))
    expect(h.posts.map((call) => call.path)).toEqual(["/api/capture_source", "/api/capture_source"])
    for (const call of h.posts) {
      expect(call.token).toBe("random-rest-key")
      expect(call.body.source_type).toBe("session")
      expect(call.body.compile_guidance).toBe(false)
      expect(String(call.body.title)).not.toContain("_abs")
      expect(String(call.body.slug)).not.toContain("_abs")
      expect(call.body.request_id).toMatch(/^[0-9a-f-]{36}$/)
      expect(call.body.idempotency_key).toBe(call.body.request_id)
    }
  })

  test("one stage instance isolates and routes two containers independently", async () => {
    const h = harness()
    const services = new Map([
      ["container-a", { ...h.service, base_url: "http://127.0.0.1:41001", bearer_token: "token-a", container_tag: "container-a" }],
      ["container-b", { ...h.service, base_url: "http://127.0.0.1:41002", bearer_token: "token-b", container_tag: "container-b" }],
    ])
    const ensured: string[] = []
    const routed: Array<{ tag: string | undefined; token: string; path: string }> = []
    const provider = new ExomemProvider({
      ensureService: async (tag) => {
        ensured.push(tag)
        const service = services.get(tag)
        if (!service) throw new Error("unknown fixture container")
        return service
      },
      post: async (service, path, body) => {
        routed.push({ tag: service.container_tag, token: service.bearer_token, path })
        if (path === "/api/capture_source") return { path: `${body.slug}.md` }
        if (path === "/api/ask_memory") return [{ path: `${service.container_tag}.md` }]
        return { body: `body:${service.container_tag}` }
      },
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
      retireService: async () => {},
    })

    await provider.ingest(sessions.slice(0, 1), { containerTag: "container-a" })
    await provider.ingest(sessions.slice(1), { containerTag: "container-b" })
    await provider.search("a", { containerTag: "container-a", limit: 1 })
    await provider.search("b", { containerTag: "container-b", limit: 1 })

    expect(new Set(ensured)).toEqual(new Set(["container-a", "container-b"]))
    expect(routed.filter((call) => call.tag === "container-a").every((call) => call.token === "token-a")).toBe(true)
    expect(routed.filter((call) => call.tag === "container-b").every((call) => call.token === "token-b")).toBe(true)
    expect(h.cleared).toEqual(["container-a", "container-b"])
  })

  test("default residency never exceeds one service across five sequential container tags", async () => {
    const h = harness()
    const active = new Set<string>()
    const retired: string[] = []
    let peak = 0
    const provider = new ExomemProvider({
      ensureService: async (tag) => {
        active.add(tag)
        peak = Math.max(peak, active.size)
        return {
          ...h.service,
          container_tag: tag,
          base_url: `http://127.0.0.1:${41_000 + active.size}`,
          bearer_token: `token-${tag}`,
          work_root: `/fixture/work/services/exomem/${tag}`,
          evidence_root: `/fixture/evidence/exomem/${tag}`,
        }
      },
      post: h.post,
      doctor: h.doctor,
      retireService: async (service) => {
        const tag = service.container_tag!
        expect(active.has(tag)).toBe(true)
        active.delete(tag)
        retired.push(tag)
      },
      clearService: async (tag) => { active.delete(tag) },
    })

    for (const tag of ["container-1", "container-2", "container-3", "container-4", "container-5"]) {
      await provider.ingest(sessions.slice(0, 1), { containerTag: tag })
    }

    expect(peak).toBe(1)
    expect(active).toEqual(new Set(["container-5"]))
    expect(retired).toEqual(["container-1", "container-2", "container-3", "container-4"])
  })

  test("residency proves derived state before retiring an evicted service", async () => {
    const h = harness()
    const events: string[] = []
    const provider = new ExomemProvider({
      ensureService: async (tag) => ({
        ...h.service,
        container_tag: tag,
        work_root: `/fixture/work/${tag}`,
        evidence_root: `/fixture/evidence/${tag}`,
      }),
      post: h.post,
      doctor: h.doctor,
      prepareRetirement: async (service) => events.push(`prepare:${service.container_tag}`),
      retireService: async (service) => events.push(`retire:${service.container_tag}`),
      clearService: async () => {},
    })

    await provider.ingest(sessions.slice(0, 1), { containerTag: "container-a" })
    await provider.ingest(sessions.slice(0, 1), { containerTag: "container-b" })

    expect(events).toEqual(["prepare:container-a", "retire:container-a"])
  })

  test("a failed retirement barrier refuses eviction and clears every live service", async () => {
    const h = harness()
    const events: string[] = []
    const provider = new ExomemProvider({
      ensureService: async (tag) => ({
        ...h.service,
        container_tag: tag,
        work_root: `/fixture/work/${tag}`,
        evidence_root: `/fixture/evidence/${tag}`,
      }),
      post: h.post,
      doctor: h.doctor,
      prepareRetirement: async () => { throw new Error("derived state not current") },
      retireService: async () => events.push("retired"),
      clearService: async () => {},
      clearAllServices: async () => events.push("cleared-all"),
    })

    await provider.ingest(sessions.slice(0, 1), { containerTag: "container-a" })
    await expect(
      provider.ingest(sessions.slice(0, 1), { containerTag: "container-b" })
    ).rejects.toThrow("derived state not current")

    expect(events).toEqual(["cleared-all"])
  })

  test("the default retirement barrier performs and verifies a graph-current reconcile", async () => {
    const h = harness()
    const calls: Array<{ service: unknown; attemptId: string }> = []

    await prepareExomemRetirement(h.service, async (service, attemptId) => {
      calls.push({ service, attemptId })
      return { graph_status: "refreshed" }
    })

    expect(calls).toHaveLength(1)
    expect(calls[0].service).toBe(h.service)
    expect(calls[0].attemptId).toMatch(/^[0-9a-f-]{36}$/)

    await expect(
      prepareExomemRetirement(h.service, async () => ({ graph_status: "unavailable" }))
    ).rejects.toThrow("graph-current")
  })

  test("the retirement barrier retries only exact stabilization exhaustion", async () => {
    const h = harness()
    const requestIds: string[] = []
    let attempts = 0

    await prepareExomemRetirement(h.service, async (_service, attemptId) => {
      attempts += 1
      requestIds.push(attemptId)
      if (attempts === 1) {
        return {
          graph_status: "unavailable",
          graph_sync_code: "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
        }
      }
      return { graph_status: "current" }
    })

    expect(attempts).toBe(2)
    expect(new Set(requestIds).size).toBe(2)
  })

  test("the retirement barrier bounds repeated stabilization exhaustion", async () => {
    const h = harness()
    let attempts = 0

    await expect(
      prepareExomemRetirement(h.service, async () => {
        attempts += 1
        return {
          graph_status: "unavailable",
          graph_sync_code: "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
        }
      })
    ).rejects.toThrow("graph-current")

    expect(attempts).toBe(3)
  })

  test("a separate stage instance reattaches the requested container service", async () => {
    const h = harness()
    const service = { ...h.service, container_tag: "container-b", bearer_token: "reattached-b" }
    const ensured: string[] = []
    const provider = new ExomemProvider({
      ensureService: async (tag) => { ensured.push(tag); return service },
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
    })
    const result = await provider.ingest(sessions.slice(0, 1), { containerTag: "container-b" })
    await provider.awaitIndexing(result, "container-b")
    expect(ensured).toEqual(["container-b"])
  })

  test("capture content uses canonical neutral sessions without changing dataset text", async () => {
    const h = harness()
    const provider = new ExomemProvider({ ensureService: async () => h.service, post: h.post, doctor: h.doctor })
    await provider.ingest(sessions, { containerTag: "container" })
    const captures = h.posts.filter((call) => call.path === "/api/capture_source")
    expect(captures[0].body.content).toBe(
      'Session timestamp: 2025-01-02T00:00:00Z\nSession ordinal: 1\n\nuser: Tea < coffee.'
    )
    expect(captures[1].body.content).toBe(
      'Session timestamp: 2025-01-03T00:00:00Z\nSession ordinal: 2\n\nassistant: Coffee is available.'
    )
  })

  test("awaitIndexing requires doctor hybrid success and all semantic checks", async () => {
    const h = harness()
    const provider = new ExomemProvider({
      ensureService: async () => h.service,
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
      retireService: async (service) => {
        expect(service).toBe(h.service)
        h.cleared.push("retired:container")
      },
    })
    await provider.initialize({ apiKey: "none" })
    const result = await provider.ingest(sessions.slice(0, 1), { containerTag: "container" })
    const progress: unknown[] = []
    await provider.awaitIndexing(result, "container", (value) => progress.push(value))
    expect(progress).toEqual([
      { completedIds: [sessions[0].sessionId], failedIds: [], total: 1 },
    ])
    expect(h.cleared).toEqual(["retired:container"])
  })

  test.each(["question", "indexing"] as const)(
    "%s failure clears every live service before the error escapes",
    async (phase) => {
      const h = harness()
      const active = new Set<string>()
      const cleanupSnapshots: string[][] = []
      const provider = new ExomemProvider({
        ensureService: async (tag) => {
          active.add(tag)
          return { ...h.service, container_tag: tag }
        },
        post: phase === "question" ? async () => { throw new Error("question exploded") } : h.post,
        doctor: phase === "indexing" ? async () => { throw new Error("indexing exploded") } : h.doctor,
        retireService: async () => {},
        clearService: async (tag) => { active.delete(tag) },
        clearAllServices: async () => {
          cleanupSnapshots.push([...active])
          active.clear()
        },
      })

      if (phase === "question") {
        await expect(provider.search("question", { containerTag: "container", limit: 1 })).rejects.toThrow(
          "question exploded"
        )
      } else {
        const result = await provider.ingest(sessions.slice(0, 1), { containerTag: "container" })
        await expect(provider.awaitIndexing(result, "container")).rejects.toThrow("indexing exploded")
      }

      expect(cleanupSnapshots).toEqual([["container"]])
      expect(active.size).toBe(0)
    }
  )

  test.each(["warning", "failure", "missing", "wrong-profile"])(
    "doctor %s refuses indexing",
    async (failure) => {
      const h = harness()
      const base = await h.doctor()
      const doctor = async () => {
        if (failure === "warning") {
          return { ...base, checks: base.checks.map((check) => check.id === "models.cache" ? { ...check, status: "warn" } : check) }
        }
        if (failure === "failure") {
          return {
            ...base,
            success: false,
            checks: base.checks.map((check) => check.id === "embeddings.sidecar" ? { ...check, status: "fail" } : check),
          }
        }
        if (failure === "missing") {
          return { ...base, checks: base.checks.filter((check) => check.id !== "dep.sentence-transformers") }
        }
        return { ...base, profile: "lean" }
      }
      const provider = new ExomemProvider({
        ensureService: async () => h.service,
        post: h.post,
        doctor,
        clearService: async (tag) => h.cleared.push(tag),
      })
      await provider.initialize({ apiKey: "none" })
      const result = await provider.ingest(sessions.slice(0, 1), { containerTag: "container" })
      await expect(provider.awaitIndexing(result, "container")).rejects.toThrow(/doctor|hybrid|embedding|warning/)
    }
  )

  test.each(["duplicate", "malformed"])("doctor refuses %s check identifiers", async (kind) => {
    const h = harness()
    const base = await h.doctor()
    const doctor = async () => ({
      ...base,
      checks: kind === "duplicate"
        ? [...base.checks, { ...base.checks[0] }]
        : base.checks.map((check, index) => index === 0 ? { ...check, id: 7 } : check),
    })
    const provider = new ExomemProvider({ ensureService: async () => h.service, post: h.post, doctor })
    const result = await provider.ingest(sessions.slice(0, 1), { containerTag: "container" })
    await expect(provider.awaitIndexing(result, "container")).rejects.toThrow(/doctor|check|duplicate|malformed/)
  })

  test("search sends exact hybrid full request, reads every selected path, and returns flat bodies", async () => {
    const h = harness()
    const provider = new ExomemProvider({
      ensureService: async () => h.service,
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
    })
    await provider.initialize({ apiKey: "none" })
    const results = await provider.search("preferred drink", { containerTag: "container", limit: 2 })
    expect(h.posts[0]).toMatchObject({
      path: "/api/ask_memory",
      body: { query: "preferred drink", limit: 2, scope: "kb", mode: "hybrid", detail: "full" },
    })
    expect(Object.keys(h.posts[0].body).sort()).toEqual(["detail", "limit", "mode", "query", "scope"])
    expect(h.posts.slice(1).map((call) => call.path)).toEqual(["/api/read_memory", "/api/read_memory"])
    expect(results).toEqual([
      { content: "body:Knowledge Base/Sources/Sessions/a.md", score: 0.0 },
      { content: "body:Knowledge Base/Sources/Sessions/b.md", score: 0.0 },
    ])
    expect(h.cleared).toEqual(["container"])
  })

  test.each(["warming", "degraded", "overlimit", "missing-path"])(
    "search refuses %s response",
    async (failure) => {
      const h = harness()
      const post = async (service: typeof h.service, path: string, body: Record<string, unknown>) => {
        if (path === "/api/ask_memory") {
          if (failure === "warming") return { status: "warming", data: [] }
          if (failure === "degraded") return { status: "degraded", data: [] }
          if (failure === "overlimit") return [{ path: "a" }, { path: "b" }, { path: "c" }]
          return [{ title: "no path" }]
        }
        return h.post(service, path, body)
      }
      const provider = new ExomemProvider({
        ensureService: async () => h.service,
        post,
        doctor: h.doctor,
        clearService: async (tag) => h.cleared.push(tag),
      })
      await provider.initialize({ apiKey: "none" })
      await expect(provider.search("query", { containerTag: "container", limit: 2 })).rejects.toThrow(
        /warming|degraded|limit|path|response/
      )
    }
  )

  test("clear on an absent container never creates a service", async () => {
    const h = harness()
    let ensureCalls = 0
    const provider = new ExomemProvider({
      ensureService: async () => { ensureCalls += 1; return h.service },
      post: h.post,
      doctor: h.doctor,
      clearService: async (tag) => h.cleared.push(tag),
    })
    await provider.initialize({ apiKey: "none" })
    await provider.clear("container-a")
    expect(ensureCalls).toBe(0)
    expect(h.cleared).toEqual(["container-a"])
  })
})
