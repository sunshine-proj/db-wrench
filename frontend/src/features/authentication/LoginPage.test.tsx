import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "@/hooks/useAuth";
import { mockApi, problem, sessionFixture, userFixture } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import { LoginPage } from "./LoginPage";

afterEach(() => vi.unstubAllGlobals());

function renderLogin() {
  return renderWithProviders(
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup" element={<p>SETUP SCREEN</p>} />
        <Route path="/" element={<p>HOME</p>} />
      </Routes>
    </AuthProvider>,
    { route: "/login" },
  );
}

describe("LoginPage", () => {
  it("redirects to setup on a fresh installation", async () => {
    mockApi({
      "GET auth/setup-status": () => ({ setup_required: true }),
      "GET auth/session": () => problem(401, "unauthorized"),
    });
    renderLogin();
    expect(await screen.findByText("SETUP SCREEN")).toBeInTheDocument();
  });

  it("shows a generic error for bad credentials and clears nothing sensitive into the URL", async () => {
    mockApi({
      "GET auth/setup-status": () => ({ setup_required: false }),
      "GET auth/session": () => problem(401, "unauthorized"),
      "POST auth/login": () =>
        problem(401, "unauthorized", "Invalid credentials, or the account is temporarily locked."),
    });
    renderLogin();
    await userEvent.type(await screen.findByLabelText("Username"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/Invalid credentials/)).toBeInTheDocument();
  });

  it("walks through the second factor step", async () => {
    let state: "anon" | "pending" | "ok" = "anon";
    const api = mockApi({
      "GET auth/setup-status": () => ({ setup_required: false }),
      "GET auth/session": () =>
        state === "anon"
          ? problem(401, "unauthorized")
          : sessionFixture({
              mfa_state: state === "pending" ? "pending_verify" : "ok",
              csrf_token: "c1",
            }),
      "POST auth/login": () => {
        state = "pending";
        return { mfa_state: "pending_verify", csrf_token: "c1", user: userFixture };
      },
      "POST auth/mfa/verify": () => {
        state = "ok";
        return { mfa_state: "ok", csrf_token: "c2", user: userFixture };
      },
    });
    renderLogin();
    await userEvent.type(await screen.findByLabelText("Username"), "alice");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Two-factor authentication")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByText("HOME")).toBeInTheDocument();
    const verify = api.calls.find((c) => c.path === "auth/mfa/verify");
    expect(verify?.headers["X-CSRF-Token"]).toBe("c1"); // second-factor request is CSRF-protected
    await waitFor(() => expect(api.calls.filter((c) => c.path === "auth/login")).toHaveLength(1));
  });
});
