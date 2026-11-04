import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Download, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Select, Textarea } from "@/components/ui/form";
import { Alert, Badge, Card, Spinner } from "@/components/ui/misc";
import { BulkDialog } from "@/features/data-editor/BulkDialog";
import { DocumentEditor } from "@/features/data-editor/DocumentEditor";
import { canOn } from "@/hooks/useAuth";
import { formatExtendedJson } from "@/lib/format";
import { t } from "@/i18n";
import { editorApi, explorerApi } from "@/services/api";
import type { Connection } from "@/types/api";
import { JsonNode } from "./JsonTree";
import { usePaging } from "./usePaging";

const PAGE_SIZES = [10, 25, 50, 100, 250];

export function DocumentBrowser({
  cid,
  db,
  collection,
  connection,
}: {
  cid: string;
  db: string;
  collection: string;
  connection: Connection;
}) {
  const qc = useQueryClient();
  const paging = usePaging();
  const [draft, setDraft] = useState({ filter: "", sort: "", projection: "" });
  const [applied, setApplied] = useState(draft);
  const [limit, setLimit] = useState(25);
  const [editor, setEditor] = useState<{ mode: "insert" | "update"; index?: number } | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [bulk, setBulk] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const request = { limit, cursor: paging.cursor, ...applied };
  const query = useQuery({
    queryKey: ["documents", cid, db, collection, request],
    queryFn: () => explorerApi.documents(cid, db, collection, request),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const { reset } = paging;
  useEffect(() => reset(), [applied, limit, reset]);

  const page = query.data;
  const first = (paging.page - 1) * limit + 1;
  const can = (p: string) => canOn(connection, p);
  const writable = !connection.read_only;
  const production = connection.environment === "production";
  const refresh = () => qc.invalidateQueries({ queryKey: ["documents", cid, db, collection] });
  const filterValue = (): unknown => {
    const text = applied.filter.trim();
    return text ? (JSON.parse(text) as unknown) : {};
  };

  return (
    <div className="space-y-3">
      <form
        className="grid gap-3 md:grid-cols-3"
        aria-label={t("docs.query")}
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(draft);
        }}
      >
        <Field label={t("docs.filter")} hint={t("docs.filterHint")}>
          {(p) => (
            <Textarea
              {...p}
              spellCheck={false}
              placeholder='{ "status": "active" }'
              value={draft.filter}
              onChange={(e) => setDraft({ ...draft, filter: e.target.value })}
            />
          )}
        </Field>
        <Field label={t("docs.sort")} hint={t("docs.sortHint")}>
          {(p) => (
            <Textarea
              {...p}
              spellCheck={false}
              placeholder='{ "createdAt": -1 }'
              value={draft.sort}
              onChange={(e) => setDraft({ ...draft, sort: e.target.value })}
            />
          )}
        </Field>
        <Field label={t("docs.projection")} hint={t("docs.projectionHint")}>
          {(p) => (
            <Textarea
              {...p}
              spellCheck={false}
              placeholder='{ "name": 1, "_id": 0 }'
              value={draft.projection}
              onChange={(e) => setDraft({ ...draft, projection: e.target.value })}
            />
          )}
        </Field>
        <div className="flex flex-wrap items-center gap-2 md:col-span-3">
          <Button type="submit" size="sm">
            {t("docs.run")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              const empty = { filter: "", sort: "", projection: "" };
              setDraft(empty);
              setApplied(empty);
            }}
          >
            {t("filter.clear")}
          </Button>
          {writable && can("data.insert") && (
            <Button variant="outline" size="sm" onClick={() => setEditor({ mode: "insert" })}>
              <Plus /> {t("docs.add")}
            </Button>
          )}
          {writable &&
            can("data.delete") &&
            can("data.bulk_write") &&
            applied.filter.trim() !== "" && (
              <Button variant="outline" size="sm" onClick={() => setBulk(true)}>
                {t("docs.bulkDelete")}
              </Button>
            )}
          {can("export.export") && (
            <details className="relative">
              <summary className="inline-flex h-8 cursor-pointer list-none items-center gap-2 rounded-md border bg-card px-3 text-xs font-medium hover:bg-muted">
                <Download className="size-4" aria-hidden /> {t("export.title")}
              </summary>
              <ul className="absolute z-20 mt-1 w-44 rounded-md border bg-card p-1 shadow-lg">
                {(["json", "ndjson"] as const).map((f) => (
                  <li key={f}>
                    <a
                      className="block rounded px-2 py-1.5 text-sm hover:bg-muted"
                      download
                      href={editorApi.docExportUrl(cid, db, collection, applied, f)}
                    >
                      {t(`export.${f}`)}
                    </a>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
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
        </div>
      </form>

      {notice && <Alert variant="success">{notice}</Alert>}
      {query.error && <ErrorAlert error={query.error} />}
      {page?.warnings.map((w) => (
        <Alert key={w} variant="warning">
          {w}
        </Alert>
      ))}

      {page && (
        <>
          {page.documents.length === 0 && <Alert variant="info">{t("docs.empty")}</Alert>}
          <ol className="space-y-2" aria-label={t("docs.list")}>
            {page.documents.map((doc, i) => {
              const id = formatExtendedJson(doc["_id"]) ?? JSON.stringify(doc["_id"] ?? "");
              const key = page.doc_keys[i];
              return (
                <li key={`${first + i}-${id}`}>
                  <Card className="p-2">
                    <div className="mb-1 flex items-center justify-between gap-2 px-1">
                      <span className="truncate font-mono text-xs text-muted-foreground" title={id}>
                        #{first + i} · {id}
                      </span>
                      {writable && key && (
                        <span className="flex shrink-0 gap-1">
                          {can("data.update") && (
                            <Button
                              size="icon"
                              variant="ghost"
                              aria-label={t("docs.editItem", { n: first + i })}
                              onClick={() => setEditor({ mode: "update", index: i })}
                            >
                              <Pencil />
                            </Button>
                          )}
                          {can("data.delete") && (
                            <Button
                              size="icon"
                              variant="ghost"
                              aria-label={t("docs.deleteItem", { n: first + i })}
                              onClick={() => setDeleting(i)}
                            >
                              <Trash2 />
                            </Button>
                          )}
                        </span>
                      )}
                    </div>
                    <JsonNode value={doc} defaultOpenDepth={1} />
                  </Card>
                </li>
              );
            })}
          </ol>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <div className="flex flex-wrap items-center gap-2">
              {page.documents.length > 0 && (
                <span>
                  {t("browser.range", { from: first, to: first + page.documents.length - 1 })}
                </span>
              )}
              {page.total_estimate !== null && (
                <span>
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

      {editor && (
        <DocumentEditor
          mode={editor.mode}
          initial={editor.index !== undefined ? page?.documents[editor.index] : undefined}
          onClose={() => setEditor(null)}
          onSubmit={async (doc) => {
            if (editor.mode === "insert") await editorApi.insertDocument(cid, db, collection, doc);
            else {
              const key = page?.doc_keys[editor.index!];
              await editorApi.replaceDocument(
                cid,
                db,
                collection,
                key!,
                page!.documents[editor.index!],
                doc,
              );
            }
            await refresh();
          }}
        />
      )}
      {deleting !== null && page && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDeleting(null)}
          title={t("docs.deleteTitle")}
          description={t("docs.deleteBody")}
          confirmLabel={t("common.delete")}
          requireText={production ? collection : undefined}
          onConfirm={async () => {
            await editorApi.deleteDocument(cid, db, collection, page.doc_keys[deleting]!, {
              original: page.documents[deleting],
              confirm: production ? collection : undefined,
            });
            await refresh();
          }}
        />
      )}
      {bulk && (
        <BulkDialog
          title={t("docs.bulkDeleteTitle")}
          summary={t("bulk.deleteSummary")}
          objectName={collection}
          confirmLabel={t("bulk.deleteGo")}
          preview={() =>
            editorApi.docBulkPreview(cid, db, collection, {
              operation: "delete",
              filter: filterValue(),
            })
          }
          execute={({ token, count, confirm }) =>
            editorApi.docBulkDelete(cid, db, collection, {
              filter: filterValue(),
              token,
              expected_count: count,
              confirm,
            })
          }
          onClose={() => setBulk(false)}
          onDone={async (n) => {
            setBulk(false);
            setNotice(t("bulk.done", { n }));
            await refresh();
          }}
        />
      )}
    </div>
  );
}
