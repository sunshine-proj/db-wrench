import { describe, expect, it } from "vitest";
import { t } from "./i18n";
import { messages } from "./messages.en";

describe("i18n", () => {
  it("interpolates named placeholders", () => {
    expect(t("grid.selectRow", { n: 3 })).toBe("Select row 3");
    expect(t("browser.range", { from: 1, to: 100 })).toBe("Rows 1–100");
  });
  it("leaves unknown placeholders visible instead of throwing", () => {
    expect(t("grid.selectRow")).toBe("Select row {n}");
    expect(t("grid.selectRow", { other: 1 })).toBe("Select row {n}");
  });
  it("has a message for every dynamic key family", () => {
    for (const k of ["production", "staging", "development", "other"])
      expect(messages).toHaveProperty(`env.${k}`);
    for (const k of ["disable", "require", "verify-ca", "verify-full"])
      expect(messages).toHaveProperty(`tls.${k}`);
    for (const k of ["keyset", "offset", "unordered_offset"])
      expect(messages).toHaveProperty(`browser.pagination.${k}`);
  });
  it("never ships empty messages", () => {
    for (const [key, value] of Object.entries(messages)) expect(value.trim(), key).not.toBe("");
  });
});
