import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input } from "@/components/ui/form";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
} from "@/components/ui/misc";
import { t } from "@/i18n";
import { rolesApi } from "@/services/api";
import type { Role } from "@/types/api";

function groupPermissions(all: string[]): [string, string[]][] {
  const groups = new Map<string, string[]>();
  for (const p of all) {
    const key = p.split(".")[0] ?? p;
    groups.set(key, [...(groups.get(key) ?? []), p]);
  }
  return [...groups.entries()];
}

function CreateRoleDialog({
  permissions,
  onClose,
}: {
  permissions: string[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [requireMfa, setRequireMfa] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const groups = useMemo(() => groupPermissions(permissions), [permissions]);
  const create = useMutation({
    mutationFn: () =>
      rolesApi.create({ name, description, permissions: [...selected], require_mfa: requireMfa }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["roles"] });
      onClose();
    },
  });
  const toggle = (p: string, on: boolean) =>
    setSelected((s) => {
      const n = new Set(s);
      if (on) n.add(p);
      else n.delete(p);
      return n;
    });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={t("roles.create")}
        description={t("roles.createHint")}
        className="max-w-2xl"
      >
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("roles.name")}>
            {(p) => (
              <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />
            )}
          </Field>
          <Field label={t("roles.descriptionLabel")}>
            {(p) => (
              <Input {...p} value={description} onChange={(e) => setDescription(e.target.value)} />
            )}
          </Field>
          <Checkbox
            label={t("roles.requireMfa")}
            checked={requireMfa}
            onChange={(e) => setRequireMfa(e.target.checked)}
          />
          <fieldset className="space-y-3 rounded-md border p-3">
            <legend className="px-1 text-sm font-medium">{t("roles.permissions")}</legend>
            {groups.map(([group, perms]) => (
              <div key={group}>
                <p className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                  {group}
                </p>
                <div className="grid gap-1 sm:grid-cols-2">
                  {perms.map((p) => (
                    <Checkbox
                      key={p}
                      label={p}
                      checked={selected.has(p)}
                      onChange={(e) => toggle(p, e.target.checked)}
                    />
                  ))}
                </div>
              </div>
            ))}
          </fieldset>
          {create.error && <ErrorAlert error={create.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {t("common.create")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function RolesPage() {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list });
  const perms = useQuery({ queryKey: ["permissions"], queryFn: rolesApi.permissions });
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<Role | null>(null);
  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("roles.title")}
        description={t("roles.description")}
        actions={
          <Button onClick={() => setCreating(true)} disabled={!perms.data}>
            <Plus /> {t("roles.create")}
          </Button>
        }
      />
      {(roles.isLoading || perms.isLoading) && <Spinner label={t("common.loading")} />}
      {(roles.error || perms.error) && <ErrorAlert error={roles.error ?? perms.error} />}
      <div className="grid gap-3 lg:grid-cols-2">
        {roles.data?.map((r) => (
          <Card key={r.id}>
            <CardHeader className="flex-row items-start justify-between">
              <div>
                <CardTitle>
                  {r.name}{" "}
                  {r.builtin && (
                    <Badge variant="secondary" className="ml-1">
                      {t("roles.builtin")}
                    </Badge>
                  )}{" "}
                  {r.require_mfa && <Badge variant="outline">MFA</Badge>}
                </CardTitle>
                <p className="text-sm text-muted-foreground">{r.description}</p>
              </div>
              {!r.builtin && (
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={t("common.deleteItem", { name: r.name })}
                  onClick={() => setRemoving(r)}
                >
                  <Trash2 />
                </Button>
              )}
            </CardHeader>
            <CardContent>
              <ul className="flex flex-wrap gap-1" aria-label={t("roles.permissions")}>
                {r.permissions.map((p) => (
                  <li key={p}>
                    <Badge variant="outline" className="font-mono">
                      {p}
                    </Badge>
                  </li>
                ))}
                {r.permissions.length === 0 && (
                  <li className="text-xs text-muted-foreground">{t("roles.noPermissions")}</li>
                )}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
      {creating && perms.data && (
        <CreateRoleDialog permissions={perms.data} onClose={() => setCreating(false)} />
      )}
      {removing && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setRemoving(null)}
          title={t("roles.deleteTitle")}
          description={t("roles.deleteBody", { name: removing.name })}
          confirmLabel={t("common.delete")}
          onConfirm={async () => {
            await rolesApi.remove(removing.id);
            await qc.invalidateQueries({ queryKey: ["roles"] });
          }}
        />
      )}
    </div>
  );
}
