import { afterEach, describe, expect, test } from "bun:test"
import { spawn } from "node:child_process"
import { EventEmitter, once } from "node:events"
import { chmod, lstat, mkdtemp, readFile, readdir, rm, symlink } from "node:fs/promises"
import { createServer } from "node:http"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  GUEST_TIMEOUTS_MS,
  appendGuestEvidence,
  descriptorPath,
  finalizeBasicMemoryService,
  postJsonWithRetry,
  postExomem,
  processStartIdentity,
  readSecureDescriptor,
  sha256Hex,
  writeSecureDescriptor,
} from "../_guest_transport"

const roots: string[] = []

afterEach(async () => {
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})

async function root(): Promise<string> {
  const value = await mkdtemp(join(tmpdir(), "memorybench-guest-test-"))
  roots.push(value)
  return value
}

describe("guest transport", () => {
  test("pins exact deadlines and bounded retry policy", () => {
    expect(GUEST_TIMEOUTS_MS).toEqual({
      startup: 30_000,
      ingest: 180_000,
      search: 70_000,
      cleanup: 120_000,
    })
  })

  test("derives stable digests without exposing the preimage", async () => {
    const tagged = "question_answer_17_abs-run"
    const digest = await sha256Hex(tagged)
    expect(digest).toHaveLength(64)
    expect(digest).not.toContain("_abs")
    expect(await sha256Hex(tagged)).toBe(digest)
  })

  test("bounded admission stays within the cap across five tags and separate stage passes", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    type ResidencyRecord = { containerTag: string; lastUsed: number; service: Record<string, unknown> }
    type EnforceResidency = (options: {
      containerTag: string
      maxLiveServices: number
      discover: () => Promise<ResidencyRecord[]>
      retire: (service: Record<string, unknown>) => Promise<void>
      launch: () => Promise<Record<string, unknown>>
      touch: (service: Record<string, unknown>) => Promise<void>
    }) => Promise<Record<string, unknown>>
    const enforce = transport.enforceExomemResidency as EnforceResidency | undefined
    expect(typeof enforce).toBe("function")

    const live = new Map<string, ResidencyRecord>()
    const events: string[] = []
    let clock = 0
    let peak = 0
    const admit = async (containerTag: string) => enforce!({
      containerTag,
      maxLiveServices: 1,
      discover: async () => [...live.values()],
      retire: async (service) => {
        const tag = String(service.container_tag)
        events.push(`retire:${tag}`)
        live.delete(tag)
      },
      launch: async () => {
        expect(live.size).toBeLessThan(1)
        const service = { provider: "exomem", container_tag: containerTag }
        live.set(containerTag, { containerTag, lastUsed: ++clock, service })
        peak = Math.max(peak, live.size)
        events.push(`launch:${containerTag}`)
        return service
      },
      touch: async () => {
        const record = live.get(containerTag)
        if (record) record.lastUsed = ++clock
      },
    })

    const tags = ["container-1", "container-2", "container-3", "container-4", "container-5"]
    for (const tag of tags) await admit(tag)
    // A fresh stage process has no local provider map; descriptor discovery still sees container-5.
    for (const tag of [...tags].reverse()) await admit(tag)

    expect(peak).toBe(1)
    expect(live.size).toBe(1)
    expect(live.has("container-1")).toBe(true)
    expect(events.indexOf("retire:container-1")).toBeLessThan(events.indexOf("launch:container-2"))
  })

  test("residency admission evicts the least-recently-used service at a configured cap", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    const enforce = transport.enforceExomemResidency as undefined | ((options: Record<string, unknown>) => Promise<unknown>)
    expect(typeof enforce).toBe("function")
    const retired: string[] = []
    const records = [
      { containerTag: "recent", lastUsed: 20, service: { container_tag: "recent" } },
      { containerTag: "oldest", lastUsed: 10, service: { container_tag: "oldest" } },
    ]
    await enforce!({
      containerTag: "new",
      maxLiveServices: 2,
      discover: async () => records,
      retire: async (service: { container_tag: string }) => { retired.push(service.container_tag) },
      launch: async () => ({ container_tag: "new" }),
      touch: async () => {},
    })
    expect(retired).toEqual(["oldest"])
  })

  test.each(["SIGINT", "SIGTERM"] as const)(
    "%s handler retires every live service before re-raising",
    async (signal) => {
      const transport = await import("../_guest_transport") as Record<string, unknown>
      type Install = (
        cleanup: (trigger: string) => Promise<void>,
        dependencies: { emitter: EventEmitter; rerase: (trigger: string, error?: unknown) => void }
      ) => () => void
      const install = transport.installGuestProcessCleanupHandlers as Install | undefined
      expect(typeof install).toBe("function")
      const emitter = new EventEmitter()
      const live = new Set(["one", "two", "three"])
      let reraised = ""
      let finish!: () => void
      const done = new Promise<void>((resolveDone) => { finish = resolveDone })
      const dispose = install!(
        async () => { live.clear() },
        {
          emitter,
          rerase: (trigger) => {
            expect(live.size).toBe(0)
            reraised = trigger
            finish()
          },
        }
      )
      emitter.emit(signal)
      await done
      expect(reraised).toBe(signal)
      dispose()
    }
  )

  test("termination cleanup is idempotent while retirement is already in flight", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    const install = transport.installGuestProcessCleanupHandlers as undefined | ((
      cleanup: (trigger: string) => Promise<void>,
      dependencies: { emitter: EventEmitter; rerase: (trigger: string) => void }
    ) => () => void)
    expect(typeof install).toBe("function")
    const emitter = new EventEmitter()
    let cleanupCalls = 0
    let release!: () => void
    const retiring = new Promise<void>((resolveRetirement) => { release = resolveRetirement })
    let finish!: () => void
    const done = new Promise<void>((resolveDone) => { finish = resolveDone })
    const reraised: string[] = []
    const dispose = install!(
      async () => { cleanupCalls += 1; await retiring },
      { emitter, rerase: (trigger) => { reraised.push(trigger); finish() } }
    )
    emitter.emit("SIGINT")
    emitter.emit("SIGTERM")
    release()
    await done
    expect(cleanupCalls).toBe(1)
    expect(reraised).toEqual(["SIGINT"])
    dispose()
  })

  test("uncaught run failure cleans live services and preserves the original error", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    const install = transport.installGuestProcessCleanupHandlers as undefined | ((
      cleanup: (trigger: string) => Promise<void>,
      dependencies: { emitter: EventEmitter; rerase: (trigger: string, error?: unknown) => void }
    ) => () => void)
    expect(typeof install).toBe("function")
    const emitter = new EventEmitter()
    const live = new Set(["one", "two"])
    const original = new Error("whole run exploded")
    let observed: unknown
    let finish!: () => void
    const done = new Promise<void>((resolveDone) => { finish = resolveDone })
    const dispose = install!(
      async () => { live.clear() },
      {
        emitter,
        rerase: (_trigger, error) => {
          expect(live.size).toBe(0)
          observed = error
          finish()
        },
      }
    )
    emitter.emit("uncaughtException", original)
    await done
    expect(observed).toBe(original)
    dispose()
  })

  test("retries only reset refusal and explicit retryable responses with the same bytes", async () => {
    const bodies: string[] = []
    let attempt = 0
    const fetcher: typeof fetch = (async (_input: string | URL | Request, init?: RequestInit) => {
      bodies.push(String(init?.body))
      attempt += 1
      if (attempt === 1) throw Object.assign(new TypeError("reset"), { code: "ECONNRESET" })
      return new Response(
        JSON.stringify({ protocol_version: 1, request_id: requestId, ok: true, data: { value: 7 } }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    }) as typeof fetch
    const requestId = crypto.randomUUID()
    const envelope = { protocol_version: 1, request_id: requestId, value: "same" }

    const result = await postJsonWithRetry({
      url: "http://127.0.0.1:9/v1/test",
      token: "not-evidenced",
      envelope,
      timeoutMs: 1_000,
      fetcher,
      sleep: async () => {},
    })

    expect(result).toEqual({ value: 7 })
    expect(bodies).toEqual([JSON.stringify(envelope), JSON.stringify(envelope)])
    expect(attempt).toBe(2)
  })

  test("strict v1 error retry uses nested retryability and preserves request bytes", async () => {
    const requestId = crypto.randomUUID()
    const bodies: string[] = []
    let attempt = 0
    const fetcher: typeof fetch = (async (_input, init) => {
      bodies.push(String(init?.body))
      attempt += 1
      if (attempt === 1) {
        return new Response(JSON.stringify({
          protocol_version: 1,
          request_id: requestId,
          ok: false,
          error: {
            code: "temporarily_unavailable",
            message: "remote text must stay private",
            retryable: true,
            retry_after_ms: 10,
            evidence_ref: null,
          },
        }), { status: 503, headers: { "content-type": "application/json" } })
      }
      return new Response(JSON.stringify({ protocol_version: 1, request_id: requestId, ok: true, data: { value: 9 } }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    }) as typeof fetch
    const envelope = { protocol_version: 1, request_id: requestId, operation: "same" }
    const result = await postJsonWithRetry<{ value: number }>({
      url: "http://127.0.0.1:9/v1/ingest",
      token: "private",
      envelope,
      timeoutMs: 1_000,
      fetcher,
      sleep: async (milliseconds) => { expect(milliseconds).toBe(10) },
    })
    expect(result).toEqual({ value: 9 })
    expect(bodies).toEqual([JSON.stringify(envelope), JSON.stringify(envelope)])
  })

  test.each(["mismatched-request", "unknown-success-field", "ambiguous-error"])(
    "strict v1 envelope refuses %s",
    async (kind) => {
      const requestId = crypto.randomUUID()
      const response = kind === "mismatched-request"
        ? { protocol_version: 1, request_id: crypto.randomUUID(), ok: true, data: {} }
        : kind === "unknown-success-field"
          ? { protocol_version: 1, request_id: requestId, ok: true, data: {}, retryable: false }
          : {
              protocol_version: 1,
              request_id: requestId,
              ok: false,
              data: {},
              error: { code: "refused", message: "hidden", retryable: false, retry_after_ms: null, evidence_ref: null },
            }
      const fetcher: typeof fetch = (async () => new Response(JSON.stringify(response), {
        status: kind === "ambiguous-error" ? 400 : 200,
        headers: { "content-type": "application/json" },
      })) as typeof fetch
      await expect(postJsonWithRetry({
        url: "http://127.0.0.1:9/v1/search",
        token: "private",
        envelope: { protocol_version: 1, request_id: requestId },
        timeoutMs: 1_000,
        fetcher,
      })).rejects.toThrow(/envelope|response|refused/)
    }
  )

  test.each([400, 401, 409, 413, 415, 422])("never retries semantic status %s", async (status) => {
    let calls = 0
    const requestId = crypto.randomUUID()
    const fetcher: typeof fetch = (async () => {
      calls += 1
      return new Response(
        JSON.stringify({
          protocol_version: 1,
          request_id: requestId,
          ok: false,
          error: {
            code: "refused",
            message: "remote message",
            retryable: false,
            retry_after_ms: null,
            evidence_ref: null,
          },
        }),
        { status, headers: { "content-type": "application/json" } }
      )
    }) as typeof fetch
    await expect(
      postJsonWithRetry({
        url: "http://127.0.0.1:9/v1/test",
        token: "secret",
        envelope: { protocol_version: 1, request_id: requestId },
        timeoutMs: 1_000,
        fetcher,
        sleep: async () => {},
      })
    ).rejects.toThrow("request refused")
    expect(calls).toBe(1)
  })

  test("publishes a mode-0600 descriptor atomically and reads it no-follow", async () => {
    const work = await root()
    const path = descriptorPath(work, "basic-memory")
    const descriptor = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:23456",
      bearer_token: "descriptor-only-token",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "a".repeat(40),
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    await writeSecureDescriptor(path, descriptor)
    expect((await lstat(path)).mode & 0o777).toBe(0o600)
    expect(await readSecureDescriptor(path, descriptor)).toEqual(descriptor)
    expect(await readFile(path, "utf8")).toContain("descriptor-only-token")
  })

  test("refuses descriptor symlinks wrong modes stale identity and root mismatch", async () => {
    const work = await root()
    const path = descriptorPath(work, "basic-memory")
    const expected = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:23456",
      bearer_token: "token",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "a".repeat(40),
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    await writeSecureDescriptor(path, expected)
    await chmod(path, 0o644)
    await expect(readSecureDescriptor(path, expected)).rejects.toThrow(/mode|0600/)

    await chmod(path, 0o600)
    await expect(
      readSecureDescriptor(path, { ...expected, process_start_identity: "stale" })
    ).rejects.toThrow(/identity|mismatch/)
    await expect(
      readSecureDescriptor(path, { ...expected, work_root: join(work, "other") })
    ).rejects.toThrow(/root|mismatch/)

    const target = join(work, "real.json")
    await writeSecureDescriptor(target, expected)
    await rm(path)
    await symlink(target, path)
    await expect(readSecureDescriptor(path, expected)).rejects.toThrow(/symlink|regular|no-follow/)
  })

  test("refuses a crafted 0600 descriptor bound to the current unrelated process", async () => {
    const work = await root()
    const path = descriptorPath(work, "basic-memory")
    const crafted = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:23456",
      bearer_token: "attacker-controlled",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "a".repeat(40),
      checkout_root: join(work, "checkout"),
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    await writeSecureDescriptor(path, crafted)
    await expect(readSecureDescriptor(path, {
      ...crafted,
      expected_command: ["uv", "run", "--no-sync", "python", "sidecar.py"],
      expected_environment: { MEMORYBENCH_GUEST_INSTANCE_ID: "independently-derived-instance" },
      require_process_group_leader: true,
    } as never)).rejects.toThrow(/command|environment|instance|process group|descriptor/)
  })

  test("descriptor schema is exact and base URL must be loopback", async () => {
    const work = await root()
    const path = descriptorPath(work, "basic-memory")
    const descriptor = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "https://attacker.invalid:443",
      bearer_token: "token",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "a".repeat(40),
      work_root: work,
      evidence_root: join(work, "evidence"),
      unexpected: "field",
    }
    await writeSecureDescriptor(path, descriptor)
    await expect(readSecureDescriptor(path, descriptor)).rejects.toThrow(/schema|field|loopback/)
  })

  test("Exomem launch and doctor environment overrides ambient values with lme_profile pins", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    expect(transport.EXOMEM_LME_PROFILE_PROVENANCE).toBe("benchmarks/lme/adapter.py::lme_profile")
    expect(transport.EXOMEM_LME_ENV).toEqual({
      EXOMEM_DISABLE_EMBEDDINGS: "",
      EXOMEM_DISABLE_WARMUP: "1",
      EXOMEM_DISABLE_FILE_WATCHER: "1",
      EXOMEM_DISABLE_MODE_WATCH: "1",
      EXOMEM_DISABLE_CORPUS_CACHE: "1",
      EXOMEM_VEC_BACKEND: "numpy",
      EXOMEM_LEXICAL_BACKEND: "python",
      EXOMEM_ALLOW_CPU_TORCH: "1",
      HF_HUB_OFFLINE: "1",
      TRANSFORMERS_OFFLINE: "1",
    })
    const build = transport.buildExomemChildEnvironment as undefined | ((ambient: Record<string, string>, owned: Record<string, string>) => Record<string, string>)
    expect(typeof build).toBe("function")
    const environment = build!({
      EXOMEM_DISABLE_EMBEDDINGS: "1",
      EXOMEM_DISABLE_WARMUP: "0",
      EXOMEM_VEC_BACKEND: "sqlite-vec",
      EXOMEM_LEXICAL_BACKEND: "native",
      HF_HUB_OFFLINE: "0",
      TRANSFORMERS_OFFLINE: "0",
    }, { EXOMEM_VAULT_PATH: "/owned/vault", EXOMEM_REST_API_KEY: "owned-key" })
    expect(Object.fromEntries(Object.keys(transport.EXOMEM_LME_ENV as object).map((key) => [key, environment[key]])))
      .toEqual(transport.EXOMEM_LME_ENV)
    expect(environment.EXOMEM_VAULT_PATH).toBe("/owned/vault")
    expect(environment.EXOMEM_REST_API_KEY).toBe("owned-key")
  })

  test("Exomem doctor preserves a structured nonzero report for fail-closed inspection", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    const parse = transport.parseExomemDoctorProcessResult as undefined |
      ((result: { status: number | null; stdout: string }) => unknown)
    expect(typeof parse).toBe("function")
    const report = {
      success: false,
      profile: "hybrid",
      checks: [{ id: "embeddings.sidecar", status: "fail" }],
    }

    expect(parse!({ status: 1, stdout: JSON.stringify(report) })).toEqual(report)
    expect(() => parse!({ status: 1, stdout: JSON.stringify({ success: true }) })).toThrow(
      "Exomem doctor failed"
    )
    expect(() => parse!({ status: 1, stdout: "not-json" })).toThrow("Exomem doctor failed")
    expect(() => parse!({ status: 0, stdout: "not-json" })).toThrow(
      "Exomem doctor returned non-JSON output"
    )
  })

  test("Exomem writer-lease state is bound inside the owned service root", async () => {
    const transport = await import("../_guest_transport") as Record<string, unknown>
    const bind = transport.exomemOwnedStateEnvironment as undefined |
      ((workRoot: string) => Record<string, string>)
    expect(typeof bind).toBe("function")
    const work = join(await root(), "service")
    expect(bind!(work)).toEqual({
      EXOMEM_WRITER_LEASE_STATE_DIR: join(work, "writer-lease-state"),
    })

    const source = await readFile(new URL("../_guest_transport.ts", import.meta.url), "utf8")
    const expectation = source.slice(
      source.indexOf("async function exomemDescriptorExpectation"),
      source.indexOf("const exomemRetirements")
    )
    const launch = source.slice(
      source.indexOf("export async function ensureExomemService"),
      source.indexOf("export async function runExomemDoctor")
    )
    const doctor = source.slice(
      source.indexOf("export async function runExomemDoctor"),
      source.indexOf("async function processIsLive")
    )
    expect(expectation).toContain("...exomemOwnedStateEnvironment(work)")
    expect(launch.match(/\.\.\.exomemOwnedStateEnvironment\(roots\.work\)/g)).toHaveLength(2)
    expect(doctor).toContain("...exomemOwnedStateEnvironment(service.work_root)")
  })

  test("Exomem REST transport moves stable mutation identities to authenticated headers", async () => {
    const received: Array<{ headers: Record<string, string | string[] | undefined>; body: string }> = []
    const server = createServer((request, response) => {
      let body = ""
      request.setEncoding("utf8")
      request.on("data", (chunk) => { body += chunk })
      request.on("end", () => {
        received.push({ headers: request.headers, body })
        response.writeHead(200, { "content-type": "application/json" })
        response.end(JSON.stringify({ success: true, data: { path: "Knowledge Base/Sources/Sessions/a.md" } }))
      })
    })
    server.listen(0, "127.0.0.1")
    await once(server, "listening")
    const address = server.address()
    if (!address || typeof address === "string") throw new Error("fixture server address missing")
    const work = await root()
    const requestId = crypto.randomUUID()
    const service = {
      protocol_version: 1 as const,
      provider: "exomem" as const,
      base_url: `http://127.0.0.1:${address.port}`,
      bearer_token: "fixture-rest-key",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    try {
      await expect(postExomem(service, "/api/capture_source", {
        title: "neutral",
        request_id: requestId,
        idempotency_key: requestId,
      })).resolves.toEqual({ path: "Knowledge Base/Sources/Sessions/a.md" })
    } finally {
      server.close()
      await once(server, "close")
    }
    expect(received).toHaveLength(1)
    expect(received[0].headers.authorization).toBe("Bearer fixture-rest-key")
    expect(received[0].headers["x-exomem-request-id"]).toBe(requestId)
    expect(received[0].headers["idempotency-key"]).toBe(requestId)
    expect(JSON.parse(received[0].body)).toEqual({ title: "neutral" })
  })

  test("remote sidecar and Exomem messages never enter local errors or evidence, even encoded", async () => {
    const token = "printable-private-token"
    const path = join("/", "home", "operator", "private", "payload.json")
    const variants = [token, path, Buffer.from(token).toString("base64"), Buffer.from(path).toString("hex")]
    const requestId = crypto.randomUUID()
    const fetcher: typeof fetch = (async () => new Response(JSON.stringify({
      protocol_version: 1,
      request_id: requestId,
      ok: false,
      error: { code: "remote_failure", message: variants.join(" "), retryable: false, retry_after_ms: null, evidence_ref: null },
    }), { status: 422, headers: { "content-type": "application/json" } })) as typeof fetch
    let thrown = ""
    try {
      await postJsonWithRetry({
        url: "http://127.0.0.1:9/v1/ingest",
        token,
        envelope: { protocol_version: 1, request_id: requestId },
        timeoutMs: 1_000,
        fetcher,
      })
    } catch (error) {
      thrown = String(error)
    }
    for (const variant of variants) expect(thrown).not.toContain(variant)

    const work = await root()
    const service = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:1",
      bearer_token: token,
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    const reference = await appendGuestEvidence(service, "remote-failure", { message: variants.join(" ") })
    const evidence = await readFile(join(service.evidence_root, reference.path), "utf8")
    for (const variant of variants) expect(evidence).not.toContain(variant)
  })

  test("Exomem HTTP failures expose only stable local errors", async () => {
    const token = "exomem-private-token"
    const privatePath = join("/", "home", "operator", "private", "vault.md")
    const encoded = Buffer.from(`${token}:${privatePath}`).toString("base64")
    const server = createServer((_request, response) => {
      response.writeHead(500, { "content-type": "application/json" })
      response.end(JSON.stringify({
        success: false,
        error: { message: `${token} ${privatePath} ${encoded}`, retryable: false },
      }))
    })
    server.listen(0, "127.0.0.1")
    await once(server, "listening")
    const address = server.address()
    if (!address || typeof address === "string") throw new Error("fixture server address missing")
    const work = await root()
    const requestId = crypto.randomUUID()
    const service = {
      protocol_version: 1 as const,
      provider: "exomem" as const,
      base_url: `http://127.0.0.1:${address.port}`,
      bearer_token: token,
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    let thrown = ""
    try {
      await postExomem(service, "/api/capture_source", {
        title: "neutral",
        request_id: requestId,
        idempotency_key: requestId,
      })
    } catch (error) {
      thrown = String(error)
    } finally {
      server.close()
      await once(server, "close")
    }
    expect(thrown).toMatch(/Exomem request refused|remote_failure/)
    expect(thrown).not.toContain(token)
    expect(thrown).not.toContain(privatePath)
    expect(thrown).not.toContain(encoded)
  })

  test("launch failure and sidecar Abort paths are wired to owned-group retirement", async () => {
    const source = await readFile(new URL("../_guest_transport.ts", import.meta.url), "utf8")
    expect(/export async function retireOwnedProcessGroup/.test(source)).toBe(true)
    const basicLaunch = source.slice(
      source.indexOf("export async function ensureBasicMemoryService"),
      source.indexOf("async function reservePort")
    )
    const exomemLaunch = source.slice(
      source.indexOf("export async function ensureExomemService"),
      source.indexOf("export async function runExomemDoctor")
    )
    const sidecarPost = source.slice(
      source.indexOf("export async function postSidecar"),
      source.indexOf("export async function postExomem")
    )
    expect(/catch[\s\S]*await retireOwnedProcessGroup/.test(basicLaunch)).toBe(true)
    expect(/catch[\s\S]*await retireExomemService/.test(exomemLaunch)).toBe(true)
    expect(/AbortError[\s\S]*await retireOwnedProcessGroup/.test(sidecarPost)).toBe(true)
  })

  test("signal cleanup prevents a launch retry from creating a new live service", async () => {
    const source = await readFile(new URL("../_guest_transport.ts", import.meta.url), "utf8")
    const exomemLaunch = source.slice(
      source.indexOf("export async function ensureExomemService"),
      source.indexOf("export async function runExomemDoctor")
    )
    expect(exomemLaunch).toMatch(/if \(exomemShutdownInProgress\)[\s\S]*const child = spawn/)
  })

  test("Exomem retirement and terminal cleanup assert every existing absence proof", async () => {
    const source = await readFile(new URL("../_guest_transport.ts", import.meta.url), "utf8")
    const retirement = source.slice(
      source.indexOf("export async function retireExomemService"),
      source.indexOf("export async function clearExomemService")
    )
    const cleanup = source.slice(
      source.indexOf("export async function clearExomemService"),
      source.indexOf("export async function finalizeBasicMemoryService")
    )
    expect(retirement).toMatch(/retireOwnedProcessGroup[\s\S]*ownedProcessGroupAbsent/)
    expect(cleanup).toMatch(/ownedProcessGroupAbsent[\s\S]*ownedGuestProcessesAbsent[\s\S]*owned Exomem root still exists/)
  })

  test("startup failure and operation abort retirement waits for owned process absence", async () => {
    const work = await root()
    const child = spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});setInterval(()=>{},1000)"], {
      detached: true,
      stdio: "ignore",
    })
    if (!child.pid) throw new Error("fixture process failed to start")
    const service = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:1",
      bearer_token: "fixture",
      pid: child.pid,
      process_start_identity: await processStartIdentity(child.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    try {
      const transport = await import("../_guest_transport") as Record<string, unknown>
      const retire = transport.retireOwnedProcessGroup as undefined | ((descriptor: typeof service, reason: string) => Promise<void>)
      expect(typeof retire).toBe("function")
      await retire!(service, "startup_projection_failure")
      expect(() => process.kill(child.pid!, 0)).toThrow()
    } finally {
      try { process.kill(-child.pid, "SIGKILL") } catch { /* already absent */ }
    }
  }, 8_000)

  test("retirement waits for every live member of the exact owned process group", async () => {
    const work = await root()
    const instance = `whole-group-${crypto.randomUUID()}`
    const leader = spawn(process.execPath, ["-e", `
      const { spawn } = require("node:child_process")
      process.on("SIGTERM", () => process.exit(0))
      const child = spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});process.stdout.write('ready');setInterval(()=>{},1000)"], { stdio: ["ignore", "pipe", "ignore"] })
      child.stdout.once("data", () => process.stdout.write(String(child.pid) + "\\n"))
      setInterval(() => {}, 1000)
    `], {
      detached: true,
      env: {
        ...process.env,
        MEMORYBENCH_GUEST_PROVIDER: "basic-memory",
        MEMORYBENCH_GUEST_INSTANCE_ID: instance,
        MEMORYBENCH_GUEST_BEARER_TOKEN: "fixture",
      },
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (!leader.pid || !leader.stdout) throw new Error("fixture process group failed to start")
    const [pidBytes] = await once(leader.stdout, "data")
    const stubbornPid = Number(String(pidBytes).trim())
    if (!Number.isSafeInteger(stubbornPid) || stubbornPid <= 0) throw new Error("fixture child pid missing")
    const group = leader.pid
    const liveGroupMembers = async (): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const stat = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const close = stat.lastIndexOf(")")
          const fields = stat.slice(close + 2).split(" ")
          if (Number(fields[2]) === group && fields[0] !== "Z") members.push(Number(entry.name))
        } catch { /* process exited during the scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    const service = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:1",
      bearer_token: "fixture",
      pid: leader.pid,
      process_start_identity: await processStartIdentity(leader.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
      instance_id: instance,
    }
    try {
      const transport = await import("../_guest_transport") as Record<string, unknown>
      const retire = transport.retireOwnedProcessGroup as undefined | ((descriptor: typeof service, reason: string) => Promise<void>)
      expect(typeof retire).toBe("function")
      await retire!(service, "entire_group_regression")
      expect(await liveGroupMembers()).toEqual([])
      expect((await liveGroupMembers()).includes(stubbornPid)).toBe(false)
    } finally {
      try { process.kill(-group, "SIGKILL") } catch { /* exact owned group already absent */ }
    }
  }, 10_000)

  test("a reaped group leader never bypasses descendant retirement or finalization", async () => {
    const bounded = async <T>(promise: Promise<T>, milliseconds: number, label: string): Promise<T> => {
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        return await Promise.race([
          promise,
          new Promise<T>((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds)
          }),
        ])
      } finally {
        if (timer !== undefined) clearTimeout(timer)
      }
    }
    const liveGroupMembers = async (group: number): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const stat = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const close = stat.lastIndexOf(")")
          const fields = stat.slice(close + 2).split(" ")
          if (Number(fields[2]) === group && fields[0] !== "Z") members.push(Number(entry.name))
        } catch { /* process exited during the scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    const leaks: Array<{ operation: string; members: number[] }> = []
    for (const operation of ["retire", "finalize"] as const) {
      const work = await root()
      const instance = `dead-leader-${operation}-${crypto.randomUUID()}`
      const leader = spawn(process.execPath, ["-e", `
        const { spawn } = require("node:child_process")
        const child = spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});process.stdout.write('ready');setInterval(()=>{},1000)"], { stdio: ["ignore", "pipe", "ignore"] })
        child.stdout.once("data", () => process.stdout.write(String(child.pid) + "\\n"))
        setInterval(() => {}, 1000)
      `], {
        detached: true,
        env: {
          ...process.env,
          MEMORYBENCH_GUEST_PROVIDER: "basic-memory",
          MEMORYBENCH_GUEST_INSTANCE_ID: instance,
          MEMORYBENCH_GUEST_BEARER_TOKEN: "fixture",
        },
        stdio: ["ignore", "pipe", "ignore"],
      })
      if (!leader.pid || !leader.stdout) throw new Error("orphan-group fixture failed to start")
      const group = leader.pid
      try {
        const [pidBytes] = await bounded(once(leader.stdout, "data"), 2_000, "fixture readiness")
        const descendant = Number(String(pidBytes).trim())
        if (!Number.isSafeInteger(descendant) || descendant <= 0) throw new Error("fixture child pid missing")
        const service = {
          protocol_version: 1 as const,
          provider: "basic-memory" as const,
          base_url: "http://127.0.0.1:1",
          bearer_token: "fixture",
          pid: leader.pid,
          process_start_identity: await processStartIdentity(leader.pid),
          checkout_pin: "fixture",
          work_root: work,
          evidence_root: join(work, "evidence"),
          instance_id: instance,
        }
        const environment = (await readFile(`/proc/${descendant}/environ`, "utf8")).split("\0")
        expect(environment).toContain("MEMORYBENCH_GUEST_PROVIDER=basic-memory")
        expect(environment).toContain(`MEMORYBENCH_GUEST_INSTANCE_ID=${instance}`)
        expect(await liveGroupMembers(group)).toContain(descendant)

        const leaderExit = once(leader, "exit")
        process.kill(leader.pid, "SIGKILL")
        await bounded(leaderExit, 2_000, "leader reap")
        expect(await liveGroupMembers(group)).toContain(descendant)

        if (operation === "retire") {
          const transport = await import("../_guest_transport") as Record<string, unknown>
          const retire = transport.retireOwnedProcessGroup as undefined | ((descriptor: typeof service, reason: string) => Promise<void>)
          expect(typeof retire).toBe("function")
          await retire!(service, "dead_leader_regression")
        } else {
          await finalizeBasicMemoryService(service)
        }
        const remaining = await liveGroupMembers(group)
        if (remaining.length > 0) leaks.push({ operation, members: remaining })
      } finally {
        try { process.kill(-group, "SIGKILL") } catch { /* exact owned group already absent */ }
        const deadline = Date.now() + 2_000
        while ((await liveGroupMembers(group)).length > 0 && Date.now() <= deadline) {
          await new Promise((resolveSleep) => setTimeout(resolveSleep, 25))
        }
        if ((await liveGroupMembers(group)).length > 0) throw new Error("exact-group fixture cleanup failed")
      }
    }
    expect(leaks).toEqual([])
  }, 15_000)

  test("a dead leader cannot authorize a survivor with any forged guest binding", async () => {
    const bounded = async <T>(promise: Promise<T>, milliseconds: number, label: string): Promise<T> => {
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        return await Promise.race([
          promise,
          new Promise<T>((_resolve, reject) => {
            timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds)
          }),
        ])
      } finally {
        if (timer !== undefined) clearTimeout(timer)
      }
    }
    const liveGroupMembers = async (group: number): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const stat = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const close = stat.lastIndexOf(")")
          const fields = stat.slice(close + 2).split(" ")
          if (Number(fields[2]) === group && fields[0] !== "Z") members.push(Number(entry.name))
        } catch { /* process exited during the scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    for (const forged of ["provider", "instance", "bearer"] as const) {
      const work = await root()
      const instance = `forged-${forged}-${crypto.randomUUID()}`
      const bearer = `bearer-${crypto.randomUUID()}`
      const marker = join(work, `${forged}-term-marker`)
      const leader = spawn(process.execPath, ["-e", `
        const { spawn } = require("node:child_process")
        const child = spawn(process.execPath, ["-e", "const fs=require('node:fs');process.on('SIGTERM',()=>fs.writeFileSync(process.env.TERM_MARKER,'term'));process.stdout.write('ready');setInterval(()=>{},1000)"], { stdio: ["ignore", "pipe", "ignore"] })
        child.stdout.once("data", () => process.stdout.write(String(child.pid) + "\\n"))
        setInterval(() => {}, 1000)
      `], {
        detached: true,
        env: {
          ...process.env,
          MEMORYBENCH_GUEST_PROVIDER: forged === "provider" ? "exomem" : "basic-memory",
          MEMORYBENCH_GUEST_INSTANCE_ID: forged === "instance" ? `wrong-${instance}` : instance,
          MEMORYBENCH_GUEST_BEARER_TOKEN: forged === "bearer" ? `wrong-${bearer}` : bearer,
          TERM_MARKER: marker,
        },
        stdio: ["ignore", "pipe", "ignore"],
      })
      if (!leader.pid || !leader.stdout) throw new Error("forged-orphan fixture failed to start")
      const group = leader.pid
      try {
        const [pidBytes] = await bounded(once(leader.stdout, "data"), 2_000, "fixture readiness")
        const survivor = Number(String(pidBytes).trim())
        if (!Number.isSafeInteger(survivor) || survivor <= 0) throw new Error("fixture child pid missing")
        const service = {
          protocol_version: 1 as const,
          provider: "basic-memory" as const,
          base_url: "http://127.0.0.1:1",
          bearer_token: bearer,
          pid: leader.pid,
          process_start_identity: await processStartIdentity(leader.pid),
          checkout_pin: "fixture",
          work_root: work,
          evidence_root: join(work, "evidence"),
          instance_id: instance,
        }
        const survivorIdentity = await processStartIdentity(survivor)
        const leaderExit = once(leader, "exit")
        process.kill(leader.pid, "SIGKILL")
        await bounded(leaderExit, 2_000, "leader reap")
        expect(await liveGroupMembers(group)).toEqual([survivor])

        const transport = await import("../_guest_transport") as Record<string, unknown>
        const retire = transport.retireOwnedProcessGroup as undefined | ((descriptor: typeof service, reason: string) => Promise<void>)
        expect(typeof retire).toBe("function")
        await expect(retire!(service, `forged_${forged}`)).rejects.toThrow(
          "orphaned process group environment mismatch"
        )
        expect(await processStartIdentity(survivor)).toBe(survivorIdentity)
        expect(await liveGroupMembers(group)).toEqual([survivor])
        await expect(lstat(marker)).rejects.toThrow()
      } finally {
        try { process.kill(-group, "SIGKILL") } catch { /* exact owned group already absent */ }
        const deadline = Date.now() + 2_000
        while ((await liveGroupMembers(group)).length > 0 && Date.now() <= deadline) {
          await new Promise((resolveSleep) => setTimeout(resolveSleep, 25))
        }
        if ((await liveGroupMembers(group)).length > 0) throw new Error("exact-group fixture cleanup failed")
      }
    }
  }, 25_000)

  test("one hundred concurrent evidence appends allocate unique durable sequences", async () => {
    const work = await root()
    const evidence = join(work, "evidence")
    const service = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:1",
      bearer_token: "fixture-secret",
      pid: process.pid,
      process_start_identity: await processStartIdentity(process.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: evidence,
    }
    const references = await Promise.all(Array.from({ length: 100 }, (_unused, index) =>
      appendGuestEvidence(service, "concurrent-append", { index })
    ))
    expect(new Set(references.map((reference) => reference.path)).size).toBe(100)
    expect(new Set(references.map((reference) => reference.sha256)).size).toBe(100)
    const sequences = references.map((reference) => Number(reference.path.match(/^operation-(\d{6})-/)?.[1]))
    expect(sequences.sort((left, right) => left - right)).toEqual(
      Array.from({ length: 100 }, (_unused, index) => index + 1)
    )
    expect((await readdir(evidence)).filter((name) => /^operation-\d+-/.test(name))).toHaveLength(100)
    for (const reference of references) {
      const bytes = await readFile(join(evidence, reference.path))
      expect(await sha256Hex(bytes)).toBe(reference.sha256)
    }
  })

  test("final Basic cleanup terminates only its owned process group and removes its work root", async () => {
    const work = await root()
    const child = spawn("sleep", ["30"], { detached: true, stdio: "ignore" })
    if (!child.pid) throw new Error("fixture process failed to start")
    child.unref()
    const service = {
      protocol_version: 1 as const,
      provider: "basic-memory" as const,
      base_url: "http://127.0.0.1:1",
      bearer_token: "fixture",
      pid: child.pid,
      process_start_identity: await processStartIdentity(child.pid),
      checkout_pin: "fixture",
      work_root: work,
      evidence_root: join(work, "evidence"),
    }
    await finalizeBasicMemoryService(service)
    await expect(lstat(work)).rejects.toThrow()
    expect(() => process.kill(child.pid!, 0)).toThrow()
  })
})
