import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { AuditSink, OidcConfig } from "@/types/api";
import { formatMappings, IntegrationsPage, parseMappings } from "./IntegrationsPage";

afterEach(() => vi.unstubAllGlobals());

const oidc: OidcConfig = {
  enabled: true,
  issuer: "https://idp.example.com",
  client_id: "dbw",
  client_secret_set: true,
  scopes: "openid email profile",
  groups_claim: "groups",
  username_claim: "preferred_username",
  display_name: "Corp SSO",
  role_mappings: [{ group: "db-admins", role: "Database Admin" }],
  default_role: null,
  redirect_uri: "https://wrench.example.com/api/v1/auth/oidc/callback",
};
const sink: AuditSink = {
  enabled: false,
  protocol: "tcp",
  host: "",
  port: 514,
  format: "rfc5424",
  facility: 13,
  last_forwarded_id: 0,
}; // fmt: skip

describe("group mappings", () => {
  it("round-trips group=Role lines and keeps '=' inside group names", () => {
    expect(parseMappings("a=Viewer\n\nb=c=Editor ")).toEqual([
      { group: "a", role: "Viewer" },
      { group: "b=c", role: "Editor" },
    ]);
    expect(formatMappings([{ group: "g", role: "Viewer" }])).toBe("g=Viewer");
  });
});

describe("IntegrationsPage", () => {
  const routes = {
    "GET settings/oidc": () => oidc,
    "GET settings/audit-sink": () => sink,
    "GET roles": () => [
      { id: "1", name: "Super Admin" },
      { id: "2", name: "Viewer" },
    ],
  };

  it("never shows the client secret and does not resend it unless changed", async () => {
    const api = mockApi({ ...routes, "PUT settings/oidc": () => oidc });
    renderWithProviders(<IntegrationsPage />);
    const secret = await screen.findByLabelText("Client secret");
    expect(secret).toHaveValue("");
    expect(screen.getByText("Leave empty to keep the stored secret")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);
    await waitFor(() => expect(api.calls.some((c) => c.method === "PUT")).toBe(true));
    const body = api.calls.find((c) => c.path === "settings/oidc" && c.method === "PUT")
      ?.body as Record<string, unknown>;
    expect(body).not.toHaveProperty("client_secret");
    expect(body["role_mappings"]).toEqual([{ group: "db-admins", role: "Database Admin" }]);
    expect(
      screen.getByText("https://wrench.example.com/api/v1/auth/oidc/callback"),
    ).toBeInTheDocument();
  });

  it("never offers Super Admin as the fallback role", async () => {
    mockApi(routes);
    renderWithProviders(<IntegrationsPage />);
    const select = await screen.findByLabelText(/Role when no group matches/);
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toContain("Viewer");
    expect(options).not.toContain("Super Admin");
  });

  it("sends the forwarding settings and reports a failed test message", async () => {
    const api = mockApi({
      ...routes,
      "PUT settings/audit-sink": () => ({ ...sink, enabled: true, host: "siem.local" }),
      "POST settings/audit-sink/test": () => ({ ok: false, error: "ConnectionRefusedError" }),
    });
    renderWithProviders(<IntegrationsPage />);
    await userEvent.type(await screen.findByLabelText("Host"), "siem.local");
    await userEvent.click(screen.getByLabelText("Forward audit events"));
    await userEvent.click(screen.getAllByRole("button", { name: "Save" })[1]!);
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "PUT" && c.path === "settings/audit-sink")).toBe(
        true,
      ),
    );
    expect(
      api.calls.find((c) => c.path === "settings/audit-sink" && c.method === "PUT")?.body,
    ).toMatchObject({
      enabled: true,
      host: "siem.local",
      protocol: "tcp",
      port: 514,
      backfill: false,
    }); // fmt: skip
    await userEvent.click(screen.getByRole("button", { name: "Send test message" }));
    expect(await screen.findByText("Delivery failed: ConnectionRefusedError")).toBeInTheDocument();
  });
});
