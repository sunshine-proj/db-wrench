import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Play, Plus, Square, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { cn } from "@/components/cn";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EnvBanner } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Checkbox, Select } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner } from "@/components/ui/misc";
import { DataGrid } from "@/features/data-browser/DataGrid";
import { QueryPanels } from "@/features/query-panels/QueryPanels";
import { useTabs } from "@/features/query-panels/useTabs";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { ApiError, connectionsApi, explorerApi, queryApi } from "@/services/api";
import type { ExportFormat, SqlExecuteResult } from "@/types/api";
import { LazyEditor } from "./LazyEditor";

const TIMEOUTS = [10, 30, 60, 120, 300];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

interface PendingConfirm {
  message: string;
  destructive: boolean;
}

export function SqlWorkspace() {
  const { cid = "" } = useParams();
  const qc = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const connection = connections.data?.find((c) => c.id === cid);

  const databases = useQuery({
    queryKey: ["databases", cid],
    queryFn: () => explorerApi.databases(cid),
    enabled: !!connection,
  });
  const blank = useCallback(() => "", []);
  const tabs = useTabs(`dbwrench.sql.tabs.${cid}`, blank);
  const database = tabs.active.database ?? connection?.database ?? databases.data?.[0]?.name ?? "";
  const hasSchemas = connection?.capabilities.supports_schemas ?? false;
  const schemas = useQuery({
    queryKey: ["schemas", cid, database],
    queryFn: () => explorerApi.schemas(cid, database),
    enabled: !!connection && hasSchemas && !!database,
  });
  const schema = hasSchemas
    ? (tabs.active.schema ??
      (schemas.data?.some((s) => s.name === "public") ? "public" : schemas.data?.[0]?.name))
    : undefined;

  const [result, setResult] = useState<SqlExecuteResult | null>(null);
  const [resultTab, setResultTab] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [running, setRunning] = useState<{ op: string; startedAt: number } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [rollback, setRollback] = useState(false);
  const [timeoutS, setTimeoutS] = useState(30);
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const cancelled = useRef(false);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setElapsed(Date.now() - running.startedAt), 200);
    return () => clearInterval(id);
  }, [running]);

  const canRun =
    !!connection &&
    (canOn(connection, "query.execute_read") || canOn(connection, "query.execute_write"));
  const canWrite =
    !!connection && !connection.read_only && canOn(connection, "query.execute_write");
  const production = connection?.environment === "production";
  const sql = tabs.active.text;

  const run = useCallback(
    async (extra: { confirm_destructive?: boolean; confirm?: string } = {}) => {
      if (!connection || running || !sql.trim()) return;
      const op = crypto.randomUUID();
      cancelled.current = false;
      setRunning({ op, startedAt: Date.now() });
      setElapsed(0);
      setError(null);
      setNotice(null);
      try {
        const res = await queryApi.execute(cid, {
          sql,
          database,
          schema,
          operation_id: op,
          rollback: rollback || undefined,
          timeout_s: timeoutS,
          ...extra,
        });
        setResult(res);
        setResultTab(0);
      } catch (e) {
        if (e instanceof ApiError && e.code === "cancelled") setNotice(t("workspace.cancelled"));
        else if (e instanceof ApiError && e.status === 428)
          setPending({ message: e.message, destructive: /destructive/i.test(e.message) });
        else {
          setResult(null);
          setError(e);
        }
      } finally {
        setRunning(null);
        await qc.invalidateQueries({ queryKey: ["query-history", cid] });
      }
    },
    [connection, running, sql, cid, database, schema, rollback, timeoutS, qc],
  );

  async function cancel() {
    if (!running) return;
    cancelled.current = true;
    // The query may still be starting; retry briefly until the backend has a handle to cancel.
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

  async function exportResult(format: ExportFormat) {
    setExporting(true);
    setError(null);
    try {
      await queryApi.exportQuery(cid, { sql, database, schema, format });
    } catch (e) {
      setError(e);
    } finally {
      setExporting(false);
    }
  }

  const current = useMemo(() => ({ statement: sql, database, schema }), [sql, database, schema]);

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
  if (connection.capabilities.family !== "relational")
    return (
      <div className="p-6">
        <Alert variant="warning">{t("workspace.notSql")}</Alert>
      </div>
    );
  if (!canRun)
    return (
      <div className="p-6">
        <Alert variant="warning" title={t("common.forbidden")}>
          {t("workspace.noPermission")}
        </Alert>
      </div>
    );

  const set = result?.results[resultTab];
  const singleRead = result?.statements.length === 1;

  return (
    <div className="flex min-h-screen flex-col">
      <EnvBanner
        env={connection.environment}
        name={connection.name}
        readOnly={connection.read_only}
      />
      <div className="grid min-h-0 flex-1 gap-4 p-4 md:p-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-3">
          <PageHeader title={t("workspace.sqlTitle")} description={connection.name} />

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
                onChange={(e) => tabs.update({ database: e.target.value, schema: undefined })}
              >
                {(databases.data ?? []).map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </label>
            {hasSchemas && (
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                {t("workspace.schema")}
                <Select
                  className="h-8 w-40"
                  value={schema ?? ""}
                  onChange={(e) => tabs.update({ schema: e.target.value })}
                >
                  {(schemas.data ?? []).map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}
                    </option>
                  ))}
                </Select>
              </label>
            )}
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              {t("workspace.timeout")}
              <Select
                className="h-8 w-24"
                value={timeoutS}
                onChange={(e) => setTimeoutS(Number(e.target.value))}
              >
                {TIMEOUTS.map((s) => (
                  <option key={s} value={s}>
                    {s} s
                  </option>
                ))}
              </Select>
            </label>
            {canWrite && (
              <Checkbox
                label={t("workspace.rollback")}
                checked={rollback}
                onChange={(e) => setRollback(e.target.checked)}
              />
            )}
          </div>

          <LazyEditor
            value={sql}
            onChange={(v) => tabs.update({ text: v })}
            language={connection.db_type === "mysql" ? "mysql" : "pgsql"}
            ariaLabel={t("workspace.editor")}
            onRun={() => void run()}
          />

          <div className="flex flex-wrap items-center gap-2">
            {!running ? (
              <Button onClick={() => void run()} disabled={!sql.trim() || !database}>
                <Play /> {t("workspace.run")}
              </Button>
            ) : (
              <Button variant="destructive" onClick={() => void cancel()}>
                <Square /> {t("workspace.cancel")}
              </Button>
            )}
            <span className="text-xs text-muted-foreground">{t("workspace.runHint")}</span>
            {running && (
              <Spinner label={t("workspace.running", { s: (elapsed / 1000).toFixed(1) })} />
            )}
          </div>

          {error !== null && <ErrorAlert error={error} />}
          {notice && <Alert variant="info">{notice}</Alert>}

          {result && set && (
            <section aria-label={t("workspace.results")} className="space-y-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="success">{t("workspace.done")}</Badge>
                <span>{result.elapsed_ms} ms</span>
                {set.columns.length > 0 ? (
                  <span>{t("panels.rows", { n: set.rows.length })}</span>
                ) : (
                  set.row_count >= 0 && <span>{t("workspace.affected", { n: set.row_count })}</span>
                )}
                {set.truncated && <Badge variant="warning">{t("workspace.truncated")}</Badge>}
                {result.committed && <Badge>{t("workspace.committed")}</Badge>}
                {rollback && result.statements.length > 0 && !result.committed && canWrite && (
                  <Badge variant="secondary">{t("workspace.rolledBack")}</Badge>
                )}
                {singleRead && set.columns.length > 0 && canOn(connection, "export.export") && (
                  <span className="ml-auto flex gap-1">
                    {(["csv", "json"] as const).map((f) => (
                      <Button
                        key={f}
                        size="sm"
                        variant="outline"
                        disabled={exporting}
                        onClick={() => void exportResult(f)}
                      >
                        <Download /> {t(`export.${f}`)}
                      </Button>
                    ))}
                  </span>
                )}
              </div>
              {result.results.length > 1 && (
                <div role="tablist" className="flex gap-1">
                  {result.results.map((_, i) => (
                    <button
                      key={i}
                      role="tab"
                      type="button"
                      aria-selected={i === resultTab}
                      onClick={() => setResultTab(i)}
                      className={cn(
                        "rounded border px-2 py-1 text-xs",
                        i === resultTab && "bg-accent",
                      )}
                    >
                      {t("workspace.resultN", { n: i + 1 })}
                    </button>
                  ))}
                </div>
              )}
              {set.columns.length > 0 ? (
                <DataGrid columns={set.columns} rows={set.rows} selectable={false} />
              ) : (
                <Alert variant="success">{t("workspace.noRows")}</Alert>
              )}
            </section>
          )}
        </div>

        <aside className="min-w-0 xl:border-l xl:pl-4">
          <QueryPanels
            cid={cid}
            kind="sql"
            current={current}
            canViewHistory={canOn(connection, "query.view_history")}
            canSave={canOn(connection, "query.manage_saved")}
            onLoad={(q) =>
              tabs.update({
                text: q.statement,
                database: q.database ?? undefined,
                schema: q.schema ?? undefined,
              })
            }
          />
        </aside>
      </div>

      {pending && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setPending(null)}
          title={
            pending.destructive ? t("workspace.confirmDestructive") : t("workspace.confirmWrite")
          }
          description={pending.message}
          confirmLabel={t("workspace.runAnyway")}
          requireText={production ? connection.name : undefined}
          onConfirm={async () => {
            setPending(null);
            await run({
              confirm_destructive: pending.destructive || undefined,
              confirm: production ? connection.name : undefined,
            });
          }}
        />
      )}
    </div>
  );
}
