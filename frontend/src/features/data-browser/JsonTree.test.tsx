import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { JsonNode } from "./JsonTree";

const doc = {
  _id: { $oid: "507f1f77bcf86cd799439011" },
  name: "Ada",
  big: { $numberLong: "9007199254740994" },
  address: { city: "Paris", geo: { lat: 1.5 } },
  tags: ["a", "b"],
  missing: null,
};

describe("JsonNode", () => {
  it("shows BSON types in shell notation", () => {
    render(<JsonNode value={doc} defaultOpenDepth={1} />);
    expect(screen.getByText('ObjectId("507f1f77bcf86cd799439011")')).toBeInTheDocument();
    expect(screen.getByText('NumberLong("9007199254740994")')).toBeInTheDocument();
    expect(screen.getByText("null")).toBeInTheDocument();
  });

  it("expands and collapses nested documents and arrays", async () => {
    render(<JsonNode value={doc} defaultOpenDepth={1} />);
    expect(screen.queryByText("Paris")).not.toBeInTheDocument(); // depth 2 is collapsed
    await userEvent.click(screen.getByRole("button", { name: /address/ }));
    expect(screen.getByText('"Paris"')).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /address/ }));
    expect(screen.queryByText('"Paris"')).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /tags/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("never interprets strings as markup", () => {
    const { container } = render(<JsonNode value={{ x: "<b onclick=alert(1)>hi</b>" }} />);
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(/<b onclick=alert\(1\)>hi<\/b>/)).toBeInTheDocument();
  });
});
