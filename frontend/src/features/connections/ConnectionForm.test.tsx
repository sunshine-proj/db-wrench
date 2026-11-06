import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { ConnectionFormDialog } from "./ConnectionForm";

afterEach(() => vi.unstubAllGlobals());

const existing: Connection = {
  id: "c1",
  name: "Orders DB",
  db_type: "postgresql",
  environment: "production",
  host: "db.internal",
  port: 5432,
  database: "orders",
  username: "app",
  has_password: true,
  credential_model: "shared",
  tls_mode: "verify-full",
  has_tls_ca: true,
  connect_timeout_s: 10,
  pool_max: 10,
  read_only: true,
  enabled: true,
  options: {},
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  permissions: [],
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
};

describe("ConnectionFormDialog", () => {
  it("defaults to the safe option: read-only on", () => {
    mockApi({});
    renderWithProviders(<ConnectionFormDialog open onOpenChange={() => undefined} />);
    expect(screen.getByLabelText("Read-only (recommended)")).toBeChecked();
  });

  it("switches the default port with the engine", async () => {
    mockApi({});
    renderWithProviders(<ConnectionFormDialog open onOpenChange={() => undefined} />);
    expect(screen.getByLabelText("Port")).toHaveValue(5432);
    await userEvent.selectOptions(screen.getByLabelText("Engine"), "mongodb");
    expect(screen.getByLabelText("Port")).toHaveValue(27017);
    expect(screen.getByLabelText("Authentication database")).toBeInTheDocument();
  });

  it("asks for a file, not a network destination, for SQLite", async () => {
    const api = mockApi({
      "POST connections": () => ({ ...existing }),
      "GET connections": () => [],
    });
    const onOpenChange = vi.fn();
    renderWithProviders(<ConnectionFormDialog open onOpenChange={onOpenChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Engine"), "sqlite");
    expect(screen.queryByLabelText("Host")).toBeNull();
    expect(screen.queryByLabelText("Port")).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), "Local file");
    await userEvent.type(screen.getByLabelText("Database file"), "data/app.db");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const body = api.calls.find((c) => c.method === "POST")?.body as Record<string, unknown>;
    expect(body).toMatchObject({
      db_type: "sqlite",
      database: "data/app.db",
      host: "local-file",
      port: 1,
      username: "",
      password: "",
      tls_mode: "disable",
    });
  });

  it("creates a connection with the entered values", async () => {
    const api = mockApi({
      "POST connections": () => ({ ...existing }),
      "GET connections": () => [],
    });
    const onOpenChange = vi.fn();
    renderWithProviders(<ConnectionFormDialog open onOpenChange={onOpenChange} />);
    await userEvent.type(screen.getByLabelText("Name"), "Reporting");
    await userEvent.type(screen.getByLabelText("Host"), "10.0.0.7");
    await userEvent.type(screen.getByLabelText("Username"), "reader");
    await userEvent.type(screen.getByLabelText("Password"), "hunter2-hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const body = api.calls.find((c) => c.method === "POST")?.body as Record<string, unknown>;
    expect(body).toMatchObject({
      name: "Reporting",
      db_type: "postgresql",
      host: "10.0.0.7",
      port: 5432,
      username: "reader",
      password: "hunter2-hunter2",
      read_only: true,
    });
  });

  it("never pre-fills or re-sends the stored password when editing", async () => {
    const api = mockApi({ "PATCH connections/c1": () => existing, "GET connections": () => [] });
    renderWithProviders(
      <ConnectionFormDialog open onOpenChange={() => undefined} connection={existing} />,
    );
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.getByText("Leave blank to keep the stored password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Engine")).toBeDisabled(); // engine cannot change after creation
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Orders DB 2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "PATCH")).toBe(true));
    const body = api.calls.find((c) => c.method === "PATCH")?.body as Record<string, unknown>;
    expect(body).not.toHaveProperty("password");
    expect(body).not.toHaveProperty("db_type");
    expect(body).not.toHaveProperty("tls_ca"); // stored certificate is kept when left blank
    expect(body["name"]).toBe("Orders DB 2");
  });

  it("warns when enabling writes on a production connection", async () => {
    mockApi({});
    renderWithProviders(
      <ConnectionFormDialog open onOpenChange={() => undefined} connection={existing} />,
    );
    await userEvent.click(screen.getByLabelText("Read-only (recommended)"));
    expect(screen.getByText(/production connection with writes enabled/)).toBeInTheDocument();
  });

  it("shows server-side validation errors such as network policy denials", async () => {
    mockApi({
      "POST connections": () =>
        new Response(
          JSON.stringify({
            type: "about:blank",
            title: "Destination denied",
            status: 403,
            code: "network_policy_denied",
            detail: "169.254.169.254 is in a range that is never permitted",
          }),
          { status: 403, headers: { "Content-Type": "application/problem+json" } },
        ),
    });
    renderWithProviders(<ConnectionFormDialog open onOpenChange={() => undefined} />);
    await userEvent.type(screen.getByLabelText("Name"), "Metadata");
    await userEvent.type(screen.getByLabelText("Host"), "169.254.169.254");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/never permitted/)).toBeInTheDocument();
  });
});
