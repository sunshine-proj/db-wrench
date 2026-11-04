import { useQuery } from "@tanstack/react-query";
import { Clock, Database, Plug, ScrollText } from "lucide-react";
import { Link } from "react-router-dom";
import { EnvBadge } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
} from "@/components/ui/misc";
import { useRecent } from "@/features/explorer/useRecent";
import { explorerRoutes } from "@/features/explorer/routes";
import { canOn, useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { auditApi, connectionsApi } from "@/services/api";

export function DashboardPage() {
  const { session, can } = useAuth();
  const connections = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const audit = useQuery({
    queryKey: ["audit", "dashboard"],
    queryFn: () => auditApi.events({ limit: 8 }),
    enabled: can("audit.view"),
  });
  const { items: recent } = useRecent();

  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("dashboard.title", {
          name: session?.user.display_name || session?.user.username || "",
        })}
        description={t("dashboard.description")}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Plug className="size-4" aria-hidden /> {t("nav.connections")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {connections.isLoading && <Spinner label={t("common.loading")} />}
            {connections.error && <ErrorAlert error={connections.error} />}
            {connections.data?.length === 0 && (
              <p className="text-sm text-muted-foreground">{t("connections.empty")}</p>
            )}
            <ul className="divide-y">
              {connections.data?.map((c) => (
                <li key={c.id} className="flex items-center justify-between gap-2 py-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{c.name}</p>
                    <p className="truncate font-mono text-xs text-muted-foreground">
                      {c.db_type} · {c.host}:{c.port}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <EnvBadge env={c.environment} />
                    {!c.enabled && <Badge variant="secondary">{t("connections.disabled")}</Badge>}
                    {c.enabled && canOn(c, "metadata.view") && (
                      <Link
                        className="rounded p-1 text-primary hover:bg-muted"
                        to={explorerRoutes.connection(c.id)}
                        aria-label={t("connections.browseNamed", { name: c.name })}
                      >
                        <Database className="size-4" />
                      </Link>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Clock className="size-4" aria-hidden /> {t("dashboard.recent")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {recent.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("dashboard.recentEmpty")}</p>
            ) : (
              <ul className="divide-y">
                {recent.map((r) => (
                  <li key={r.to} className="py-2">
                    <Link to={r.to} className="text-sm font-medium text-primary hover:underline">
                      {r.label}
                    </Link>
                    <p className="text-xs text-muted-foreground">{r.connection}</p>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {can("audit.view") && (
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ScrollText className="size-4" aria-hidden /> {t("dashboard.activity")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {audit.isLoading && <Spinner label={t("common.loading")} />}
              {audit.error && <ErrorAlert error={audit.error} />}
              <ul className="divide-y">
                {audit.data?.events.map((e) => (
                  <li
                    key={e.id}
                    className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
                  >
                    <span>
                      <span className="font-mono text-xs">{e.action}</span> ·{" "}
                      {e.actor_username ?? "—"}
                    </span>
                    <span className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Badge
                        variant={
                          e.result === "success"
                            ? "success"
                            : e.result === "denied"
                              ? "warning"
                              : "destructive"
                        }
                      >
                        {e.result}
                      </Badge>
                      {new Date(e.ts).toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
              <Link to="/audit" className="mt-2 inline-block text-sm text-primary hover:underline">
                {t("dashboard.allActivity")}
              </Link>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
