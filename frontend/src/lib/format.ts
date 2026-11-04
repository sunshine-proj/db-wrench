/**
 * Formatting helpers for database values. Everything returned here is plain text; callers
 * render it as text nodes only (never as HTML).
 */

export type Marker =
  | { kind: "truncated"; preview: string; length: number }
  | { kind: "binary"; length: number; previewBase64: string; truncated: boolean };

export function asMarker(v: unknown): Marker | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  if (o["$truncated"] === true && typeof o["preview"] === "string") {
    return { kind: "truncated", preview: o["preview"], length: Number(o["length"] ?? 0) };
  }
  if (o["$binary"] === true) {
    return {
      kind: "binary",
      length: Number(o["length"] ?? 0),
      previewBase64: String(o["preview_base64"] ?? ""),
      truncated: o["truncated"] === true,
    };
  }
  return null;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

/** Short text for a grid cell; `null` for SQL NULL is handled by the caller. */
export function cellText(v: unknown): string {
  const m = asMarker(v);
  if (m?.kind === "truncated") return `${m.preview}…`;
  if (m?.kind === "binary") return `‹binary ${formatBytes(m.length)}›`;
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean" || typeof v === "bigint") return String(v);
  return JSON.stringify(v);
}

export function isNumeric(v: unknown): boolean {
  return typeof v === "number";
}

// ---- MongoDB canonical Extended JSON -> shell-style text --------------------------------

const EJSON_FORMATTERS: Record<string, (inner: unknown) => string> = {
  $oid: (v) => `ObjectId("${String(v)}")`,
  $numberLong: (v) => `NumberLong("${String(v)}")`,
  $numberInt: (v) => String(v),
  $numberDouble: (v) => String(v),
  $numberDecimal: (v) => `Decimal128("${String(v)}")`,
  $date: (v) => {
    const raw =
      typeof v === "object" && v !== null && "$numberLong" in v
        ? Number((v as Record<string, unknown>)["$numberLong"])
        : v;
    const d = new Date(raw as string | number);
    return `ISODate("${Number.isNaN(d.getTime()) ? String(raw) : d.toISOString()}")`;
  },
  $uuid: (v) => `UUID("${String(v)}")`,
  $binary: (v) => {
    const o = v as { base64?: string; subType?: string };
    return `BinData(${parseInt(o.subType ?? "0", 16)}, "${o.base64 ?? ""}")`;
  },
  $timestamp: (v) => {
    const o = v as { t?: number; i?: number };
    return `Timestamp(${o.t ?? 0}, ${o.i ?? 0})`;
  },
  $regularExpression: (v) => {
    const o = v as { pattern?: string; options?: string };
    return `/${o.pattern ?? ""}/${o.options ?? ""}`;
  },
  $minKey: () => "MinKey",
  $maxKey: () => "MaxKey",
};

/** Returns shell-style text if `v` is an Extended JSON scalar wrapper, else null. */
export function formatExtendedJson(v: unknown): string | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const keys = Object.keys(v);
  const key = keys[0];
  if (keys.length !== 1 || key === undefined) return null;
  const fmt = EJSON_FORMATTERS[key];
  return fmt ? fmt((v as Record<string, unknown>)[key]) : null;
}

export function bsonTypeLabel(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (typeof v === "object") {
    const key = Object.keys(v)[0];
    if (Object.keys(v).length === 1 && key && key in EJSON_FORMATTERS) return key.slice(1);
    return "object";
  }
  return typeof v;
}
