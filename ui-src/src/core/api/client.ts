import type { HealthResponse, LoginRequest, SessionResponse } from "./contract";

/**
 * The session token is handed to the webview in its URL, read once, and kept
 * in module memory. Never localStorage and never a cookie: a token that
 * survives the process outlives the launch it was minted for, and one that is
 * readable from storage is readable by anything that gets script execution in
 * this page (architecture §5).
 */
const params = new URLSearchParams(window.location.search);
const SESSION_TOKEN = params.get("t") ?? "";

// Strip it from the address bar so it does not end up in a screenshot, a
// crash report, or the webview's own history.
if (params.has("t")) {
  window.history.replaceState(null, "", window.location.pathname);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** True when the till could not reach something it needed, not a rejection. */
  get isUnavailable(): boolean {
    return this.status === 503 || this.status === 0;
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${SESSION_TOKEN}`,
        ...(init.headers ?? {}),
      },
    });
  } catch (cause) {
    // The local service is down. The splash and the diagnostics screen exist
    // for exactly this, so surface it as a status rather than a raw throw.
    throw new ApiError(0, "The till service is not responding.");
  }

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      (body && typeof body.detail === "string" && body.detail) ||
      `Request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return body as T;
}

export const api = {
  health: () => request<HealthResponse>("/health"),

  login: (body: LoginRequest) =>
    request<SessionResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  session: () => request<SessionResponse>("/auth/session"),

  logout: () => request<void>("/auth/logout", { method: "POST" }),
};
