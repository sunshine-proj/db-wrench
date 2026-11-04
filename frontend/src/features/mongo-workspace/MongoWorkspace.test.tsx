import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import {
  AGGREGATE_TEMPLATE,
  FIND_TEMPLATE,
  MongoWorkspace,
  parseMongoQuery,
} from "./MongoWorkspace";

vi.mock("@/features/sql-workspace/LazyEditor", () => ({
  LazyEditor: (p: { value: string; onChange: (v: string) => void; ariaLabel: string }) => (
    <textarea
      aria-label={p.ariaLabel}
      value={p.value}
      onChange={(e) => p.onChange(e.target.value)}
    />
  ),
}));

describe("parseMongoQuery", () => {
  it("builds a find request and drops empty parts", () => {
    const q = parseMongoQuery(
      '{"kind":"find","collection":"c","filter":{"a":1},"sort":{},"projection":{},"limit":5}',
      "shop",
    );
    expect(q).toEqual({
      database: "shop",
      collection: "c",
      kind: "find",
      filter: { a: 1 },
      limit: 5,
    });
  });
  it("builds an aggregation request", () => {
    const q = parseMongoQuery(
      '{"kind":"aggregate","collection":"c","pipeline":[{"$match":{}}]}',
      "shop",
    );
    expect(q).toMatchObject({ kind: "aggregate", pipeline: [{ $match: {} }] });
  });
  it("explains each way a query can be wrong", () => {
    expect(() => parseMongoQuery("{oops", "d")).toThrow(/not valid JSON/);
    expect(() => parseMongoQuery("[1]", "d")).toThrow(/JSON object/);
    expect(() => parseMongoQuery('{"collection":"c"}', "d")).toThrow(/kind/);
    expect(() => parseMongoQuery('{"kind":"find"}', "d")).toThrow(/collection/);
    expect(() => parseMongoQuery('{"kind":"aggregate","collection":"c"}', "d")).toThrow(/pipeline/);
  });
  it("ships valid templates", () => {
    expect(JSON.parse(FIND_TEMPLATE).kind).toBe("find");
    expect(JSON.parse(AGGREGATE_TEMPLATE).pipeline).toHaveLength(2);
  });
});

const conn = (over: Partial<Connection> = {}): Connection => ({
  id: "m1",
  name: "Mongo",
  db_type: "mongodb",
  environment: "staging",
  host: "h",
  port: 27017,
  database: "shop",
  username: "u",
  has_password: true,
  credential_model: "shared",
  tls_mode: "disable",
  has_tls_ca: false,
  connect_timeout_s: 10,
  pool_max: 10,
  read_only: true,
  enabled: true,
  options: {},
  created_at: "",
  updated_at: "",
  permissions: [
    "connection.view",
    "metadata.view",
    "query.execute_read",
    "query.view_history",
    "query.manage_saved",
  ],
  capabilities: {
    family: "document",
    engine: "mongodb",
    supports_schemas: false,
    supports_views: true,
    supports_sequences: false,
    supports_functions: false,
    supports_indexes: true,
    supports_foreign_keys: false,
    supports_keyset_pagination: true,
  },
  ...over,
});

function open(api: Record<string, () => unknown> = {}) {
  const m = mockApi({
    "GET connections": () => [conn()],
    "GET connections/m1/mongo/databases": () => [{ name: "shop" }],
    "GET connections/m1/mongo/databases/shop/collections": () => [
      { name: "customers", type: "collection" },
    ],
    "GET connections/m1/queries/history": () => [],
    "GET connections/m1/queries/saved": () => [],
    ...api,
  });
  renderWithProviders(
    <Routes>
      <Route path="/workspace/mongo/:cid" element={<MongoWorkspace />} />
    </Routes>,
    { route: "/workspace/mongo/m1" },
  );
  return m;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("MongoWorkspace", () => {
  it("runs a find and renders documents as Extended JSON", async () => {
    const api = open({
      "POST connections/m1/mongo/queries/execute": () => ({
        operation_id: "o",
        documents: [
          {
            _id: { $oid: "507f1f77bcf86cd799439011" },
            name: "Ada",
            big: { $numberLong: "9007199254740993" },
          },
        ],
        count: 1,
        truncated: false,
        elapsed_ms: 7,
      }),
    });
    const editor = await screen.findByLabelText("Query editor");
    await userEvent.clear(editor);
    await userEvent.click(editor);
    await userEvent.paste('{"kind":"find","collection":"customers","filter":{"name":"Ada"}}');
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(await screen.findByText("Completed")).toBeInTheDocument();
    expect(screen.getAllByText('ObjectId("507f1f77bcf86cd799439011")').length).toBeGreaterThan(0);
    expect(screen.getByText('NumberLong("9007199254740993")')).toBeInTheDocument();
    const sent = api.calls.find((c) => c.path.endsWith("mongo/queries/execute"))!.body as Record<
      string,
      unknown
    >;
    expect(sent).toMatchObject({
      database: "shop",
      collection: "customers",
      kind: "find",
      filter: { name: "Ada" },
    });
    expect(sent["operation_id"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("reports invalid JSON before calling the server", async () => {
    const api = open();
    const editor = await screen.findByLabelText("Query editor");
    await userEvent.clear(editor);
    await userEvent.click(editor);
    await userEvent.paste("{nope");
    await userEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(await screen.findByText(/not valid JSON/)).toBeInTheDocument();
    expect(api.calls.some((c) => c.path.endsWith("execute"))).toBe(false);
  });

  it("the collection picker edits the JSON and the templates reset it", async () => {
    open();
    const editor = await screen.findByLabelText("Query editor");
    await userEvent.selectOptions(await screen.findByLabelText("Collection"), "customers");
    await waitFor(() =>
      expect(JSON.parse((editor as HTMLTextAreaElement).value).collection).toBe("customers"),
    );
    await userEvent.click(screen.getByRole("button", { name: "New aggregation" }));
    expect(JSON.parse((editor as HTMLTextAreaElement).value).kind).toBe("aggregate");
  });

  it("is only for document connections and needs permission", async () => {
    mockApi({
      "GET connections": () => [
        conn({
          db_type: "postgresql",
          capabilities: { ...conn().capabilities, family: "relational" },
        }),
      ],
    });
    renderWithProviders(
      <Routes>
        <Route path="/workspace/mongo/:cid" element={<MongoWorkspace />} />
      </Routes>,
      { route: "/workspace/mongo/m1" },
    );
    expect(await screen.findByText(/This is a SQL connection/)).toBeInTheDocument();
  });
});
