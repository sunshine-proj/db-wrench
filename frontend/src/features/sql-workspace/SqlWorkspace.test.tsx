import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi, problem } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { SqlWorkspace } from "./SqlWorkspace";

// Monaco cannot run in jsdom; a textarea with the same contract stands in for it.
vi.mock("./LazyEditor", () => ({
  LazyEditor: (p: {
    value: string;
    onChange: (v: string) => void;
    ariaLabel: string;
    onRun?: () => void;
  }) => (
    <textarea
      aria-label={p.ariaLabel}
      value={p.value}
      onChange={(e) => p.onChange(e.target.value)}
      onKeyDown={(e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") p.onRun?.();
      }}
    />
  ),
}));

const conn = (over: Partial<Connection> = {}): Connection => ({
  id: "c1",
  name: "Orders DB",
  db_type: "postgresql",
  environment: "development",
  host: "h",
  port: 5432,
  database: "shop",
  username: "u",
  has_password: true,
  credential_model: "shared",
  tls_mode: "disable",
  has_tls_ca: false,
  connect_timeout_s: 10,
  pool_max: 10,
  read_only: false,
  enabled: true,
  options: {},
  created_at: "",
  updated_at: "",
  permissions: [
    "connection.view",
    "metadata.view",
    "query.execute_read",
    "query.execute_write",
    "query.view_history",
    "query.manage_saved",
    "export.export",
  ],
  capabilities: {
    family: "relational",
    engine: "postgresql",
    supports_schemas: true,
    supports_views: true,
    supports_sequences: true,
    supports_functions: true,
    supports_indexes: true,
    supports_foreign_keys: true,
    supports_keyset_pagination: true,
  },
  ...over,
});

const okResult = (extra = {}) => ({
  operation_id: "op",
  statements: ["SELECT 1"],
  elapsed_ms: 12,
  committed: false,
  results: [
    { columns: [{ name: "n", type: "int4" }], rows: [[1], [2]], row_count: 2, truncated: false },
  ],
  ...extra,
});

function routes(over: Record<string, () => unknown> = {}) {
  return mockApi({
    "GET connections": () => [conn()],
    "GET connections/c1/databases": () => [{ name: "shop" }],
    "GET connections/c1/databases/shop/schemas": () => [{ name: "public", owner: null }],
    "GET connections/c1/queries/history": () => [],
    "GET connections/c1/queries/saved": () => [],
    ...over,
  });
}

function open() {
  return renderWithProviders(
    <Routes>
      <Route path="/workspace/sql/:cid" element={<SqlWorkspace />} />
    </Routes>,
    { route: "/workspace/sql/c1" },
  );
}

async function type(sql: string) {
  const editor = await screen.findByLabelText("Query editor");
  await userEvent.clear(editor);
  await userEvent.click(editor);
  await userEvent.paste(sql);
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("SqlWorkspace", () => {
  it("runs a query and shows typed results with timing", async () => {
    const api = routes({ "POST connections/c1/queries/execute": () => okResult() });
    open();
    await type("SELECT 1");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(await screen.findByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("12 ms")).toBeInTheDocument();
    expect(screen.getByText("int4")).toBeInTheDocument();
    const sent = api.calls.find((c) => c.path === "connections/c1/queries/execute")!.body as Record<
      string,
      unknown
    >;
    expect(sent).toMatchObject({
      sql: "SELECT 1",
      database: "shop",
      schema: "public",
      timeout_s: 30,
    });
    expect(sent["operation_id"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("Ctrl+Enter runs the query", async () => {
    const api = routes({ "POST connections/c1/queries/execute": () => okResult() });
    open();
    await type("SELECT 1");
    await userEvent.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() =>
      expect(api.calls.some((c) => c.path.endsWith("queries/execute"))).toBe(true),
    );
  });

  it("shows database errors without hiding the editor", async () => {
    routes({
      "POST connections/c1/queries/execute": () =>
        problem(400, "query_failed", 'syntax error at or near "SELEKT"'),
    });
    open();
    await type("SELEKT 1");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(await screen.findByText(/syntax error at or near/)).toBeInTheDocument();
    expect(screen.getByLabelText("Query editor")).toHaveValue("SELEKT 1");
  });

  it("asks for confirmation of destructive statements and re-runs with the flags", async () => {
    let calls = 0;
    const api = routes({
      "POST connections/c1/queries/execute": () => {
        calls += 1;
        return calls === 1
          ? problem(
              428,
              "confirmation_required",
              "This statement looks destructive (DROP, TRUNCATE, or DELETE/UPDATE without WHERE). Confirm explicitly to run it.",
            )
          : okResult({
              committed: true,
              results: [{ columns: [], rows: [], row_count: 4, truncated: false }],
            });
      },
    });
    open();
    await type("DELETE FROM t");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(
      await screen.findByRole("heading", { name: "Run destructive statement?" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run statement" }));
    expect(await screen.findByText("4 rows affected")).toBeInTheDocument();
    const second = api.calls.filter((c) => c.path.endsWith("queries/execute"))[1]!.body as Record<
      string,
      unknown
    >;
    expect(second["confirm_destructive"]).toBe(true);
  });

  it("production writes require typing the connection name", async () => {
    let calls = 0;
    const api = mockApi({
      "GET connections": () => [conn({ environment: "production" })],
      "GET connections/c1/databases": () => [{ name: "shop" }],
      "GET connections/c1/databases/shop/schemas": () => [{ name: "public", owner: null }],
      "GET connections/c1/queries/history": () => [],
      "GET connections/c1/queries/saved": () => [],
      "POST connections/c1/queries/execute": () => {
        calls += 1;
        return calls === 1
          ? problem(
              428,
              "confirmation_required",
              "This is a production connection. Type the object name 'Orders DB' to confirm.",
            )
          : okResult({ committed: true });
      },
    });
    open();
    await type("UPDATE t SET a = 1 WHERE id = 1");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    const go = await screen.findByRole("button", { name: "Run statement" });
    expect(go).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type “Orders DB” to confirm/), "Orders DB");
    await userEvent.click(go);
    await screen.findByText("Completed");
    const second = api.calls.filter((c) => c.path.endsWith("queries/execute"))[1]!.body as Record<
      string,
      unknown
    >;
    expect(second["confirm"]).toBe("Orders DB");
  });

  it("cancels a running query using the operation id it chose", async () => {
    let release: (v: unknown) => void = () => undefined;
    const api = routes({
      "POST connections/c1/queries/execute": () =>
        new Promise((r) => {
          release = r;
        }),
      "POST connections/c1/queries/op-x/cancel": () => ({
        status: "cancelling",
        best_effort: false,
      }),
    });
    // the id is generated client-side; capture it from the request
    const real = api.fn.getMockImplementation()!;
    let op = "";
    api.fn.mockImplementation(async (input: string, init: RequestInit = {}) => {
      if (input.includes("queries/execute") && init.body)
        op = JSON.parse(init.body as string).operation_id;
      if (input.includes("/cancel"))
        return real(input.replace(/queries\/[^/]+\/cancel/, "queries/op-x/cancel"), init);
      return real(input, init);
    });
    open();
    await type("SELECT pg_sleep(30)");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    const cancel = await screen.findByRole("button", { name: /Cancel/ });
    await userEvent.click(cancel);
    await waitFor(() => expect(api.calls.some((c) => c.path.endsWith("/cancel"))).toBe(true));
    expect(op).toMatch(/^[0-9a-f-]{36}$/);
    release(problemBody(409, "cancelled"));
  });

  it("read-only connections hide the dry-run option; no permission shows an explanation", async () => {
    mockApi({
      "GET connections": () => [conn({ read_only: true })],
      "GET connections/c1/databases": () => [{ name: "shop" }],
      "GET connections/c1/databases/shop/schemas": () => [],
      "GET connections/c1/queries/history": () => [],
      "GET connections/c1/queries/saved": () => [],
    });
    open();
    await screen.findByLabelText("Query editor");
    expect(screen.queryByLabelText(/Roll back after running/)).not.toBeInTheDocument();
  });

  it("explains missing permissions", async () => {
    mockApi({ "GET connections": () => [conn({ permissions: ["connection.view"] })] });
    open();
    expect(
      await screen.findByText("Your role cannot run queries on this connection."),
    ).toBeInTheDocument();
  });

  it("offers server-side export of a single read statement", async () => {
    const api = routes({
      "POST connections/c1/queries/execute": () => okResult(),
      "POST connections/c1/queries/export": () => ({}),
    });
    vi.stubGlobal(
      "URL",
      Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }),
    );
    open();
    await type("SELECT 1");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    await screen.findByText("Completed");
    expect(screen.getByRole("button", { name: /CSV/ })).toBeInTheDocument();
    expect(api.calls.filter((c) => c.path.endsWith("/export"))).toHaveLength(0); // nothing exported until asked
  });

  it("tabs are independent and remembered per connection", async () => {
    routes();
    const { unmount } = open();
    await type("SELECT 'first'");
    await userEvent.click(screen.getByRole("button", { name: "New tab" }));
    await type("SELECT 'second'");
    await userEvent.click(screen.getByRole("tab", { name: "Query 1" }));
    expect(screen.getByLabelText("Query editor")).toHaveValue("SELECT 'first'");
    unmount();
    open();
    expect(await screen.findAllByRole("tab", { name: /^Query \d+$/ })).toHaveLength(2);
  });
});

function problemBody(status: number, code: string) {
  return new Response(JSON.stringify({ type: "about:blank", title: code, status, code }), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}
