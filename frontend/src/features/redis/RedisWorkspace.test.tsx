import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { editableText, formatTtl, parseEditable, RedisWorkspace } from "./RedisWorkspace";

const conn = (over: Partial<Connection> = {}): Connection => ({
  id: "r1",
  name: "Cache",
  db_type: "redis",
  environment: "staging",
  host: "h",
  port: 6379,
  database: "0",
  username: "",
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
    "data.read",
    "data.update",
    "data.delete",
    "data.insert",
    "query.execute_read",
  ],
  capabilities: {
    family: "keyvalue",
    engine: "redis",
    supports_schemas: false,
    supports_views: false,
    supports_sequences: false,
    supports_functions: false,
    supports_indexes: false,
    supports_foreign_keys: false,
    supports_keyset_pagination: false,
  },
  ...over,
});

const keyPage = (name = "<img src=x onerror=alert(1)>") => ({
  keys: [{ key: "a2V5", name, type: "string", ttl_ms: 60000, binary: false }],
  limit: 100,
  next_cursor: null,
  total_estimate: 1,
  truncated: false,
  warnings: [],
});

const detail = (over: Record<string, unknown> = {}) => ({
  key: "a2V5",
  name: "<img src=x onerror=alert(1)>",
  type: "string",
  ttl_ms: 60000,
  encoding: "embstr",
  size_bytes: 64,
  version: "v1",
  editable: true,
  binary: false,
  value: { value: "<b>bold</b>", next_cursor: null, length: 11, truncated: false },
  ...over,
});

function open(api: Record<string, () => unknown> = {}, c: Connection = conn()) {
  const m = mockApi({
    "GET connections": () => [c],
    "GET connections/r1/redis/databases": () => [
      { index: 0, name: "0", keys: 1, expires: 0 },
      { index: 1, name: "1", keys: 0, expires: 0 },
    ],
    "GET connections/r1/redis/databases/0/keys": () => keyPage(),
    "GET connections/r1/redis/databases/0/keys/a2V5": () => detail(),
    ...api,
  });
  renderWithProviders(
    <Routes>
      <Route path="/redis/:cid" element={<RedisWorkspace />} />
    </Routes>,
    { route: "/redis/r1" },
  );
  return m;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("helpers", () => {
  it("formats TTLs", () => {
    expect(formatTtl(null)).toBe("no expiry");
    expect(formatTtl(30000)).toBe("30s");
    expect(formatTtl(600000)).toBe("10m");
    expect(formatTtl(7200000)).toBe("2h");
    expect(formatTtl(3 * 86400000)).toBe("3d");
  });
  it("round-trips editable values", () => {
    expect(editableText({ type: "string", value: { value: "x" } as never })).toBe("x");
    const txt = editableText({ type: "hash", value: { value: { a: "1" } } as never });
    expect(parseEditable("hash", txt)).toEqual({ a: "1" });
    expect(parseEditable("string", "{raw}")).toBe("{raw}");
    expect(() => parseEditable("list", "[oops")).toThrow();
  });
});

describe("RedisWorkspace", () => {
  it("lists keys and shows values as text, never as HTML", async () => {
    open();
    const row = await screen.findByRole("button", { name: /onerror/ });
    expect(row.querySelector("img")).toBeNull(); // the key name is text
    await userEvent.click(row);
    const value = await screen.findByTestId("redis-value");
    expect(value.textContent).toBe("<b>bold</b>");
    expect(value.querySelector("b")).toBeNull();
    expect(screen.getAllByText("string").length).toBeGreaterThan(0);
  });

  it("saves an edited value with the version it read", async () => {
    const api = open({
      "PUT connections/r1/redis/databases/0/keys/a2V5": () => detail({ version: "v2" }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /onerror/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit value" }));
    const box = await screen.findByLabelText("Value editor");
    await userEvent.clear(box);
    await userEvent.type(box, "new");
    await userEvent.click(screen.getByRole("button", { name: "Save value" }));
    await waitFor(() => {
      const put = api.calls.find((c) => c.method === "PUT");
      expect(put?.body).toEqual({ type: "string", value: "new", version: "v1" });
    });
  });

  it("hides every write control on a read-only connection", async () => {
    open({}, conn({ read_only: true }));
    await userEvent.click(await screen.findByRole("button", { name: /onerror/ }));
    await screen.findByTestId("redis-value");
    expect(screen.queryByRole("button", { name: "Edit value" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete key" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Create key" })).toBeNull();
  });

  it("refuses a non-Redis connection", async () => {
    open({}, conn({ db_type: "postgresql" }));
    expect(await screen.findByText("This is not a Redis connection.")).toBeInTheDocument();
  });

  it("runs a console command and prints the reply as text", async () => {
    const api = open({
      "POST connections/r1/redis/commands/execute": () => ({
        reply: "PONG",
        truncated: false,
        elapsed_ms: 1,
        command: "PING",
        kind: "read",
      }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: "Console" }));
    await userEvent.type(await screen.findByLabelText("Command"), "PING");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect((await screen.findByTestId("redis-reply")).textContent).toBe('"PONG"');
    expect(api.calls.find((c) => c.method === "POST")?.body).toMatchObject({
      database: "0",
      command: "PING",
    });
  });
});
