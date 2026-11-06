import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EnvBanner } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { Alert, Badge, Card, PageHeader, Spinner } from "@/components/ui/misc";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { connectionsApi, searchApi } from "@/services/api";
import type { Connection, SearchDoc } from "@/types/api";

/** Parses the JSON typed in an editor; throws a readable error for invalid input. */
export function parseJsonObject(text: string): Record<string, unknown> {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    throw new Error("Not valid JSON.");
  }
  if (typeof v !== "object" || v === null || Array.isArray(v))
    throw new Error("Expected a JSON object.");
  return v as Record<string, unknown>;
}

export function totalLabel(total: number | null, relation: "eq" | "gte"): string {
  if (total === null) return "";
  return relation === "gte"
    ? t("search.totalAtLeast", { n: total })
    : t("search.total", { n: total });
}

function DocPanel({
  cid,
  index,
  doc,
  connection,
  onChanged,
  onGone,
}: {
  cid: string;
  index: string;
  doc: SearchDoc;
  connection: Connection;
  onChanged: (d: SearchDoc) => void;
  onGone: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [del, setDel] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const writable = !connection.read_only;
  const save = useMutation({
    mutationFn: () => {
      if (draft === null || doc.seq_no === null || doc.primary_term === null)
        throw new Error("nothing to save");
      return searchApi.replaceDoc(cid, index, doc.key, {
        source: parseJsonObject(draft),
        seq_no: doc.seq_no,
        primary_term: doc.primary_term,
      });
    },
    onSuccess: (d) => {
      setDraft(null);
      onChanged(d);
    },
  });
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 break-all text-base font-semibold">{doc.id}</h2>
        {doc.score !== null && <Badge variant="outline">score {doc.score.toFixed(2)}</Badge>}
      </div>
      {draft === null ? (
        <pre
          className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted p-3 text-xs"
          data-testid="search-doc"
        >
          {JSON.stringify(doc.source, null, 2)}
        </pre>
      ) : (
        <Field label={t("search.docSource")}>
          {(p) => (
            <Textarea
              {...p}
              rows={16}
              className="font-mono text-xs"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setLocalError(null);
              }}
            />
          )}
        </Field>
      )}
      {localError && <Alert variant="destructive">{localError}</Alert>}
      {save.error && <ErrorAlert error={save.error} />}
      <div className="flex flex-wrap gap-2">
        {writable && canOn(connection, "data.update") && draft === null && (
          <Button size="sm" onClick={() => setDraft(JSON.stringify(doc.source, null, 2))}>
            {t("search.editDoc")}
          </Button>
        )}
        {draft !== null && (
          <>
            <Button
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                try {
                  parseJsonObject(draft);
                } catch (e) {
                  setLocalError((e as Error).message);
                  return;
                }
                save.mutate();
              }}
            >
              {t("search.saveDoc")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setDraft(null)}>
              {t("common.cancel")}
            </Button>
          </>
        )}
        {writable && canOn(connection, "data.delete") && (
          <Button size="sm" variant="destructive" onClick={() => setDel(true)}>
            {t("search.deleteDoc")}
          </Button>
        )}
      </div>
      <ConfirmDialog
        open={del}
        onOpenChange={setDel}
        title={t("search.deleteDoc")}
        description={t("search.deleteDocBody", { id: doc.id })}
        confirmLabel={t("common.delete")}
        requireText={connection.environment === "production" ? index : undefined}
        onConfirm={async () => {
          await searchApi.deleteDoc(cid, index, doc.key, {
            seq_no: doc.seq_no,
            primary_term: doc.primary_term,
            confirm: connection.environment === "production" ? index : undefined,
          });
          onGone();
        }}
      />
    </div>
  );
}

function Documents({
  cid,
  index,
  connection,
}: {
  cid: string;
  index: string;
  connection: Connection;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [sort, setSort] = useState("");
  const [applied, setApplied] = useState({ q: "", sort: "" });
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<SearchDoc | null>(null);
  const [creating, setCreating] = useState(false);
  const [newId, setNewId] = useState("");
  const [newSource, setNewSource] = useState("{\n  \n}");
  const [newError, setNewError] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["search-index", cid, index],
    queryFn: () => searchApi.index(cid, index),
  });
  const page = useQuery({
    queryKey: ["search-docs", cid, index, applied, cursor],
    queryFn: () =>
      searchApi.documents(cid, index, {
        q: applied.q || undefined,
        sort: applied.sort || undefined,
        cursor,
        limit: 25,
      }),
  });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["search-docs", cid, index] });
  const create = useMutation({
    mutationFn: () =>
      searchApi.createDoc(cid, index, {
        ...(newId.trim() ? { id: newId.trim() } : {}),
        source: parseJsonObject(newSource),
      }),
    onSuccess: (d) => {
      setCreating(false);
      setNewId("");
      setSelected(d);
      refresh();
    },
  });
  const canCreate = !connection.read_only && canOn(connection, "data.insert");
  const sortable = (detail.data?.fields ?? []).filter(
    (f) => !["text", "object", "nested"].includes(f.type),
  );
  return (
    <div className="grid gap-4 lg:grid-cols-[24rem_1fr]">
      <div className="space-y-3">
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            setCursor(null);
            setApplied({ q: text, sort });
          }}
        >
          <Field label={t("search.query")} hint={t("search.queryHint")}>
            {(p) => <Input {...p} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <Field label={t("search.sort")}>
            {(p) => (
              <Select {...p} value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="">{t("search.noSort")}</option>
                {sortable.flatMap((f) => [
                  <option key={`${f.path}:asc`} value={`${f.path}:asc`}>
                    {f.path} ↑
                  </option>,
                  <option key={`${f.path}:desc`} value={`${f.path}:desc`}>
                    {f.path} ↓
                  </option>,
                ])}
              </Select>
            )}
          </Field>
          <Button type="submit" size="sm">
            {t("search.go")}
          </Button>
        </form>
        {page.error && <ErrorAlert error={page.error} />}
        {page.isLoading && <Spinner label={t("common.loading")} />}
        {page.data && (
          <p className="text-xs text-muted-foreground">
            {totalLabel(page.data.total, page.data.total_relation)}
          </p>
        )}
        {page.data?.truncated && <Alert variant="info">{t("search.window")}</Alert>}
        {page.data && page.data.hits.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("search.noDocs")}</p>
        )}
        <ul
          aria-label={t("search.docsTab")}
          className="max-h-[26rem] divide-y overflow-auto rounded-md border"
        >
          {page.data?.hits.map((h) => (
            <li key={h.key}>
              <button
                type="button"
                aria-current={selected?.key === h.key}
                className={`block w-full px-2 py-1.5 text-left text-sm hover:bg-muted ${
                  selected?.key === h.key ? "bg-accent" : ""
                }`}
                onClick={() => {
                  setCreating(false);
                  setSelected(h);
                }}
              >
                <span className="block truncate font-medium">{h.id}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {JSON.stringify(h.source)}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" disabled={!cursor} onClick={() => setCursor(null)}>
            {t("common.previous")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!page.data?.next_cursor}
            onClick={() => setCursor(page.data?.next_cursor ?? null)}
          >
            {t("common.next")}
          </Button>
        </div>
        {canCreate && (
          <Button size="sm" onClick={() => setCreating(true)}>
            {t("search.newDoc")}
          </Button>
        )}
      </div>
      <Card className="min-w-0 p-4">
        {creating ? (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              try {
                parseJsonObject(newSource);
              } catch (err) {
                setNewError((err as Error).message);
                return;
              }
              create.mutate();
            }}
          >
            <h2 className="text-base font-semibold">{t("search.newDoc")}</h2>
            <Field label={t("search.docId")} hint={t("search.docIdHint")}>
              {(p) => <Input {...p} value={newId} onChange={(e) => setNewId(e.target.value)} />}
            </Field>
            <Field label={t("search.docSource")}>
              {(p) => (
                <Textarea
                  {...p}
                  rows={12}
                  className="font-mono text-xs"
                  value={newSource}
                  onChange={(e) => {
                    setNewSource(e.target.value);
                    setNewError(null);
                  }}
                />
              )}
            </Field>
            {newError && <Alert variant="destructive">{newError}</Alert>}
            {create.error && <ErrorAlert error={create.error} />}
            <Button type="submit" size="sm" disabled={create.isPending}>
              {t("search.createDoc")}
            </Button>
          </form>
        ) : selected ? (
          <DocPanel
            key={`${selected.key}:${selected.seq_no}`}
            cid={cid}
            index={index}
            doc={selected}
            connection={connection}
            onChanged={(d) => {
              setSelected(d);
              refresh();
            }}
            onGone={() => {
              setSelected(null);
              refresh();
            }}
          />
        ) : (
          <p className="text-sm text-muted-foreground">{t("search.pickDoc")}</p>
        )}
      </Card>
    </div>
  );
}

function Mapping({ cid, index }: { cid: string; index: string }) {
  const q = useQuery({
    queryKey: ["search-index", cid, index],
    queryFn: () => searchApi.index(cid, index),
  });
  if (q.isLoading) return <Spinner label={t("common.loading")} />;
  if (q.error) return <ErrorAlert error={q.error} />;
  if (!q.data) return null;
  return (
    <div className="space-y-4">
      <div>
        <h3 className="mb-1 text-sm font-medium">{t("search.fields")}</h3>
        <table className="w-full text-sm">
          <tbody>
            {q.data.fields.map((f) => (
              <tr key={f.path} className="border-b">
                <td className="py-1 pr-4 font-mono text-xs">{f.path}</td>
                <td className="py-1 text-muted-foreground">{f.type}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <h3 className="mb-1 text-sm font-medium">{t("search.settings")}</h3>
        <pre className="overflow-auto rounded-md border bg-muted p-3 text-xs">
          {JSON.stringify(q.data.settings, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function Console({ cid, index }: { cid: string; index: string }) {
  const [body, setBody] = useState('{\n  "query": { "match_all": {} },\n  "size": 10\n}');
  const [err, setErr] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => searchApi.execute(cid, { index, body: parseJsonObject(body) }),
  });
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        try {
          parseJsonObject(body);
        } catch (x) {
          setErr((x as Error).message);
          return;
        }
        setErr(null);
        run.mutate();
      }}
    >
      <Field label={t("search.consoleBody")} hint={t("search.consoleHint")}>
        {(p) => (
          <Textarea
            {...p}
            rows={10}
            className="font-mono text-xs"
            spellCheck={false}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        )}
      </Field>
      <Button type="submit" size="sm" disabled={run.isPending}>
        {t("search.run")}
      </Button>
      {err && <Alert variant="destructive">{err}</Alert>}
      {run.error && <ErrorAlert error={run.error} />}
      {run.data && (
        <div>
          <p className="text-xs text-muted-foreground">
            {t("search.result")} · {run.data.took_ms} ms
          </p>
          <pre
            className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted p-3 text-xs"
            data-testid="search-result"
          >
            {JSON.stringify(run.data.body, null, 2)}
          </pre>
          {run.data.truncated && <Alert variant="info">{t("search.truncatedResult")}</Alert>}
        </div>
      )}
    </form>
  );
}

function NewIndex({ cid, onCreated }: { cid: string; onCreated: (n: string) => void }) {
  const [name, setName] = useState("");
  const [shards, setShards] = useState("1");
  const [replicas, setReplicas] = useState("1");
  const create = useMutation({
    mutationFn: () =>
      searchApi.createIndex(cid, { name, shards: Number(shards), replicas: Number(replicas) }),
    onSuccess: () => {
      onCreated(name);
      setName("");
    },
  });
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <h3 className="text-sm font-medium">{t("search.newIndex")}</h3>
      <Field label={t("search.indexName")}>
        {(p) => <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label={t("search.shards")}>
          {(p) => (
            <Input
              {...p}
              type="number"
              min={1}
              max={20}
              value={shards}
              onChange={(e) => setShards(e.target.value)}
            />
          )}
        </Field>
        <Field label={t("search.replicas")}>
          {(p) => (
            <Input
              {...p}
              type="number"
              min={0}
              max={5}
              value={replicas}
              onChange={(e) => setReplicas(e.target.value)}
            />
          )}
        </Field>
      </div>
      {create.error && <ErrorAlert error={create.error} />}
      <Button type="submit" size="sm" disabled={create.isPending}>
        {t("search.createIndex")}
      </Button>
    </form>
  );
}

/** `/search/:cid`: indices, documents, mapping and a validated search console. */
export function SearchWorkspace() {
  const { cid = "" } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"docs" | "mapping" | "console">("docs");
  const [index, setIndex] = useState("");
  const [del, setDel] = useState(false);
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const connection = list.data?.find((c) => c.id === cid);
  const indices = useQuery({
    queryKey: ["search-indices", cid],
    queryFn: () => searchApi.indices(cid),
    enabled: connection?.db_type === "elasticsearch",
  });
  if (list.isLoading) return <Spinner label={t("common.loading")} />;
  if (list.error) return <ErrorAlert error={list.error} />;
  if (!connection || connection.db_type !== "elasticsearch")
    return <Alert variant="destructive">{t("search.notSearch")}</Alert>;
  if (!canOn(connection, "data.read"))
    return <Alert variant="warning">{t("search.noPermission")}</Alert>;
  const active = index || indices.data?.[0]?.name || "";
  const refresh = () => void qc.invalidateQueries({ queryKey: ["search-indices", cid] });
  return (
    <div className="flex min-h-screen flex-col">
      <EnvBanner
        env={connection.environment}
        name={connection.name}
        readOnly={connection.read_only}
      />
      <div className="space-y-4 p-4 md:p-6">
        <PageHeader title={t("search.title")} description={connection.name} />
        <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
          <aside className="space-y-3">
            <h2 className="text-sm font-medium">{t("search.indices")}</h2>
            {indices.error && <ErrorAlert error={indices.error} />}
            {indices.isLoading && <Spinner label={t("common.loading")} />}
            {indices.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">{t("search.noIndices")}</p>
            )}
            <ul aria-label={t("search.indices")} className="divide-y rounded-md border">
              {indices.data?.map((i) => (
                <li key={i.name}>
                  <button
                    type="button"
                    aria-current={i.name === active}
                    className={`flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted ${
                      i.name === active ? "bg-accent" : ""
                    }`}
                    onClick={() => setIndex(i.name)}
                  >
                    <span className="min-w-0 truncate">{i.name}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">{i.docs ?? "—"}</span>
                  </button>
                </li>
              ))}
            </ul>
            {!connection.read_only && canOn(connection, "admin.manage_databases") && (
              <>
                <Card className="p-3">
                  <NewIndex
                    cid={cid}
                    onCreated={(n) => {
                      setIndex(n);
                      refresh();
                    }}
                  />
                </Card>
                {active && (
                  <>
                    <Button size="sm" variant="destructive" onClick={() => setDel(true)}>
                      {t("search.deleteIndex")}
                    </Button>
                    <ConfirmDialog
                      open={del}
                      onOpenChange={setDel}
                      title={t("search.deleteIndex")}
                      description={t("search.deleteIndexBody", { name: active })}
                      confirmLabel={t("common.delete")}
                      requireText={active}
                      onConfirm={async () => {
                        await searchApi.deleteIndex(cid, active);
                        setIndex("");
                        refresh();
                      }}
                    />
                  </>
                )}
              </>
            )}
          </aside>
          <section className="min-w-0 space-y-4">
            {active ? (
              <>
                <div role="tablist" className="flex gap-1">
                  {(["docs", "mapping", "console"] as const).map((x) => (
                    <Button
                      key={x}
                      role="tab"
                      aria-selected={tab === x}
                      size="sm"
                      variant={tab === x ? "default" : "outline"}
                      onClick={() => setTab(x)}
                    >
                      {t(
                        x === "docs"
                          ? "search.docsTab"
                          : x === "mapping"
                            ? "search.mappingTab"
                            : "search.consoleTab",
                      )}
                    </Button>
                  ))}
                </div>
                {tab === "docs" && (
                  <Documents key={active} cid={cid} index={active} connection={connection} />
                )}
                {tab === "mapping" && <Mapping key={active} cid={cid} index={active} />}
                {tab === "console" && <Console key={active} cid={cid} index={active} />}
              </>
            ) : (
              <p className="text-sm text-muted-foreground">{t("search.noIndices")}</p>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
