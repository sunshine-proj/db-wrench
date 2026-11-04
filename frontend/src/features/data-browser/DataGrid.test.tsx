import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DataGrid } from "./DataGrid";

const columns = [
  { name: "id", type: "integer" },
  { name: "name", type: "text" },
  { name: "email", type: "text" },
];

describe("DataGrid", () => {
  it("shows SQL NULL distinctly from the text 'NULL' and the empty string", () => {
    render(
      <DataGrid
        columns={columns}
        rows={[
          [1, "NULL", null],
          [2, "", "x@y.z"],
        ]}
      />,
    );
    const nulls = screen.getAllByTestId("null-cell");
    expect(nulls).toHaveLength(1);
    expect(screen.getByText("NULL", { selector: "span.block" })).toBeInTheDocument(); // literal text
  });

  it("renders database content as text, never as HTML", () => {
    const evil =
      '<img src=x onerror="window.__pwned = true"><script>window.__pwned = true</script>';
    const { container } = render(<DataGrid columns={columns} rows={[[1, evil, "ok"]]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText(evil, { exact: false })).toBeInTheDocument();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it("marks primary-key columns and exposes sorting state to assistive tech", async () => {
    const onSort = vi.fn();
    render(
      <DataGrid
        columns={columns}
        rows={[[1, "a", "b"]]}
        primaryKey={["id"]}
        sort={[{ column: "name", direction: "desc" }]}
        onSort={onSort}
      />,
    );
    expect(screen.getByLabelText("Primary key")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader");
    expect(headers.find((h) => h.textContent?.includes("name"))).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    await userEvent.click(
      within(headers.find((h) => h.textContent?.includes("email"))!).getByRole("button"),
    );
    expect(onSort).toHaveBeenCalledWith("email");
  });

  it("opens truncated and binary values in a dialog with an explanation", async () => {
    render(
      <DataGrid
        columns={[
          { name: "notes", type: "text" },
          { name: "photo", type: "bytea" },
        ]}
        rows={[
          [
            { $truncated: true, preview: "x".repeat(10), length: 6000 },
            { $binary: true, length: 4, preview_base64: "3q2+7w==", truncated: false },
          ],
        ]}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "Show full value" });
    await userEvent.click(buttons[0]!);
    expect(await screen.findByText(/Showing the first 10 of 6000 characters/)).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getAllByRole("button", { name: "Show full value" })[1]!);
    expect(await screen.findByText(/Binary value, 4 B/)).toBeInTheDocument();
    expect(screen.getByText("3q2+7w==")).toBeInTheDocument();
  });

  it("keeps large integers and decimals exactly as the server sent them", () => {
    render(
      <DataGrid
        columns={[
          { name: "big", type: "bigint" },
          { name: "bal", type: "numeric" },
        ]}
        rows={[["9007199254740994", "10.25"]]}
      />,
    );
    expect(screen.getByText("9007199254740994")).toBeInTheDocument();
    expect(screen.getByText("10.25")).toBeInTheDocument();
  });

  it("supports row selection and reports the selected rows", async () => {
    const onSelectionChange = vi.fn();
    render(
      <DataGrid
        columns={columns}
        rows={[
          [1, "a", "b"],
          [2, "c", "d"],
        ]}
        onSelectionChange={onSelectionChange}
      />,
    );
    await userEvent.click(screen.getByLabelText("Select row 2"));
    expect(onSelectionChange).toHaveBeenLastCalledWith([[2, "c", "d"]]);
    await userEvent.click(screen.getByLabelText("Select all rows"));
    expect(onSelectionChange).toHaveBeenLastCalledWith([
      [1, "a", "b"],
      [2, "c", "d"],
    ]);
  });

  it("says so when there are no rows", () => {
    render(<DataGrid columns={columns} rows={[]} />);
    expect(screen.getByText("No rows")).toBeInTheDocument();
  });

  it("renders large pages without mounting every row", () => {
    const rows = Array.from({ length: 1000 }, (_, i) => [i, `n${i}`, null]);
    render(<DataGrid columns={columns} rows={rows} />);
    expect(screen.getAllByRole("row").length).toBeLessThan(1000);
  });
});
