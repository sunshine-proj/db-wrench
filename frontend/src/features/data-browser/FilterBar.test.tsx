import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { buildFilters, FilterBar, newDraft, type DraftFilter } from "./FilterBar";
import { nextSort } from "./RowBrowser";

describe("buildFilters", () => {
  const d = (over: Partial<DraftFilter>): DraftFilter => ({ ...newDraft("name"), ...over });

  it("drops incomplete rows and keeps values as strings (no precision loss)", () => {
    const out = buildFilters([
      d({ column: "", op: "eq", value: "x" }),
      d({ op: "eq", value: "" }),
      d({ column: "big", op: "eq", value: "9007199254740993" }),
    ]);
    expect(out).toEqual([{ column: "big", op: "eq", value: "9007199254740993" }]);
  });
  it("handles NULL operators without a value", () => {
    expect(
      buildFilters([d({ op: "is_null" }), d({ op: "is_not_null", value: "ignored" })]),
    ).toEqual([
      { column: "name", op: "is_null" },
      { column: "name", op: "is_not_null" },
    ]);
  });
  it("splits IN lists and ignores empty items", () => {
    expect(buildFilters([d({ op: "in", value: " 1, 2 ,, 3 " })])).toEqual([
      { column: "name", op: "in", value: ["1", "2", "3"] },
    ]);
    expect(buildFilters([d({ op: "in", value: " , " })])).toEqual([]);
  });
});

describe("nextSort", () => {
  it("cycles ascending, descending, none, and replaces other columns", () => {
    let s = nextSort([], "a");
    expect(s).toEqual([{ column: "a", direction: "asc" }]);
    s = nextSort(s, "a");
    expect(s).toEqual([{ column: "a", direction: "desc" }]);
    expect(nextSort(s, "a")).toEqual([]);
    expect(nextSort(s, "b")).toEqual([{ column: "b", direction: "asc" }]);
  });
});

function Harness({ onApply }: { onApply: (f: ReturnType<typeof buildFilters>) => void }) {
  const [drafts, setDrafts] = useState<DraftFilter[]>([]);
  return (
    <FilterBar
      columns={["id", "name"]}
      drafts={drafts}
      onChange={setDrafts}
      onApply={() => onApply(buildFilters(drafts))}
    />
  );
}

describe("FilterBar", () => {
  it("lets the user compose and apply a filter", async () => {
    const onApply = vi.fn();
    render(<Harness onApply={onApply} />);
    await userEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await userEvent.selectOptions(screen.getByLabelText("Column"), "name");
    await userEvent.selectOptions(screen.getByLabelText("Operator"), "contains");
    await userEvent.type(screen.getByLabelText("Value"), "Cust");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(onApply).toHaveBeenCalledWith([{ column: "name", op: "contains", value: "Cust" }]);
  });

  it("hides the value box for NULL operators", async () => {
    render(<Harness onApply={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "Add filter" }));
    expect(screen.getByLabelText("Value")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Operator"), "is_null");
    expect(screen.queryByLabelText("Value")).not.toBeInTheDocument();
  });

  it("removes a filter row", async () => {
    render(<Harness onApply={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "Add filter" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove filter" }));
    expect(screen.queryByLabelText("Column")).not.toBeInTheDocument();
  });
});
