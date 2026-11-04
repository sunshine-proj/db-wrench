import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";
import { cn } from "@/components/cn";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import {
  Alert,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
  Table,
  Td,
  Th,
} from "@/components/ui/misc";
import { DocumentBrowser } from "@/features/data-browser/DocumentBrowser";
import { JsonNode } from "@/features/data-browser/JsonTree";
import { RowBrowser } from "@/features/data-browser/RowBrowser";
import { canOn } from "@/hooks/useAuth";
import { formatBytes } from "@/lib/format";
import { t, type MessageKey } from "@/i18n";
import { connectionsApi, explorerApi, type TablePath } from "@/services/api";
import type { Connection, FunctionInfo, TestResult } from "@/types/api";
import { workspacePath } from "@/features/sql-workspace/WorkspaceIndex";
import { explorerRoutes } from "./routes";
import { StructureView } from "./StructureView";
import { useRecent } from "./useRecent";

function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: MessageKey }[];
  value: T;
  onChange: (t: T) => void;
}) {
  return (
    <div role="tablist" className="mb-4 flex gap-1 border-b">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          type="button"
          aria-selected={value === tab.id}
          className={cn(
            "-mb-px border-b-2 px-3 py-2 text-sm font-medium",
            value === tab.id
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
          onClick={() => onChange(tab.id)}
        >
          {t(tab.label)}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- connection home

export function ConnectionHome() {
  const c = useOutletContext<Connection>();
  const [result, setResult] = useState<TestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const caps = c.capabilities;
  const rows: [string, string][] = [
    [t("connections.type"), c.db_type],
    [t("connections.address"), `${c.host}:${c.port}`],
    [t("connections.database"), c.database ?? "—"],
    [t("connections.tls"), t(`tls.${c.tls_mode}`)],
    [t("connections.poolMax"), String(c.pool_max)],
    [t("explorer.access"), c.read_only ? t("connections.readOnlyBadge") : t("explorer.readWrite")],
  ];
  return (
    <div className="space-y-4">
      <PageHeader
        title={c.name}
        description={t("explorer.homeHint")}
        actions={
          <>
            {canOn(c, "query.execute_read") && (
              <Button asChild variant="outline">
                <Link to={workspacePath(c)}>{t("workspace.open")}</Link>
              </Button>
            )}
            {canOn(c, "connection.test") && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    setResult(await connectionsApi.test(c.id));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {t("connections.test")}
              </Button>
            )}
          </>
        }
      />
      {result && (
        <Alert
          variant={result.ok ? "success" : "destructive"}
          title={
            result.ok
              ? t("connections.testOk", { ms: result.latency_ms ?? 0 })
              : t("connections.testFailed")
          }
        >
          {result.ok ? result.server_version : result.error}
        </Alert>
      )}
      <Card>
        <CardContent className="pt-4">
          <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
            {rows.map(([k, v]) => (
              <div key={k}>
                <dt className="text-xs text-muted-foreground">{k}</dt>
                <dd className="text-sm font-medium">{v}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("explorer.capabilities")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-1.5">
          {(
            [
              ["supports_schemas", "cap.schemas"],
              ["supports_views", "cap.views"],
              ["supports_sequences", "cap.sequences"],
              ["supports_functions", "cap.functions"],
              ["supports_foreign_keys", "cap.foreignKeys"],
              ["supports_indexes", "cap.indexes"],
            ] as const
          ).map(([key, label]) => (
            <Badge key={key} variant={caps[key] ? "default" : "secondary"}>
              {t(label)}
              {caps[key] ? "" : ` — ${t("cap.unsupported")}`}
            </Badge>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- schema overview (PostgreSQL)

function FunctionDialog({
  cid,
  db,
  schema,
  fn,
  onClose,
}: {
  cid: string;
  db: string;
  schema: string;
  fn: FunctionInfo;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["function", cid, db, schema, fn.id],
    queryFn: () => explorerApi.fn(cid, db, schema, fn.id),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={`${fn.name}(${fn.arguments})`} className="max-w-3xl">
        {q.isLoading && <Spinner label={t("common.loading")} />}
        {q.error && <ErrorAlert error={q.error} />}
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs">
          {q.data?.definition ?? t("schema.noDefinition")}
        </pre>
      </DialogContent>
    </Dialog>
  );
}

export function SchemaOverview() {
  const { cid = "", db = "", schema = "" } = useParams();
  const [tab, setTab] = useState<"tables" | "sequences" | "functions">("tables");
  const [open, setOpen] = useState<FunctionInfo | null>(null);
  const tables = useQuery({
    queryKey: ["tables", cid, db, schema],
    queryFn: () => explorerApi.tables(cid, db, schema),
  });
  const seqs = useQuery({
    queryKey: ["sequences", cid, db, schema],
    queryFn: () => explorerApi.sequences(cid, db, schema),
    enabled: tab === "sequences",
  });
  const fns = useQuery({
    queryKey: ["functions", cid, db, schema],
    queryFn: () => explorerApi.functions(cid, db, schema),
    enabled: tab === "functions",
  });
  return (
    <div>
      <PageHeader title={`${db}.${schema}`} description={t("schema.description")} />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "tables", label: "schema.tables" },
          { id: "sequences", label: "schema.sequences" },
          { id: "functions", label: "schema.functions" },
        ]}
      />
      {tab === "tables" &&
        (tables.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : tables.error ? (
          <ErrorAlert error={tables.error} />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("structure.kind")}</Th>
                <Th>{t("structure.estimatedRows")}</Th>
                <Th>{t("structure.comment")}</Th>
              </tr>
            </thead>
            <tbody>
              {tables.data?.map((tb) => (
                <tr key={tb.name}>
                  <Td className="font-medium">
                    <Link
                      className="text-primary hover:underline"
                      to={explorerRoutes.schemaTable(cid, db, schema, tb.name)}
                    >
                      {tb.name}
                    </Link>
                  </Td>
                  <Td>
                    <Badge variant="outline">{tb.kind}</Badge>
                  </Td>
                  <Td className="font-mono text-xs">
                    {tb.estimated_rows?.toLocaleString() ?? "—"}
                  </Td>
                  <Td className="text-xs text-muted-foreground">{tb.comment ?? ""}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ))}
      {tab === "sequences" &&
        (seqs.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : seqs.error ? (
          <ErrorAlert error={seqs.error} />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("structure.type")}</Th>
                <Th>{t("schema.start")}</Th>
                <Th>{t("schema.increment")}</Th>
                <Th>{t("schema.min")}</Th>
                <Th>{t("schema.max")}</Th>
                <Th>{t("schema.cycle")}</Th>
              </tr>
            </thead>
            <tbody>
              {seqs.data?.map((s) => (
                <tr key={s.name}>
                  <Td className="font-medium">{s.name}</Td>
                  <Td className="font-mono text-xs">{s.data_type}</Td>
                  <Td>{String(s.start)}</Td>
                  <Td>{String(s.increment)}</Td>
                  <Td>{String(s.min_value)}</Td>
                  <Td>{String(s.max_value)}</Td>
                  <Td>{s.cycle ? t("common.yes") : t("common.no")}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ))}
      {tab === "functions" &&
        (fns.isLoading ? (
          <Spinner label={t("common.loading")} />
        ) : fns.error ? (
          <ErrorAlert error={fns.error} />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("schema.arguments")}</Th>
                <Th>{t("schema.returns")}</Th>
                <Th>{t("schema.language")}</Th>
                <Th>{t("structure.kind")}</Th>
              </tr>
            </thead>
            <tbody>
              {fns.data?.map((f) => (
                <tr key={f.id}>
                  <Td className="font-medium">
                    <button
                      type="button"
                      className="text-primary hover:underline"
                      onClick={() => setOpen(f)}
                    >
                      {f.name}
                    </button>
                  </Td>
                  <Td className="font-mono text-xs">{f.arguments}</Td>
                  <Td className="font-mono text-xs">{f.returns ?? "—"}</Td>
                  <Td>{f.language}</Td>
                  <Td>
                    <Badge variant="outline">{f.kind}</Badge>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ))}
      {open && (
        <FunctionDialog cid={cid} db={db} schema={schema} fn={open} onClose={() => setOpen(null)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- table view

export function TableView() {
  const c = useOutletContext<Connection>();
  const { cid = "", db = "", schema, table = "" } = useParams();
  const [tab, setTab] = useState<"data" | "structure">("data");
  const path: TablePath = { cid, db, schema, table };
  const structure = useQuery({
    queryKey: ["structure", path],
    queryFn: () => explorerApi.structure(path),
    retry: false,
  });
  const { push } = useRecent();
  const canRead = canOn(c, "data.read");

  useEffect(() => {
    push({
      to:
        schema !== undefined
          ? explorerRoutes.schemaTable(cid, db, schema, table)
          : explorerRoutes.table(cid, db, table),
      label: [db, schema, table].filter(Boolean).join("."),
      connection: c.name,
    });
  }, [cid, db, schema, table, c.name, push]);

  if (structure.isLoading) return <Spinner label={t("common.loading")} />;
  if (structure.error) return <ErrorAlert error={structure.error} />;
  const s = structure.data;
  if (!s) return null;
  return (
    <div>
      <PageHeader
        title={[db, schema, table].filter(Boolean).join(".")}
        description={s.comment ?? undefined}
        actions={
          <>
            <Badge variant="outline">{s.kind}</Badge>
            {s.estimated_rows !== null && (
              <Badge variant="secondary">
                ~{s.estimated_rows.toLocaleString()} {t("structure.rows")}
              </Badge>
            )}
          </>
        }
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "data", label: "table.data" },
          { id: "structure", label: "table.structure" },
        ]}
      />
      {tab === "data" &&
        (canRead ? (
          <RowBrowser path={path} structure={s} connection={c} />
        ) : (
          <Alert variant="warning">{t("table.noDataPermission")}</Alert>
        ))}
      {tab === "structure" && <StructureView structure={s} connection={c} db={db} />}
    </div>
  );
}

// ---------------------------------------------------------------- collection view

export function CollectionView() {
  const c = useOutletContext<Connection>();
  const { cid = "", db = "", coll = "" } = useParams();
  const [tab, setTab] = useState<"documents" | "metadata">("documents");
  const detail = useQuery({
    queryKey: ["collection", cid, db, coll],
    queryFn: () => explorerApi.mongoCollection(cid, db, coll),
    retry: false,
  });
  const { push } = useRecent();
  useEffect(() => {
    push({
      to: explorerRoutes.collection(cid, db, coll),
      label: `${db}.${coll}`,
      connection: c.name,
    });
  }, [cid, db, coll, c.name, push]);

  if (detail.isLoading) return <Spinner label={t("common.loading")} />;
  if (detail.error) return <ErrorAlert error={detail.error} />;
  const d = detail.data;
  if (!d) return null;
  const stat = (k: string) => d.stats[k];
  return (
    <div>
      <PageHeader
        title={`${db}.${coll}`}
        actions={
          <>
            <Badge variant="outline">{d.type}</Badge>
            {d.capped && <Badge variant="secondary">capped</Badge>}
          </>
        }
      />
      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "documents", label: "collection.documents" },
          { id: "metadata", label: "collection.metadata" },
        ]}
      />
      {tab === "documents" &&
        (canOn(c, "data.read") ? (
          <DocumentBrowser cid={cid} db={db} collection={coll} connection={c} />
        ) : (
          <Alert variant="warning">{t("table.noDataPermission")}</Alert>
        ))}
      {tab === "metadata" && (
        <div className="space-y-4">
          {d.type === "collection" && (
            <Card>
              <CardHeader>
                <CardTitle>{t("collection.stats")}</CardTitle>
              </CardHeader>
              <CardContent>
                <dl className="grid gap-3 sm:grid-cols-3">
                  {(
                    [
                      ["collection.count", stat("count"), false],
                      ["collection.size", stat("size"), true],
                      ["collection.avgObj", stat("avg_obj_size"), true],
                      ["collection.storage", stat("storage_size"), true],
                      ["collection.indexSize", stat("total_index_size"), true],
                      ["collection.indexCount", stat("index_count"), false],
                    ] as const
                  ).map(([label, v, bytes]) => (
                    <div key={label}>
                      <dt className="text-xs text-muted-foreground">{t(label)}</dt>
                      <dd className="text-sm font-medium">
                        {typeof v === "number"
                          ? bytes
                            ? formatBytes(v)
                            : v.toLocaleString()
                          : "—"}
                      </dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            </Card>
          )}
          <section className="space-y-2">
            <h3 className="text-sm font-semibold">{t("structure.indexes")}</h3>
            {d.indexes.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("structure.noIndexes")}</p>
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>{t("structure.name")}</Th>
                    <Th>{t("collection.keys")}</Th>
                    <Th>{t("structure.flags")}</Th>
                  </tr>
                </thead>
                <tbody>
                  {d.indexes.map((i) => (
                    <tr key={i.name}>
                      <Td className="font-medium">{i.name}</Td>
                      <Td className="font-mono text-xs">
                        {i.keys.map(([k, v]) => `${k}: ${v}`).join(", ")}
                      </Td>
                      <Td className="space-x-1">
                        {i.unique && <Badge variant="secondary">UNIQUE</Badge>}
                        {i.sparse && <Badge variant="secondary">SPARSE</Badge>}
                        {i.ttl_seconds !== null && (
                          <Badge variant="outline">TTL {i.ttl_seconds}s</Badge>
                        )}
                        {i.partial_filter && <Badge variant="outline">PARTIAL</Badge>}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </section>
          {Object.keys(d.options).length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>{t("collection.options")}</CardTitle>
              </CardHeader>
              <CardContent>
                <JsonNode value={d.options} defaultOpenDepth={2} />
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
