import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/form";
import { t, type MessageKey } from "@/i18n";
import type { FilterOp, RowFilter } from "@/types/api";

export interface DraftFilter {
  id: string;
  column: string;
  op: FilterOp;
  value: string;
}

export const FILTER_OPS: { op: FilterOp; label: MessageKey; needsValue: boolean }[] = [
  { op: "eq", label: "filter.op.eq", needsValue: true },
  { op: "ne", label: "filter.op.ne", needsValue: true },
  { op: "contains", label: "filter.op.contains", needsValue: true },
  { op: "starts_with", label: "filter.op.starts_with", needsValue: true },
  { op: "ends_with", label: "filter.op.ends_with", needsValue: true },
  { op: "gt", label: "filter.op.gt", needsValue: true },
  { op: "gte", label: "filter.op.gte", needsValue: true },
  { op: "lt", label: "filter.op.lt", needsValue: true },
  { op: "lte", label: "filter.op.lte", needsValue: true },
  { op: "in", label: "filter.op.in", needsValue: true },
  { op: "is_null", label: "filter.op.is_null", needsValue: false },
  { op: "is_not_null", label: "filter.op.is_not_null", needsValue: false },
];

const needsValue = (op: FilterOp) => FILTER_OPS.find((o) => o.op === op)?.needsValue ?? true;

let counter = 0;
export function newDraft(column = ""): DraftFilter {
  counter += 1;
  return { id: `f${counter}`, column, op: "eq", value: "" };
}

/** Converts editable rows to API filters, dropping incomplete ones. Values stay strings so
 *  64-bit integers and decimals keep their exact digits; the database casts them. */
export function buildFilters(drafts: DraftFilter[]): RowFilter[] {
  const out: RowFilter[] = [];
  for (const d of drafts) {
    if (!d.column) continue;
    if (!needsValue(d.op)) {
      out.push({ column: d.column, op: d.op });
    } else if (d.op === "in") {
      const values = d.value
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      if (values.length) out.push({ column: d.column, op: d.op, value: values });
    } else if (d.value !== "") {
      out.push({ column: d.column, op: d.op, value: d.value });
    }
  }
  return out;
}

export function FilterBar({
  columns,
  drafts,
  onChange,
  onApply,
}: {
  columns: string[];
  drafts: DraftFilter[];
  onChange: (next: DraftFilter[]) => void;
  onApply: () => void;
}) {
  const update = (id: string, patch: Partial<DraftFilter>) =>
    onChange(drafts.map((d) => (d.id === id ? { ...d, ...patch } : d)));

  return (
    <form
      className="space-y-2"
      aria-label={t("filter.title")}
      onSubmit={(e) => {
        e.preventDefault();
        onApply();
      }}
    >
      {drafts.map((d) => (
        <div key={d.id} className="flex flex-wrap items-center gap-2">
          <Select
            aria-label={t("filter.column")}
            className="w-44"
            value={d.column}
            onChange={(e) => update(d.id, { column: e.target.value })}
          >
            <option value="">{t("filter.column")}…</option>
            {columns.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
          <Select
            aria-label={t("filter.operator")}
            className="w-40"
            value={d.op}
            onChange={(e) => update(d.id, { op: e.target.value as FilterOp })}
          >
            {FILTER_OPS.map((o) => (
              <option key={o.op} value={o.op}>
                {t(o.label)}
              </option>
            ))}
          </Select>
          {needsValue(d.op) && (
            <Input
              aria-label={t("filter.value")}
              className="w-56"
              placeholder={d.op === "in" ? t("filter.inHint") : t("filter.value")}
              value={d.value}
              onChange={(e) => update(d.id, { value: e.target.value })}
            />
          )}
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("filter.remove")}
            onClick={() => onChange(drafts.filter((x) => x.id !== d.id))}
          >
            <X />
          </Button>
        </div>
      ))}
      <div className="flex gap-2">
        <Button variant="outline" size="sm" onClick={() => onChange([...drafts, newDraft()])}>
          <Plus /> {t("filter.add")}
        </Button>
        {drafts.length > 0 && (
          <>
            <Button type="submit" size="sm">
              {t("filter.apply")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                onChange([]);
                setTimeout(onApply, 0);
              }}
            >
              {t("filter.clear")}
            </Button>
          </>
        )}
      </div>
    </form>
  );
}
