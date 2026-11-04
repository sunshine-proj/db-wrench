/**
 * Helpers for the data editor: turning form text into API values, and building the
 * "previous values" that make updates conflict-safe (optimistic concurrency).
 */
import { asMarker } from "@/lib/format";

export type ColumnKind =
  "text" | "integer" | "decimal" | "float" | "boolean" | "json" | "binary" | "temporal" | "other";

export function columnKind(dataType: string): ColumnKind {
  const t = dataType.toLowerCase();
  if (/^(tinyint\(1\)|bool)/.test(t)) return "boolean";
  if (/^(small|medium|big|tiny)?(int|serial)|^integer|^int\b/.test(t)) return "integer";
  if (/^(numeric|decimal|money)/.test(t)) return "decimal";
  if (/^(float|double|real)/.test(t)) return "float";
  if (/^jsonb?$|^json\b/.test(t)) return "json";
  if (/^(bytea|blob|longblob|mediumblob|tinyblob|binary|varbinary)/.test(t)) return "binary";
  if (/^(date|time|timestamp|datetime|interval|year)/.test(t)) return "temporal";
  if (/(char|text|citext|uuid|enum|name)/.test(t)) return "text";
  return "other";
}

const INTEGER = /^[+-]?\d+$/;
const DECIMAL = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/** Parse form text. Throws an `Error` with a user-readable message when it is invalid. */
export function parseInput(kind: ColumnKind, text: string): unknown {
  const v = text.trim();
  switch (kind) {
    case "integer": {
      if (!INTEGER.test(v)) throw new Error("Enter a whole number.");
      const n = Number(v);
      // Keep exact digits for values beyond the JavaScript safe range.
      return Number.isSafeInteger(n) ? n : v.replace(/^\+/, "");
    }
    case "decimal":
      if (!DECIMAL.test(v)) throw new Error("Enter a decimal number.");
      return v.replace(/^\+/, ""); // exact digits are preserved as text
    case "float": {
      const n = Number(v);
      if (v === "" || Number.isNaN(n)) throw new Error("Enter a number.");
      return n;
    }
    case "boolean":
      if (v === "true") return true;
      if (v === "false") return false;
      throw new Error("Choose true or false.");
    case "json":
      try {
        return JSON.parse(text) as unknown;
      } catch {
        throw new Error("Enter valid JSON.");
      }
    case "binary":
      if (!/^[A-Za-z0-9+/]*={0,2}$/.test(v)) throw new Error("Enter base64.");
      return { $base64: v };
    default:
      return text;
  }
}

/** Text shown in an input for an existing cell value. */
export function toInputText(kind: ColumnKind, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (kind === "json") return JSON.stringify(value, null, 2);
  if (typeof value === "string") return value;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Values the server cannot compare exactly (JSON, binary, truncated, masked, arrays). */
export function isUncomparable(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  return true;
}

export const UNCHECKED = { $unchecked: true } as const;

/**
 * The previous value of every edited column. Scalars are compared by the server; anything it
 * cannot compare exactly is sent as an explicit `$unchecked` marker (and shows up in the audit).
 */
export function buildExpected(
  original: Record<string, unknown>,
  editedColumns: string[],
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const c of editedColumns) {
    const v = original[c];
    out[c] = isUncomparable(v) || asMarker(v) ? UNCHECKED : (v ?? null);
  }
  return out;
}

export function rowToObject(
  columns: { name: string }[],
  row: readonly unknown[],
): Record<string, unknown> {
  return Object.fromEntries(columns.map((c, i) => [c.name, row[i]]));
}

export function isMasked(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as Record<string, unknown>)["$masked"] === true
  );
}
