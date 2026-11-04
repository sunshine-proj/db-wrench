import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useNavigate, useParams } from "react-router-dom";
import { EnvBanner } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Select } from "@/components/ui/form";
import { Alert, PageHeader, Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import { connectionsApi } from "@/services/api";
import { canOn } from "@/hooks/useAuth";
import { ObjectTree } from "./ObjectTree";
import { explorerRoutes } from "./routes";

/** `/explorer`: pick a connection. */
export function ExplorerIndex() {
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const usable = list.data?.filter((c) => c.enabled && canOn(c, "metadata.view")) ?? [];
  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader title={t("explorer.title")} description={t("explorer.pick")} />
      {list.isLoading && <Spinner label={t("common.loading")} />}
      {list.error && <ErrorAlert error={list.error} />}
      {list.data && usable.length === 0 && <Alert variant="info">{t("explorer.none")}</Alert>}
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {usable.map((c) => (
          <li key={c.id}>
            <Link
              to={explorerRoutes.connection(c.id)}
              className="block rounded-lg border bg-card p-3 hover:bg-muted"
            >
              <p className="font-medium">{c.name}</p>
              <p className="text-xs text-muted-foreground">
                {c.db_type} · {c.environment}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Two-pane layout: object tree on the left, selected object on the right. */
export function ExplorerLayout() {
  const { cid = "" } = useParams();
  const nav = useNavigate();
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const connection = list.data?.find((c) => c.id === cid);
  const usable = list.data?.filter((c) => c.enabled && canOn(c, "metadata.view")) ?? [];

  if (list.isLoading)
    return (
      <div className="p-6">
        <Spinner label={t("common.loading")} />
      </div>
    );
  if (list.error)
    return (
      <div className="p-6">
        <ErrorAlert error={list.error} />
      </div>
    );
  if (!connection)
    return (
      <div className="p-6">
        <Alert variant="destructive" title={t("explorer.notFound")} />
      </div>
    );
  if (!connection.enabled || !canOn(connection, "metadata.view")) {
    return (
      <div className="p-6">
        <Alert variant="warning">{t("explorer.unavailable")}</Alert>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <EnvBanner
        env={connection.environment}
        name={connection.name}
        readOnly={connection.read_only}
      />
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <aside className="max-h-72 overflow-auto border-b p-3 lg:max-h-none lg:w-72 lg:shrink-0 lg:border-b-0 lg:border-r">
          <Select
            aria-label={t("explorer.connection")}
            className="mb-3"
            value={cid}
            onChange={(e) => nav(explorerRoutes.connection(e.target.value))}
          >
            {usable.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
          <ObjectTree key={connection.id} connection={connection} />
        </aside>
        <section className="min-w-0 flex-1 p-4 md:p-6">
          <Outlet context={connection} />
        </section>
      </div>
    </div>
  );
}
