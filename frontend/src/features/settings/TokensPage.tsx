import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { connectionsApi, tokensApi } from "@/services/api";
import type { ApiTokenCreated, ApiTokenInfo } from "@/types/api";

function CreateDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (t: ApiTokenCreated) => void;
}) {
  const scopes = useQuery({ queryKey: ["token-scopes"], queryFn: tokensApi.scopes });
  const conns = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const [name, setName] = useState("");
  const [days, setDays] = useState("30");
  const [picked, setPicked] = useState<string[]>(["connection.view", "metadata.view", "data.read"]);
  const [limit, setLimit] = useState<string[]>([]);
  const create = useMutation({
    mutationFn: () =>
      tokensApi.create({
        name,
        scopes: picked,
        connection_ids: limit.length ? limit : null,
        expires_in_days: Number(days),
      }),
    onSuccess: onCreated,
  });
  const toggle = (list: string[], set: (v: string[]) => void, v: string, on: boolean) =>
    set(on ? [...list, v] : list.filter((x) => x !== v));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={t("tokens.create")} className="max-w-2xl">
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <Field label={t("tokens.name")}>
            {(p) => (
              <Input
                {...p}
                required
                maxLength={120}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            )}
          </Field>
          <Field label={t("tokens.expires")}>
            {(p) => (
              <Input
                {...p}
                type="number"
                min={1}
                max={365}
                value={days}
                onChange={(e) => setDays(e.target.value)}
              />
            )}
          </Field>
          <fieldset className="space-y-1">
            <legend className="text-sm font-medium">{t("tokens.scopes")}</legend>
            <div className="grid max-h-48 gap-1 overflow-y-auto sm:grid-cols-2">
              {(scopes.data?.scopes ?? []).map((s) => (
                <Checkbox
                  key={s}
                  label={s}
                  checked={picked.includes(s)}
                  onChange={(e) => toggle(picked, setPicked, s, e.target.checked)}
                />
              ))}
            </div>
          </fieldset>
          <fieldset className="space-y-1">
            <legend className="text-sm font-medium">{t("tokens.connections")}</legend>
            <div className="grid max-h-32 gap-1 overflow-y-auto sm:grid-cols-2">
              {(conns.data ?? []).map((c) => (
                <Checkbox
                  key={c.id}
                  label={c.name}
                  checked={limit.includes(c.id)}
                  onChange={(e) => toggle(limit, setLimit, c.id, e.target.checked)}
                />
              ))}
            </div>
          </fieldset>
          {create.error && <ErrorAlert error={create.error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || !name || picked.length === 0}>
              {t("common.create")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function TokensPage() {
  const qc = useQueryClient();
  const { can } = useAuth();
  const [showAll, setShowAll] = useState(false);
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<ApiTokenCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiTokenInfo | null>(null);
  const list = useQuery({
    queryKey: ["tokens", showAll],
    queryFn: () => tokensApi.list(showAll ? "all" : "mine"),
  });
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("tokens.title")}
        description={t("tokens.description")}
        actions={<Button onClick={() => setCreating(true)}>{t("tokens.create")}</Button>}
      />
      {can("app.manage_users") && (
        <Checkbox
          label={t("tokens.all")}
          checked={showAll}
          onChange={(e) => setShowAll(e.target.checked)}
        />
      )}
      {secret && (
        <Alert variant="warning" title={t("tokens.secretTitle")}>
          <p>{t("tokens.secretBody")}</p>
          <p className="mt-2 flex items-center gap-2">
            <code className="break-all rounded bg-muted px-2 py-1 font-mono text-xs">
              {secret.secret}
            </code>
            <Button
              size="sm"
              variant="outline"
              onClick={() => navigator.clipboard?.writeText(secret.secret)}
            >
              {t("common.copy")}
            </Button>
          </p>
        </Alert>
      )}
      {list.isLoading && <Spinner label={t("common.loading")} />}
      {list.error && <ErrorAlert error={list.error} />}
      {list.data && (
        <Table>
          <thead>
            <tr>
              <Th>{t("tokens.name")}</Th>
              <Th>{t("tokens.owner")}</Th>
              <Th>{t("tokens.scopes")}</Th>
              <Th>{t("tokens.expires")}</Th>
              <Th>{t("tokens.lastUsed")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {list.data.map((k) => (
              <tr key={k.id}>
                <Td>
                  <div className="font-medium">{k.name}</div>
                  <div className="font-mono text-xs text-muted-foreground">{k.prefix}…</div>
                </Td>
                <Td>{k.owner}</Td>
                <Td className="max-w-xs text-xs">{k.scopes.join(", ")}</Td>
                <Td>
                  <Badge variant={k.active ? "success" : "secondary"}>
                    {k.active ? t("tokens.active") : t("tokens.inactive")}
                  </Badge>
                  <div className="text-xs text-muted-foreground">
                    {new Date(k.expires_at).toLocaleDateString()}
                  </div>
                </Td>
                <Td className="text-xs">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : t("tokens.never")}
                </Td>
                <Td>
                  {k.active && (
                    <Button size="sm" variant="outline" onClick={() => setRevoking(k)}>
                      {t("tokens.revoke")}
                    </Button>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {creating && (
        <CreateDialog
          onClose={() => setCreating(false)}
          onCreated={async (tok) => {
            setCreating(false);
            setSecret(tok);
            await qc.invalidateQueries({ queryKey: ["tokens"] });
          }}
        />
      )}
      {revoking && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setRevoking(null)}
          title={t("tokens.revoke")}
          description={t("tokens.revokeConfirm", { name: revoking.name })}
          confirmLabel={t("tokens.revoke")}
          onConfirm={async () => {
            await tokensApi.revoke(revoking.id);
            await qc.invalidateQueries({ queryKey: ["tokens"] });
          }}
        />
      )}
    </div>
  );
}
