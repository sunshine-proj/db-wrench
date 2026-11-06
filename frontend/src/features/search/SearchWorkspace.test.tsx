import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { parseJsonObject, SearchWorkspace, totalLabel } from "./SearchWorkspace";

const conn = (over: Partial<Connection> = {}): Connection => ({
  id: "e1",
  name: "Logs",
  db_type: "elasticsearch",
  environment: "staging",
  host: "h",
  port: 9200,
  database: null,
  username: "elastic",
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
    "metadata.view",
    "query.execute_read",
    "admin.manage_databases",
  ],
  capabilities: {
    family: "search",
    engine: "elasticsearch",
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

const doc = {
  key: "MQ",
  id: "1",
  source: { msg: "<script>alert(1)</script>", n: 1 },
  score: 1.2,
  seq_no: 4,
  primary_term: 1,
  sort: null,
};

const page = {
  hits: [doc],
  total: 1,
  total_relation: "eq",
  took_ms: 3,
  offset: 0,
  size: 25,
  next_cursor: null,
  truncated: false,
  timed_out: false,
  warnings: [],
};

function open(api: Record<string, () => unknown> = {}, c: Connection = conn()) {
  const m = mockApi({
    "GET connections": () => [c],
    "GET connections/e1/search/indices": () => [
      {
        name: "logs",
        health: "green",
        status: "open",
        docs: 1,
        size_bytes: 10,
        primaries: 1,
        replicas: 0,
        aliases: [],
      },
    ],
    "GET connections/e1/search/indices/logs": () => ({
      name: "logs",
      fields: [
        { path: "msg", type: "text" },
        { path: "n", type: "long" },
      ],
      settings: { number_of_shards: "1" },
      aliases: [],
      docs: 1,
      size_bytes: 10,
    }),
    "GET connections/e1/search/indices/logs/documents": () => page,
    ...api,
  });
  renderWithProviders(
    <Routes>
      <Route path="/search/:cid" element={<SearchWorkspace />} />
    </Routes>,
    { route: "/search/e1" },
  );
  return m;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("helpers", () => {
  it("parses only JSON objects", () => {
    expect(parseJsonObject('{"a":1}')).toEqual({ a: 1 });
    expect(() => parseJsonObject("[1]")).toThrow(/object/);
    expect(() => parseJsonObject("{oops")).toThrow(/valid JSON/);
  });
  it("labels exact and lower-bound totals", () => {
    expect(totalLabel(5, "eq")).toBe("5 matches");
    expect(totalLabel(10000, "gte")).toBe("10000+ matches");
    expect(totalLabel(null, "eq")).toBe("");
  });
});

describe("SearchWorkspace", () => {
  it("shows documents as text, never as HTML", async () => {
    open();
    await userEvent.click(await screen.findByRole("button", { name: /alert/ }));
    const pre = await screen.findByTestId("search-doc");
    expect(pre.textContent).toContain("<script>alert(1)</script>");
    expect(pre.querySelector("script")).toBeNull();
  });

  it("saves an edited document with the sequence numbers it read", async () => {
    const api = open({
      "PUT connections/e1/search/indices/logs/documents/MQ": () => ({ ...doc, seq_no: 5 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /alert/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit document" }));
    const box = await screen.findByLabelText("Document (JSON)");
    await userEvent.clear(box);
    await userEvent.click(box);
    await userEvent.paste('{"msg":"fixed"}');
    await userEvent.click(screen.getByRole("button", { name: "Save document" }));
    await waitFor(() => {
      const put = api.calls.find((c) => c.method === "PUT");
      expect(put?.body).toEqual({ source: { msg: "fixed" }, seq_no: 4, primary_term: 1 });
    });
  });

  it("refuses to save invalid JSON without calling the server", async () => {
    const api = open();
    await userEvent.click(await screen.findByRole("button", { name: /alert/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit document" }));
    const box = await screen.findByLabelText("Document (JSON)");
    await userEvent.clear(box);
    await userEvent.click(box);
    await userEvent.paste("{not json");
    await userEvent.click(screen.getByRole("button", { name: "Save document" }));
    expect(await screen.findByText("Not valid JSON.")).toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("hides every write control on a read-only connection", async () => {
    open({}, conn({ read_only: true }));
    await userEvent.click(await screen.findByRole("button", { name: /alert/ }));
    await screen.findByTestId("search-doc");
    for (const name of [
      "Edit document",
      "Delete document",
      "New document",
      "Delete index",
      "Create index",
    ])
      expect(screen.queryByRole("button", { name })).toBeNull();
  });

  it("refuses other engines", async () => {
    open({}, conn({ db_type: "redis" }));
    expect(await screen.findByText(/not an Elasticsearch/)).toBeInTheDocument();
  });

  it("runs a console search and prints the response as text", async () => {
    const api = open({
      "POST connections/e1/search/execute": () => ({
        body: { hits: { total: { value: 0 } } },
        took_ms: 2,
        truncated: false,
      }),
    });
    await userEvent.click(await screen.findByRole("tab", { name: "Console" }));
    await userEvent.click(await screen.findByRole("button", { name: "Run search" }));
    const out = await screen.findByTestId("search-result");
    expect(out.textContent).toContain('"value": 0');
    expect(api.calls.find((c) => c.method === "POST")?.body).toMatchObject({ index: "logs" });
  });
});
