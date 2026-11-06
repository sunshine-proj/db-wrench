import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection, Principal } from "@/types/api";
import { PrincipalsPage, privilegesFor } from "./PrincipalsPage";

afterEach(() => vi.unstubAllGlobals());

const perms = ["admin.view_users", "admin.manage_users", "admin.manage_privileges"];
const conn = (over: Partial<Connection> = {}): Connection =>
  ({
    id: "c1",
    name: "Shop",
    db_type: "postgresql",
    environment: "development",
    database: "shop",
    read_only: false,
    permissions: perms,
    capabilities: { supports_schemas: true },
    ...over,
  }) as Connection; // fmt: skip

function page(c: Connection) {
  return renderWithProviders(
    <Routes>
      <Route element={<Outlet2 c={c} />}>
        <Route path="/" element={<PrincipalsPage />} />
      </Route>
    </Routes>,
  );
}
import { Outlet } from "react-router-dom";
function Outlet2({ c }: { c: Connection }) {
  return <Outlet context={c} />;
}

const alice: Principal = {
  name: "alice",
  kind: "user",
  can_login: true,
  protected: false,
  host: null,
  member_of: ["staff"],
  attributes: { createdb: true },
}; // fmt: skip
const postgres: Principal = {
  name: "postgres",
  kind: "user",
  can_login: true,
  protected: true,
  host: null,
  member_of: [],
  attributes: { superuser: true },
}; // fmt: skip

describe("privilegesFor", () => {
  it("offers only what the server allows per engine and object", () => {
    expect(privilegesFor("postgresql", "database")).toEqual(["CONNECT", "CREATE", "TEMPORARY"]);
    expect(privilegesFor("postgresql", "schema")).toEqual(["USAGE", "CREATE"]);
    expect(privilegesFor("postgresql", "table")).toContain("TRUNCATE");
    expect(privilegesFor("mssql", "table")).not.toContain("EXECUTE");
    expect(privilegesFor("mssql", "schema")).toContain("EXECUTE");
    expect(privilegesFor("mariadb", "table")).toEqual(privilegesFor("mysql", "table"));
    expect(privilegesFor("mysql", "table")).not.toContain("EXECUTE");
    expect(privilegesFor("mysql", "database")).toContain("EXECUTE");
    for (const list of [privilegesFor("mysql", "database"), privilegesFor("postgresql", "table")]) {
      for (const bad of ["GRANT OPTION", "SUPER", "FILE", "ALL"]) expect(list).not.toContain(bad);
    }
  });
});

describe("PrincipalsPage (relational)", () => {
  it("lists accounts and gives protected ones no actions", async () => {
    mockApi({ "GET connections/c1/principals": () => [alice, postgres] });
    page(conn());
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("Protected")).toBeInTheDocument();
    expect(screen.getByText("superuser")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Change password" })).toHaveLength(1);
  });

  it("needs the view permission", () => {
    mockApi({});
    page(conn({ permissions: [] }));
    expect(screen.getByText("Your role does not include access to this page.")).toBeInTheDocument();
  });

  it("creates a login role with the chosen options", async () => {
    const api = mockApi({
      "GET connections/c1/principals": () => [],
      "GET approvals": () => [],
      "POST connections/c1/principals": () => alice,
    });
    page(conn());
    await userEvent.click(await screen.findByRole("button", { name: "New user / role" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "bob");
    await userEvent.type(within(dialog).getByLabelText("Password"), "Long-Enough-Pass1!");
    await userEvent.click(within(dialog).getByLabelText("Can create databases"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST")).toBe(true));
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "bob",
      password: "Long-Enough-Pass1!",
      options: { login: true, createdb: true, createrole: false },
    }); // fmt: skip
  });

  it("drops only after the name is typed", async () => {
    const api = mockApi({
      "GET connections/c1/principals": () => [alice],
      "GET approvals": () => [],
      "DELETE connections/c1/principals/alice": () => undefined,
    });
    page(conn());
    await userEvent.click(await screen.findByRole("button", { name: "Drop alice" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Delete" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type “alice” to confirm"), "alice");
    await userEvent.click(confirm);
    await waitFor(() => expect(api.calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(api.calls.find((c) => c.method === "DELETE")?.body).toEqual({ confirm: "alice" });
  });

  it("grants table privileges and requires the name on production", async () => {
    const api = mockApi({
      "GET connections/c1/principals": () => [alice],
      "GET connections/c1/principals/alice/grants": () => [
        {
          kind: "table",
          database: "shop",
          schema: "public",
          object: "orders",
          privileges: ["SELECT"],
          grantable: false,
        },
      ],
      "GET approvals": () => [],
      "POST connections/c1/principals/alice/grants": () => undefined,
    }); // fmt: skip
    page(conn({ environment: "production" }));
    await userEvent.click(await screen.findByRole("button", { name: "Privileges" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("orders", { exact: false })).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText(/Table \/ collection/), "customers");
    await userEvent.click(within(dialog).getByLabelText("SELECT"));
    const grant = within(dialog).getAllByRole("button", { name: "Grant" })[0]!;
    expect(grant).toBeDisabled(); // production: name must be typed
    await userEvent.type(
      within(dialog).getByLabelText("Type “alice” to confirm this change"),
      "alice",
    );
    await userEvent.click(grant);
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST")).toBe(true));
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({
      privileges: ["SELECT"],
      target: { kind: "table", database: "shop", schema: "public", object: "customers" },
      confirm: "alice",
    });
  });
});

describe("PrincipalsPage (MongoDB)", () => {
  const mongo = conn({ db_type: "mongodb", capabilities: { supports_schemas: false } as never });
  it("lists users of a database and revokes a role", async () => {
    const api = mockApi({
      "GET connections/c1/mongo/databases": () => [{ name: "admin" }, { name: "app" }],
      "GET connections/c1/mongo/databases/app/users": () => [
        { name: "svc", database: "app", roles: [{ role: "read", db: "app" }], protected: false },
      ],
      "GET connections/c1/mongo/databases/app/roles": () => [],
      "GET approvals": () => [],
      "DELETE connections/c1/mongo/databases/app/users/svc/roles": () => undefined,
    });
    page(mongo);
    expect(await screen.findByText("svc")).toBeInTheDocument(); // skips the system databases by default
    await userEvent.click(screen.getByRole("button", { name: "Revoke read" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(api.calls.find((c) => c.method === "DELETE")?.body).toEqual({
      roles: [{ role: "read", db: "app" }],
      confirm: "svc",
    }); // fmt: skip
  });
});
