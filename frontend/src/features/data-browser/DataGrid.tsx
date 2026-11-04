import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type RowSelectionState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowUp, ArrowUpDown, KeyRound, Lock } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { cn } from "@/components/cn";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { columnKind, isMasked, toInputText } from "@/lib/editing";
import { asMarker, cellText, formatBytes } from "@/lib/format";
import { t } from "@/i18n";
import type { SortSpec } from "@/types/api";

const VIRTUALIZE_ABOVE = 100;
const ROW_HEIGHT = 34;

export function CellValue({ value, onOpen }: { value: unknown; onOpen: () => void }) {
  if (value === null || value === undefined) {
    return (
      <span
        className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground italic"
        data-testid="null-cell"
      >
        NULL
      </span>
    );
  }
  if (isMasked(value)) {
    return (
      <span
        className="inline-flex items-center gap-1 text-muted-foreground"
        data-testid="masked-cell"
        title={t("grid.maskedHint")}
      >
        <Lock className="size-3" aria-hidden />
        <span aria-label={t("grid.masked")}>••••••</span>
      </span>
    );
  }
  const marker = asMarker(value);
  const isComplex = typeof value === "object";
  const text = cellText(value);
  const long = typeof value === "string" && value.length > 60;
  const clickable = marker !== null || isComplex || long;
  const body = (
    <span
      className={cn(
        "block max-w-[28rem] truncate",
        typeof value === "number" && "text-right font-mono",
        (marker || isComplex) && "font-mono text-xs text-muted-foreground",
      )}
      title={clickable ? undefined : text}
    >
      {text}
    </span>
  );
  return clickable ? (
    <button
      type="button"
      className="text-left hover:underline"
      onClick={onOpen}
      aria-label={t("grid.openValue")}
    >
      {body}
    </button>
  ) : (
    body
  );
}

interface Detail {
  name: string;
  value: unknown;
  row: number;
}

function ValueDialog({
  detail,
  onClose,
  onFetchCell,
}: {
  detail: Detail;
  onClose: () => void;
  onFetchCell?: DataGridProps["onFetchCell"];
}) {
  const [full, setFull] = useState<unknown>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const shown = full !== undefined ? full : detail.value;
  const marker = asMarker(shown);
  const canLoad =
    onFetchCell !== undefined && full === undefined && asMarker(detail.value) !== null;
  let body: string;
  let note: string | null = null;
  if (marker?.kind === "truncated") {
    body = marker.preview;
    note = t("grid.truncatedNote", { shown: marker.preview.length, total: marker.length });
  } else if (marker?.kind === "binary") {
    const b64 = (shown as Record<string, unknown>)["base64"];
    body = typeof b64 === "string" ? b64 : marker.previewBase64;
    note = t("grid.binaryNote", { size: formatBytes(marker.length) });
  } else if (typeof shown === "string") {
    body = shown;
  } else {
    body = JSON.stringify(shown, null, 2);
  }

  async function load() {
    if (!onFetchCell) return;
    setLoading(true);
    setError(null);
    try {
      setFull(await onFetchCell(detail.row, detail.name));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={detail.name} className="max-w-2xl">
        {note && full === undefined && <p className="mb-2 text-sm text-muted-foreground">{note}</p>}
        {error !== null && <ErrorAlert error={error} />}
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 font-mono text-xs">
          {body}
        </pre>
        <div className="mt-3 flex justify-end gap-2">
          {canLoad && (
            <Button variant="outline" onClick={load} disabled={loading}>
              {t("grid.loadFull")}
            </Button>
          )}
          <Button variant="outline" onClick={() => navigator.clipboard?.writeText(body)}>
            {t("common.copy")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export interface DataGridProps {
  columns: { name: string; type: string }[];
  rows: unknown[][];
  primaryKey?: string[];
  sort?: SortSpec[];
  onSort?: (column: string) => void;
  selectable?: boolean;
  onSelectionChange?: (selected: unknown[][]) => void;
  onSelectionIndices?: (indices: number[]) => void;
  /** double-click a scalar cell to edit it in place */
  onCellEdit?: (rowIndex: number, column: string, text: string) => Promise<void>;
  /** load the full value of a truncated or binary cell */
  onFetchCell?: (rowIndex: number, column: string) => Promise<unknown>;
}

/** Only the identity of the edited cell lives in the grid. The text being typed stays local to
 *  the input: keeping it here would rebuild every column definition on each keystroke and remount
 *  the input, dropping focus after each character. */
interface Editing {
  row: number;
  column: string;
  initial: string;
}

function InlineCellEditor({
  name,
  initial,
  onCommit,
  onCancel,
}: {
  name: string;
  initial: string;
  onCommit: (text: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  return (
    <div>
      <input
        autoFocus
        aria-label={t("grid.editCell", { name })}
        className="h-7 w-full min-w-32 rounded border border-primary bg-card px-1 text-sm"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            onCommit(text).catch((err: unknown) =>
              setError(err instanceof Error ? err.message : String(err)),
            );
          }
          if (e.key === "Escape") onCancel();
        }}
        onBlur={onCancel}
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

const NOT_INLINE = new Set(["json", "binary"]);

export function DataGrid({
  columns,
  rows,
  primaryKey = [],
  sort = [],
  onSort,
  selectable = true,
  onSelectionChange,
  onSelectionIndices,
  onCellEdit,
  onFetchCell,
}: DataGridProps) {
  const [selection, setSelection] = useState<RowSelectionState>({});
  const [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const canInline = (column: string, type: string, value: unknown): boolean =>
    onCellEdit !== undefined &&
    !primaryKey.includes(column) &&
    !NOT_INLINE.has(columnKind(type)) &&
    !isMasked(value) &&
    (value === null || typeof value !== "object");

  const defs = useMemo<ColumnDef<unknown[]>[]>(
    () => [
      ...(selectable
        ? [
            {
              id: "__select",
              header: ({ table }) => (
                <input
                  type="checkbox"
                  aria-label={t("grid.selectAll")}
                  className="size-4 accent-[var(--primary)]"
                  checked={table.getIsAllRowsSelected()}
                  onChange={table.getToggleAllRowsSelectedHandler()}
                />
              ),
              cell: ({ row }) => (
                <input
                  type="checkbox"
                  aria-label={t("grid.selectRow", { n: row.index + 1 })}
                  className="size-4 accent-[var(--primary)]"
                  checked={row.getIsSelected()}
                  onChange={row.getToggleSelectedHandler()}
                />
              ),
            } satisfies ColumnDef<unknown[]>,
          ]
        : []),
      ...columns.map<ColumnDef<unknown[]>>((c, i) => ({
        id: c.name,
        accessorFn: (r) => r[i],
        header: () => {
          const s = sort.find((x) => x.column === c.name);
          const Icon = s ? (s.direction === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
          return (
            <button
              type="button"
              className="flex items-center gap-1 font-semibold hover:text-foreground"
              onClick={() => onSort?.(c.name)}
              disabled={!onSort}
            >
              {primaryKey.includes(c.name) && (
                <KeyRound className="size-3 text-warning" aria-label={t("grid.primaryKey")} />
              )}
              <span>{c.name}</span>
              <span className="font-normal opacity-60">{c.type}</span>
              {onSort && <Icon className="size-3" aria-hidden />}
            </button>
          );
        },
        cell: ({ getValue, row }) => {
          const value = getValue();
          if (editing && editing.row === row.index && editing.column === c.name) {
            return (
              <InlineCellEditor
                name={c.name}
                initial={editing.initial}
                onCancel={() => setEditing(null)}
                onCommit={async (text) => {
                  await onCellEdit?.(row.index, c.name, text);
                  setEditing(null);
                }}
              />
            );
          }
          const cell = (
            <CellValue
              value={value}
              onOpen={() => setDetail({ name: c.name, value, row: row.index })}
            />
          );
          if (!canInline(c.name, c.type, value)) return cell;
          return (
            <span
              onDoubleClick={() =>
                setEditing({
                  row: row.index,
                  column: c.name,
                  initial: toInputText(columnKind(c.type), value),
                })
              }
              title={t("grid.doubleClickEdit")}
            >
              {cell}
            </span>
          );
        },
      })),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [columns, primaryKey, sort, onSort, selectable, editing, onCellEdit],
  );

  const table = useReactTable({
    data: rows,
    columns: defs,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (_, index) => String(index),
    enableRowSelection: selectable,
    state: { rowSelection: selection },
    onRowSelectionChange: (updater) => {
      const next = typeof updater === "function" ? updater(selection) : updater;
      setSelection(next);
      const indices = rows.map((_, i) => i).filter((i) => next[String(i)]);
      onSelectionChange?.(indices.map((i) => rows[i]!));
      onSelectionIndices?.(indices);
    },
  });

  const tableRows = table.getRowModel().rows;
  const virtualize = tableRows.length > VIRTUALIZE_ABOVE;
  const virtualizer = useVirtualizer({
    count: tableRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    enabled: virtualize,
  });
  const items = virtualize
    ? virtualizer.getVirtualItems()
    : tableRows.map((_, index) => ({ index, start: 0, end: 0 }));
  const padTop = virtualize && items.length ? (items[0]?.start ?? 0) : 0;
  const padBottom =
    virtualize && items.length
      ? virtualizer.getTotalSize() - (items[items.length - 1]?.end ?? 0)
      : 0;

  return (
    <>
      <div
        ref={scrollRef}
        className="max-h-[62vh] overflow-auto rounded-md border"
        tabIndex={0}
        aria-label={t("grid.label")}
      >
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-muted">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => {
                  const s = sort.find((x) => x.column === h.column.id);
                  return (
                    <th
                      key={h.id}
                      scope="col"
                      aria-sort={
                        s ? (s.direction === "asc" ? "ascending" : "descending") : undefined
                      }
                      className="h-9 whitespace-nowrap px-3 text-left text-xs text-muted-foreground"
                    >
                      {flexRender(h.column.columnDef.header, h.getContext())}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {padTop > 0 && (
              <tr>
                <td style={{ height: padTop }} colSpan={defs.length} />
              </tr>
            )}
            {items.map((item) => {
              const row = tableRows[item.index];
              if (!row) return null;
              return (
                <tr
                  key={row.id}
                  data-selected={row.getIsSelected() || undefined}
                  className="border-t hover:bg-muted/50 data-[selected]:bg-accent"
                  style={virtualize ? { height: ROW_HEIGHT } : undefined}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="whitespace-nowrap px-3 py-1.5">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              );
            })}
            {padBottom > 0 && (
              <tr>
                <td style={{ height: padBottom }} colSpan={defs.length} />
              </tr>
            )}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="p-6 text-center text-sm text-muted-foreground">{t("grid.empty")}</p>
        )}
      </div>
      {detail && (
        <ValueDialog detail={detail} onClose={() => setDetail(null)} onFetchCell={onFetchCell} />
      )}
    </>
  );
}
