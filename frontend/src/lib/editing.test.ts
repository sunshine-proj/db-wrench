import { describe, expect, it } from "vitest";
import {
  buildExpected,
  columnKind,
  isMasked,
  parseInput,
  rowToObject,
  toInputText,
  UNCHECKED,
} from "./editing";

describe("columnKind", () => {
  it.each([
    ["integer", "integer"],
    ["bigint", "integer"],
    ["int(11)", "integer"],
    ["bigint unsigned", "integer"],
    ["smallint", "integer"],
    ["numeric(10,2)", "decimal"],
    ["decimal(12,2)", "decimal"],
    ["double precision", "float"],
    ["real", "float"],
    ["boolean", "boolean"],
    ["tinyint(1)", "boolean"],
    ["jsonb", "json"],
    ["json", "json"],
    ["bytea", "binary"],
    ["longblob", "binary"],
    ["timestamp with time zone", "temporal"],
    ["datetime", "temporal"],
    ["character varying(100)", "text"],
    ["text", "text"],
    ["uuid", "text"],
    ["geometry", "other"],
  ])("%s -> %s", (type, kind) => expect(columnKind(type)).toBe(kind));
});

describe("parseInput", () => {
  it("keeps big integers and decimals exact", () => {
    expect(parseInput("integer", "42")).toBe(42);
    expect(parseInput("integer", "9007199254740993")).toBe("9007199254740993"); // not a lossy number
    expect(parseInput("integer", "+7")).toBe(7);
    expect(parseInput("decimal", "12345678901234567890.123456789")).toBe(
      "12345678901234567890.123456789",
    );
    expect(parseInput("decimal", "-.5")).toBe("-.5");
  });
  it("rejects malformed numbers with a readable message", () => {
    expect(() => parseInput("integer", "1.5")).toThrow("whole number");
    expect(() => parseInput("integer", "")).toThrow();
    expect(() => parseInput("decimal", "1e5")).toThrow("decimal");
    expect(() => parseInput("float", "abc")).toThrow("number");
    expect(parseInput("float", "1e3")).toBe(1000);
  });
  it("parses booleans, JSON and binary", () => {
    expect(parseInput("boolean", "true")).toBe(true);
    expect(parseInput("boolean", "false")).toBe(false);
    expect(() => parseInput("boolean", "yes")).toThrow();
    expect(parseInput("json", '{"a": [1, null]}')).toEqual({ a: [1, null] });
    expect(() => parseInput("json", "{oops")).toThrow("JSON");
    expect(parseInput("binary", "3q2+7w==")).toEqual({ $base64: "3q2+7w==" });
    expect(() => parseInput("binary", "not base64!")).toThrow("base64");
  });
  it("leaves text exactly as typed (including whitespace)", () => {
    expect(parseInput("text", "  hi  ")).toBe("  hi  ");
    expect(parseInput("text", "")).toBe("");
  });
});

describe("toInputText", () => {
  it("renders cells for editing", () => {
    expect(toInputText("text", null)).toBe("");
    expect(toInputText("integer", 5)).toBe("5");
    expect(toInputText("decimal", "10.25")).toBe("10.25");
    expect(toInputText("json", { a: 1 })).toBe('{\n  "a": 1\n}');
    expect(toInputText("boolean", true)).toBe("true");
  });
});

describe("buildExpected", () => {
  const original = {
    qty: 3,
    note: null,
    big: "9007199254740993",
    meta: { k: 1 },
    photo: { $binary: true, length: 4, preview_base64: "", truncated: false },
    text: { $truncated: true, preview: "x", length: 9000 },
    secret: { $masked: true },
  };
  it("sends scalars (including NULL) as they were seen", () => {
    expect(buildExpected(original, ["qty", "note", "big"])).toEqual({
      qty: 3,
      note: null,
      big: "9007199254740993",
    });
  });
  it("marks values the server cannot compare as explicitly unchecked", () => {
    expect(buildExpected(original, ["meta", "photo", "text", "secret"])).toEqual({
      meta: UNCHECKED,
      photo: UNCHECKED,
      text: UNCHECKED,
      secret: UNCHECKED,
    });
  });
  it("treats a missing column as NULL", () => {
    expect(buildExpected({}, ["a"])).toEqual({ a: null });
  });
});

describe("misc", () => {
  it("maps rows to objects and recognises masked cells", () => {
    expect(rowToObject([{ name: "a" }, { name: "b" }], [1, 2])).toEqual({ a: 1, b: 2 });
    expect(isMasked({ $masked: true })).toBe(true);
    expect(isMasked(null)).toBe(false);
    expect(isMasked("x")).toBe(false);
  });
});
