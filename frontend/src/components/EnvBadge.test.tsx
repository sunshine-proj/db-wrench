import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EnvBadge, EnvBanner } from "./EnvBadge";

describe("environment indicators", () => {
  it("makes production visually unmistakable and states read-only mode", () => {
    render(<EnvBanner env="production" name="Orders DB" readOnly />);
    const banner = screen.getByTestId("env-banner");
    expect(banner).toHaveTextContent("production");
    expect(banner).toHaveTextContent("Orders DB");
    expect(banner).toHaveTextContent("read-only");
    expect(banner.className).toContain("bg-destructive");
  });
  it("does not claim read-only for writable connections", () => {
    render(<EnvBanner env="staging" name="Stage" readOnly={false} />);
    expect(screen.getByTestId("env-banner")).not.toHaveTextContent("read-only");
  });
  it("labels badges with the environment name (not by colour alone)", () => {
    render(<EnvBadge env="development" />);
    expect(screen.getByText("development")).toBeInTheDocument();
  });
});
