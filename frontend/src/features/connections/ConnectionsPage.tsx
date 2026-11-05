import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Database,
  Pencil,
  Plus,
  ShieldCheck,
  Trash2,
  XCircle,
  Zap,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EnvBadge } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Alert, Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { useAuth, canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { connectionsApi } from "@/services/api";
import type { Connection, TestResult } from "@/types/api";
import { ConnectionFormDialog } from "./ConnectionForm";
import { CredentialsDialog } from "./CredentialsDialog";
import { GrantsDialog } from "./GrantsDialog";
import { ScopeGrantsDialog } from "./ScopeGrantsDialog";

const ENGINE_LABEL = { postgresql: "PostgreSQL", mysql: "MySQL", mongodb: "MongoDB" } as const;

export function ConnectionsPage() {
  const qc = useQueryClient();
  const { can } = useAuth();
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const [editing, setEditing] = useState<Connection | "new" | null>(null);
  const [access, setAccess] = useState<Connection | null>(null);
  const [scoped, setScoped] = useState<Connection | null>(null);
  const [creds, setCreds] = useState<Connection | null>(null);
  const [removing, setRemoving] = useState<Connection | null>(null);
  const [results, setResults] = useState<Record<string, TestResult>>({});

  const test = useMutation({
    mutationFn: async (c: Connection) => ({ id: c.id, result: await connectionsApi.test(c.id) }),
    onSuccess: ({ id, result }) => setResults((r) => ({ ...r, [id]: result })),
  });

  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("connections.title")}
        description={t("connections.description")}
        actions={
          can("connection.create") && (
            <Button onClick={() => setEditing("new")}>
              <Plus /> {t("connections.create")}
            </Button>
          )
        }
      />
      {list.isLoading && <Spinner label={t("common.loading")} />}
      {list.error && <ErrorAlert error={list.error} />}
      {test.error && <ErrorAlert error={test.error} />}
      {list.data?.length === 0 && <Alert variant="info">{t("connections.empty")}</Alert>}
      {list.data && list.data.length > 0 && (
        <Table>
          <thead>
            <tr>
              <Th>{t("connections.name")}</Th>
              <Th>{t("connections.type")}</Th>
              <Th>{t("connections.environment")}</Th>
              <Th>{t("connections.address")}</Th>
              <Th>{t("connections.status")}</Th>
              <Th>
                <span className="sr-only">{t("common.actions")}</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((c) => {
              const r = results[c.id];
              return (
                <tr key={c.id}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td>{ENGINE_LABEL[c.db_type]}</Td>
                  <Td>
                    <EnvBadge env={c.environment} />
                  </Td>
                  <Td className="font-mono text-xs">
                    {c.host}:{c.port}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {!c.enabled && <Badge variant="secondary">{t("connections.disabled")}</Badge>}
                      {c.read_only && (
                        <Badge variant="outline">
                          <ShieldCheck className="mr-1 size-3" aria-hidden />
                          {t("connections.readOnlyBadge")}
                        </Badge>
                      )}
                      {r && (
                        <span
                          className="inline-flex items-center gap-1 text-xs"
                          title={r.error ?? r.server_version ?? ""}
                        >
                          {r.ok ? (
                            <CheckCircle2 className="size-4 text-success" aria-hidden />
                          ) : (
                            <XCircle className="size-4 text-destructive" aria-hidden />
                          )}
                          {r.ok ? `${r.latency_ms} ms` : (r.error_code ?? t("common.error"))}
                        </span>
                      )}
                    </div>
                    {r && !r.ok && r.error && (
                      <p className="mt-1 max-w-xs break-words text-xs text-destructive">
                        {r.error}
                      </p>
                    )}
                    {r?.ok && r.server_version && (
                      <p
                        className="mt-1 max-w-xs truncate text-xs text-muted-foreground"
                        title={r.server_version}
                      >
                        {r.server_version}
                      </p>
                    )}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap justify-end gap-1">
                      {canOn(c, "metadata.view") && c.enabled && (
                        <Button asChild size="sm" variant="outline">
                          <Link to={`/explorer/${c.id}`}>
                            <Database /> {t("connections.browse")}
                          </Link>
                        </Button>
                      )}
                      {canOn(c, "connection.test") && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => test.mutate(c)}
                          disabled={test.isPending && test.variables?.id === c.id}
                        >
                          <Zap /> {t("connections.test")}
                        </Button>
                      )}
                      {c.credential_model === "per_user" && (
                        <Button size="sm" variant="outline" onClick={() => setCreds(c)}>
                          {t("credentials.open")}
                        </Button>
                      )}
                      {canOn(c, "connection.manage_access") && (
                        <Button size="sm" variant="outline" onClick={() => setAccess(c)}>
                          {t("connections.access")}
                        </Button>
                      )}
                      {canOn(c, "connection.manage_access") && (
                        <Button size="sm" variant="outline" onClick={() => setScoped(c)}>
                          {t("scope.open")}
                        </Button>
                      )}
                      {canOn(c, "connection.edit") && (
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t("common.editItem", { name: c.name })}
                          onClick={() => setEditing(c)}
                        >
                          <Pencil />
                        </Button>
                      )}
                      {canOn(c, "connection.delete") && (
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t("common.deleteItem", { name: c.name })}
                          onClick={() => setRemoving(c)}
                        >
                          <Trash2 />
                        </Button>
                      )}
                    </div>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}

      {editing !== null && (
        <ConnectionFormDialog
          key={editing === "new" ? "new" : `${editing.id}-${editing.updated_at}`}
          open
          onOpenChange={(o) => !o && setEditing(null)}
          connection={editing === "new" ? undefined : editing}
        />
      )}
      {access && (
        <GrantsDialog connection={access} open onOpenChange={(o) => !o && setAccess(null)} />
      )}
      {scoped && <ScopeGrantsDialog connection={scoped} onClose={() => setScoped(null)} />}
      {creds && <CredentialsDialog connection={creds} onClose={() => setCreds(null)} />}
      {removing && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setRemoving(null)}
          title={t("connections.deleteTitle")}
          description={t("connections.deleteBody", { name: removing.name })}
          confirmLabel={t("common.delete")}
          requireText={removing.environment === "production" ? removing.name : undefined}
          onConfirm={async () => {
            await connectionsApi.remove(removing.id);
            await qc.invalidateQueries({ queryKey: ["connections"] });
          }}
        />
      )}
    </div>
  );
}
