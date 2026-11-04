import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, Square, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { cn } from "@/components/cn";
import { EnvBanner } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Card, Alert, Badge, PageHeader, Spinner } from "@/components/ui/misc";
import { Select } from "@/components/ui/form";
import { JsonNode } from "@/features/data-browser/JsonTree";
import { QueryPanels } from "@/features/query-panels/QueryPanels";
import { useTabs } from "@/features/query-panels/useTabs";
import { LazyEditor } from "@/features/sql-workspace/LazyEditor";
import { canOn } from "@/hooks/useAuth";
import { formatExtendedJson } from "@/lib/format";
import { t } from "@/i18n";
import { ApiError, connectionsApi, explorerApi, queryApi } from "@/services/api";
import type { MongoExecuteRequest, MongoExecuteResult } from "@/types/api";

export const FIND_TEMPLATE = `{
  "kind": "find",
  "collection": "",
  "filter": {},
  "sort": {},
  "projection": {},
  "limit": 50
}`;

export const AGGREGATE_TEMPLATE = `{
  "kind": "aggregate",
  "collection": "",
  "pipeline": [
    { "$match": {} },
    { "$limit": 50 }
  ]
}`;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const isEmpty = (v: unknown) =>
  v === undefined || v === null || (typeof v === "object" && Object.keys(v as object).length === 0);

/** Turn the editor's JSON into an API request. Throws a readable `Error` when invalid. */
export function parseMongoQuery(text: string, database: string): MongoExecuteRequest {
  let doc: Record<string, unknown>;
  try {
    doc = JSON.parse(text) as Record<string, unknown>;
  } catch (e) {
    throw new Error(t("workspace.invalidJson", { msg: (e as Error).message }), { cause: e });
  }
  if (typeof doc !== "object" || doc === null || Array.isArray(doc))
    throw new Error(t("workspace.mustBeObject"));
  const kind = doc["kind"];
  if (kind !== "find" && kind !== "aggregate") throw new Error(t("workspace.kindRequired"));
  const collection = doc["collection"];
  if (typeof collection !== "string" || !collection)
    throw new Error(t("workspace.collectionRequired"));
  const req: MongoExecuteRequest = { database, collection, kind };
  if (kind === "find") {
    if (!isEmpty(doc["filter"])) req.filter = doc["filter"];
    if (!isEmpty(doc["sort"])) req.sort = doc["sort"];
    if (!isEmpty(doc["projection"])) req.projection = doc["projection"];
  } else {
    if (!Array.isArray(doc["pipeline"])) throw new Error(t("workspace.pipelineRequired"));
    req.pipeline = doc["pipeline"];
  }
  if (typeof doc["limit"] === "number") req.limit = doc["limit"];
  return req;
}

export function MongoWorkspace() {
  const { cid = "" } = useParams();
  const qc = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const connection = connections.data?.find((c) => c.id === cid);
  const databases = useQuery({
    queryKey: ["databases", cid, "mongo"],
    queryFn: () => explorerApi.mongoDatabases(cid),
    enabled: !!connection,
  });
  const blank = useCallback(() => FIND_TEMPLATE, []);
  const tabs = useTabs(`dbwrench.mongo.tabs.${cid}`, blank);
  const database = tabs.active.database ?? connection?.database ?? databases.data?.[0]?.name ?? "";
  const collections = useQuery({
    queryKey: ["collections", cid, database],
    queryFn: () => explorerApi.mongoCollections(cid, database),
    enabled: !!connection && !!database,
  });

  const [result, setResult] = useState<MongoExecuteResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [running, setRunning] = useState<{ op: string; startedAt: number } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setElapsed(Date.now() - running.startedAt), 200);
    return () => clearInterval(id);
  }, [running]);

  const text = tabs.active.text;
  const setCollection = (name: string) => {
    try {
      const doc = JSON.parse(text) as Record<string, unknown>;
      tabs.update({ text: JSON.stringify({ ...doc, collection: name }, null, 2) });
    } catch {
      /* leave invalid JSON alone */
    }
  };

  const run = useCallback(async () => {
    if (!connection || running) return;
    let body: MongoExecuteRequest;
    try {
      body = parseMongoQuery(text, database);
    } catch (e) {
      setError(e);
      return;
    }
    const op = crypto.randomUUID();
    cancelled.current = false;
    setRunning({ op, startedAt: Date.now() });
    setElapsed(0);
    setError(null);
    setNotice(null);
    try {
      setResult(await queryApi.mongoExecute(cid, { ...body, operation_id: op }));
    } catch (e) {
      if (e instanceof ApiError && e.code === "cancelled") setNotice(t("workspace.cancelled"));
      else {
        setResult(null);
        setError(e);
      }
    } finally {
      setRunning(null);
      await qc.invalidateQueries({ queryKey: ["query-history", cid] });
    }
  }, [connection, running, text, database, cid, qc]);

  async function cancel() {
    if (!running) return;
    cancelled.current = true;
    for (let i = 0; i < 8; i++) {
      try {
        await queryApi.cancel(cid, running.op);
        return;
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 409 || !/starting/i.test(e.message)) return;
        await sleep(300);
      }
    }
  }

  const current = useMemo(() => ({ statement: text, database, schema: null }), [text, database]);

  if (connections.isLoading)
    return (
      <div className="p-6">
        <Spinner label={t("common.loading")} />
      </div>
    );
  if (!connection)
    return (
      <div className="p-6">
        <Alert variant="destructive" title={t("explorer.notFound")} />
      </div>
    );
  if (connection.capabilities.family !== "document")
    return (
      <div className="p-6">
        <Alert variant="warning">{t("workspace.notMongo")}</Alert>
      </div>
    );
  if (!canOn(connection, "query.execute_read"))
    return (
      <div className="p-6">
        <Alert variant="warning" title={t("common.forbidden")}>
          {t("workspace.noPermission")}
        </Alert>
      </div>
    );

  return (
    <div className="flex min-h-screen flex-col">
      <EnvBanner
        env={connection.environment}
        name={connection.name}
        readOnly={connection.read_only}
      />
      <div className="grid min-h-0 flex-1 gap-4 p-4 md:p-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-3">
          <PageHeader title={t("workspace.mongoTitle")} description={connection.name} />
          <div
            role="tablist"
            aria-label={t("workspace.tabs")}
            className="flex flex-wrap items-center gap-1"
          >
            {tabs.tabs.map((tab) => (
              <div
                key={tab.id}
                className={cn(
                  "flex items-center rounded-t-md border border-b-0 px-2 py-1 text-sm",
                  tab.id === tabs.active.id
                    ? "bg-card font-medium"
                    : "bg-muted text-muted-foreground",
                )}
              >
                <button
                  role="tab"
                  type="button"
                  aria-selected={tab.id === tabs.active.id}
                  onClick={() => tabs.select(tab.id)}
                >
                  {tab.title}
                </button>
                {tabs.tabs.length > 1 && (
                  <button
                    type="button"
                    className="ml-1 rounded p-0.5 hover:bg-background"
                    aria-label={t("workspace.closeTab", { name: tab.title })}
                    onClick={() => tabs.close(tab.id)}
                  >
                    <X className="size-3" />
                  </button>
                )}
              </div>
            ))}
            <Button
              size="icon"
              variant="ghost"
              aria-label={t("workspace.newTab")}
              onClick={() => tabs.add()}
            >
              <Plus />
            </Button>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              {t("workspace.database")}
              <Select
                className="h-8 w-44"
                value={database}
                onChange={(e) => tabs.update({ database: e.target.value })}
              >
                {(databases.data ?? []).map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </label>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              {t("workspace.collection")}
              <Select
                className="h-8 w-48"
                value=""
                onChange={(e) => e.target.value && setCollection(e.target.value)}
              >
                <option value="">{t("workspace.pickCollection")}</option>
                {(collections.data ?? []).map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </label>
            <Button
              size="sm"
              variant="outline"
              onClick={() => tabs.update({ text: FIND_TEMPLATE })}
            >
              {t("workspace.templateFind")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => tabs.update({ text: AGGREGATE_TEMPLATE })}
            >
              {t("workspace.templateAggregate")}
            </Button>
          </div>

          <LazyEditor
            value={text}
            onChange={(v) => tabs.update({ text: v })}
            language="json"
            ariaLabel={t("workspace.editor")}
            onRun={() => void run()}
          />
          <div className="flex flex-wrap items-center gap-2">
            {!running ? (
              <Button onClick={() => void run()} disabled={!database}>
                <Play /> {t("workspace.run")}
              </Button>
            ) : (
              <Button variant="destructive" onClick={() => void cancel()}>
                <Square /> {t("workspace.cancel")}
              </Button>
            )}
            <span className="text-xs text-muted-foreground">{t("workspace.mongoHint")}</span>
            {running && (
              <Spinner label={t("workspace.running", { s: (elapsed / 1000).toFixed(1) })} />
            )}
          </div>

          {error !== null && <ErrorAlert error={error} />}
          {notice && <Alert variant="info">{notice}</Alert>}
          {result && (
            <section aria-label={t("workspace.results")} className="space-y-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="success">{t("workspace.done")}</Badge>
                <span>{result.elapsed_ms} ms</span>
                <span>{t("docs.count", { n: result.count })}</span>
                {result.truncated && <Badge variant="warning">{t("workspace.truncated")}</Badge>}
              </div>
              {result.documents.length === 0 && <Alert variant="info">{t("docs.empty")}</Alert>}
              <ol className="space-y-2" aria-label={t("docs.list")}>
                {result.documents.map((doc, i) => (
                  <li key={i}>
                    <Card className="p-2">
                      <div className="mb-1 truncate px-1 font-mono text-xs text-muted-foreground">
                        #{i + 1}
                        {doc["_id"] !== undefined
                          ? ` · ${formatExtendedJson(doc["_id"]) ?? JSON.stringify(doc["_id"])}`
                          : ""}
                      </div>
                      <JsonNode value={doc} defaultOpenDepth={1} />
                    </Card>
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
        <aside className="min-w-0 xl:border-l xl:pl-4">
          <QueryPanels
            cid={cid}
            kind="mongo"
            current={current}
            canViewHistory={canOn(connection, "query.view_history")}
            canSave={canOn(connection, "query.manage_saved")}
            onLoad={(q) =>
              tabs.update({
                text: JSON.stringify(JSON.parse(q.statement), null, 2),
                database: q.database ?? undefined,
              })
            }
          />
        </aside>
      </div>
    </div>
  );
}
