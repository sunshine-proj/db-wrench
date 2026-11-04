import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/services/api";
import type { ColumnInfo } from "@/types/api";
import { RowForm } from "./RowForm";

const col = (name: string, data_type: string, over: Partial<ColumnInfo> = {}): ColumnInfo => ({
  name,
  data_type,
  nullable: true,
  default: null,
  is_identity: false,
  is_generated: false,
  comment: null,
  ordinal: 1,
  ...over,
});

const columns = [
  col("id", "integer", { nullable: false, is_identity: true }),
  col("name", "text", { nullable: false }),
  col("qty", "integer", { default: "0" }),
  col("price", "numeric(10,2)"),
  col("active", "boolean"),
  col("meta", "jsonb"),
  col("total", "integer", { is_generated: true }),
];

describe("RowForm (insert)", () => {
  it("sends only what the user filled in; defaults and NULLable columns are left out", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.type(screen.getByLabelText("name"), "Widget");
    await userEvent.click(screen.getByRole("button", { name: "Insert row" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit).toHaveBeenCalledWith({ values: { name: "Widget" }, expected: undefined });
  });

  it("parses typed values and keeps big integers exact", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.type(screen.getByLabelText("name"), "X");
    const qty = screen.getByLabelText("qty");
    await userEvent.click(within(qty.closest("div.rounded-md")!).getByLabelText("Use default"));
    await userEvent.type(qty, "9007199254740993");
    const meta = screen.getByLabelText("meta");
    await userEvent.click(within(meta.closest("div.rounded-md")!).getByLabelText("Use default"));
    await userEvent.click(meta);
    await userEvent.paste('{"a": [1, 2]}');
    await userEvent.click(screen.getByRole("button", { name: "Insert row" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0]![0].values).toEqual({
      name: "X",
      qty: "9007199254740993",
      meta: { a: [1, 2] },
    });
  });

  it("explicit NULL is sent as null", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.type(screen.getByLabelText("name"), "N");
    const price = screen.getByLabelText("price").closest("div.rounded-md") as HTMLElement;
    await userEvent.click(within(price).getByLabelText("Use default"));
    await userEvent.click(within(price).getByLabelText("NULL"));
    await userEvent.click(screen.getByRole("button", { name: "Insert row" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0]![0].values).toEqual({ name: "N", price: null });
  });

  it("shows field errors and does not submit invalid input", async () => {
    const onSubmit = vi.fn();
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.type(screen.getByLabelText("name"), "N");
    const qty = screen.getByLabelText("qty").closest("div.rounded-md") as HTMLElement;
    await userEvent.click(within(qty).getByLabelText("Use default"));
    await userEvent.type(screen.getByLabelText("qty"), "12.5");
    await userEvent.click(screen.getByRole("button", { name: "Insert row" }));
    expect(await screen.findByText("Enter a whole number.")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("generated columns are read-only", () => {
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={() => undefined}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("total")).toBeDisabled();
    expect(screen.getByText("Generated column; the database computes it.")).toBeInTheDocument();
  });

  it("surfaces server errors and stays open", async () => {
    const onClose = vi.fn();
    const onSubmit = vi.fn().mockRejectedValue(
      new ApiError({
        type: "x",
        title: "t",
        status: 400,
        code: "query_failed",
        detail: "duplicate key value",
      }),
    );
    render(
      <RowForm
        mode="insert"
        columns={columns}
        primaryKey={["id"]}
        onClose={onClose}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.type(screen.getByLabelText("name"), "N");
    await userEvent.click(screen.getByRole("button", { name: "Insert row" }));
    expect(await screen.findByText("duplicate key value")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

const rowColumns = columns.map((c) => ({ name: c.name }));
const row = [7, "Old", 3, "10.25", true, { k: 1 }, 9];

describe("RowForm (update)", () => {
  it("sends only changed columns together with their previous values", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="update"
        columns={columns}
        primaryKey={["id"]}
        row={row}
        rowColumns={rowColumns}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    const name = screen.getByLabelText("name");
    await userEvent.clear(name);
    await userEvent.type(name, "New");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit).toHaveBeenCalledWith({ values: { name: "New" }, expected: { name: "Old" } });
  });

  it("marks values it cannot compare exactly as unchecked", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="update"
        columns={columns}
        primaryKey={["id"]}
        row={row}
        rowColumns={rowColumns}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    const meta = screen.getByLabelText("meta");
    await userEvent.clear(meta);
    await userEvent.click(meta);
    await userEvent.paste('{"k": 2}');
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0]![0]).toEqual({
      values: { meta: { k: 2 } },
      expected: { meta: { $unchecked: true } },
    });
  });

  it("closes without calling the server when nothing changed", async () => {
    const onClose = vi.fn();
    const onSubmit = vi.fn();
    render(
      <RowForm
        mode="update"
        columns={columns}
        primaryKey={["id"]}
        row={row}
        rowColumns={rowColumns}
        onClose={onClose}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClose).toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("primary keys and masked values cannot be edited", () => {
    const masked = [7, { $masked: true }, 3, "10.25", true, null, 9];
    render(
      <RowForm
        mode="update"
        columns={columns}
        primaryKey={["id"]}
        row={masked}
        rowColumns={rowColumns}
        onClose={() => undefined}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("id")).toBeDisabled();
    expect(screen.getByLabelText("name")).toBeDisabled();
    expect(
      screen.getByText("Sensitive value you are not allowed to see or change."),
    ).toBeInTheDocument();
  });

  it("setting a nullable column to NULL sends null with the old value", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <RowForm
        mode="update"
        columns={columns}
        primaryKey={["id"]}
        row={row}
        rowColumns={rowColumns}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );
    const price = screen.getByLabelText("price").closest("div.rounded-md") as HTMLElement;
    await userEvent.click(within(price).getByLabelText("NULL"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0]![0]).toEqual({
      values: { price: null },
      expected: { price: "10.25" },
    });
  });
});
