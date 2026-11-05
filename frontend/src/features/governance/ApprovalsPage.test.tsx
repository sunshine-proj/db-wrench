import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Approval } from "@/types/api";
import { ApprovalsPage } from "./ApprovalsPage";

afterEach(() => vi.unstubAllGlobals());

const pending: Approval = {
  id: "a1111111-0000-0000-0000-000000000000",
  connection_id: "c1",
  connection_name: "Orders DB",
  environment: "production",
  requester: "bob",
  operation: "admin.drop_database",
  summary: { database: "legacy" },
  status: "pending",
  reason: null,
  decided_by: null,
  created_at: "2024-01-01T00:00:00Z",
  decided_at: null,
  expires_at: "2024-01-02T00:00:00Z",
};

describe("ApprovalsPage", () => {
  it("shows what would run and approves with a reason", async () => {
    const api = mockApi({
      "GET approvals": () => [pending],
      [`POST approvals/${pending.id}/approve`]: () => ({ ...pending, status: "approved" }),
    });
    renderWithProviders(<ApprovalsPage />);
    expect(await screen.findByText("admin.drop_database")).toBeInTheDocument();
    expect(screen.getByText("legacy")).toBeInTheDocument(); // the exact target is visible
    expect(screen.getByText("Orders DB")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Reason (optional)"), "ticket 42");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "POST" && c.path.endsWith("/approve"))).toBe(true),
    );
    const call = api.calls.find((c) => c.path.endsWith("/approve"));
    expect(call?.body).toEqual({ reason: "ticket 42" });
    expect(api.calls[0]?.path).toBe("approvals");
  });

  it("offers cancel (not approve) on your own requests", async () => {
    mockApi({
      "GET approvals": () => [pending],
      [`POST approvals/${pending.id}/cancel`]: () => ({ ...pending, status: "cancelled" }),
    });
    renderWithProviders(<ApprovalsPage />);
    await userEvent.click(await screen.findByRole("tab", { name: "My requests" }));
    expect(await screen.findByRole("button", { name: "Cancel request" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("says so when there is nothing to review", async () => {
    mockApi({ "GET approvals": () => [] });
    renderWithProviders(<ApprovalsPage />);
    expect(await screen.findByText("Nothing here.")).toBeInTheDocument();
  });
});
