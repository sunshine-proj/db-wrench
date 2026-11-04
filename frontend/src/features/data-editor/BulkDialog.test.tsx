import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/services/api";
import { renderWithProviders as render } from "@/test/utils";
import { BulkDialog, type PreviewData } from "./BulkDialog";

const preview = (over: Partial<PreviewData> = {}): PreviewData => ({
  count: 3,
  token: "tok",
  requires_confirmation: false,
  columns: ["id", "name"],
  sample: [
    [1, "a"],
    [2, null],
  ],
  ...over,
});

function setup(p: PreviewData, executeImpl = vi.fn().mockResolvedValue({ affected: 3 })) {
  const onDone = vi.fn();
  render(
    <BulkDialog
      title="Delete multiple rows"
      summary="Review"
      objectName="orders"
      confirmLabel="Delete rows"
      preview={() => Promise.resolve(p)}
      execute={executeImpl}
      onClose={() => undefined}
      onDone={onDone}
    />,
  );
  return { execute: executeImpl, onDone };
}

describe("BulkDialog", () => {
  it("previews the affected rows before anything can be confirmed", async () => {
    const { execute } = setup(preview());
    expect(await screen.findByText("3 rows will be affected.")).toBeInTheDocument();
    expect(screen.getByText("Sample of 2 affected rows")).toBeInTheDocument();
    expect(screen.getByText("NULL")).toBeInTheDocument();
    expect(execute).not.toHaveBeenCalled();
  });

  it("executes with the previewed token and count", async () => {
    const { execute, onDone } = setup(preview());
    await userEvent.click(await screen.findByRole("button", { name: "Delete rows" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(3));
    expect(execute).toHaveBeenCalledWith({ token: "tok", count: 3, confirm: undefined });
  });

  it("nothing to do when no rows match", async () => {
    setup(preview({ count: 0, sample: [] }));
    expect(await screen.findByText("No rows match; nothing to do.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete rows" })).toBeDisabled();
  });

  it("production requires typing the object name exactly", async () => {
    const { execute } = setup(preview({ requires_confirmation: true }));
    const button = await screen.findByRole("button", { name: "Delete rows" });
    expect(button).toBeDisabled();
    const input = screen.getByLabelText(/Type “orders” to confirm/);
    await userEvent.type(input, "order");
    expect(button).toBeDisabled();
    await userEvent.type(input, "s");
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() =>
      expect(execute).toHaveBeenCalledWith({ token: "tok", count: 3, confirm: "orders" }),
    );
  });

  it("shows the server's message when the selection changed since the preview", async () => {
    const failing = vi.fn().mockRejectedValue(
      new ApiError({
        type: "x",
        title: "Conflict",
        status: 409,
        code: "conflict",
        detail:
          "The selection changed since the preview (4 rows now, 3 previewed). Nothing was modified; preview again.",
      }),
    );
    const { onDone } = setup(preview(), failing);
    await userEvent.click(await screen.findByRole("button", { name: "Delete rows" }));
    expect(await screen.findByText(/selection changed since the preview/)).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("reports preview failures", async () => {
    render(
      <BulkDialog
        title="T"
        summary="S"
        objectName="o"
        confirmLabel="Go"
        preview={() =>
          Promise.reject(
            new ApiError({
              type: "x",
              title: "t",
              status: 422,
              code: "validation_failed",
              detail: "This would affect more than 10000 rows.",
            }),
          )
        }
        execute={vi.fn()}
        onClose={() => undefined}
        onDone={() => undefined}
      />,
    );
    expect(await screen.findByText(/more than 10000 rows/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();
  });
});
