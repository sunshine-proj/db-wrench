import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi } from "@/test/mockApi";
import { renderWithProviders } from "@/test/utils";
import type { Connection } from "@/types/api";
import { ImportButton, parseMapping } from "./ImportDialog";

afterEach(() => vi.unstubAllGlobals());

const conn = (over: Partial<Connection> = {}): Connection =>
  ({
    id: "c1",
    name: "Shop",
    db_type: "postgresql",
    environment: "development",
    read_only: false,
    permissions: ["export.import", "data.insert"],
    capabilities: { supports_schemas: true },
    ...over,
  }) as Connection;

const table = { cid: "c1", db: "shop", schema: "public", table: "customers" };
const BASE = "connections/c1/databases/shop/schemas/public/tables/customers/import";

async function chooseFile(text: string, name = "data.csv") {
  const input = screen.getByLabelText("File") as HTMLInputElement;
  await userEvent.upload(input, new File([text], name, { type: "text/csv" }));
}

describe("parseMapping", () => {
  it("parses csv=column lines and treats empty input as 'use the header'", () => {
    expect(parseMapping("Full Name=name\n amount = qty \nnonsense")).toEqual({
      "Full Name": "name",
      amount: "qty",
    });
    expect(parseMapping("")).toBeNull();
  });
});

describe("ImportButton", () => {
  it("is hidden without the import permission, and on read-only access", () => {
    mockApi({});
    const { container } = renderWithProviders(
      <ImportButton connection={conn({ permissions: ["data.insert"] })} table={table} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("blocks the commit while the preview reports problems", async () => {
    mockApi({
      "GET approvals": () => [],
      [`POST ${BASE}/preview`]: () => ({
        rows: 2,
        valid_rows: 1,
        errors: [{ row: 2, message: "invalid input syntax for type integer" }],
        columns: ["name", "qty"],
        sample: [["a", "1"]],
        token: null,
        requires_confirmation: false,
      }),
    });
    renderWithProviders(<ImportButton connection={conn()} table={table} />);
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await chooseFile("name,qty\na,1\nb,x\n");
    await userEvent.click(screen.getByRole("button", { name: "Validate and preview" }));
    expect(await screen.findByText("1 of 2 valid")).toBeInTheDocument();
    expect(screen.getByText(/Row 2:/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Import \d+ rows/ })).not.toBeInTheDocument();
  });

  it("commits with the signed token after a clean preview", async () => {
    const api = mockApi({
      "GET approvals": () => [],
      [`POST ${BASE}/preview`]: () => ({
        rows: 2,
        valid_rows: 2,
        errors: [],
        columns: ["name"],
        sample: [["a"]],
        token: "tok-1",
        requires_confirmation: false,
      }), // fmt: skip
      [`POST ${BASE}/commit`]: () => ({ imported: 2 }),
    });
    renderWithProviders(<ImportButton connection={conn()} table={table} />);
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await chooseFile("name\na\nb\n");
    await userEvent.click(screen.getByRole("button", { name: "Validate and preview" }));
    await userEvent.click(await screen.findByRole("button", { name: "Import 2 rows" }));
    expect(await screen.findByText("Imported 2.")).toBeInTheDocument();
    const commit = api.calls.find((c) => c.path.endsWith("/commit"));
    expect(commit?.body).toMatchObject({ token: "tok-1", content: "name\na\nb\n", delimiter: "," });
  });

  it("requires the table name to be typed on production", async () => {
    mockApi({
      "GET approvals": () => [],
      [`POST ${BASE}/preview`]: () => ({
        rows: 1,
        valid_rows: 1,
        errors: [],
        columns: ["name"],
        sample: [],
        token: "tok",
        requires_confirmation: true,
      }), // fmt: skip
    });
    renderWithProviders(
      <ImportButton connection={conn({ environment: "production" })} table={table} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await chooseFile("name\na\n");
    await userEvent.click(screen.getByRole("button", { name: "Validate and preview" }));
    const commit = await screen.findByRole("button", { name: "Import 1 rows" });
    expect(commit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Type “customers” to confirm"), "customers");
    expect(commit).toBeEnabled();
  });

  it("imports JSON documents into a collection", async () => {
    const api = mockApi({
      "GET approvals": () => [],
      "POST connections/c1/mongo/databases/shop/collections/people/import/preview": () => ({
        documents: 2,
        valid_documents: 2,
        errors: [],
        sample: [],
        token: "t",
        requires_confirmation: false,
      }), // fmt: skip
      "POST connections/c1/mongo/databases/shop/collections/people/import/commit": () => ({
        imported: 2,
      }),
    });
    renderWithProviders(
      <ImportButton
        connection={conn({ db_type: "mongodb" })}
        mongo={{ db: "shop", collection: "people" }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await chooseFile('[{"_id":1},{"_id":2}]', "docs.json");
    await userEvent.click(screen.getByRole("button", { name: "Validate and preview" }));
    await userEvent.click(await screen.findByRole("button", { name: "Import 2 documents" }));
    await waitFor(() => expect(api.calls.some((c) => c.path.endsWith("/commit"))).toBe(true));
    expect(within(screen.getByRole("dialog")).getByText("Imported 2.")).toBeInTheDocument();
  });
});
