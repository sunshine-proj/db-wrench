import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/hooks/useAuth";
import { mockApi, problem, sessionFixture } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import { AppLayout, RequireAuth } from "./Layout";

afterEach(() => vi.unstubAllGlobals());

function renderGate() {
  return renderWithProviders(
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<p>LOGIN SCREEN</p>} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <p>PROTECTED CONTENT</p>
            </RequireAuth>
          }
        />
      </Routes>
    </AuthProvider>,
  );
}

describe("RequireAuth", () => {
  it("sends anonymous visitors to the login page", async () => {
    mockApi({ "GET auth/session": () => problem(401, "unauthorized") });
    renderGate();
    expect(await screen.findByText("LOGIN SCREEN")).toBeInTheDocument();
    expect(screen.queryByText("PROTECTED CONTENT")).not.toBeInTheDocument();
  });

  it("shows protected content to a fully authenticated user", async () => {
    mockApi({ "GET auth/session": () => sessionFixture() });
    renderGate();
    expect(await screen.findByText("PROTECTED CONTENT")).toBeInTheDocument();
  });

  it("keeps a half-authenticated session (MFA code pending) on the login page", async () => {
    mockApi({ "GET auth/session": () => sessionFixture({ mfa_state: "pending_verify" }) });
    renderGate();
    expect(await screen.findByText("LOGIN SCREEN")).toBeInTheDocument();
    expect(screen.queryByText("PROTECTED CONTENT")).not.toBeInTheDocument();
  });

  it("blocks everything except enrollment when the role requires MFA", async () => {
    mockApi({ "GET auth/session": () => sessionFixture({ mfa_state: "pending_enroll" }) });
    renderGate();
    expect(await screen.findByText("Set up two-factor authentication")).toBeInTheDocument();
    expect(screen.queryByText("PROTECTED CONTENT")).not.toBeInTheDocument();
  });

  it("forces a password change before anything else", async () => {
    mockApi({ "GET auth/session": () => sessionFixture({ password_change_required: true }) });
    renderGate();
    expect(await screen.findByText("Choose a new password")).toBeInTheDocument();
    expect(screen.queryByText("PROTECTED CONTENT")).not.toBeInTheDocument();
  });
});

describe("AppLayout navigation", () => {
  async function nav(perms: string[]) {
    mockApi({ "GET auth/session": () => sessionFixture({ app_permissions: perms }) });
    renderWithProviders(
      <AuthProvider>
        <Routes>
          <Route
            element={
              <RequireAuth>
                <AppLayout />
              </RequireAuth>
            }
          >
            <Route index element={<p>HOME</p>} />
          </Route>
        </Routes>
      </AuthProvider>,
    );
    await screen.findByText("HOME");
  }

  it("hides administration links from users without the permission", async () => {
    await nav([]);
    expect(screen.getByRole("link", { name: "Connections" })).toBeInTheDocument();
    for (const name of ["Users", "Roles", "Network & keys", "Audit log"]) {
      expect(screen.queryByRole("link", { name })).not.toBeInTheDocument();
    }
  });

  it("shows each administration link only with its own permission", async () => {
    await nav(["app.manage_users", "audit.view"]);
    expect(screen.getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Roles" })).not.toBeInTheDocument();
  });
});
