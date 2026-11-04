import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Database, Eye, Folder, Layers, Table2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { cn } from "@/components/cn";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import { explorerApi } from "@/services/api";
import type { Connection, TableKind } from "@/types/api";
import { explorerRoutes as routes } from "./routes";

const KIND_ICON = {
  table: Table2,
  partitioned_table: Table2,
  view: Eye,
  materialized_view: Layers,
  foreign_table: Table2,
} satisfies Record<TableKind, unknown>;

function Branch({
  label,
  icon,
  to,
  children,
}: {
  label: string;
  icon: ReactNode;
  to?: string;
  children: (open: boolean) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li>
      <div className="flex items-center">
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? t("tree.collapse") : t("tree.expand")} ${label}`}
          className="rounded p-1 hover:bg-muted"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </button>
        {to ? (
          <NavLink
            to={to}
            end
            className={({ isActive }) =>
              cn(
                "flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-1 text-sm hover:bg-muted",
                isActive && "bg-accent text-accent-foreground",
              )
            }
          >
            {icon}
            <span className="truncate">{label}</span>
          </NavLink>
        ) : (
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-1 text-left text-sm hover:bg-muted"
            onClick={() => setOpen((o) => !o)}
          >
            {icon}
            <span className="truncate">{label}</span>
          </button>
        )}
      </div>
      {open && <div className="ml-4 border-l pl-1">{children(open)}</div>}
    </li>
  );
}

function Leaf({
  to,
  icon,
  label,
  hint,
}: {
  to: string;
  icon: ReactNode;
  label: string;
  hint?: string;
}) {
  return (
    <li>
      <NavLink
        to={to}
        title={hint}
        className={({ isActive }) =>
          cn(
            "ml-5 flex items-center gap-1.5 rounded px-1 py-1 text-sm hover:bg-muted",
            isActive && "bg-accent text-accent-foreground",
          )
        }
      >
        {icon}
        <span className="truncate">{label}</span>
      </NavLink>
    </li>
  );
}

function Status({
  q,
  empty,
}: {
  q: { isLoading: boolean; error: unknown; isEmpty: boolean };
  empty: string;
}) {
  if (q.isLoading)
    return (
      <div className="ml-5 py-1">
        <Spinner label={t("common.loading")} />
      </div>
    );
  if (q.error)
    return (
      <div className="py-1">
        <ErrorAlert error={q.error} />
      </div>
    );
  if (q.isEmpty) return <p className="ml-5 py-1 text-xs text-muted-foreground">{empty}</p>;
  return null;
}

function TableList({ cid, db, schema }: { cid: string; db: string; schema?: string }) {
  const q = useQuery({
    queryKey: ["tables", cid, db, schema],
    queryFn: () => explorerApi.tables(cid, db, schema),
  });
  return (
    <ul>
      <Status
        q={{ isLoading: q.isLoading, error: q.error, isEmpty: q.data?.length === 0 }}
        empty={t("tree.noTables")}
      />
      {q.data?.map((tb) => {
        const Icon = KIND_ICON[tb.kind];
        return (
          <Leaf
            key={tb.name}
            label={tb.name}
            hint={tb.kind}
            icon={<Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
            to={
              schema !== undefined
                ? routes.schemaTable(cid, db, schema, tb.name)
                : routes.table(cid, db, tb.name)
            }
          />
        );
      })}
    </ul>
  );
}

function SchemaList({ cid, db }: { cid: string; db: string }) {
  const q = useQuery({
    queryKey: ["schemas", cid, db],
    queryFn: () => explorerApi.schemas(cid, db),
  });
  return (
    <ul>
      <Status
        q={{ isLoading: q.isLoading, error: q.error, isEmpty: q.data?.length === 0 }}
        empty={t("tree.noSchemas")}
      />
      {q.data?.map((s) => (
        <Branch
          key={s.name}
          label={s.name}
          to={routes.schema(cid, db, s.name)}
          icon={<Folder className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
        >
          {() => <TableList cid={cid} db={db} schema={s.name} />}
        </Branch>
      ))}
    </ul>
  );
}

function CollectionList({ cid, db }: { cid: string; db: string }) {
  const q = useQuery({
    queryKey: ["collections", cid, db],
    queryFn: () => explorerApi.mongoCollections(cid, db),
  });
  return (
    <ul>
      <Status
        q={{ isLoading: q.isLoading, error: q.error, isEmpty: q.data?.length === 0 }}
        empty={t("tree.noCollections")}
      />
      {q.data?.map((c) => (
        <Leaf
          key={c.name}
          label={c.name}
          hint={c.type}
          icon={
            c.type === "view" ? (
              <Eye className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            ) : (
              <Folder className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            )
          }
          to={routes.collection(cid, db, c.name)}
        />
      ))}
    </ul>
  );
}

export function ObjectTree({ connection }: { connection: Connection }) {
  const cid = connection.id;
  const isDoc = connection.capabilities.family === "document";
  const q = useQuery({
    queryKey: ["databases", cid],
    queryFn: () => (isDoc ? explorerApi.mongoDatabases(cid) : explorerApi.databases(cid)),
  });
  return (
    <nav aria-label={t("tree.label")}>
      <ul>
        <Status
          q={{ isLoading: q.isLoading, error: q.error, isEmpty: q.data?.length === 0 }}
          empty={t("tree.noDatabases")}
        />
        {q.data?.map((d) => (
          <Branch
            key={d.name}
            label={d.name}
            icon={<Database className="size-3.5 shrink-0 text-primary" aria-hidden />}
          >
            {() =>
              isDoc ? (
                <CollectionList cid={cid} db={d.name} />
              ) : connection.capabilities.supports_schemas ? (
                <SchemaList cid={cid} db={d.name} />
              ) : (
                <TableList cid={cid} db={d.name} />
              )
            }
          </Branch>
        ))}
      </ul>
    </nav>
  );
}
