import { describe, expect, test } from "bun:test"
import type { UnifiedSession } from "../../types/unified"
import { BasicMemoryProvider } from "../basic-memory"

const session: UnifiedSession = {
  sessionId: "answer_session_7_abs",
  messages: [
    { role: "user", content: "I prefer tea." },
    { role: "assistant", content: "Noted." },
  ],
  metadata: { date: "2025-01-02", formattedDate: "January 2, 2025" },
}

function harness() {
  const calls: Array<{ route: string; body: Record<string, unknown> }> = []
  const service = {
    protocol_version: 1 as const,
    provider: "basic-memory" as const,
    base_url: "http://127.0.0.1:43210",
    bearer_token: "descriptor-token",
    pid: process.pid,
    process_start_identity: "fixture-start",
    checkout_pin: "816accaa9befe8281668ba8819eaf74d11ce2385",
    work_root: "/fixture/work",
    evidence_root: "/fixture/evidence",
  }
  const post = async (_service: typeof service, route: string, body: Record<string, unknown>) => {
    calls.push({ route, body })
    if (route === "/v1/ingest") {
      return {
        document_id: (body.session as { session_id: string }).session_id,
        namespace: "mb-0123456789abcdef01234567",
        readiness: {
          protocol_version: 1,
          verified: true,
          container_tag: body.container_tag,
          document_id: (body.session as { session_id: string }).session_id,
          rendered_sha256: "a".repeat(64),
          fallback_detected: false,
          evidence_refs: [{ path: "ingest/1.json", sha256: "b".repeat(64) }],
        },
      }
    }
    if (route === "/v1/search") {
      return {
        namespace: "mb-0123456789abcdef01234567",
        hits: [
          { text: "tea", score: 0.91, metadata: { title: "neutral" }, source_path: "note.md" },
          { text: null, score: null, metadata: {} },
        ],
      }
    }
    return { namespace: "mb-0123456789abcdef01234567", final: true, absence_proved: true }
  }
  return { calls, service, post }
}

describe("Basic Memory guest provider", () => {
  test("declares sequential concurrency and accepts no API key", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    expect(provider.concurrency).toEqual({ default: 1, ingest: 1, indexing: 1, search: 1 })
    await provider.initialize({ apiKey: "none" })
  })

  test("ingest preserves public identity for correlation and stores a current receipt", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    await provider.initialize({ apiKey: "none" })
    const result = await provider.ingest([session], { containerTag: "question-7-run" })
    expect(result.documentIds).toEqual(["answer_session_7_abs"])
    expect(h.calls).toHaveLength(1)
    expect(h.calls[0].route).toBe("/v1/ingest")
    expect(h.calls[0].body.container_tag).toBe("question-7-run")
    expect((h.calls[0].body.session as { session_id: string }).session_id).toBe(session.sessionId)
    expect((h.calls[0].body.session as { date: string }).date).toBe("January 2, 2025")
  })

  test("renderer date projection prefers formattedDate and falls back to date", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    const fallback: UnifiedSession = {
      ...session,
      sessionId: "fallback-session",
      metadata: { date: "2025-02-03" },
    }
    await provider.ingest([session, fallback], { containerTag: "question-7-run" })
    expect((h.calls[0].body.session as { date: string }).date).toBe("January 2, 2025")
    expect((h.calls[1].body.session as { date: string }).date).toBe("2025-02-03")
  })

  test("awaitIndexing makes no sidecar call and reports only same-container current receipts", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    await provider.initialize({ apiKey: "none" })
    const result = await provider.ingest([session], { containerTag: "question-7-run" })
    const before = h.calls.length
    const progress: unknown[] = []
    await provider.awaitIndexing(result, "question-7-run", (value) => progress.push(value))
    expect(h.calls).toHaveLength(before)
    expect(progress).toEqual([
      { completedIds: ["answer_session_7_abs"], failedIds: [], total: 1 },
    ])
    await expect(provider.awaitIndexing(result, "wrong-container")).rejects.toThrow(/container|receipt/)
    await expect(
      provider.awaitIndexing({ documentIds: ["missing-document"] }, "question-7-run")
    ).rejects.toThrow(/missing|receipt/)
  })

  test("search forwards the exact limit and returns only flat content and score", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    await provider.initialize({ apiKey: "none" })
    const results = await provider.search("preferred drink", { containerTag: "question-7-run", limit: 7 })
    expect(h.calls.at(-1)?.route).toBe("/v1/search")
    expect(h.calls.at(-1)?.body.limit).toBe(7)
    expect(results).toEqual([
      { content: "tea", score: 0.91 },
      { content: "", score: 0 },
    ])
  })

  test("clear delegates cleanup for only the owned namespace", async () => {
    const h = harness()
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    await provider.initialize({ apiKey: "none" })
    await provider.clear("question-7-run")
    expect(h.calls).toHaveLength(1)
    expect(h.calls[0]).toMatchObject({ route: "/v1/cleanup", body: { container_tag: "question-7-run" } })
  })

  test("refuses stale fallback-tainted and cross-container receipts", async () => {
    const h = harness()
    h.post = async (_service, route, body) => {
      h.calls.push({ route, body })
      return {
        document_id: session.sessionId,
        namespace: "mb-0123456789abcdef01234567",
        readiness: {
          protocol_version: 1,
          verified: true,
          container_tag: "different",
          document_id: session.sessionId,
          rendered_sha256: "a".repeat(64),
          fallback_detected: true,
          evidence_refs: [],
        },
      }
    }
    const provider = new BasicMemoryProvider({ ensureService: async () => h.service, post: h.post })
    await provider.initialize({ apiKey: "none" })
    await expect(provider.ingest([session], { containerTag: "question-7-run" })).rejects.toThrow(
      /fallback|container|receipt/
    )
  })
})
