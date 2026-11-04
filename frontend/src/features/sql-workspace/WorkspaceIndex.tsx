import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ErrorAlert } from "@/components/ErrorAlert";
import { EnvBadge } from "@/components/EnvBadge";
import { Alert, PageHeader, Spinner } from "@/components/ui/misc";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { connectionsApi } from "@/services/api";
import type { Connection } from "@/types/api";

export const workspacePath = (c: Pick<Connection, "id" | "db_type">) =>
  `/workspace/${c.db_type === "mongodb" ? "mongo" : "sql"}/${encodeURIComponent(c.id)}`;

/** `/workspace`: pick a connection to open its query workspace. */
export function WorkspaceIndex() {
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const usable = list.data?.filter((c) => c.enabled && canOn(c, "query.execute_read")) ?? [];
  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader title={t("workspace.title")} description={t("workspace.pick")} />
      {list.isLoading && <Spinner label={t("common.loading")} />}
      {list.error && <ErrorAlert error={list.error} />}
      {list.data && usable.length === 0 && <Alert variant="info">{t("workspace.none")}</Alert>}
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {usable.map((c) => (
          <li key={c.id}>
            <Link
              to={workspacePath(c)}
              className="block rounded-lg border bg-card p-3 hover:bg-muted"
            >
              <p className="flex items-center justify-between gap-2 font-medium">
                {c.name} <EnvBadge env={c.environment} />
              </p>
              <p className="text-xs text-muted-foreground">
                {c.db_type === "mongodb" ? t("workspace.mongoTitle") : t("workspace.sqlTitle")}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
