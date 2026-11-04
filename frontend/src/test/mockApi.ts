import { vi } from "vitest";
import { jsonResponse } from "./utils";

type Handler = (url: URL, init: RequestInit) => Response | Promise<Response> | unknown;

/** Routes `fetch` by "METHOD /api/v1/path". Unmatched requests fail the test loudly. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body: unknown; headers: Record<string, string> }[] =
    [];
  const fn = vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, "http://localhost");
    const method = init.method ?? "GET";
    const key = `${method} ${url.pathname.replace(/^\/api\/v1\//, "")}`;
    calls.push({
      method,
      path: key.slice(method.length + 1),
      body: init.body ? JSON.parse(init.body as string) : undefined,
      headers: (init.headers ?? {}) as Record<string, string>,
    });
    const handler = routes[key];
    if (!handler) throw new Error(`Unmocked request: ${key}`);
    const out = await handler(url, init);
    return out instanceof Response ? out : jsonResponse(out ?? null, out === undefined ? 204 : 200);
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

export const problem = (status: number, code: string, detail?: string) =>
  jsonResponse({ type: "about:blank", title: detail ?? code, status, code, detail }, status);

export const userFixture = {
  id: "u1",
  username: "alice",
  display_name: "Alice",
  email: null,
  role_id: "r1",
  role: "Editor",
  is_active: true,
  mfa_enabled: false,
  must_change_password: false,
  last_login_at: null,
  created_at: "2024-01-01T00:00:00Z",
};

export const sessionFixture = (over: Record<string, unknown> = {}) => ({
  authenticated: true,
  user: userFixture,
  csrf_token: "csrf-1",
  mfa_state: "ok",
  app_permissions: ["connection.create"],
  password_change_required: false,
  ...over,
});
