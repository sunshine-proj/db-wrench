import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { ApprovalPicker } from "@/components/ApprovalPicker";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/misc";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { adminApi, explorerApi, withApproval, type TablePath } from "@/services/api";
import type { Connection } from "@/types/api";

/** Runs an administration call, optionally under an approved request, then refreshes the tree. */
export function useAdminCall<A>(
  operation: string,
  connection: Connection,
  run: (arg: A) => Promise<unknown>,
  onDone?: () => void,
) {
  const qc = useQueryClient();
  const [approvalId, setApprovalId] = useState("");
  const m = useMutation({
    mutationFn: (arg: A) => withApproval(approvalId || undefined, () => run(arg)),
    onSuccess: async () => {
      await qc.invalidateQueries();
      onDone?.();
    },
  });
  const picker = (
    <ApprovalPicker
      connectionId={connection.id}
      operation={operation}
      value={approvalId}
      onChange={setApprovalId}
    />
  );
  return { m, picker };
}

function FormDialog({
  title,
  onClose,
  onSubmit,
  pending,
  error,
  children,
  submitLabel,
}: {
  title: string;
  onClose: () => void;
  onSubmit: () => void;
  pending: boolean;
  error: unknown;
  children: ReactNode;
  submitLabel: string;
}) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={title}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit();
          }}
        >
          {children}
          {error != null && <ErrorAlert error={error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={pending}>
              {submitLabel}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------ databases

export function CreateDatabaseButton({ connection }: { connection: Connection }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [first, setFirst] = useState("");
  const mongo = connection.db_type === "mongodb";
  const { m, picker } = useAdminCall(
    "admin.create_database",
    connection,
    () =>
      mongo
        ? adminApi.mongoCreateDatabase(connection.id, name, first)
        : adminApi.createDatabase(connection.id, name),
    () => setOpen(false),
  );
  if (!canOn(connection, "admin.manage_databases")) return null;
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Plus /> {t("admin.createDatabase")}
      </Button>
      {open && (
        <FormDialog
          title={t("admin.createDatabase")}
          onClose={() => setOpen(false)}
          onSubmit={() => m.mutate(undefined)}
          pending={m.isPending}
          error={m.error}
          submitLabel={t("common.create")}
        >
          <Field label={t("admin.name")}>
            {(p) => (
              <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />
            )}
          </Field>
          {mongo && (
            <Field label={t("admin.firstCollection")}>
              {(p) => (
                <Input {...p} required value={first} onChange={(e) => setFirst(e.target.value)} />
              )}
            </Field>
          )}
          {picker}
        </FormDialog>
      )}
    </>
  );
}

/** Drop a database, schema or collection: the object's name must be typed back, always. */
export function DropButton({
  connection,
  kind,
  name,
  db,
  collection,
}: {
  connection: Connection;
  kind: "database" | "schema" | "collection";
  name: string;
  db?: string;
  collection?: string;
}) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const mongo = connection.db_type === "mongodb";
  const perm = kind === "database" ? "admin.manage_databases" : "admin.manage_schemas";
  const op = `admin.drop_${kind}`;
  const { m, picker } = useAdminCall(
    op,
    connection,
    async () => {
      if (kind === "database")
        return mongo
          ? adminApi.mongoDropDatabase(connection.id, name)
          : adminApi.dropDatabase(connection.id, name);
      if (kind === "schema") return adminApi.dropSchema(connection.id, db ?? "", name);
      return adminApi.mongoDropCollection(connection.id, db ?? "", collection ?? name);
    },
    () => nav(`/explorer/${connection.id}`),
  );
  if (!canOn(connection, perm)) return null;
  const label = t(
    kind === "database"
      ? "admin.dropDatabase"
      : kind === "schema"
        ? "admin.dropSchema"
        : "admin.dropCollection",
  );
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Trash2 /> {label}
      </Button>
      {open && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setOpen(false)}
          title={`${label}: ${name}`}
          description={
            <div className="space-y-2">
              <p>{t("admin.dropWarning", { name })}</p>
              {!mongo && kind !== "collection" && <p>{t("admin.dropRestrict")}</p>}
              {picker}
            </div>
          }
          confirmLabel={label}
          requireText={name}
          onConfirm={() => m.mutateAsync(undefined)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------- schema / collection

export function CreateNamedButton({
  connection,
  kind,
  db,
}: {
  connection: Connection;
  kind: "schema" | "collection";
  db: string;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [capped, setCapped] = useState(false);
  const [size, setSize] = useState("1048576");
  const [maxDocs, setMaxDocs] = useState("");
  const { m, picker } = useAdminCall(
    `admin.create_${kind}`,
    connection,
    () =>
      kind === "schema"
        ? adminApi.createSchema(connection.id, db, name)
        : adminApi.mongoCreateCollection(connection.id, db, {
            name,
            ...(capped
              ? {
                  capped: true,
                  size_bytes: Number(size),
                  ...(maxDocs ? { max_docs: Number(maxDocs) } : {}),
                }
              : {}),
          }),
    () => {
      setOpen(false);
      setName("");
    },
  );
  if (!canOn(connection, "admin.manage_schemas")) return null;
  const label = t(kind === "schema" ? "admin.createSchema" : "admin.createCollection");
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Plus /> {label}
      </Button>
      {open && (
        <FormDialog
          title={label}
          onClose={() => setOpen(false)}
          onSubmit={() => m.mutate(undefined)}
          pending={m.isPending}
          error={m.error}
          submitLabel={t("common.create")}
        >
          <Field label={t("admin.name")}>
            {(p) => (
              <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />
            )}
          </Field>
          {kind === "collection" && (
            <>
              <Checkbox
                label={t("admin.capped")}
                checked={capped}
                onChange={(e) => setCapped(e.target.checked)}
              />
              {capped && (
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label={t("admin.sizeBytes")}>
                    {(p) => (
                      <Input
                        {...p}
                        type="number"
                        min={1}
                        value={size}
                        onChange={(e) => setSize(e.target.value)}
                      />
                    )}
                  </Field>
                  <Field label={t("admin.maxDocs")}>
                    {(p) => (
                      <Input
                        {...p}
                        type="number"
                        min={1}
                        value={maxDocs}
                        onChange={(e) => setMaxDocs(e.target.value)}
                      />
                    )}
                  </Field>
                </div>
              )}
            </>
          )}
          {picker}
        </FormDialog>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------- indexes

const csv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

interface IndexRow {
  name: string;
  locked: boolean;
}

/** Index administration for one table or collection. */
export function IndexManager({
  connection,
  table,
  mongo,
  indexes,
}: {
  connection: Connection;
  table?: TablePath;
  mongo?: { db: string; collection: string };
  indexes: IndexRow[];
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [cols, setCols] = useState("");
  const [unique, setUnique] = useState(false);
  const [sparse, setSparse] = useState(false);
  const [ttl, setTtl] = useState("");
  const [dropping, setDropping] = useState<string | null>(null);

  const create = useAdminCall(
    "admin.create_index",
    connection,
    () =>
      mongo
        ? adminApi.mongoCreateIndex(connection.id, mongo.db, mongo.collection, {
            keys: csv(cols).map((c) =>
              c.startsWith("-") ? { field: c.slice(1), direction: -1 } : { field: c, direction: 1 },
            ),
            ...(name ? { name } : {}),
            unique,
            sparse,
            ...(ttl ? { ttl_seconds: Number(ttl) } : {}),
          })
        : adminApi.createIndex(table as TablePath, name, csv(cols), unique),
    () => {
      setOpen(false);
      setName("");
      setCols("");
    },
  );
  const drop = useAdminCall("admin.drop_index", connection, (idx: string) =>
    mongo
      ? adminApi.mongoDropIndex(connection.id, mongo.db, mongo.collection, idx, idx)
      : adminApi.dropIndex(table as TablePath, idx, idx),
  );
  if (!canOn(connection, "admin.manage_indexes")) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          <Plus /> {t("admin.addIndex")}
        </Button>
        {indexes.map((i) =>
          i.locked ? (
            <span key={i.name} className="text-xs text-muted-foreground">
              {i.name} · {t("admin.indexLocked")}
            </span>
          ) : (
            <Button
              key={i.name}
              variant="ghost"
              size="sm"
              aria-label={t("admin.dropIndex", { name: i.name })}
              onClick={() => setDropping(i.name)}
            >
              <Trash2 /> {i.name}
            </Button>
          ),
        )}
      </div>
      {drop.m.error != null && <ErrorAlert error={drop.m.error} />}
      {open && (
        <FormDialog
          title={t("admin.addIndex")}
          onClose={() => setOpen(false)}
          onSubmit={() => create.m.mutate(undefined)}
          pending={create.m.isPending}
          error={create.m.error}
          submitLabel={t("common.create")}
        >
          <Field label={t("admin.indexName")}>
            {(p) => (
              <Input
                {...p}
                required={!mongo}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            )}
          </Field>
          <Field label={t(mongo ? "admin.indexKeys" : "admin.indexColumns")}>
            {(p) => (
              <Input {...p} required value={cols} onChange={(e) => setCols(e.target.value)} />
            )}
          </Field>
          <Checkbox
            label={t("admin.unique")}
            checked={unique}
            onChange={(e) => setUnique(e.target.checked)}
          />
          {mongo && (
            <>
              <Checkbox
                label={t("admin.sparse")}
                checked={sparse}
                onChange={(e) => setSparse(e.target.checked)}
              />
              <Field label={t("admin.ttl")}>
                {(p) => (
                  <Input
                    {...p}
                    type="number"
                    min={0}
                    value={ttl}
                    onChange={(e) => setTtl(e.target.value)}
                  />
                )}
              </Field>
            </>
          )}
          {create.picker}
        </FormDialog>
      )}
      {dropping && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDropping(null)}
          title={t("admin.dropIndex", { name: dropping })}
          description={<div className="space-y-2">{drop.picker}</div>}
          confirmLabel={t("common.delete")}
          requireText={connection.environment === "production" ? dropping : undefined}
          onConfirm={() => drop.m.mutateAsync(dropping)}
        />
      )}
      {create.m.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
    </div>
  );
}

/** Databases of a connection with the structural actions the user is allowed to run. */
export function AdminDatabases({ connection }: { connection: Connection }) {
  const mongo = connection.db_type === "mongodb";
  const caps = connection.capabilities;
  const canAny =
    canOn(connection, "admin.manage_databases") || canOn(connection, "admin.manage_schemas");
  const q = useQuery({
    queryKey: ["databases", connection.id],
    queryFn: () =>
      mongo ? explorerApi.mongoDatabases(connection.id) : explorerApi.databases(connection.id),
    enabled: canAny,
  });
  if (!canAny) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.databases")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {q.error && <ErrorAlert error={q.error} />}
        {(q.data ?? []).map((d) => (
          <div
            key={d.name}
            className="flex flex-wrap items-center justify-between gap-2 border-b pb-2"
          >
            <span className="font-medium">{d.name}</span>
            <div className="flex flex-wrap gap-2">
              {(caps.supports_schemas || mongo) && (
                <CreateNamedButton
                  connection={connection}
                  kind={mongo ? "collection" : "schema"}
                  db={d.name}
                />
              )}
              <DropButton connection={connection} kind="database" name={d.name} />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
