import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { bsonTypeLabel, formatExtendedJson } from "@/lib/format";
import { t } from "@/i18n";

function Scalar({ value }: { value: unknown }) {
  const ejson = formatExtendedJson(value);
  if (ejson !== null) {
    return (
      <span className="font-mono text-xs text-primary" title={bsonTypeLabel(value)}>
        {ejson}
      </span>
    );
  }
  if (value === null)
    return <span className="font-mono text-xs italic text-muted-foreground">null</span>;
  if (typeof value === "string")
    return <span className="break-all font-mono text-xs text-success">&quot;{value}&quot;</span>;
  return <span className="font-mono text-xs">{String(value)}</span>;
}

interface NodeProps {
  name?: string;
  value: unknown;
  depth?: number;
  defaultOpenDepth?: number;
}

/** Collapsible rendering of canonical Extended JSON. Values are rendered as text only. */
export function JsonNode({ name, value, depth = 0, defaultOpenDepth = 1 }: NodeProps) {
  const [open, setOpen] = useState(depth < defaultOpenDepth);
  const isContainer =
    typeof value === "object" && value !== null && formatExtendedJson(value) === null;

  const label = name !== undefined && (
    <span className="mr-1 font-mono text-xs font-medium">{name}:</span>
  );
  if (!isContainer) {
    return (
      <div className="py-0.5 pl-5">
        {label}
        <Scalar value={value} />
      </div>
    );
  }
  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);
  const summary = Array.isArray(value)
    ? t("json.items", { count: entries.length })
    : t("json.fields", { count: entries.length });

  return (
    <div className="py-0.5">
      <button
        type="button"
        className="flex items-center gap-1 rounded text-left hover:bg-muted"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? (
          <ChevronDown className="size-4" aria-hidden />
        ) : (
          <ChevronRight className="size-4" aria-hidden />
        )}
        {label}
        <span className="text-xs text-muted-foreground">
          {Array.isArray(value) ? "[ ]" : "{ }"} {summary}
        </span>
      </button>
      {open && (
        <div className="ml-2 border-l pl-2">
          {entries.map(([k, v]) => (
            <JsonNode
              key={k}
              name={k}
              value={v}
              depth={depth + 1}
              defaultOpenDepth={defaultOpenDepth}
            />
          ))}
        </div>
      )}
    </div>
  );
}
