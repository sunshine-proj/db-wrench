import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  Columns3,
  Download,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Alert, Badge, Spinner } from "@/components/ui/misc";
import { BulkDialog } from "@/features/data-editor/BulkDialog";
import { RowForm } from "@/features/data-editor/RowForm";
import { canOn } from "@/hooks/useAuth";
import { buildExpected, columnKind, isMasked, parseInput, rowToObject } from "@/lib/editing";
import { t } from "@/i18n";
import { editorApi, explorerApi, type TablePath } from "@/services/api";
import type {
  Connection,
  ExportFormat,
  RowFilter,
  RowRequest,
  Selector,
  SortSpec,
  TableStructure,
} from "@/types/api";
import { DataGrid } from "./DataGrid";
import { buildFilters, FilterBar, type DraftFilter } from "./FilterBar";
import { usePaging } from "./usePaging";

const PAGE_SIZES = [25, 50, 100, 250, 500, 1000];
const TEXTY = /char|text|citext|uuid|enum|name|json/i;

/** Next sort state for a header click: asc -> desc -> none. */
export function nextSort(current: SortSpec[], column: string): SortSpec[] {
  const existing = current.find((s) => s.column === column);
  if (!existing) return [{ column, direction: "asc" }];
  if (existing.direction === "asc") return [{ column, direction: "desc" }];
  return [];
}

type Bulk =
  | { op: "delete"; selector: Selector }
  | { op: "update"; selector: Selector; values: Record<string, unknown> };

function BulkUpdateSetup({
  structure,
  onCancel,
  onNext,
}: {
  structure: TableStructure;
  onCancel: () => void;
  onNext: (values: Record<string, unknown>) => void;
}) {
  const editable = structure.columns.filter(
    (c) => !structure.primary_key.includes(c.name) && !c.is_generated,
  );
  const [column, setColumn] = useState(editable[0]?.name ?? "");
  const [text, setText] = useState("");
  const [isNull, setIsNull] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const col = editable.find((c) => c.name === column);
  return (
    <Dialog open onOpenChange={(o) => !o && onCancel()}>
      <DialogContent title={t("bulk.updateSetupTitle")} description={t("bulk.updateSetupHint")}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!col) return;
            try {
              onNext({ [col.name]: isNull ? null : parseInput(columnKind(col.data_type), text) });
            } catch (err) {
              setError((err as Error).message);
            }
          }}
        >
          <Field label={t("filter.column")}>
            {(p) => (
              <Select {...p} value={column} onChange={(e) => setColumn(e.target.value)}>
                {editable.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("filter.value")} error={error ?? undefined}>
            {(p) => (
              <Input
                {...p}
                disabled={isNull}
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setError(null);
                }}
              />
            )}
          </Field>
          {col?.nullable && (
            <Checkbox
              label={t("editor.null")}
              checked={isNull}
              onChange={(e) => setIsNull(e.target.checked)}
            />
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onCancel}>
              {t("common.cancel")}
            </Button>
            <Button type="submit">{t("bulk.continue")}</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function RowBrowser({
  path,
  structure,
  connection,
}: {
  path: TablePath;
  structure: TableStructure;
  connection: Connection;
}) {
  const qc = useQueryClient();
  const paging = usePaging();
  const allColumns = useMemo(() => structure.columns.map((c) => c.name), [structure]);
  const [drafts, setDrafts] = useState<DraftFilter[]>([]);
  const [applied, setApplied] = useState<RowFilter[]>(buildFilters([]));
  const [sort, setSort] = useState<SortSpec[]>([]);
  const [limit, setLimit] = useState(100);
  const [visible, setVisible] = useState<string[]>(allColumns);
  const [searchText, setSearchText] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [form, setForm] = useState<{ mode: "insert" | "update"; index?: number } | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [bulk, setBulk] = useState<Bulk | null>(null);
  const [setup, setSetup] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const searchColumns = useMemo(
    () => structure.columns.filter((c) => TEXTY.test(c.data_type)).map((c) => c.name),
    [structure],
  );
  const columns = visible.length === allColumns.length ? undefined : visible;
  const request: RowRequest = {
    limit,
    cursor: paging.cursor,
    sort,
    filters: applied,
    columns,
    search,
    searchColumns,
  };
  const query = useQuery({
    queryKey: ["rows", path, request],
    queryFn: () => explorerApi.rows(path, request),
    placeholderData: keepPreviousData,
  });

  const { reset } = paging;
  useEffect(() => {
    reset();
    setSelected([]);
  }, [applied, sort, limit, visible, search, reset]);

  const page = query.data;
  const first = (paging.page - 1) * limit + 1;
  const can = (p: string) => canOn(connection, p);
  const writable = !!page?.editable && !connection.read_only;
  const refresh = async () => {
    setSelected([]);
    await qc.invalidateQueries({ queryKey: ["rows", path] });
  };
  const rowObject = (i: number) => (page ? rowToObject(page.columns, page.rows[i] ?? []) : {});
  const keyOf = (i: number) => page?.row_keys[i] ?? null;
  const production = connection.environment === "production";
  const filterSelector = (): Selector => ({
    filters: applied,
    search: search || undefined,
    search_columns: search ? searchColumns : undefined,
  });

  async function inlineEdit(i: number, column: string, text: string) {
    const key = keyOf(i);
    const col = structure.columns.find((c) => c.name === column);
    if (!key || !col) return;
    const before = rowObject(i)[column];
    const value =
      text === "" && col.nullable && before === null
        ? null
        : parseInput(columnKind(col.data_type), text);
    if (String(value) === String(before)) return;
    await editorApi.updateRow(
      path,
      key,
      { [column]: value },
      buildExpected(rowObject(i), [column]),
    );
    await refresh();
  }

  const exportHref = (format: ExportFormat) => editorApi.exportUrl(path, request, format);

  return (
    <div className="space-y-3">
      <FilterBar
        columns={allColumns}
        drafts={drafts}
        onChange={setDrafts}
        onApply={() => setApplied(buildFilters(drafts))}
      />
      <div className="flex flex-wrap items-center gap-2">
        <form
          className="flex gap-2"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(searchText.trim());
          }}
        >
          <Input
            aria-label={t("browser.search")}
            placeholder={t("browser.search")}
            className="w-56"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
          />
          <Button type="submit" variant="outline" size="sm">
            {t("browser.searchGo")}
          </Button>
        </form>
        <details className="relative">
          <summary className="inline-flex h-8 cursor-pointer list-none items-center gap-2 rounded-md border bg-card px-3 text-xs font-medium hover:bg-muted">
            <Columns3 className="size-4" aria-hidden /> {t("browser.columns")} ({visible.length}/
            {allColumns.length})
          </summary>
          <div className="absolute z-20 mt-1 max-h-72 w-60 space-y-1 overflow-auto rounded-md border bg-card p-3 shadow-lg">
            {allColumns.map((c) => (
              <Checkbox
                key={c}
                label={c}
                checked={visible.includes(c)}
                onChange={(e) =>
                  setVisible((v) =>
                    e.target.checked
                      ? allColumns.filter((x) => x === c || v.includes(x))
                      : v.filter((x) => x !== c),
                  )
                }
              />
            ))}
          </div>
        </details>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          {t("browser.pageSize")}
          <Select
            className="h-8 w-20"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </label>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("common.refresh")}
          onClick={() => query.refetch()}
        >
          <RefreshCw className={query.isFetching ? "animate-spin" : ""} />
        </Button>
        {query.isFetching && <Spinner />}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {can("export.export") && (
            <details className="relative">
              <summary className="inline-flex h-8 cursor-pointer list-none items-center gap-2 rounded-md border bg-card px-3 text-xs font-medium hover:bg-muted">
                <Download className="size-4" aria-hidden /> {t("export.title")}
              </summary>
              <ul className="absolute right-0 z-20 mt-1 w-44 rounded-md border bg-card p-1 shadow-lg">
                {(["csv", "json", "ndjson", "sql"] as const).map((f) => (
                  <li key={f}>
                    <a
                      className="block rounded px-2 py-1.5 text-sm hover:bg-muted"
                      href={exportHref(f)}
                      download
                    >
                      {t(`export.${f}`)}
                    </a>
                  </li>
                ))}
                <li className="px-2 py-1 text-[11px] text-muted-foreground">{t("export.hint")}</li>
              </ul>
            </details>
          )}
          {writable && can("data.insert") && (
            <Button size="sm" onClick={() => setForm({ mode: "insert" })}>
              <Plus /> {t("editor.addRow")}
            </Button>
          )}
        </div>
      </div>

      {writable && selected.length > 0 && (
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border bg-accent px-3 py-2 text-sm"
          role="toolbar"
          aria-label={t("editor.selection")}
        >
          <span>{t("editor.selected", { n: selected.length })}</span>
          {selected.length === 1 && can("data.update") && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setForm({ mode: "update", index: selected[0] })}
            >
              <Pencil /> {t("editor.edit")}
            </Button>
          )}
          {can("data.delete") && (selected.length === 1 || can("data.bulk_write")) && (
            <Button
              size="sm"
              variant="destructive"
              onClick={() =>
                selected.length === 1
                  ? setDeleting(selected[0]!)
                  : setBulk({
                      op: "delete",
                      selector: { row_keys: selected.map((i) => keyOf(i)!).filter(Boolean) },
                    })
              }
            >
              <Trash2 /> {t("editor.delete")}
            </Button>
          )}
        </div>
      )}
      {writable && can("data.bulk_write") && (applied.length > 0 || search) && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t("editor.matching")}</span>
          {can("data.update") && (
            <Button size="sm" variant="outline" onClick={() => setSetup(true)}>
              {t("bulk.updateMatching")}
            </Button>
          )}
          {can("data.delete") && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setBulk({ op: "delete", selector: filterSelector() })}
            >
              {t("bulk.deleteMatching")}
            </Button>
          )}
        </div>
      )}

      {notice && <Alert variant="success">{notice}</Alert>}
      {query.error && <ErrorAlert error={query.error} />}
      {structure.primary_key.length === 0 && structure.kind === "table" && (
        <Alert variant="warning">{t("browser.noPrimaryKey")}</Alert>
      )}
      {connection.read_only && can("data.read") && structure.primary_key.length > 0 && (
        <p className="text-xs text-muted-foreground">{t("editor.readOnlyConnection")}</p>
      )}
      {page?.warnings.map((w) => (
        <Alert key={w} variant="warning">
          {w}
        </Alert>
      ))}

      {page && (
        <>
          <DataGrid
            columns={page.columns}
            rows={page.rows}
            primaryKey={structure.primary_key}
            sort={sort}
            onSort={(c) => setSort((s) => nextSort(s, c))}
            selectable={writable}
            onSelectionIndices={setSelected}
            onCellEdit={writable && can("data.update") ? inlineEdit : undefined}
            onFetchCell={
              can("data.read")
                ? async (i, col) => {
                    const key = keyOf(i);
                    if (!key) throw new Error(t("editor.noKey"));
                    return (await editorApi.cell(path, key, col)).value;
                  }
                : undefined
            }
          />
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center gap-2">
              <span>
                {page.rows.length
                  ? t("browser.range", { from: first, to: first + page.rows.length - 1 })
                  : t("grid.empty")}
              </span>
              {page.total_estimate !== null && (
                <span title={t("browser.estimateHint")}>
                  {t("browser.estimate", { count: page.total_estimate.toLocaleString() })}
                </span>
              )}
              <Badge variant="outline">{t(`browser.pagination.${page.pagination}`)}</Badge>
              {page.truncated && <Badge variant="warning">{t("browser.truncated")}</Badge>}
            </div>
            <div className="flex items-center gap-1">
              <Button variant="outline" size="sm" onClick={paging.prev} disabled={!paging.hasPrev}>
                <ChevronLeft /> {t("common.previous")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => page.next_cursor && paging.next(page.next_cursor)}
                disabled={!page.next_cursor}
              >
                {t("common.next")} <ChevronRight />
              </Button>
            </div>
          </div>
        </>
      )}

      {form && page && (
        <RowForm
          mode={form.mode}
          columns={structure.columns}
          primaryKey={structure.primary_key}
          row={form.index !== undefined ? page.rows[form.index] : undefined}
          rowColumns={page.columns}
          onClose={() => setForm(null)}
          onSubmit={async ({ values, expected }) => {
            if (form.mode === "insert") await editorApi.insertRow(path, values);
            else await editorApi.updateRow(path, keyOf(form.index!)!, values, expected ?? {});
            await refresh();
          }}
        />
      )}
      {deleting !== null && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDeleting(null)}
          title={t("editor.deleteTitle")}
          description={t("editor.deleteBody")}
          confirmLabel={t("common.delete")}
          requireText={production ? path.table : undefined}
          onConfirm={async () => {
            const obj = rowObject(deleting);
            const expected = Object.fromEntries(
              Object.entries(obj).filter(
                ([, v]) => !isMasked(v) && (v === null || typeof v !== "object"),
              ),
            );
            await editorApi.deleteRow(path, keyOf(deleting)!, {
              expected,
              confirm: production ? path.table : undefined,
            });
            await refresh();
          }}
        />
      )}
      {setup && (
        <BulkUpdateSetup
          structure={structure}
          onCancel={() => setSetup(false)}
          onNext={(values) => {
            setSetup(false);
            setBulk({ op: "update", selector: filterSelector(), values });
          }}
        />
      )}
      {bulk && (
        <BulkDialog
          title={bulk.op === "delete" ? t("bulk.deleteTitle") : t("bulk.updateTitle")}
          summary={bulk.op === "delete" ? t("bulk.deleteSummary") : t("bulk.updateSummary")}
          objectName={path.table}
          confirmLabel={bulk.op === "delete" ? t("bulk.deleteGo") : t("bulk.updateGo")}
          preview={() =>
            editorApi.bulkPreview(path, {
              operation: bulk.op,
              selector: bulk.selector,
              values: bulk.op === "update" ? bulk.values : undefined,
            })
          }
          execute={({ token, count, confirm }) =>
            bulk.op === "delete"
              ? editorApi.bulkDelete(path, {
                  selector: bulk.selector,
                  token,
                  expected_count: count,
                  confirm,
                })
              : editorApi.bulkUpdate(path, {
                  selector: bulk.selector,
                  values: bulk.values,
                  token,
                  expected_count: count,
                  confirm,
                })
          }
          onClose={() => setBulk(null)}
          onDone={async (n) => {
            setBulk(null);
            setNotice(t("bulk.done", { n }));
            await refresh();
          }}
        />
      )}
    </div>
  );
}
