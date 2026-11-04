import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/services/api";
import { ConfirmDialog } from "./ConfirmDialog";

function setup(props: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  const onOpenChange = vi.fn();
  render(
    <ConfirmDialog
      open
      onOpenChange={onOpenChange}
      title="Delete connection"
      description="Sure?"
      confirmLabel="Delete"
      onConfirm={onConfirm}
      {...props}
    />,
  );
  return { onConfirm, onOpenChange };
}

describe("ConfirmDialog", () => {
  it("confirms immediately when no typed text is required", async () => {
    const { onConfirm, onOpenChange } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("requires the exact object name for production objects", async () => {
    const { onConfirm } = setup({ requireText: "prod-db" });
    const confirm = screen.getByRole("button", { name: "Delete" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type “prod-db” to confirm/), "prod-d");
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type “prod-db” to confirm/), "b");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(onConfirm).toHaveBeenCalled());
  });

  it("does not match case-insensitively", async () => {
    setup({ requireText: "Prod-DB" });
    await userEvent.type(screen.getByLabelText(/to confirm/), "prod-db");
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("keeps the dialog open and shows the server's error on failure", async () => {
    const onConfirm = vi.fn().mockRejectedValue(
      new ApiError({
        type: "x",
        title: "Forbidden",
        status: 403,
        code: "forbidden",
        detail: "Missing permission: connection.delete",
        request_id: "req1",
      }),
    );
    const { onOpenChange } = setup({ onConfirm });
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Missing permission: connection.delete")).toBeInTheDocument();
    expect(screen.getByText(/req1/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
