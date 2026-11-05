import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/hooks/useAuth";
import { mockApi, sessionFixture } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { ApiTokenInfo } from "@/types/api";
import { TokensPage } from "./TokensPage";

afterEach(() => vi.unstubAllGlobals());

const token: ApiTokenInfo = {
  id: "t1",
  name: "ci-bot",
  prefix: "dbw_abcd1234",
  owner: "alice",
  scopes: ["data.read"],
  connection_ids: null,
  created_at: "2024-01-01T00:00:00Z",
  expires_at: "2999-01-01T00:00:00Z",
  last_used_at: null,
  revoked_at: null,
  active: true,
};

const routes = {
  "GET auth/session": () => sessionFixture({ app_permissions: ["app.manage_tokens"] }),
  "GET tokens": () => [token],
  "GET tokens/scopes": () => ({ scopes: ["connection.view", "data.read", "data.insert"] }),
  "GET connections": () => [],
};

function page() {
  return renderWithProviders(
    <AuthProvider>
      <TokensPage />
    </AuthProvider>,
  );
}

describe("TokensPage", () => {
  it("lists tokens by prefix only", async () => {
    mockApi(routes);
    page();
    expect(await screen.findByText("ci-bot")).toBeInTheDocument();
    expect(screen.getByText("dbw_abcd1234…")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
  });

  it("creates a token and shows the secret exactly once", async () => {
    const api = mockApi({
      ...routes,
      "POST tokens": () => ({ ...token, id: "t2", name: "new", secret: "dbw_SECRET-VALUE" }),
    });
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "new");
    await userEvent.click(await within(dialog).findByLabelText("data.insert"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await screen.findByText("dbw_SECRET-VALUE")).toBeInTheDocument();
    expect(screen.getByText("Copy your token now")).toBeInTheDocument();
    const body = api.calls.find((c) => c.method === "POST")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ name: "new", expires_in_days: 30, connection_ids: null });
    expect(body["scopes"]).toEqual(expect.arrayContaining(["data.insert"]));
  });

  it("revokes after confirmation", async () => {
    const api = mockApi({ ...routes, "DELETE tokens/t1": () => undefined });
    page();
    await userEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "DELETE")).toBe(true));
  });
});
