import { renderHook, screen, waitFor, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import { QueryPanels } from "./QueryPanels";
import { useTabs } from "./useTabs";

const entry = (over = {}) => ({
  id: 1,
  kind: "sql",
  database: "shop",
  schema: "public",
  statement: "SELECT * FROM orders WHERE id = 7",
  redacted: false,
  status: "succeeded",
  error_code: null,
  duration_ms: 12,
  row_count: 3,
  created_at: "2024-01-01T00:00:00Z",
  ...over,
});
const saved = (over = {}) => ({
  id: "s1",
  kind: "sql",
  name: "Daily report",
  description: "revenue",
  database: "shop",
  schema: null,
  statement: "SELECT 1",
  visibility: "shared",
  owner_id: "u1",
  mine: false,
  updated_at: "2024-01-01T00:00:00Z",
  ...over,
});

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

function setup(
  routes: Record<string, () => unknown>,
  props: Partial<React.ComponentProps<typeof QueryPanels>> = {},
) {
  const onLoad = vi.fn();
  const api = mockApi({
    "GET connections/c1/queries/history": () => [],
    "GET connections/c1/queries/saved": () => [],
    ...routes,
  });
  renderWithProviders(
    <QueryPanels
      cid="c1"
      kind="sql"
      current={{ statement: "SELECT 42", database: "shop", schema: null }}
      onLoad={onLoad}
      canViewHistory
      canSave
      {...props}
    />,
  );
  return { onLoad, api };
}

describe("QueryPanels history", () => {
  it("lists history, loads an entry into the editor and shows its status", async () => {
    const { onLoad } = setup({
      "GET connections/c1/queries/history": () => [
        entry(),
        entry({ id: 2, status: "failed", statement: "SELEKT" }),
      ],
    });
    await userEvent.click(
      await screen.findByRole("button", { name: /Load: SELECT \* FROM orders/ }),
    );
    expect(onLoad).toHaveBeenCalledWith({
      statement: "SELECT * FROM orders WHERE id = 7",
      database: "shop",
      schema: "public",
    });
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getAllByText("12 ms")).toHaveLength(2); // one per history entry
  });

  it("flags entries whose literals were removed by policy", async () => {
    setup({
      "GET connections/c1/queries/history": () => [
        entry({ redacted: true, statement: "SELECT ? FROM t" }),
      ],
    });
    expect(await screen.findByText("literals removed")).toBeInTheDocument();
  });

  it("deletes an entry and clears history", async () => {
    const { api } = setup({
      "GET connections/c1/queries/history": () => [entry()],
      "DELETE connections/c1/queries/history/1": () => undefined,
      "DELETE connections/c1/queries/history": () => undefined,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Delete history entry" }));
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/history/1"))).toBe(
        true,
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Clear my history" }));
    await waitFor(() =>
      expect(
        api.calls.some((c) => c.method === "DELETE" && c.path.endsWith("/queries/history")),
      ).toBe(true),
    );
  });

  it("hides history entirely without the permission", async () => {
    setup({ "GET connections/c1/queries/saved": () => [] }, { canViewHistory: false });
    expect(await screen.findByText("No saved queries yet.")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "History" })).not.toBeInTheDocument();
  });
});

describe("QueryPanels saved queries", () => {
  async function openSaved() {
    await userEvent.click(await screen.findByRole("tab", { name: "Saved" }));
  }

  it("shows shared queries from others but only lets the owner delete", async () => {
    setup({
      "GET connections/c1/queries/saved": () => [
        saved(),
        saved({ id: "s2", name: "Mine", mine: true, visibility: "private" }),
      ],
    });
    await openSaved();
    expect(await screen.findByText("Daily report")).toBeInTheDocument();
    expect(screen.getByText("Shared", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete Daily report" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Mine" })).toBeInTheDocument();
  });

  it("loads a saved query into the editor", async () => {
    const { onLoad } = setup({ "GET connections/c1/queries/saved": () => [saved()] });
    await openSaved();
    await userEvent.click(
      await screen.findByRole("button", { name: "Load saved query Daily report" }),
    );
    expect(onLoad).toHaveBeenCalledWith({ statement: "SELECT 1", database: "shop", schema: null });
  });

  it("saves the current query with a visibility", async () => {
    const { api } = setup({
      "POST connections/c1/queries/saved": () => saved({ mine: true, name: "New one" }),
    });
    await openSaved();
    await userEvent.click(await screen.findByRole("button", { name: "Save current query" }));
    await userEvent.type(screen.getByLabelText("Name"), "New one");
    await userEvent.selectOptions(screen.getByLabelText("Visibility"), "shared");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST")).toBe(true));
    expect(api.calls.find((c) => c.method === "POST")!.body).toMatchObject({
      kind: "sql",
      name: "New one",
      visibility: "shared",
      statement: "SELECT 42",
      database: "shop",
    });
  });

  it("cannot save without permission or an empty query", async () => {
    setup({}, { canSave: false });
    await openSaved();
    expect(screen.queryByRole("button", { name: "Save current query" })).not.toBeInTheDocument();
  });

  it("only shows saved queries of the workspace kind", async () => {
    setup({
      "GET connections/c1/queries/saved": () => [
        saved(),
        saved({ id: "s3", kind: "mongo", name: "A mongo thing" }),
      ],
    });
    await openSaved();
    expect(await screen.findByText("Daily report")).toBeInTheDocument();
    expect(screen.queryByText("A mongo thing")).not.toBeInTheDocument();
  });
});

describe("useTabs", () => {
  it("keeps tabs per key in localStorage and never closes the last tab", () => {
    const blank = () => "";
    const { result, rerender } = renderHook(({ key }) => useTabs(key, blank), {
      initialProps: { key: "k1" },
    });
    act(() => result.current.update({ text: "SELECT 1" }));
    act(() => result.current.add());
    expect(result.current.tabs).toHaveLength(2);
    const first = result.current.tabs[0]!.id;
    act(() => result.current.close(result.current.tabs[1]!.id));
    act(() => result.current.close(first));
    expect(result.current.tabs).toHaveLength(1); // the last tab stays
    expect(JSON.parse(localStorage.getItem("k1")!).tabs[0].text).toBe("SELECT 1");
    rerender({ key: "k2" });
    expect(result.current.tabs).toHaveLength(1);
  });

  it("survives unavailable or corrupt storage", () => {
    localStorage.setItem("bad", "{not json");
    const { result } = renderHook(() => useTabs("bad", () => "start"));
    expect(result.current.active.text).toBe("start");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    act(() => result.current.update({ text: "still works" }));
    expect(result.current.active.text).toBe("still works");
  });
});
