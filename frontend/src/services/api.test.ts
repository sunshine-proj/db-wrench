import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "@/test/utils";
import {
  ApiError,
  authApi,
  connectionsApi,
  explorerApi,
  onUnauthorized,
  rowParams,
  setCsrfToken,
} from "./api";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  setCsrfToken(null);
  onUnauthorized(null);
});
afterEach(() => vi.unstubAllGlobals());

function lastCall() {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init, headers: init.headers as Record<string, string> };
}

describe("api client", () => {
  it("sends the CSRF token on state-changing requests only", async () => {
    setCsrfToken("tok123");
    fetchMock.mockResolvedValue(jsonResponse([]));
    await connectionsApi.list();
    expect(lastCall().headers["X-CSRF-Token"]).toBeUndefined();

    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await connectionsApi.test("abc");
    expect(lastCall().headers["X-CSRF-Token"]).toBe("tok123");
    expect(lastCall().init.method).toBe("POST");
  });

  it("always uses same-origin cookies and never puts credentials in the URL", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ mfa_state: "ok", csrf_token: "t", user: {} }));
    await authApi.login("alice", "s3cret!pw");
    const { url, init } = lastCall();
    expect(init.credentials).toBe("same-origin");
    expect(url).not.toContain("s3cret");
    expect(init.body).toContain("s3cret!pw"); // only in the body
  });

  it("stores the CSRF token returned by login and session", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ mfa_state: "ok", csrf_token: "fromlogin", user: {} }),
    );
    await authApi.login("a", "b");
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: 1 }));
    await connectionsApi.test("x");
    expect(lastCall().headers["X-CSRF-Token"]).toBe("fromlogin");
  });

  it("turns problem+json into ApiError with code and request id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          type: "about:blank",
          title: "Forbidden",
          status: 403,
          code: "forbidden",
          detail: "Missing permission: data.read",
          request_id: "abc123",
        },
        403,
      ),
    );
    const err = await connectionsApi.list().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect([e.status, e.code, e.requestId, e.message]).toEqual([
      403,
      "forbidden",
      "abc123",
      "Missing permission: data.read",
    ]);
  });

  it("survives non-JSON error bodies", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>502</html>", { status: 502, statusText: "Bad Gateway" }),
    );
    const e = (await connectionsApi.list().catch((x: unknown) => x)) as ApiError;
    expect(e.status).toBe(502);
    expect(e.code).toBe("http_502");
  });

  it("reports network failures as ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const e = (await connectionsApi.list().catch((x: unknown) => x)) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect(e.code).toBe("network_error");
  });

  it("notifies on 401 except for failed logins", async () => {
    const handler = vi.fn();
    onUnauthorized(handler);
    fetchMock.mockResolvedValue(
      jsonResponse({ status: 401, code: "unauthorized", title: "x" }, 401),
    );
    await connectionsApi.list().catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
    await authApi.login("a", "wrong").catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1); // bad password must not look like session expiry
  });

  it("handles 204 responses", async () => {
    fetchMock.mockResolvedValue(jsonResponse(null, 204));
    await expect(connectionsApi.remove("id")).resolves.toBeUndefined();
  });

  it("encodes every path segment so names cannot alter the route", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ rows: [] }));
    await explorerApi.rows(
      { cid: "c1", db: "my db", schema: "a/b", table: "t?x=1#frag" },
      { limit: 10 },
    );
    const { url } = lastCall();
    expect(url).toContain("/databases/my%20db/schemas/a%2Fb/tables/t%3Fx%3D1%23frag/rows");
    expect(url.split("?")).toHaveLength(2);
  });

  it("serialises row requests with JSON for complex values", () => {
    const p = rowParams({
      limit: 50,
      cursor: "abc",
      sort: [{ column: "name", direction: "desc" }],
      filters: [{ column: "id", op: "in", value: ["1", "2"] }],
      columns: ["id"],
      search: "x",
      searchColumns: ["name"],
    });
    expect(p.limit).toBe(50);
    expect(JSON.parse(p.sort as string)).toEqual([{ column: "name", direction: "desc" }]);
    expect(JSON.parse(p.filters as string)[0].value).toEqual(["1", "2"]);
    expect(JSON.parse(p.search_columns as string)).toEqual(["name"]);
    expect(rowParams({ limit: 5 }).filters).toBeUndefined();
    expect(rowParams({ limit: 5, searchColumns: ["a"] }).search_columns).toBeUndefined();
  });
});

describe("approvals and Phase 5 clients", () => {
  it("attaches Approval-Id only to the wrapped request", async () => {
    const { withApproval, adminApi } = await import("./api");
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ name: "x" })));
    await withApproval("appr-1", () => adminApi.createDatabase("c1", "x"));
    expect(lastCall().headers["Approval-Id"]).toBe("appr-1");
    await adminApi.createDatabase("c1", "y");
    expect(lastCall().headers["Approval-Id"]).toBeUndefined(); // never leaks to later calls
    await withApproval(undefined, () => adminApi.createDatabase("c1", "z"));
    expect(lastCall().headers["Approval-Id"]).toBeUndefined();
  });

  it("sends DELETE confirmations in the body, path-encoded", async () => {
    const { adminApi } = await import("./api");
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await adminApi.dropDatabase("c1", "my db/../x");
    const { url, init } = lastCall();
    expect(url).toBe("/api/v1/connections/c1/databases/my%20db%2F..%2Fx");
    expect(JSON.parse(init.body as string)).toEqual({ confirm: "my db/../x" });
  });

  it("addresses PostgreSQL and MySQL indexes with and without the schema segment", async () => {
    const { adminApi } = await import("./api");
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ name: "ix" })));
    await adminApi.createIndex({ cid: "c", db: "d", schema: "s", table: "t" }, "ix", ["a"], true);
    expect(lastCall().url).toBe("/api/v1/connections/c/databases/d/schemas/s/tables/t/indexes");
    await adminApi.createIndex({ cid: "c", db: "d", table: "t" }, "ix", ["a"], false);
    expect(lastCall().url).toBe("/api/v1/connections/c/databases/d/tables/t/indexes");
  });

  it("builds same-origin export URLs with the chosen filters", async () => {
    const { auditSinkApi } = await import("./api");
    expect(auditSinkApi.exportUrl("csv", { action: "auth", result: "" })).toBe(
      "/api/v1/audit/export?format=csv&action=auth",
    );
  });
});
