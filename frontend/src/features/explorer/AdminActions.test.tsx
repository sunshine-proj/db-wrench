import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, problem } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { CreateDatabaseButton, DropButton } from "./AdminActions";

afterEach(() => vi.unstubAllGlobals());

const conn = (over: Partial<Connection> = {}): Connection =>
  ({
    id: "c1",
    name: "Shop",
    db_type: "postgresql",
    environment: "development",
    read_only: false,
    permissions: ["admin.manage_databases", "admin.manage_schemas"],
    capabilities: { supports_schemas: true },
    ...over,
  }) as Connection;

describe("administration actions", () => {
  it("shows nothing without the permission", () => {
    mockApi({});
    const { container } = renderWithProviders(
      <>
        <CreateDatabaseButton connection={conn({ permissions: [] })} />
        <DropButton connection={conn({ permissions: [] })} kind="database" name="x" />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("creates a database", async () => {
    const api = mockApi({
      "GET approvals": () => [],
      "POST connections/c1/databases": () => ({ name: "reports" }),
    });
    renderWithProviders(<CreateDatabaseButton connection={conn()} />);
    await userEvent.click(screen.getByRole("button", { name: "New database" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "reports");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST")).toBe(true));
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "reports",
      options: {},
    });
  });

  it("a MongoDB database needs a first collection", async () => {
    const api = mockApi({
      "GET approvals": () => [],
      "POST connections/c1/mongo/databases": () => ({ name: "reports" }),
    });
    renderWithProviders(<CreateDatabaseButton connection={conn({ db_type: "mongodb" })} />);
    await userEvent.click(screen.getByRole("button", { name: "New database" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "reports");
    await userEvent.type(
      within(dialog).getByLabelText("First collection (creates the database)"),
      "events",
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST")).toBe(true));
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "reports",
      collection: "events",
    });
  });

  it("dropping always needs the name typed, and sends it as the confirmation", async () => {
    const api = mockApi({
      "GET approvals": () => [],
      "DELETE connections/c1/databases/legacy": () => undefined,
    });
    renderWithProviders(<DropButton connection={conn()} kind="database" name="legacy" />);
    await userEvent.click(screen.getByRole("button", { name: "Drop database" }));
    const dialog = await screen.findByRole("dialog");
    const confirmBtn = within(dialog).getByRole("button", { name: "Drop database" });
    expect(confirmBtn).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type “legacy” to confirm"), "legacy");
    await userEvent.click(confirmBtn);
    await waitFor(() => expect(api.calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(api.calls.find((c) => c.method === "DELETE")?.body).toEqual({ confirm: "legacy" });
  });

  it("explains an approval requirement instead of a generic error", async () => {
    mockApi({
      "GET approvals": () => [],
      "POST connections/c1/databases": () =>
        new Response(
          JSON.stringify({
            type: "about:blank",
            title: "x",
            status: 428,
            code: "approval_required",
            detail: "needs approval",
            approval_id: "abcd1234-0000",
          }), // fmt: skip
          { status: 428, headers: { "Content-Type": "application/problem+json" } },
        ),
    });
    renderWithProviders(<CreateDatabaseButton connection={conn()} />);
    await userEvent.click(screen.getByRole("button", { name: "New database" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "reports");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(
      await screen.findByText("This operation needs approval from a second person."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open approvals" })).toHaveAttribute(
      "href",
      "/approvals",
    );
    expect(problem).toBeDefined();
  });
});
