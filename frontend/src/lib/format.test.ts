import { describe, expect, it } from "vitest";
import { asMarker, bsonTypeLabel, cellText, formatBytes, formatExtendedJson } from "./format";

describe("cell markers", () => {
  it("recognises truncated text and binary markers", () => {
    expect(asMarker({ $truncated: true, preview: "abc", length: 9000 })).toEqual({
      kind: "truncated",
      preview: "abc",
      length: 9000,
    });
    expect(
      asMarker({ $binary: true, length: 4, preview_base64: "3q2+7w==", truncated: false })?.kind,
    ).toBe("binary");
    expect(asMarker({ a: 1 })).toBeNull();
    expect(asMarker(null)).toBeNull();
    expect(asMarker([1])).toBeNull();
  });
  it("produces readable text for each kind of value", () => {
    expect(cellText({ $truncated: true, preview: "abc", length: 9 })).toBe("abc…");
    expect(cellText({ $binary: true, length: 2048, preview_base64: "", truncated: true })).toBe(
      "‹binary 2.0 KB›",
    );
    expect(cellText(42)).toBe("42");
    expect(cellText(false)).toBe("false");
    expect(cellText("9007199254740994")).toBe("9007199254740994"); // big ints arrive as strings
    expect(cellText({ a: [1] })).toBe('{"a":[1]}');
  });
  it("formats byte sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("Extended JSON", () => {
  it("renders BSON scalars in shell notation", () => {
    expect(formatExtendedJson({ $oid: "507f1f77bcf86cd799439011" })).toBe(
      'ObjectId("507f1f77bcf86cd799439011")',
    );
    expect(formatExtendedJson({ $numberLong: "9007199254740994" })).toBe(
      'NumberLong("9007199254740994")',
    );
    expect(formatExtendedJson({ $numberDecimal: "1.10" })).toBe('Decimal128("1.10")');
    expect(formatExtendedJson({ $numberInt: "21" })).toBe("21");
    expect(formatExtendedJson({ $date: { $numberLong: "1704067200000" } })).toBe(
      'ISODate("2024-01-01T00:00:00.000Z")',
    );
    expect(formatExtendedJson({ $binary: { base64: "3q2+7w==", subType: "00" } })).toBe(
      'BinData(0, "3q2+7w==")',
    );
    expect(formatExtendedJson({ $regularExpression: { pattern: "^a", options: "i" } })).toBe(
      "/^a/i",
    );
  });
  it("leaves ordinary objects and values alone", () => {
    expect(formatExtendedJson({ a: 1 })).toBeNull();
    expect(formatExtendedJson({ $oid: "x", extra: 1 })).toBeNull();
    expect(formatExtendedJson("str")).toBeNull();
    expect(formatExtendedJson([1])).toBeNull();
  });
  it("labels types", () => {
    expect(bsonTypeLabel({ $oid: "x" })).toBe("oid");
    expect(bsonTypeLabel(null)).toBe("null");
    expect(bsonTypeLabel([])).toBe("array");
    expect(bsonTypeLabel({ a: 1 })).toBe("object");
  });
});
