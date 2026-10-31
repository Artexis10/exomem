const KEY_NAME = "exomem.studio.rest-key";

export class ApiError extends Error {
  constructor(message, {code = "REQUEST_FAILED", status = 0, remediation = null} = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.remediation = remediation;
  }
}

export function storedKey() {
  return window.sessionStorage.getItem(KEY_NAME) || "";
}

export function setStoredKey(key) {
  const clean = String(key || "").trim();
  if (clean) window.sessionStorage.setItem(KEY_NAME, clean);
  else window.sessionStorage.removeItem(KEY_NAME);
}

function actionKey() {
  // randomUUID exists only in secure contexts; a plain-HTTP LAN page still has getRandomValues.
  if (window.crypto.randomUUID) return window.crypto.randomUUID();
  return Array.from(window.crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function command(name, body = {}, {key = storedKey()} = {}) {
  if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new ApiError("Invalid command name");
  // One key per action: the server waits out a busy boundary for a keyed write,
  // and a repeated action (a toggled selection, a re-plan) is a new write. This key
  // cannot be reused on retry, so never send it with an action that needs one
  // (a vocabulary application binds its decision to the key).
  const headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Idempotency-Key": actionKey(),
  };
  if (key) headers.Authorization = `Bearer ${key}`;
  let response;
  try {
    response = await window.fetch(`/api/${name}`, {
      method: "POST",
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(body),
    });
  } catch (_error) {
    throw new ApiError("The Exomem service could not be reached.", {code: "NETWORK_ERROR"});
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new ApiError("The service returned an unreadable response.", {status: response.status});
  }
  if (!response.ok || !payload.success) {
    const detail = payload.error || {};
    throw new ApiError(detail.message || `Request failed (${response.status})`, {
      code: detail.code,
      status: response.status,
      remediation: detail.remediation,
    });
  }
  return payload.data;
}
