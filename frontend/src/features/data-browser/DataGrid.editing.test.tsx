import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DataGrid } from "./DataGrid";

const columns = [
  { name: "id", type: "integer" },
  { name: "name", type: "text" },
  { name: "meta", type: "jsonb" },
];

describe("DataGrid masking", () => {
  it("shows masked values as a lock, never the value, and keeps NULL visible", () => {
    render(<DataGrid columns={columns} rows={[[1, { $masked: true }, null]]} />);
    expect(screen.getByTestId("masked-cell")).toHaveTextContent("••••••");
    expect(screen.getByTestId("null-cell")).toBeInTheDocument();
  });
  it("masked cells are not editable inline", async () => {
    const onCellEdit = vi.fn();
    render(
      <DataGrid columns={columns} rows={[[1, { $masked: true }, null]]} onCellEdit={onCellEdit} />,
    );
    await userEvent.dblClick(screen.getByTestId("masked-cell"));
    expect(screen.queryByLabelText("Edit name")).not.toBeInTheDocument();
  });
});

describe("DataGrid inline editing", () => {
  it("double-click edits a scalar cell and Enter saves it", async () => {
    const onCellEdit = vi.fn().mockResolvedValue(undefined);
    render(
      <DataGrid
        columns={columns}
        rows={[[1, "old", null]]}
        primaryKey={["id"]}
        onCellEdit={onCellEdit}
      />,
    );
    await userEvent.dblClick(screen.getByText("old"));
    const input = screen.getByLabelText("Edit name");
    await userEvent.clear(input);
    await userEvent.type(input, "new{Enter}");
    await waitFor(() => expect(onCellEdit).toHaveBeenCalledWith(0, "name", "new"));
    await waitFor(() => expect(screen.queryByLabelText("Edit name")).not.toBeInTheDocument());
  });

  it("keeps the same focused input while typing (no remount per keystroke)", async () => {
    render(<DataGrid columns={columns} rows={[[1, "old", null]]} onCellEdit={vi.fn()} />);
    await userEvent.dblClick(screen.getByText("old"));
    const input = screen.getByLabelText("Edit name");
    await userEvent.type(input, "abc");
    // a remount would detach this node and move focus away
    expect(input.isConnected).toBe(true);
    expect(screen.getByLabelText("Edit name")).toBe(input);
    expect(document.activeElement).toBe(input);
    expect(input).toHaveValue("oldabc");
  });

  it("Escape cancels without saving", async () => {
    const onCellEdit = vi.fn();
    render(<DataGrid columns={columns} rows={[[1, "old", null]]} onCellEdit={onCellEdit} />);
    await userEvent.dblClick(screen.getByText("old"));
    await userEvent.type(screen.getByLabelText("Edit name"), "x{Escape}");
    expect(onCellEdit).not.toHaveBeenCalled();
    expect(screen.getByText("old")).toBeInTheDocument();
  });

  it("keeps the editor open and shows the error when the save is refused", async () => {
    const onCellEdit = vi.fn().mockRejectedValue(new Error("The row was changed by someone else."));
    render(<DataGrid columns={columns} rows={[[1, "old", null]]} onCellEdit={onCellEdit} />);
    await userEvent.dblClick(screen.getByText("old"));
    await userEvent.type(screen.getByLabelText("Edit name"), "z{Enter}");
    expect(await screen.findByText("The row was changed by someone else.")).toBeInTheDocument();
    expect(screen.getByLabelText("Edit name")).toBeInTheDocument();
  });

  it("primary keys and JSON cells are not edited inline", async () => {
    render(
      <DataGrid
        columns={columns}
        rows={[[5, "x", { a: 1 }]]}
        primaryKey={["id"]}
        onCellEdit={vi.fn()}
      />,
    );
    await userEvent.dblClick(screen.getByText("5"));
    expect(screen.queryByLabelText("Edit id")).not.toBeInTheDocument();
    await userEvent.dblClick(screen.getByText('{"a":1}'));
    expect(screen.queryByLabelText("Edit meta")).not.toBeInTheDocument();
  });

  it("no inline editing without an edit handler", async () => {
    render(<DataGrid columns={columns} rows={[[1, "old", null]]} />);
    await userEvent.dblClick(screen.getByText("old"));
    expect(screen.queryByLabelText("Edit name")).not.toBeInTheDocument();
  });
});

describe("DataGrid full-value loading", () => {
  const truncated = { $truncated: true, preview: "abc", length: 6000 };
  it("offers to load the full value of a truncated cell", async () => {
    const onFetchCell = vi.fn().mockResolvedValue("x".repeat(6000));
    render(<DataGrid columns={columns} rows={[[1, truncated, null]]} onFetchCell={onFetchCell} />);
    await userEvent.click(screen.getByRole("button", { name: "Show full value" }));
    expect(await screen.findByText(/Showing the first 3 of 6000 characters/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load full value" }));
    await waitFor(() => expect(onFetchCell).toHaveBeenCalledWith(0, "name"));
    expect(await screen.findByText("x".repeat(6000))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load full value" })).not.toBeInTheDocument();
  });

  it("explains why it failed (e.g. a sensitive column)", async () => {
    const onFetchCell = vi
      .fn()
      .mockRejectedValue(new Error("Missing permission: data.read_sensitive"));
    render(<DataGrid columns={columns} rows={[[1, truncated, null]]} onFetchCell={onFetchCell} />);
    await userEvent.click(screen.getByRole("button", { name: "Show full value" }));
    await userEvent.click(screen.getByRole("button", { name: "Load full value" }));
    expect(await screen.findByText("Missing permission: data.read_sensitive")).toBeInTheDocument();
  });

  it("no load button without a fetcher", async () => {
    render(<DataGrid columns={columns} rows={[[1, truncated, null]]} />);
    await userEvent.click(screen.getByRole("button", { name: "Show full value" }));
    expect(screen.queryByRole("button", { name: "Load full value" })).not.toBeInTheDocument();
  });
});

describe("DataGrid selection", () => {
  it("reports selected indices and can hide the selection column", async () => {
    const onSelectionIndices = vi.fn();
    const { rerender } = render(
      <DataGrid
        columns={columns}
        rows={[
          [1, "a", null],
          [2, "b", null],
        ]}
        onSelectionIndices={onSelectionIndices}
      />,
    );
    await userEvent.click(screen.getByLabelText("Select row 2"));
    expect(onSelectionIndices).toHaveBeenLastCalledWith([1]);
    rerender(<DataGrid columns={columns} rows={[[1, "a", null]]} selectable={false} />);
    expect(screen.queryByLabelText("Select all rows")).not.toBeInTheDocument();
  });
});
