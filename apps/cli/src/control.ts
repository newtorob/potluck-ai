/** Bounded local control requests. Never send the gateway key to management APIs. */
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { existsSync } from "node:fs";
import { join } from "node:path";

export type FailureCode = "unreachable" | "permission_denied" | "unauthorized" | "timeout" | "invalid_response" | "http_error";
export class ControlError extends Error {
  /** `detail`: the engine's own sentence, kept only when the caller asked for it (`detailFor`). Raw: callers strip and bound it before showing it. */
  constructor(readonly code: FailureCode, message: string, readonly status?: number, readonly detail?: string) { super(message); }
}

export function meshSocketPath(platform = process.platform, env = process.env): string {
  if (env.POTLUCK_LOCAL_API_SOCKET) return env.POTLUCK_LOCAL_API_SOCKET;
  if (platform === "win32") return "\\\\.\\pipe\\potluck-mesh";
  if (platform === "darwin") return "/tmp/potluck-mesh.sock";
  if (existsSync("/run/potluck-mesh.sock")) return "/run/potluck-mesh.sock";
  if (existsSync("/run/potluck-mesh/potluck-mesh.sock")) return "/run/potluck-mesh/potluck-mesh.sock";
  return join(env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.() ?? 1000}`, "potluck-mesh.sock");
}

export function runtimeURL(env = process.env): string {
  const raw = env.POTLUCK_SIDECAR_URL || `http://127.0.0.1:${env.POTLUCK_PYTHON_PORT || "8321"}`;
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("POTLUCK_SIDECAR_URL must be a loopback HTTP URL."); }
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("POTLUCK_SIDECAR_URL must be a loopback HTTP origin (127.0.0.1 or [::1]).");
  }
  return url.origin;
}

export function safeURL(raw: string): string {
  try {
    const u = new URL(raw);
    return `${u.protocol}//${u.host}${u.pathname}`;
  } catch { return "invalid URL"; }
}

export function requestJSON<T = Record<string, unknown>>(options: {
  url?: string; socketPath?: string; path?: string; token?: string; timeout: number;
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"; body?: unknown;
  allowArray?: boolean; allowEmpty?: boolean; signal?: AbortSignal;
  errorHints?: Readonly<Record<string, string>>;
  /** Statuses whose JSON `detail` string is kept on the error. For routes where the engine's detail is a plain sentence for this machine's own user. */
  detailFor?: readonly number[];
}): Promise<T> {
  return new Promise((resolve, reject) => {
    let url: URL;
    try { url = new URL(options.url || "http://localhost"); } catch {
      reject(new ControlError("invalid_response", "Invalid endpoint URL.")); return;
    }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      reject(new ControlError("invalid_response", "Unsupported endpoint URL.")); return;
    }
    const request = url.protocol === "https:" ? httpsRequest : httpRequest;
    const req = request({
      signal: options.signal,
      protocol: url.protocol, hostname: url.hostname.replace(/^\[|\]$/g, ""), port: url.port,
      path: options.path ?? `${url.pathname}${url.search}`, socketPath: options.socketPath,
      method: options.method || "GET", headers: { Accept: "application/json",
        ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}) },
    });
    const timer = setTimeout(() => req.destroy(new ControlError("timeout", "Request timed out.")), options.timeout);
    req.once("error", (err: NodeJS.ErrnoException) => {
      clearTimeout(timer);
      reject(err instanceof ControlError ? err : new ControlError(
        err.code === "EACCES" || err.code === "EPERM" ? "permission_denied" : "unreachable",
        "Could not reach the service.",
      ));
    });
    req.once("response", (res) => {
      const status = res.statusCode ?? 0;
      const parts: Buffer[] = [];
      let size = 0;
      // No redirect following: a redirect must never forward a local credential.
      if (status < 200 || status >= 300) {
        // Display only caller-supplied hints for a recognized code. Never echo
        // arbitrary response bodies/headers that could contain credentials.
        const code = res.headers["x-potluck-error"];
        const hint = typeof code === "string" && Object.hasOwn(options.errorHints ?? {}, code)
          ? options.errorHints![code] : undefined;
        let settled = false;
        const fail = (detail?: string) => {
          if (settled) return;
          settled = true; clearTimeout(timer);
          reject(new ControlError(status === 401 || status === 403 ? "unauthorized" : "http_error",
            hint ?? `Service returned HTTP ${status}.`, status, detail));
          req.destroy();
        };
        if (!options.detailFor?.includes(status)) { res.resume(); fail(); return; }
        // Only the `detail` string of a small JSON body is kept; the request timeout still applies while it is read.
        const body: Buffer[] = [];
        let seen = 0;
        res.on("data", (part: Buffer) => { seen += part.length; if (seen > 16384) fail(); else body.push(part); });
        res.once("error", () => fail());
        res.once("end", () => {
          try {
            const value: unknown = JSON.parse(Buffer.concat(body).toString("utf8"));
            const detail = value && typeof value === "object" ? (value as { detail?: unknown }).detail : undefined;
            fail(typeof detail === "string" ? detail.slice(0, 1000) : undefined);
          } catch { fail(); }
        });
        return;
      }
      res.on("data", (part: Buffer) => {
        size += part.length;
        if (size > 1024 * 1024) req.destroy(new ControlError("invalid_response", "Response is too large."));
        else parts.push(part);
      });
      res.once("error", () => { clearTimeout(timer); reject(new ControlError("invalid_response", "Response was interrupted.")); });
      res.once("end", () => {
        clearTimeout(timer);
        try {
          if (status === 204 && options.allowEmpty && size === 0) { resolve({} as T); return; }
          const value: unknown = JSON.parse(Buffer.concat(parts).toString("utf8"));
          if (!value || typeof value !== "object" || (Array.isArray(value) && !options.allowArray)) throw new Error();
          resolve(value as T);
        } catch { reject(new ControlError("invalid_response", "Service returned invalid JSON.")); }
      });
    });
    req.end(options.body === undefined ? undefined : JSON.stringify(options.body));
  });
}
