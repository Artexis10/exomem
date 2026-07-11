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

export async function command(name, body = {}, {key = storedKey()} = {}) {
  if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new ApiError("Invalid command name");
  const headers = {"Content-Type": "application/json", "Accept": "application/json"};
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
