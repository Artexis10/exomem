import { afterEach, describe, expect, test } from "bun:test"
import { chmod, lstat, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises"
import { spawn } from "node:child_process"
import { once } from "node:events"
import { tmpdir } from "node:os"
import { join } from "node:path"

const load = () => import("../cleanup.ts")
const roots: string[] = []
const RAW_TAG = "private-container-tag"
const HMAC_KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"

afterEach(async () => {
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})

async function digest(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value
  const hashed = await crypto.subtle.digest("SHA-256", bytes)
  return Array.from(new Uint8Array(hashed), (byte) => byte.toString(16).padStart(2, "0")).join("")
}

async function privacyHmac(domain: string, value: string): Promise<string> {
  const key = Uint8Array.from(HMAC_KEY_HEX.match(/../g)!, (part) => Number.parseInt(part, 16))
  const imported = await crypto.subtle.importKey(
    "raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  )
  const message = new TextEncoder().encode(`${domain}\0${value}`)
  return Array.from(new Uint8Array(await crypto.subtle.sign("HMAC", imported, message)),
    (byte) => byte.toString(16).padStart(2, "0")).join("")
}

async function privateJson(path: string, payload: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(payload)}\n`, { mode: 0o600 })
  await chmod(path, 0o600)
}

async function replaceRunPlan(f: Fixture, source: string): Promise<void> {
  const runPlan = structuredClone(f.runPlan)
  runPlan.dataset.source = source
  await privateJson(f.runPlanPath, runPlan)
  await privateJson(f.cleanupPlanPath, {
    ...f.cleanupPlan,
    run_plan_sha256: await digest(await readFile(f.runPlanPath)),
  })
}

interface Fixture {
  root: string
  output: string
  runPlanPath: string
  cleanupPlanPath: string
  runPlan: Record<string, any>
  cleanupPlan: Record<string, any>
  targetEvidence: { root: "output"; path: string; path_hmac_sha256: null; sha256: string }
  finalEvidence: { root: "output"; path: string; path_hmac_sha256: null; sha256: string }
}

async function fixture(provider: "exomem" | "basic-memory" = "exomem"): Promise<Fixture> {
  const root = await mkdtemp(join(tmpdir(), "memorybench-cleanup-test-"))
  roots.push(root)
  const output = join(root, "output")
  const work = join(output, "guest-work")
  const evidence = join(output, "guest-evidence")
  const memorybench = join(root, "memorybench")
  const providerCheckout = join(root, "provider-checkout")
  await mkdir(work, { recursive: true, mode: 0o700 })
  await mkdir(evidence, { recursive: true, mode: 0o700 })
  await mkdir(memorybench, { recursive: true, mode: 0o700 })
  await mkdir(providerCheckout, { recursive: true, mode: 0o700 })
  const datasetPath = join(memorybench, "data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json")
  const datasetBytes = new TextEncoder().encode('[{"question_id":"q-01"}]\n')
  await mkdir(join(memorybench, "data/benchmarks/longmemeval/datasets"), { recursive: true, mode: 0o700 })
  await writeFile(datasetPath, datasetBytes, { mode: 0o600 })
  const variant = provider === "exomem" ? "exomem-source-only" : "basic-memory-controlled"
  const providerRepository = provider === "exomem"
    ? "https://github.com/hugoa/exomem"
    : "https://github.com/basicmachines-co/basic-memory"
  const providerCommit = provider === "exomem"
    ? "a".repeat(40)
    : "816accaa9befe8281668ba8819eaf74d11ce2385"
  const providerTree = provider === "exomem"
    ? "b".repeat(40)
    : "4f0255a31c609cad90dbf3b50e3d14a517e4566e"
  const runPlan = {
    protocol_version: "1.0.0",
    schema_version: 1,
    artifact_type: "memorybench-run-plan.v1",
    run_id: "run-01",
    upstream_run_id: "run-01",
    provider,
    provider_variant: variant,
    benchmark: "longmemeval",
    selection: { mode: "full", target_question_ids: null },
    harness: {
      repository: "https://github.com/supermemoryai/memorybench",
      commit: "118209a746d97d0d85e5a7234267f0b6962857e9",
      tree: "2ee25bdbcb6bfaaecb32f917920c53775a299b37",
      bun_lock_sha256: "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da",
    },
    dataset: {
      id: "longmemeval",
      variant: "cleaned",
      source: "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
      revision: "fixture-pin",
      sha256: await digest(datasetBytes),
      case_count: 1,
    },
    dataset_path: datasetPath,
    provider_checkout: {
      root: providerCheckout,
      repository: providerRepository,
      commit: providerCommit,
      tree: providerTree,
      lock_sha256: "c".repeat(64),
    },
    memorybench_home: memorybench,
    output_root: output,
    guest_work_root: work,
    guest_evidence_root: evidence,
    contract_revision: "7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f",
    preregistration_sha256: "d".repeat(64),
    privacy_hmac_key_hex: HMAC_KEY_HEX,
  }
  const runPlanPath = join(root, "run-plan.json")
  await privateJson(runPlanPath, runPlan)

  const cleanupPlan = {
    protocol_version: "1.0.0",
    schema_version: 1,
    artifact_type: "guest-cleanup-plan.v1",
    run_id: "run-01",
    provider,
    provider_variant: variant,
    guest_work_root: work,
    guest_evidence_root: evidence,
    run_plan_path: runPlanPath,
    run_plan_sha256: await digest(await readFile(runPlanPath)),
    targets: [{
      container_tag: RAW_TAG,
      container_tag_hmac_sha256: await privacyHmac("container-tag", RAW_TAG),
      discovery_sources: ["checkpoint", "guest_evidence"],
      namespace_expected: true,
    }],
  }
  const cleanupPlanPath = join(root, "cleanup-plan.json")
  await privateJson(cleanupPlanPath, cleanupPlan)

  const targetPath = join(output, "cleanup-evidence", "target.json")
  const finalPath = join(output, "cleanup-evidence", "final.json")
  await mkdir(join(output, "cleanup-evidence"), { recursive: true, mode: 0o700 })
  await privateJson(targetPath, { protocol_version: 1, event: "target-absence" })
  await privateJson(finalPath, { protocol_version: 1, event: "final-absence" })
  return {
    root,
    output,
    runPlanPath,
    cleanupPlanPath,
    runPlan,
    cleanupPlan,
    targetEvidence: {
      root: "output",
      path: "cleanup-evidence/target.json",
      path_hmac_sha256: null,
      sha256: await digest(await readFile(targetPath)),
    },
    finalEvidence: {
      root: "output",
      path: "cleanup-evidence/final.json",
      path_hmac_sha256: null,
      sha256: await digest(await readFile(finalPath)),
    },
  }
}

function dependencies(f: Fixture, calls: string[], overrides: Record<string, unknown> = {}) {
  return {
    attachBasicMemoryService: async (binding: Record<string, unknown>) => {
      calls.push(`attach-basic:${binding.provider}`)
      return { bearer_token: "descriptor-token-secret", pid: 424242, work_root: f.runPlan.guest_work_root }
    },
    attachExomemService: async (tag: string, binding: Record<string, unknown>) => {
      calls.push(`attach-exomem:${tag}:${binding.provider}`)
      return { bearer_token: "descriptor-token-secret", pid: 424242, work_root: f.runPlan.guest_work_root }
    },
    providerFactory: (provider: string, _attached: unknown) => {
      const fixtureProvider = {
        finalizationCalls: 0,
        clear: async (tag: string) => {
          calls.push(`clear:${tag}`)
          if (provider === "basic-memory") fixtureProvider.finalizationCalls += 1
        },
      }
      return fixtureProvider
    },
    targetAbsence: async (_target: unknown) => ({
      namespace: true,
      corpus: f.cleanupPlan.provider === "basic-memory" ? true : null,
      config: null,
      descriptor: f.cleanupPlan.provider === "exomem" ? true : null,
      process_group: f.cleanupPlan.provider === "exomem" ? true : null,
      work_root: f.cleanupPlan.provider === "exomem" ? true : null,
      artifacts: [f.targetEvidence],
    }),
    finalAbsence: async () => ({
      config: true,
      descriptor: true,
      process_group: true,
      work_root: true,
      artifacts: [f.finalEvidence],
    }),
    ...overrides,
  }
}

describe("guest cleanup v1", () => {
  test("shares the exact frozen privacy HMAC vectors with Python", async () => {
    const cleanup = await load()
    const vectors = [
      ["case-id", "q-01_abs", "94e872ad0278c5e760d5ff4a7f170e513c148711365fc3d72bc45b12fc90f131"],
      ["container-tag", "q-01-run-01", "97b7ccef0e2c66cba51712bac76a50a19832e709b9534d1677d0872342e6f852"],
      ["artifact-path", "results/q-01_abs.json", "196854f5bf555f5f96463b1d1b04fe931a66c81376dac1ce82c23891458f2396"],
    ]
    for (const [domain, raw, expected] of vectors) {
      expect(await cleanup.privacyHmacSha256(HMAC_KEY_HEX, domain, raw)).toBe(expected)
    }
  })

  test("accepts the frozen registry source and rejects non-public source forms", async () => {
    const cleanup = await load()
    const f = await fixture()

    await replaceRunPlan(f, "xiaowu0162/longmemeval-cleaned")
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).resolves.toEqual(expect.any(Object))

    for (const source of ["/abs", "file:x", "./x", "../x", "a//b", "a/./b", "a/../b", "a\\b"]) {
      await replaceRunPlan(f, source)
      await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/dataset identity/)
    }
  })

  test("CLI reports cleanup-plan failures on stderr without a stdout proof", async () => {
    const f = await fixture()
    await replaceRunPlan(f, "./x")

    const child = Bun.spawn({
      cmd: ["bun", "run", join(process.cwd(), "benchmarks/memorybench/cleanup.ts"), "--plan", f.cleanupPlanPath],
      stdout: "pipe",
      stderr: "pipe",
    })
    const [exit, stdout, stderr] = await Promise.all([
      child.exited,
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
    ])

    expect(exit).toBe(3)
    expect(stdout).toBe("")
    expect(stderr).toMatch(/run plan dataset identity is invalid/)
  })

  test("reads a real owned mode-0600 full plan and refuses relative, symlinked, and insecure files", async () => {
    const cleanup = await load()
    const f = await fixture()
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).resolves.toEqual(f.cleanupPlan)
    await expect(cleanup.parseCleanupPlan("relative.json")).rejects.toThrow()

    await chmod(f.cleanupPlanPath, 0o644)
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/mode|0600/)
    await chmod(f.cleanupPlanPath, 0o600)

    const owner = await lstat(f.cleanupPlanPath)
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath, {
      currentUid: () => owner.uid + 1,
    })).rejects.toThrow(/owner/)

    await chmod(f.root, 0o770)
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/parent|writable/)
    await chmod(f.root, 0o700)

    const symlinkPath = join(f.root, "cleanup-symlink.json")
    await symlink(f.cleanupPlanPath, symlinkPath)
    await expect(cleanup.parseCleanupPlan(symlinkPath)).rejects.toThrow(/follow|regular|symbolic|symlink/)
  })

  test("revalidates the exact run-plan digest and root/provider binding", async () => {
    const cleanup = await load()
    for (const mutation of ["digest", "work-root", "provider"] as const) {
      const f = await fixture()
      const changed = structuredClone(f.cleanupPlan)
      if (mutation === "digest") changed.run_plan_sha256 = "0".repeat(64)
      if (mutation === "work-root") changed.guest_work_root = join(f.output, "different-work")
      if (mutation === "provider") changed.provider = "basic-memory"
      await privateJson(f.cleanupPlanPath, changed)
      await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/digest|binding|provider|root/)
    }
  })

  test("refuses a run-plan symlink and digest-preserving root-binding mutation", async () => {
    const cleanup = await load()
    const f = await fixture()
    const realRunPlan = join(f.root, "real-run-plan.json")
    await privateJson(realRunPlan, f.runPlan)
    const linkedRunPlan = join(f.root, "linked-run-plan.json")
    await symlink(realRunPlan, linkedRunPlan)
    const changed = structuredClone(f.cleanupPlan)
    changed.run_plan_path = linkedRunPlan
    changed.run_plan_sha256 = await digest(await readFile(realRunPlan))
    await privateJson(f.cleanupPlanPath, changed)
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/follow|regular|symbolic|symlink/)
  })

  test("deduplicates the complete discovery union into digest order", async () => {
    const cleanup = await load()
    const f = await fixture()
    const earlyTag = "sorts-first"
    const earlyDigest = await privacyHmac("container-tag", earlyTag)
    const plan = structuredClone(f.cleanupPlan)
    plan.targets = [
      plan.targets[0],
      { ...plan.targets[0], discovery_sources: ["secure_descriptor"] },
      {
        container_tag: earlyTag,
        container_tag_hmac_sha256: earlyDigest,
        discovery_sources: ["checkpoint"],
        namespace_expected: false,
      },
    ]
    const calls: string[] = []
    const proof = await cleanup.executeCleanup(plan, dependencies(f, calls))
    const rawHmac = await privacyHmac("container-tag", RAW_TAG)
    const ordered = [
      { tag: earlyTag, hmac: earlyDigest },
      { tag: RAW_TAG, hmac: rawHmac },
    ].sort((left, right) => left.hmac.localeCompare(right.hmac))
    expect(proof.targets.map((target: { container_tag_hmac_sha256: string }) => target.container_tag_hmac_sha256))
      .toEqual(ordered.map((target) => target.hmac))
    expect(proof.targets.find(
      (target: { container_tag_hmac_sha256: string }) =>
        target.container_tag_hmac_sha256 === rawHmac
    ).discovery_sources).toEqual(["checkpoint", "guest_evidence", "secure_descriptor"])
    expect(calls.filter((call) => call.startsWith("clear:")))
      .toEqual(ordered.map((target) => `clear:${target.tag}`))
  })

  test("a duplicate digest with a different raw tag or namespace expectation refuses instead of merging", async () => {
    const cleanup = await load()
    for (const mutation of ["raw-tag", "expectation"] as const) {
      const f = await fixture()
      const plan = structuredClone(f.cleanupPlan)
      plan.targets.push({
        ...plan.targets[0],
        ...(mutation === "raw-tag" ? { container_tag: "different-private-tag" } : {}),
        ...(mutation === "expectation" ? { namespace_expected: false } : {}),
        discovery_sources: ["secure_descriptor"],
      })
      await expect(cleanup.executeCleanup(plan, dependencies(f, []))).rejects.toThrow(/duplicate|digest|conflict/)
    }
  })

  test("attaches once, calls provider clear strictly sequentially, and attempts later targets after failure", async () => {
    const cleanup = await load()
    const f = await fixture()
    const secondTag = "second-private-tag"
    const plan = structuredClone(f.cleanupPlan)
    plan.targets.push({
      container_tag: secondTag,
      container_tag_hmac_sha256: await privacyHmac("container-tag", secondTag),
      discovery_sources: ["secure_descriptor"],
      namespace_expected: true,
    })
    plan.targets.sort((left: any, right: any) => left.container_tag_hmac_sha256.localeCompare(right.container_tag_hmac_sha256))
    const calls: string[] = []
    let active = 0
    let maximum = 0
    const proof = await cleanup.executeCleanup(plan, dependencies(f, calls, {
      providerFactory: () => ({
        clear: async (tag: string) => {
          active += 1
          maximum = Math.max(maximum, active)
          calls.push(`clear:${tag}`)
          active -= 1
          if (tag === plan.targets[0].container_tag) throw new Error("injected-exception-secret")
        },
      }),
      targetAbsence: async (target: { container_tag: string }) => ({
        namespace: target.container_tag !== plan.targets[0].container_tag,
        corpus: null,
        config: null,
        descriptor: true,
        process_group: true,
        work_root: true,
        artifacts: [f.targetEvidence],
      }),
    }))
    expect(maximum).toBe(1)
    expect(calls.filter((call) => call.startsWith("clear:"))).toEqual(
      plan.targets.map((target: { container_tag: string }) => `clear:${target.container_tag}`)
    )
    expect(proof.targets[0].outcome).toBe("clear_failed")
    expect(proof.targets[1].outcome).toBe("cleared")
    expect(proof.all_absent).toBe(false)
    expect(JSON.stringify(proof)).not.toContain("injected-exception-secret")
  })

  test("descriptor failure is unproved and never calls any launch or provider factory", async () => {
    const cleanup = await load()
    const f = await fixture()
    let launched = false
    let constructed = false
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, [], {
      attachExomemService: async () => { throw new Error("descriptor_stale") },
      providerFactory: () => { constructed = true; return { clear: async () => {} } },
      ensureExomemService: async () => { launched = true },
      targetAbsence: async () => ({
        namespace: false,
        corpus: null,
        config: null,
        descriptor: false,
        process_group: false,
        work_root: false,
        artifacts: [f.targetEvidence],
      }),
    }))
    expect(launched).toBe(false)
    expect(constructed).toBe(false)
    expect(proof.targets[0].outcome).toBe("absence_unproved")
    expect(proof.all_absent).toBe(false)
  })

  test("Basic uses the shared-surface null matrix and public cleanup count 0/1 rule", async () => {
    const cleanup = await load()
    const f = await fixture("basic-memory")
    const calls: string[] = []
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, calls))
    expect(calls).toEqual(["attach-basic:basic-memory", `clear:${RAW_TAG}`])
    expect(proof.basic_public_cleanup_calls).toBe(1)
    expect(proof.targets[0].absence).toEqual({
      namespace: true,
      corpus: true,
      config: null,
      descriptor: null,
      process_group: null,
      work_root: null,
    })
    expect(proof.all_absent).toBe(true)

    const zero = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, [], {
      providerFactory: () => ({ finalizationCalls: 0, clear: async () => {} }),
    }))
    expect(zero.all_absent).toBe(false)
  })

  test("Exomem attempts all targets and uses only its applicable per-target surfaces", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const calls: string[] = []
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, calls))
    expect(calls).toEqual([`attach-exomem:${RAW_TAG}:exomem`, `clear:${RAW_TAG}`])
    expect(proof.basic_public_cleanup_calls).toBe(0)
    expect(proof.targets[0].absence).toEqual({
      namespace: true,
      corpus: null,
      config: null,
      descriptor: true,
      process_group: true,
      work_root: true,
    })
    expect(proof.all_absent).toBe(true)
  })

  test("validateCleanupProof recomputes every surface and rejects forged all_absent", async () => {
    const cleanup = await load()
    const f = await fixture()
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, []))
    await expect(cleanup.validateCleanupProof(proof)).resolves.toEqual(proof)
    const forged = structuredClone(proof)
    forged.targets[0].outcome = "clear_failed"
    forged.targets[0].failure_code = "clear_failed"
    forged.failure_codes = ["clear_failed"]
    forged.all_absent = true
    await expect(cleanup.validateCleanupProof(forged)).rejects.toThrow(/all_absent|outcome|absence/)
  })

  test("main emits exactly one full token-free JSON line and returns zero only for recomputed absence", async () => {
    const cleanup = await load()
    const f = await fixture()
    const calls: string[] = []
    const stdout: string[] = []
    const exit = await cleanup.main(
      ["--plan", f.cleanupPlanPath],
      { ...dependencies(f, calls), stdout: (line: string) => stdout.push(line) },
    )
    expect(exit).toBe(0)
    expect(stdout).toHaveLength(1)
    expect(stdout[0].endsWith("\n")).toBe(true)
    const proof = JSON.parse(stdout[0])
    expect(proof).toMatchObject({
      artifact_type: "guest-cleanup.v1",
      run_id: "run-01",
      provider: "exomem",
      provider_variant: "exomem-source-only",
      trigger: "success",
      basic_public_cleanup_calls: 0,
      all_absent: true,
    })
    expect(proof.targets).toHaveLength(1)
    for (const forbidden of [
      RAW_TAG,
      f.root,
      "descriptor-token-secret",
      "424242",
      "injected-exception-secret",
    ]) expect(stdout[0]).not.toContain(forbidden)

    const failedOutput: string[] = []
    const failed = await cleanup.main(
      ["--plan", f.cleanupPlanPath],
      {
        ...dependencies(f, [], {
          targetAbsence: async () => ({
            namespace: false,
            corpus: null,
            config: null,
            descriptor: false,
            process_group: false,
            work_root: false,
            artifacts: [f.targetEvidence],
          }),
        }),
        stdout: (line: string) => failedOutput.push(line),
      },
    )
    expect(failed).toBe(3)
    expect(failedOutput).toHaveLength(1)
    expect(JSON.parse(failedOutput[0]).all_absent).toBe(false)
  })

  test.each([
    ["cleanup-plan", "top"],
    ["cleanup-plan", "nested"],
    ["run-plan", "top"],
    ["run-plan", "nested"],
  ] as const)("rejects duplicate object members in %s at %s depth", async (source, depth) => {
    const cleanup = await load()
    const f = await fixture()
    if (source === "cleanup-plan") {
      let raw = JSON.stringify(f.cleanupPlan)
      raw = depth === "top"
        ? raw.replace("{", '{"run_id":"run-01",')
        : raw.replace('"container_tag":', `"container_tag":"${RAW_TAG}","container_tag":`)
      await writeFile(f.cleanupPlanPath, `${raw}\n`, { mode: 0o600 })
    } else {
      let raw = JSON.stringify(f.runPlan)
      raw = depth === "top"
        ? raw.replace("{", '{"run_id":"run-01",')
        : raw.replace('"provider_checkout":{', '"provider_checkout":{"commit":"a'.concat("a".repeat(39), '",'))
      await writeFile(f.runPlanPath, `${raw}\n`, { mode: 0o600 })
      const changed = { ...f.cleanupPlan, run_plan_sha256: await digest(await readFile(f.runPlanPath)) }
      await privateJson(f.cleanupPlanPath, changed)
    }
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/duplicate|JSON/)
  })

  test("cleanup-plan target HMAC remains an external run-plan-key obligation", async () => {
    const cleanup = await load()
    const f = await fixture()
    const forged = structuredClone(f.cleanupPlan)
    forged.targets[0].container_tag_hmac_sha256 = "0".repeat(64)
    await privateJson(f.cleanupPlanPath, forged)
    await expect(cleanup.parseCleanupPlan(f.cleanupPlanPath)).rejects.toThrow(/HMAC|target|binding/)
  })

  test("Basic already-absent uses namespace and corpus with null per-target shared surfaces", async () => {
    const cleanup = await load()
    const f = await fixture("basic-memory")
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, [], {
      attachBasicMemoryService: async () => { throw new Error("descriptor_missing") },
      providerFactory: () => { throw new Error("must not construct after failed attach") },
      targetAbsence: async () => ({
        namespace: true,
        corpus: true,
        config: null,
        descriptor: null,
        process_group: null,
        work_root: null,
        artifacts: [f.targetEvidence],
      }),
      finalAbsence: async () => ({
        config: true,
        descriptor: true,
        process_group: true,
        work_root: true,
        artifacts: [f.finalEvidence],
      }),
    }))
    expect(proof.targets[0]).toMatchObject({
      outcome: "already_absent",
      failure_code: null,
      absence: {
        namespace: true,
        corpus: true,
        config: null,
        descriptor: null,
        process_group: null,
        work_root: null,
      },
    })
    expect(proof.basic_public_cleanup_calls).toBe(0)
    expect(proof.all_absent).toBe(true)
    await expect(cleanup.validateCleanupProof(proof)).resolves.toEqual(proof)
  })

  test.each(["pre-final", "final", "failed-attach"] as const)(
    "Basic cleanup count observes the concrete final shared-service seam: %s",
    async (scenario) => {
    const cleanup = await load()
    const { BasicMemoryProvider } = await import("../providers/basic-memory/index.ts")
      const f = await fixture("basic-memory")
      let enteredFinalizer = 0
      let providerConstructed = 0
      const service = {
        protocol_version: 1,
        provider: "basic-memory",
        base_url: "http://127.0.0.1:1",
        bearer_token: "fixture-secret",
        pid: process.pid,
        process_start_identity: `fixture:${process.pid}`,
        checkout_pin: f.runPlan.provider_checkout.commit,
        checkout_root: f.runPlan.provider_checkout.root,
        work_root: f.runPlan.guest_work_root,
        evidence_root: f.runPlan.guest_evidence_root,
        instance_id: "fixture-instance",
      } as any
      const final = scenario === "final"
      const proof = await cleanup.executeCleanup(f.cleanupPlan, {
        attachBasicMemoryService: async () => {
          if (scenario === "failed-attach") throw new Error("descriptor_missing")
          return service
        },
        providerFactory: () => {
          providerConstructed += 1
          return new BasicMemoryProvider({
            ensureService: async () => service,
            post: async () => ({ namespace: "fixture", final, absence_proved: true }),
            clearService: async () => { enteredFinalizer += 1 },
          })
        },
        targetAbsence: async () => ({
          namespace: scenario !== "failed-attach",
          corpus: scenario !== "failed-attach",
          config: null,
          descriptor: null,
          process_group: null,
          work_root: null,
          artifacts: [f.targetEvidence],
        }),
        finalAbsence: async () => ({
          config: final,
          descriptor: final,
          process_group: final,
          work_root: final,
          artifacts: [f.finalEvidence],
        }),
      })
      expect(enteredFinalizer).toBe(final ? 1 : 0)
      expect(providerConstructed).toBe(scenario === "failed-attach" ? 0 : 1)
      expect(proof.basic_public_cleanup_calls).toBe(final ? 1 : 0)
      expect(proof.all_absent).toBe(final)
      await expect(cleanup.validateCleanupProof(proof)).resolves.toEqual(proof)
  })

  test("removing service directories cannot prove absence while the exactly owned process survives", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
      detached: true,
      stdio: "ignore",
    })
    child.unref()
    if (!child.pid) throw new Error("fixture process did not start")
    const serviceWork = join(
      f.runPlan.guest_work_root,
      "services",
      "exomem",
      (await digest(RAW_TAG)).slice(0, 24),
    )
    await mkdir(serviceWork, { recursive: true, mode: 0o700 })
    let startIdentity = `linux-proc-v1:${child.pid}:unknown`
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try {
        const stat = await readFile(`/proc/${child.pid}/stat`, "utf8")
        startIdentity = `linux-proc-v1:${child.pid}:${stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19]}`
        break
      } catch { await Bun.sleep(5) }
    }
    try {
      const stdout: string[] = []
      const exit = await cleanup.main(["--plan", f.cleanupPlanPath], {
        attachExomemService: async () => ({
          protocol_version: 1,
          provider: "exomem",
          base_url: "http://127.0.0.1:1",
          bearer_token: "fixture-secret",
          pid: child.pid,
          process_start_identity: startIdentity,
          checkout_pin: f.runPlan.provider_checkout.commit,
          checkout_root: f.runPlan.provider_checkout.root,
          work_root: serviceWork,
          evidence_root: f.runPlan.guest_evidence_root,
          container_tag: RAW_TAG,
          vault_root: join(serviceWork, "vault"),
          instance_id: "fixture-instance",
        }),
        providerFactory: () => ({
          clear: async () => { await rm(join(f.runPlan.guest_work_root, "services"), { recursive: true }) },
        }),
        stdout: (line: string) => stdout.push(line),
      })
      expect(exit).toBe(3)
      const proof = JSON.parse(stdout[0])
      expect(proof.targets[0].absence.process_group).toBe(false)
      expect(proof.final_absence.process_group).toBe(false)
      expect(proof.all_absent).toBe(false)
    } finally {
      try { process.kill(-child.pid, "SIGKILL") } catch {}
    }
  })

  test("feedback3 a reaped leader cannot hide a live child in the exact owned process group", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const leader = spawn(process.execPath, ["-e", `
      const { spawn } = require("node:child_process")
      const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
        stdio: "ignore",
      })
      process.stdout.write(String(child.pid) + "\\n")
      setTimeout(() => process.exit(0), 500)
    `], {
      detached: true,
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (!leader.pid || !leader.stdout) throw new Error("fixture group leader did not start")
    const group = leader.pid
    const liveGroupMembers = async (): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const raw = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ")
          if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
        } catch { /* process exited during the bounded scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    let childPid = 0
    try {
      const [pidBytes] = await Promise.race([
        once(leader.stdout, "data"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("fixture child readiness timed out")), 2_000)),
      ])
      childPid = Number(String(pidBytes).trim())
      if (!Number.isSafeInteger(childPid) || childPid <= 0) throw new Error("fixture child pid is invalid")
      const leaderStat = await readFile(`/proc/${leader.pid}/stat`, "utf8")
      const leaderFields = leaderStat.slice(leaderStat.lastIndexOf(")") + 2).split(" ")
      const startIdentity = `linux-proc-v1:${leader.pid}:${leaderFields[19]}`
      await Promise.race([
        once(leader, "exit"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("fixture leader reap timed out")), 2_000)),
      ])
      expect(await liveGroupMembers()).toContain(childPid)

      const serviceWork = join(
        f.runPlan.guest_work_root,
        "services",
        "exomem",
        (await digest(RAW_TAG)).slice(0, 24),
      )
      await mkdir(serviceWork, { recursive: true, mode: 0o700 })
      const stdout: string[] = []
      const exit = await cleanup.main(["--plan", f.cleanupPlanPath], {
        attachExomemService: async () => ({
          protocol_version: 1,
          provider: "exomem",
          base_url: "http://127.0.0.1:1",
          bearer_token: "fixture-secret",
          pid: group,
          process_start_identity: startIdentity,
          checkout_pin: f.runPlan.provider_checkout.commit,
          checkout_root: f.runPlan.provider_checkout.root,
          work_root: serviceWork,
          evidence_root: f.runPlan.guest_evidence_root,
          container_tag: RAW_TAG,
          vault_root: join(serviceWork, "vault"),
          instance_id: "fixture-instance",
        }),
        providerFactory: () => ({
          clear: async () => {
            await rm(join(f.runPlan.guest_work_root, "services"), { recursive: true })
          },
        }),
        stdout: (line: string) => stdout.push(line),
      })
      expect(exit).toBe(3)
      const proof = JSON.parse(stdout[0])
      expect(proof.targets[0].absence.process_group).toBe(false)
      expect(proof.final_absence.process_group).toBe(false)
      expect(proof.all_absent).toBe(false)
    } finally {
      try { process.kill(-group, "SIGKILL") } catch { /* exact group already absent */ }
      const deadline = Date.now() + 2_000
      while ((await liveGroupMembers()).length > 0 && Date.now() <= deadline) await Bun.sleep(20)
      if ((await liveGroupMembers()).length > 0) throw new Error("fixture exact group cleanup failed")
    }
  }, 8_000)

  test("feedback3 process probe errors fail closed in both target and final proof", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const stdout: string[] = []
    const exit = await cleanup.main(["--plan", f.cleanupPlanPath], dependencies(f, [], {
      targetAbsence: async () => { throw new Error("injected target probe failure") },
      finalAbsence: async () => { throw new Error("injected final probe failure") },
      stdout: (line: string) => stdout.push(line),
    }))
    expect(exit).toBe(3)
    const proof = JSON.parse(stdout[0])
    expect(proof.targets[0].absence.process_group).toBe(false)
    expect(proof.final_absence.process_group).toBe(false)
    expect(proof.all_absent).toBe(false)
  })

  test("feedback4 validate-only treats a dumpable-zero bound group member as process absence unproved", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const proofPath = join(f.output, "guest-cleanup.v1.json")

    const python = [
      "import ctypes, os, time",
      "libc = ctypes.CDLL(None, use_errno=True)",
      "assert libc.prctl(4, 0, 0, 0, 0) == 0",
      "print(os.getpid(), flush=True)",
      "time.sleep(60)",
    ].join(";")
    const leaderScript = `
      const { spawn } = require("node:child_process")
      const child = spawn("python3", ["-c", ${JSON.stringify(python)}], {
        env: {
          ...process.env,
          MEMORYBENCH_GUEST_WORK_ROOT: ${JSON.stringify(f.runPlan.guest_work_root)},
          MEMORYBENCH_GUEST_PROVIDER: "exomem",
        },
        stdio: ["ignore", "pipe", "ignore"],
      })
      child.stdout.once("data", (chunk) => {
        process.stdout.write(chunk)
        setTimeout(() => process.exit(0), 100)
      })
    `
    const leader = spawn(process.execPath, ["-e", leaderScript], {
      detached: true,
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (!leader.pid || !leader.stdout) throw new Error("fixture group leader did not start")
    const group = leader.pid
    const liveGroupMembers = async (): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const raw = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ")
          if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
        } catch { /* process exited during the bounded scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    try {
      const [pidBytes] = await Promise.race([
        once(leader.stdout, "data"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("dumpable-zero fixture readiness timed out")), 2_000)),
      ])
      const childPid = Number(String(pidBytes).trim())
      if (!Number.isSafeInteger(childPid) || childPid <= 0) {
        throw new Error("dumpable-zero fixture child pid is invalid")
      }
      await Promise.race([
        once(leader, "exit"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("dumpable-zero fixture leader reap timed out")), 2_000)),
      ])
      expect(await liveGroupMembers()).toContain(childPid)
      let denial = ""
      try {
        await readFile(`/proc/${childPid}/environ`)
      } catch (error) {
        denial = (error as NodeJS.ErrnoException).code ?? ""
      }
      expect(["EACCES", "EPERM"]).toContain(denial)

      const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, [], {
        attachExomemService: async () => ({
          protocol_version: 1,
          provider: "exomem",
          base_url: "http://127.0.0.1:1",
          bearer_token: "fixture-private-token",
          pid: group,
          process_start_identity: `linux-proc-v1:${group}:fixture`,
          checkout_pin: f.runPlan.provider_checkout.commit,
          checkout_root: f.runPlan.provider_checkout.root,
          work_root: f.runPlan.guest_work_root,
          evidence_root: f.runPlan.guest_evidence_root,
          container_tag: RAW_TAG,
        }),
      }))
      expect(proof.all_absent).toBe(true)
      await privateJson(proofPath, proof)

      const stdout: string[] = []
      const exit = await cleanup.main(
        ["--validate-only", "--plan", f.cleanupPlanPath, "--proof", proofPath],
        { stdout: (line: string) => stdout.push(line) },
      )
      expect(exit).toBe(3)
      expect(JSON.parse(stdout[0])).toEqual({ observed_absent: false })
    } finally {
      try { process.kill(-group, "SIGKILL") } catch { /* exact group already absent */ }
      const deadline = Date.now() + 2_000
      while ((await liveGroupMembers()).length > 0 && Date.now() <= deadline) await Bun.sleep(20)
      if ((await liveGroupMembers()).length > 0) throw new Error("dumpable-zero fixture group survived cleanup")
    }
  }, 8_000)

  test("feedback5 validate-only fails closed on dumpable-zero guest without retained binding", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, []))
    expect(proof.all_absent).toBe(true)
    const proofPath = join(f.output, "guest-cleanup.v1.json")
    await privateJson(proofPath, proof)

    for (const reference of [proof.targets[0].artifacts[0], proof.final_absence.artifacts[0]]) {
      const evidencePath = join(f.output, reference.path)
      const evidenceBytes = await readFile(evidencePath)
      expect(await digest(evidenceBytes)).toBe(reference.sha256)
      expect(JSON.parse(String(evidenceBytes)).process_binding).toBeNull()
    }

    const fixtureBin = join(f.root, "fixture-bin")
    await mkdir(fixtureBin, { mode: 0o700 })
    const fakeExomem = join(fixtureBin, "exomem")
    await writeFile(fakeExomem, [
      "#!/usr/bin/python3",
      "import ctypes, os, time",
      "libc = ctypes.CDLL(None, use_errno=True)",
      "assert libc.prctl(4, 0, 0, 0, 0) == 0",
      "print(os.getpid(), flush=True)",
      "time.sleep(60)",
      "",
    ].join("\n"), { mode: 0o700 })
    await chmod(fakeExomem, 0o700)
    await writeFile(join(f.runPlan.provider_checkout.root, "pyproject.toml"), [
      "[project]",
      'name = "memorybench-hidden-guest-fixture"',
      'version = "0.0.0"',
      'requires-python = ">=3.11"',
      "",
    ].join("\n"), { mode: 0o600 })
    const exactCommand = [
      "run", "--project", f.runPlan.provider_checkout.root, "--no-sync", "exomem",
      "--transport", "http", "--host", "127.0.0.1", "--port", "43123",
    ]
    const child = spawn("uv", exactCommand, {
      detached: true,
      env: {
        ...process.env,
        PATH: `${fixtureBin}:${process.env.PATH ?? ""}`,
        MEMORYBENCH_GUEST_WORK_ROOT: f.runPlan.guest_work_root,
        MEMORYBENCH_GUEST_PROVIDER: "exomem",
        UV_NO_SYNC: "1",
        UV_OFFLINE: "1",
      },
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (!child.pid || !child.stdout) throw new Error("dumpable-zero guest did not start")
    const group = child.pid
    const liveGroupMembers = async (): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const raw = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ")
          if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
        } catch { /* exact fixture process exited during scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    try {
      const [pidBytes] = await Promise.race([
        once(child.stdout, "data"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("dumpable-zero guest readiness timed out")), 2_000)),
      ])
      const childPid = Number(String(pidBytes).trim())
      expect(Number.isSafeInteger(childPid) && childPid > 0).toBe(true)
      expect(await liveGroupMembers()).toContain(childPid)
      const command = String(await readFile(`/proc/${group}/cmdline`)).split("\0").filter(Boolean)
      expect(command).toEqual(["uv", ...exactCommand])
      let denial = ""
      try {
        await readFile(`/proc/${childPid}/environ`)
      } catch (error) {
        denial = (error as NodeJS.ErrnoException).code ?? ""
      }
      expect(["EACCES", "EPERM"]).toContain(denial)

      const stdout: string[] = []
      const exit = await cleanup.main(
        ["--validate-only", "--plan", f.cleanupPlanPath, "--proof", proofPath],
        { stdout: (line: string) => stdout.push(line) },
      )
      expect(exit).toBe(3)
      expect(JSON.parse(stdout[0])).toEqual({ observed_absent: false })
    } finally {
      try { process.kill(-group, "SIGKILL") } catch { /* exact group already absent */ }
      await Promise.race([
        once(child, "exit"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("dumpable-zero guest reap timed out")), 2_000)),
      ])
      if ((await liveGroupMembers()).length > 0) {
        throw new Error("dumpable-zero guest group survived cleanup")
      }
    }
  }, 8_000)

  test("feedback6 validate-only ignores an unrelated dumpable-zero Python runtime", async () => {
    const cleanup = await load()
    const f = await fixture("exomem")
    const proof = await cleanup.executeCleanup(f.cleanupPlan, dependencies(f, []))
    expect(proof.all_absent).toBe(true)
    const proofPath = join(f.output, "guest-cleanup.v1.json")
    await privateJson(proofPath, proof)

    const unrelatedEnvironment = Object.fromEntries(
      Object.entries(process.env).filter(([key]) => !key.startsWith("MEMORYBENCH_")),
    )
    expect(Object.keys(unrelatedEnvironment).filter((key) => key.startsWith("MEMORYBENCH_"))).toEqual([])
    const python = [
      "import ctypes, json, os, time",
      "libc = ctypes.CDLL(None, use_errno=True)",
      "assert libc.prctl(4, 0, 0, 0, 0) == 0",
      "print(json.dumps({'pid': os.getpid(), 'memorybench_env': sorted(k for k in os.environ if k.startswith('MEMORYBENCH_'))}), flush=True)",
      "time.sleep(60)",
    ].join(";")
    const child = spawn("python3", ["-c", python], {
      detached: true,
      env: unrelatedEnvironment,
      stdio: ["ignore", "pipe", "ignore"],
    })
    if (!child.pid || !child.stdout) throw new Error("unrelated dumpable-zero Python did not start")
    const group = child.pid
    const liveGroupMembers = async (): Promise<number[]> => {
      const members: number[] = []
      for (const entry of await readdir("/proc", { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+$/.test(entry.name)) continue
        try {
          const raw = await readFile(`/proc/${entry.name}/stat`, "utf8")
          const fields = raw.slice(raw.lastIndexOf(")") + 2).split(" ")
          if (fields[0] !== "Z" && Number(fields[2]) === group) members.push(Number(entry.name))
        } catch { /* exact fixture process exited during scan */ }
      }
      return members.sort((left, right) => left - right)
    }
    try {
      const [readinessBytes] = await Promise.race([
        once(child.stdout, "data"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("unrelated dumpable-zero Python readiness timed out")), 2_000)),
      ])
      const readiness = JSON.parse(String(readinessBytes).trim())
      expect(readiness).toEqual({ pid: group, memorybench_env: [] })
      expect(await liveGroupMembers()).toEqual([group])
      const command = String(await readFile(`/proc/${group}/cmdline`)).split("\0").filter(Boolean)
      expect(command[0].split("/").at(-1)).toMatch(/^python3(?:\.\d+)?$/)
      expect(command[1]).toBe("-c")
      expect(command.join("\0")).not.toContain("providers/basic-memory/sidecar.py")
      expect(command.join("\0")).not.toContain("--transport\0http")
      let denial = ""
      try {
        await readFile(`/proc/${group}/environ`)
      } catch (error) {
        denial = (error as NodeJS.ErrnoException).code ?? ""
      }
      expect(["EACCES", "EPERM"]).toContain(denial)

      const stdout: string[] = []
      const exit = await cleanup.main(
        ["--validate-only", "--plan", f.cleanupPlanPath, "--proof", proofPath],
        { stdout: (line: string) => stdout.push(line) },
      )
      expect(exit).toBe(0)
      expect(JSON.parse(stdout[0])).toEqual({ observed_absent: true })
    } finally {
      try { process.kill(-group, "SIGKILL") } catch { /* exact group already absent */ }
      await Promise.race([
        once(child, "exit"),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("unrelated dumpable-zero Python reap timed out")), 2_000)),
      ])
      if ((await liveGroupMembers()).length > 0) {
        throw new Error("unrelated dumpable-zero Python group survived cleanup")
      }
    }
  }, 8_000)
})
