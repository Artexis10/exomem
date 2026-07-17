import {createHash, randomUUID} from "node:crypto";
import {lstatSync, realpathSync} from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {expect, test} from "@playwright/test";

const LIVE_ENABLED = process.env.EXOMEM_LIVE_ENABLED === "1";
const LIVE_BASE_URL = process.env.EXOMEM_LIVE_BASE_URL?.trim() || "";
const LIVE_STORAGE_STATE = process.env.EXOMEM_LIVE_STORAGE_STATE?.trim() || "";
const LIVE_TRANSFER_HOST = process.env.EXOMEM_LIVE_TRANSFER_HOST?.trim() || "";
const LIVE_DOWNLOAD_PATH = process.env.EXOMEM_LIVE_DOWNLOAD_PATH?.trim() || "";
const LIVE_DOWNLOAD_MIN_BYTES = Number(
  process.env.EXOMEM_LIVE_DOWNLOAD_MIN_BYTES || "5242880"
);
const TRANSFER_GRANT_HEADER = "X-Exomem-Transfer-Grant";
const TRANSFER_GRANT_MAX_BYTES = 8192;
const TRANSFER_PATHS = {
  upload: "/public/exomem/v2/transfers/upload",
  download: "/public/exomem/v2/transfers/download",
};
const BROWSER_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = realpathSync(path.resolve(BROWSER_ROOT, "../.."));
const MIN_LARGE_DOWNLOAD_BYTES = 5 * 1024 * 1024;
const LARGE_TRANSFER_RESPONSE_TIMEOUT_MS = 4 * 60_000;
const UPLOAD_BYTES = 90 * 1024 * 1024;
const ABORT_BYTES = 64 * 1024 * 1024;
const SMALL_BYTES = 1024 * 1024;
const ZERO_CHUNK = Buffer.alloc(1024 * 1024);

let canonicalOrigin = "";
let uploadedPath = "";

test.describe.configure({mode: "serial"});

function isInside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

test.beforeAll(() => {
  if (!LIVE_ENABLED) {
    throw new Error("set EXOMEM_LIVE_ENABLED=1 to run the mutating hosted canary drill");
  }
  if (!LIVE_BASE_URL || new URL(LIVE_BASE_URL).origin !== LIVE_BASE_URL) {
    throw new Error("EXOMEM_LIVE_BASE_URL must be one canonical HTTPS origin");
  }
  if (new URL(LIVE_BASE_URL).protocol !== "https:") {
    throw new Error("EXOMEM_LIVE_BASE_URL must use HTTPS");
  }
  let storageState;
  let storageStatePath;
  try {
    storageState = lstatSync(LIVE_STORAGE_STATE);
    storageStatePath = realpathSync(LIVE_STORAGE_STATE);
  } catch {
    throw new Error("EXOMEM_LIVE_STORAGE_STATE must name the private owner storage-state file");
  }
  if (!storageState.isFile() || storageState.isSymbolicLink()) {
    throw new Error("EXOMEM_LIVE_STORAGE_STATE must be a regular, non-symlink file");
  }
  if (isInside(REPO_ROOT, storageStatePath)) {
    throw new Error("owner storage state must be outside the repository");
  }
  if (process.platform !== "win32" && (storageState.mode & 0o077) !== 0) {
    throw new Error("owner storage state must not be readable by group or other users");
  }
  if (!LIVE_TRANSFER_HOST || LIVE_TRANSFER_HOST.includes("/") || LIVE_TRANSFER_HOST.includes("@")) {
    throw new Error("EXOMEM_LIVE_TRANSFER_HOST must name the reviewed transfer host");
  }
  if (
    !Number.isSafeInteger(LIVE_DOWNLOAD_MIN_BYTES) ||
    LIVE_DOWNLOAD_MIN_BYTES < MIN_LARGE_DOWNLOAD_BYTES
  ) {
    throw new Error("EXOMEM_LIVE_DOWNLOAD_MIN_BYTES must be at least 5 MiB");
  }
  canonicalOrigin = LIVE_BASE_URL;
});

function zeroSha256(size) {
  const digest = createHash("sha256");
  let remaining = size;
  while (remaining > 0) {
    const length = Math.min(remaining, ZERO_CHUNK.length);
    digest.update(ZERO_CHUNK.subarray(0, length));
    remaining -= length;
  }
  return digest.digest("hex");
}

function mutateDownloadPathClaim(grant) {
  const parts = grant.split(".");
  if (parts.length !== 2) {
    throw new Error("download grant does not use the reviewed two-part encoding");
  }
  const [payload, signature] = parts;
  const originalJson = Buffer.from(payload, "base64url").toString("utf8");
  let claims;
  try {
    claims = JSON.parse(originalJson);
  } catch {
    throw new Error("download grant claims are not canonical transfer-v2 claims");
  }
  if (
    JSON.stringify(claims) !== originalJson ||
    claims?.op !== "download" ||
    claims?.target?.kind !== "download-v1" ||
    typeof claims.target.path !== "string"
  ) {
    throw new Error("download grant claims are not canonical transfer-v2 claims");
  }
  claims.target.path = `${claims.target.path}.altered`;
  const alteredPayload = Buffer.from(JSON.stringify(claims), "utf8").toString("base64url");
  return `${alteredPayload}.${signature}`;
}

function liveFilename(label) {
  return `${label}-${Date.now()}-${randomUUID()}.bin`;
}

function hasExactKeys(value, expected) {
  return (
    Boolean(value && typeof value === "object" && !Array.isArray(value)) &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort())
  );
}

function isExactUploadProof(result, size) {
  const body = result?.body;
  const data = body?.data;
  return (
    result?.status === 201 &&
    hasExactKeys(body, ["success", "data"]) &&
    body.success === true &&
    hasExactKeys(data, ["operation", "bytes", "sha256", "committed"]) &&
    data.operation === "upload" &&
    data.bytes === size &&
    data.sha256 === zeroSha256(size) &&
    data.committed === true
  );
}

function isExactGrantRejection(result) {
  const body = result?.body;
  const error = body?.error;
  return (
    result?.status === 401 &&
    hasExactKeys(body, ["success", "error"]) &&
    body.success === false &&
    hasExactKeys(error, ["code", "message", "retryable", "requires_new_grant"]) &&
    error.code === "TRANSFER_GRANT_REJECTED" &&
    error.message === "transfer authorization failed" &&
    error.retryable === false &&
    error.requires_new_grant === true
  );
}

async function openOwnerCell(page) {
  await page.goto("/exomem", {waitUntil: "domcontentloaded"});
  const probe = await page.evaluate(async () => {
    const response = await fetch("/api/exomem/status", {
      credentials: "same-origin",
      cache: "no-store",
    });
    return {status: response.status};
  });
  expect(probe.status, "owner/cell status probe failed").toBe(200);
}

async function issueTicket(
  page,
  endpoint,
  body,
  {expectedOperation, expectedContentType = null}
) {
  const result = await page.evaluate(
    async ({endpoint: ticketEndpoint, body: ticketBody}) => {
      const csrf = document.cookie
        .split(";")
        .map((part) => part.trim().split("="))
        .find(([name]) => name === "exomem_csrf")
        ?.slice(1)
        .join("=");
      const response = await fetch(ticketEndpoint, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-exomem-csrf": decodeURIComponent(csrf || ""),
        },
        body: JSON.stringify(ticketBody),
        credentials: "same-origin",
        cache: "no-store",
      });
      let responseBody;
      try {
        responseBody = await response.json();
      } catch {
        responseBody = null;
      }
      return {status: response.status, body: responseBody};
    },
    {endpoint, body}
  );
  expect(result.status, `ticket endpoint ${endpoint} rejected the owner session`).toBe(200);
  expect(result.body?.success === true, "ticket endpoint returned an invalid envelope").toBe(true);
  const ticket = result.body?.data;
  expect(
    Boolean(ticket && typeof ticket === "object" && !Array.isArray(ticket)),
    "ticket endpoint returned an invalid data object"
  ).toBe(true);
  const expectedMethod = expectedOperation === "upload" ? "PUT" : "GET";
  const expectedTransferPath = TRANSFER_PATHS[expectedOperation];
  expect(expectedTransferPath, "test requested an unsupported transfer operation").toBeTruthy();
  expect(ticket.method).toBe(expectedMethod);
  let transferUrl;
  try {
    if (typeof ticket.url !== "string") throw new Error("invalid URL type");
    transferUrl = new URL(ticket.url);
  } catch {
    throw new Error("ticket endpoint returned an invalid transfer URL");
  }
  const cellPath = transferUrl.pathname.slice(0, -expectedTransferPath.length);
  const urlHasNoForbiddenComponents =
    transferUrl.protocol === "https:" &&
    transferUrl.host === LIVE_TRANSFER_HOST &&
    transferUrl.origin !== canonicalOrigin &&
    transferUrl.username === "" &&
    transferUrl.password === "" &&
    transferUrl.search === "" &&
    transferUrl.hash === "" &&
    /^\/cells\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(cellPath) &&
    transferUrl.pathname === `${cellPath}${expectedTransferPath}`;
  expect(
    urlHasNoForbiddenComponents,
    "ticket transfer URL must be the exact reviewed credential-free cell route"
  ).toBe(true);

  const rawHeaders = ticket.headers;
  expect(
    Boolean(rawHeaders && typeof rawHeaders === "object" && !Array.isArray(rawHeaders)),
    "ticket endpoint returned invalid transfer headers"
  ).toBe(true);
  const expectedHeaderNames =
    expectedOperation === "upload"
      ? ["Content-Type", TRANSFER_GRANT_HEADER]
      : [TRANSFER_GRANT_HEADER];
  expect(Object.keys(rawHeaders).sort()).toEqual(expectedHeaderNames.sort());
  expect(Object.values(rawHeaders).every((value) => typeof value === "string")).toBe(true);
  const grant = rawHeaders[TRANSFER_GRANT_HEADER];
  const grantIsBoundedAscii =
    typeof grant === "string" &&
    grant.length > 0 &&
    Buffer.byteLength(grant, "ascii") === grant.length &&
    grant.length <= TRANSFER_GRANT_MAX_BYTES &&
    /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(grant);
  expect(grantIsBoundedAscii, "ticket returned a malformed transfer grant").toBe(true);
  if (expectedOperation === "upload") {
    expect(rawHeaders["Content-Type"]).toBe(expectedContentType);
  }
  expect(Number.isSafeInteger(ticket.maxBytes) && ticket.maxBytes > 0).toBe(true);

  const safeHeaders = {
    ...(expectedOperation === "upload" ? {"Content-Type": expectedContentType} : {}),
    [TRANSFER_GRANT_HEADER]: grant,
  };
  return {
    url: transferUrl.toString(),
    method: expectedMethod,
    headers: safeHeaders,
    maxBytes: ticket.maxBytes,
  };
}

async function issueUploadTicket(page, {filename, size}) {
  return issueTicket(
    page,
    "/api/exomem/upload",
    {
      metadata: {
        category: "live-transfer",
        content_type: "application/octet-stream",
        description: null,
        filename,
        scope: "hosted-alpha",
        sha256: zeroSha256(size),
        size,
      },
    },
    {expectedOperation: "upload", expectedContentType: "application/octet-stream"}
  );
}

async function issueDownloadTicket(page, path) {
  return issueTicket(
    page,
    "/api/exomem/download",
    {path},
    {expectedOperation: "download"}
  );
}

async function putZeroBytes(page, ticket, size) {
  return page.evaluate(
    async ({ticket: directTicket, size: payloadSize}) => {
      const response = await fetch(directTicket.url, {
        method: directTicket.method,
        headers: directTicket.headers,
        body: new Uint8Array(payloadSize),
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      let body;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      return {
        status: response.status,
        body,
      };
    },
    {ticket, size}
  );
}

async function fetchDirectTicket(page, ticket, bodySize = null) {
  return page.evaluate(
    async ({ticket: directTicket, bodySize: directBodySize}) => {
      const response = await fetch(directTicket.url, {
        method: directTicket.method,
        headers: directTicket.headers,
        ...(directBodySize === null ? {} : {body: new Uint8Array(directBodySize)}),
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      const contentType = response.headers.get("content-type") || "";
      if (contentType.startsWith("application/json")) {
        return {status: response.status, body: await response.json(), bytes: null};
      }
      return {
        status: response.status,
        body: null,
        bytes: (await response.arrayBuffer()).byteLength,
      };
    },
    {ticket, bodySize}
  );
}

async function abortUploadAfterProgress(page, ticket, size) {
  return page.evaluate(
    ({ticket: directTicket, size: payloadSize}) =>
      new Promise((resolve) => {
        const request = new XMLHttpRequest();
        let settled = false;
        let reachedUploadProgress = false;
        const finish = (value) => {
          if (settled) return;
          settled = true;
          clearTimeout(deadline);
          resolve(value);
        };
        request.open(directTicket.method, directTicket.url);
        request.withCredentials = false;
        for (const [name, value] of Object.entries(directTicket.headers)) {
          request.setRequestHeader(name, value);
        }
        request.upload.onprogress = (event) => {
          if (event.loaded <= 0 || (event.lengthComputable && event.loaded >= event.total)) {
            return;
          }
          reachedUploadProgress = true;
          request.abort();
          finish({aborted: true, reachedUploadProgress: true});
        };
        request.onload = () => finish({aborted: false, status: request.status});
        request.onerror = () => finish({aborted: false, networkError: true});
        request.onabort = () => finish({aborted: true, reachedUploadProgress});
        const deadline = setTimeout(() => {
          request.abort();
          finish({aborted: true, reachedUploadProgress: false});
        }, 15_000);
        request.send(
          new Blob([new Uint8Array(payloadSize)], {type: "application/octet-stream"})
        );
      }),
    {ticket, size}
  );
}

async function observeConsumedGrant(page, ticket, size) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const replay = await putZeroBytes(page, ticket, size);
    if (isExactGrantRejection(replay)) {
      return replay;
    }
    const retryableBeforeConsumption =
      [409, 503].includes(replay.status) &&
      replay.body?.error?.requires_new_grant === false;
    if (!retryableBeforeConsumption) {
      throw new Error(
        `aborted transfer grant was not consumed; replay returned status ${replay.status}`
      );
    }
    await page.waitForTimeout(250);
  }
  throw new Error("aborted transfer grant consumption was not observable within 30 seconds");
}

test("canonical CORS preflight", async ({page}) => {
  await openOwnerCell(page);
  const ticket = await issueUploadTicket(page, {filename: liveFilename("preflight"), size: 1});
  const preflightResponse = page.waitForResponse(
    (response) =>
      response.url() === ticket.url && response.request().method() === "OPTIONS"
  );
  const [response, transfer] = await Promise.all([
    preflightResponse,
    putZeroBytes(page, ticket, 1),
  ]);

  expect(response.status()).toBe(204);
  const headers = await response.allHeaders();
  expect(headers["access-control-allow-origin"]).toBe(canonicalOrigin);
  expect(headers["access-control-allow-methods"]).toBe("PUT");
  expect(headers["access-control-allow-headers"]).toBe(
    "Content-Type, X-Exomem-Transfer-Grant"
  );
  expect(headers["access-control-max-age"]).toBe("300");
  expect(headers["access-control-allow-credentials"]).toBeUndefined();
  expect(headers["vary"]).toBe("Origin");
  expect(
    isExactUploadProof(transfer, 1),
    "the browser preflight must not consume the grant"
  ).toBe(true);
});

test("exact transfer headers and methods", async ({page, request}) => {
  await openOwnerCell(page);
  const ticket = await issueUploadTicket(page, {filename: liveFilename("exact"), size: 1});
  const cases = [
    {method: "POST", headers: "content-type, x-exomem-transfer-grant"},
    {method: "PUT", headers: "content-type"},
    {method: "PUT", headers: "content-type, x-exomem-transfer-grant, authorization"},
  ];
  for (const item of cases) {
    const response = await request.fetch(ticket.url, {
      method: "OPTIONS",
      headers: {
        Origin: canonicalOrigin,
        "Access-Control-Request-Method": item.method,
        "Access-Control-Request-Headers": item.headers,
      },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(400);
    expect(response.headers()["access-control-allow-origin"]).toBeUndefined();
  }
  const correct = await putZeroBytes(page, ticket, 1);
  expect(
    isExactUploadProof(correct, 1),
    "rejected preflights must not consume the grant"
  ).toBe(true);
});

test("90 MiB upload streams through the real edge", async ({page}) => {
  test.setTimeout(5 * 60_000);
  await openOwnerCell(page);
  const filename = liveFilename("edge-90mib");
  const ticket = await issueUploadTicket(page, {filename, size: UPLOAD_BYTES});
  expect(ticket.method).toBe("PUT");
  expect(ticket.maxBytes).toBe(UPLOAD_BYTES);

  const actualResponse = page.waitForResponse(
    (response) => response.url() === ticket.url && response.request().method() === "PUT",
    {timeout: LARGE_TRANSFER_RESPONSE_TIMEOUT_MS}
  );
  const [response, result] = await Promise.all([
    actualResponse,
    putZeroBytes(page, ticket, UPLOAD_BYTES),
  ]);
  const responseHeaders = await response.allHeaders();
  expect(isExactUploadProof(result, UPLOAD_BYTES), "upload commit proof is invalid").toBe(true);
  expect(responseHeaders["access-control-allow-origin"]).toBe(canonicalOrigin);
  expect(responseHeaders["access-control-allow-credentials"]).toBeUndefined();
  expect(responseHeaders["vary"]).toBe("Origin");
  expect(responseHeaders["cache-control"]).toBe("private, no-store");
  uploadedPath = `Knowledge Base/Evidence/hosted-alpha/live-transfer/${filename}`;
});

test("large download streams through the real edge", async ({page}) => {
  test.setTimeout(5 * 60_000);
  await openOwnerCell(page);
  const path = LIVE_DOWNLOAD_PATH || uploadedPath;
  expect(path, "the 90 MiB upload must run first or EXOMEM_LIVE_DOWNLOAD_PATH must be set").not.toBe("");
  const ticket = await issueDownloadTicket(page, path);
  expect(ticket.method).toBe("GET");
  const minimumBytes = LIVE_DOWNLOAD_PATH ? LIVE_DOWNLOAD_MIN_BYTES : UPLOAD_BYTES;
  const actualResponse = page.waitForResponse(
    (response) => response.url() === ticket.url && response.request().method() === "GET",
    {timeout: LARGE_TRANSFER_RESPONSE_TIMEOUT_MS}
  );
  const [response, result] = await Promise.all([
    actualResponse,
    page.evaluate(async (directTicket) => {
      const transferResponse = await fetch(directTicket.url, {
        method: directTicket.method,
        headers: directTicket.headers,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      let bytes = 0;
      let chunks = 0;
      const reader = transferResponse.body?.getReader();
      if (reader) {
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          bytes += value.byteLength;
          chunks += 1;
        }
      }
      return {
        status: transferResponse.status,
        bytes,
        chunks,
        contentLength: transferResponse.headers.get("content-length"),
        contentType: transferResponse.headers.get("content-type"),
        contentDisposition: transferResponse.headers.get("content-disposition"),
      };
    }, ticket),
  ]);
  const responseHeaders = await response.allHeaders();

  expect(result.status).toBe(200);
  expect(result.bytes).toBeGreaterThanOrEqual(minimumBytes);
  expect(result.chunks).toBeGreaterThan(1);
  expect(Number(result.contentLength)).toBe(result.bytes);
  expect(result.contentType).toBe("application/octet-stream");
  expect(result.contentDisposition).toMatch(/^attachment;/);
  expect(responseHeaders["access-control-allow-origin"]).toBe(canonicalOrigin);
  expect(responseHeaders["access-control-allow-credentials"]).toBeUndefined();
  expect(responseHeaders["access-control-expose-headers"]).toBe(
    "Content-Disposition, Content-Length, Content-Type"
  );
  expect(responseHeaders["vary"]).toBe("Origin");
  expect(responseHeaders["cache-control"]).toBe("private, no-store");
});

test("aborted upload requires a fresh ticket", async ({page}) => {
  test.setTimeout(5 * 60_000);
  await openOwnerCell(page);
  const abortedTicket = await issueUploadTicket(page, {
    filename: liveFilename("aborted"),
    size: ABORT_BYTES,
  });
  const aborted = await abortUploadAfterProgress(page, abortedTicket, ABORT_BYTES);
  expect(aborted.aborted).toBe(true);
  expect(aborted.reachedUploadProgress).toBe(true);

  const replay = await observeConsumedGrant(page, abortedTicket, ABORT_BYTES);
  expect(isExactGrantRejection(replay), "aborted grant replay was not rejected exactly").toBe(
    true
  );

  const freshTicket = await issueUploadTicket(page, {
    filename: liveFilename("after-abort"),
    size: SMALL_BYTES,
  });
  const fresh = await putZeroBytes(page, freshTicket, SMALL_BYTES);
  expect(isExactUploadProof(fresh, SMALL_BYTES), "fresh ticket upload failed").toBe(true);
});

test("successful ticket replay is rejected", async ({page}) => {
  await openOwnerCell(page);
  const ticket = await issueUploadTicket(page, {
    filename: liveFilename("replay"),
    size: SMALL_BYTES,
  });
  const first = await putZeroBytes(page, ticket, SMALL_BYTES);
  expect(isExactUploadProof(first, SMALL_BYTES), "initial ticket upload failed").toBe(true);

  const replay = await putZeroBytes(page, ticket, SMALL_BYTES);
  expect(isExactGrantRejection(replay), "successful ticket replay was not rejected exactly").toBe(
    true
  );
});

test("grant path and operation alteration is rejected", async ({page}) => {
  await openOwnerCell(page);
  const filename = liveFilename("alteration");
  const uploadTicket = await issueUploadTicket(page, {
    filename,
    size: 1,
  });
  const uploadGrant = uploadTicket.headers[TRANSFER_GRANT_HEADER];
  const wrongOperationUrl = uploadTicket.url.replace(
    TRANSFER_PATHS.upload,
    TRANSFER_PATHS.download
  );
  expect(wrongOperationUrl).not.toBe(uploadTicket.url);
  const operationResponse = await fetchDirectTicket(page, {
    url: wrongOperationUrl,
    method: "GET",
    headers: {[TRANSFER_GRANT_HEADER]: uploadGrant},
  });
  expect(
    isExactGrantRejection(operationResponse),
    "operation-altered grant was not rejected exactly"
  ).toBe(true);

  const correctUpload = await putZeroBytes(page, uploadTicket, 1);
  expect(isExactUploadProof(correctUpload, 1), "unaltered upload grant failed").toBe(true);

  const committedPath = `Knowledge Base/Evidence/hosted-alpha/live-transfer/${filename}`;
  const downloadTicket = await issueDownloadTicket(page, committedPath);
  const originalDownloadGrant = downloadTicket.headers[TRANSFER_GRANT_HEADER];
  const pathResponse = await fetchDirectTicket(page, {
    ...downloadTicket,
    headers: {
      [TRANSFER_GRANT_HEADER]: mutateDownloadPathClaim(originalDownloadGrant),
    },
  });
  expect(
    isExactGrantRejection(pathResponse),
    "path-altered grant was not rejected exactly"
  ).toBe(true);

  const correctDownload = await fetchDirectTicket(page, downloadTicket);
  expect(correctDownload.status).toBe(200);
  expect(correctDownload.bytes).toBe(1);
});

test("hostile origin is denied", async ({page, request}) => {
  await openOwnerCell(page);
  const ticket = await issueUploadTicket(page, {
    filename: liveFilename("hostile-origin"),
    size: SMALL_BYTES,
  });
  const preflight = await request.fetch(ticket.url, {
    method: "OPTIONS",
    headers: {
      Origin: "https://attacker.invalid",
      "Access-Control-Request-Method": "PUT",
      "Access-Control-Request-Headers": "content-type, x-exomem-transfer-grant",
    },
    maxRedirects: 0,
  });
  expect(preflight.status()).toBe(403);
  expect(preflight.headers()["access-control-allow-origin"]).toBeUndefined();

  const hostilePage = await page.context().newPage();
  await hostilePage.goto("data:text/html,<title>hostile origin</title>");
  const hostile = await hostilePage.evaluate(async (directTicket) => {
    try {
      await fetch(directTicket.url, {
        method: directTicket.method,
        headers: directTicket.headers,
        body: new Uint8Array(1024 * 1024),
        credentials: "omit",
      });
      return {blocked: false};
    } catch (error) {
      return {blocked: true, name: error instanceof Error ? error.name : "unknown"};
    }
  }, ticket);
  await hostilePage.close();
  expect(hostile).toEqual({blocked: true, name: "TypeError"});

  const canonical = await putZeroBytes(page, ticket, SMALL_BYTES);
  expect(isExactUploadProof(canonical, SMALL_BYTES), "canonical origin upload failed").toBe(
    true
  );
});
