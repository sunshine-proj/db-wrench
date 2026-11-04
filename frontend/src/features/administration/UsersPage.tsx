import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Pencil, Plus, ShieldOff, UserX } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/form";
import { Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { t } from "@/i18n";
import { rolesApi, usersApi } from "@/services/api";
import type { Role, User } from "@/types/api";

function CreateUserDialog({ roles, onClose }: { roles: Role[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState({
    username: "",
    password: "",
    email: "",
    display_name: "",
    role_id: roles.find((r) => r.name === "Viewer")?.id ?? roles[0]?.id ?? "",
  });
  const create = useMutation({
    mutationFn: () =>
      usersApi.create({
        username: f.username,
        password: f.password,
        role_id: f.role_id,
        email: f.email || undefined,
        display_name: f.display_name || undefined,
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={t("users.create")} description={t("users.createHint")}>
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("auth.username")}>
            {(p) => (
              <Input
                {...p}
                required
                autoComplete="off"
                value={f.username}
                onChange={(e) => setF({ ...f, username: e.target.value })}
              />
            )}
          </Field>
          <Field label={t("setup.email")}>
            {(p) => (
              <Input
                {...p}
                type="email"
                value={f.email}
                onChange={(e) => setF({ ...f, email: e.target.value })}
              />
            )}
          </Field>
          <Field label={t("users.displayName")}>
            {(p) => (
              <Input
                {...p}
                value={f.display_name}
                onChange={(e) => setF({ ...f, display_name: e.target.value })}
              />
            )}
          </Field>
          <Field label={t("users.role")}>
            {(p) => (
              <Select
                {...p}
                value={f.role_id}
                onChange={(e) => setF({ ...f, role_id: e.target.value })}
              >
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("users.initialPassword")} hint={t("users.initialPasswordHint")}>
            {(p) => (
              <Input
                {...p}
                type="password"
                required
                autoComplete="new-password"
                value={f.password}
                onChange={(e) => setF({ ...f, password: e.target.value })}
              />
            )}
          </Field>
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

function EditUserDialog({
  user,
  roles,
  onClose,
}: {
  user: User;
  roles: Role[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [f, setF] = useState({
    display_name: user.display_name,
    email: user.email ?? "",
    role_id: user.role_id,
  });
  const save = useMutation({
    mutationFn: () =>
      usersApi.update(user.id, {
        display_name: f.display_name,
        email: f.email,
        role_id: f.role_id,
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={t("users.edit", { name: user.username })}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <Field label={t("users.displayName")}>
            {(p) => (
              <Input
                {...p}
                value={f.display_name}
                onChange={(e) => setF({ ...f, display_name: e.target.value })}
              />
            )}
          </Field>
          <Field label={t("setup.email")}>
            {(p) => (
              <Input
                {...p}
                type="email"
                value={f.email}
                onChange={(e) => setF({ ...f, email: e.target.value })}
              />
            )}
          </Field>
          <Field label={t("users.role")} hint={t("users.roleChangeHint")}>
            {(p) => (
              <Select
                {...p}
                value={f.role_id}
                onChange={(e) => setF({ ...f, role_id: e.target.value })}
              >
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {save.error && <ErrorAlert error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {t("common.save")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ResetPasswordDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const [pw, setPw] = useState("");
  const reset = useMutation({
    mutationFn: () => usersApi.resetPassword(user.id, pw),
    onSuccess: onClose,
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={t("users.resetPassword", { name: user.username })}
        description={t("users.resetPasswordHint")}
      >
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            reset.mutate();
          }}
        >
          <Field label={t("users.initialPassword")} hint={t("auth.passwordPolicy")}>
            {(p) => (
              <Input
                {...p}
                type="password"
                required
                autoComplete="new-password"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
              />
            )}
          </Field>
          {reset.error && <ErrorAlert error={reset.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={reset.isPending}>
              {t("users.resetPasswordGo")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type Action = { kind: "create" } | { kind: "edit" | "password" | "deactivate" | "mfa"; user: User };

export function UsersPage() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: usersApi.list });
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list });
  const [action, setAction] = useState<Action | null>(null);
  const reactivate = useMutation({
    mutationFn: (u: User) => usersApi.update(u.id, { is_active: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });
  const close = () => setAction(null);

  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("users.title")}
        description={t("users.description")}
        actions={
          <Button onClick={() => setAction({ kind: "create" })} disabled={!roles.data}>
            <Plus /> {t("users.create")}
          </Button>
        }
      />
      {(users.isLoading || roles.isLoading) && <Spinner label={t("common.loading")} />}
      {(users.error || roles.error) && <ErrorAlert error={users.error ?? roles.error} />}
      {reactivate.error && <ErrorAlert error={reactivate.error} />}
      {users.data && (
        <Table>
          <thead>
            <tr>
              <Th>{t("auth.username")}</Th>
              <Th>{t("users.role")}</Th>
              <Th>{t("connections.status")}</Th>
              <Th>{t("users.lastLogin")}</Th>
              <Th>
                <span className="sr-only">{t("common.actions")}</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {users.data.map((u) => (
              <tr key={u.id}>
                <Td>
                  <div className="font-medium">{u.username}</div>
                  <div className="text-xs text-muted-foreground">
                    {u.display_name !== u.username ? u.display_name : ""} {u.email ?? ""}
                  </div>
                </Td>
                <Td>{u.role}</Td>
                <Td className="space-x-1">
                  <Badge variant={u.is_active ? "success" : "secondary"}>
                    {u.is_active ? t("users.active") : t("users.inactive")}
                  </Badge>
                  {u.mfa_enabled && <Badge variant="outline">MFA</Badge>}
                  {u.must_change_password && (
                    <Badge variant="warning">{t("users.mustChange")}</Badge>
                  )}
                </Td>
                <Td className="text-xs">
                  {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "—"}
                </Td>
                <Td>
                  <div className="flex flex-wrap justify-end gap-1">
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={t("common.editItem", { name: u.username })}
                      onClick={() => setAction({ kind: "edit", user: u })}
                    >
                      <Pencil />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={t("users.resetPassword", { name: u.username })}
                      onClick={() => setAction({ kind: "password", user: u })}
                    >
                      <KeyRound />
                    </Button>
                    {u.mfa_enabled && (
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t("users.resetMfa", { name: u.username })}
                        onClick={() => setAction({ kind: "mfa", user: u })}
                      >
                        <ShieldOff />
                      </Button>
                    )}
                    {u.is_active ? (
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t("users.deactivate", { name: u.username })}
                        onClick={() => setAction({ kind: "deactivate", user: u })}
                      >
                        <UserX />
                      </Button>
                    ) : (
                      <Button size="sm" variant="outline" onClick={() => reactivate.mutate(u)}>
                        {t("users.reactivate")}
                      </Button>
                    )}
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {action?.kind === "create" && roles.data && (
        <CreateUserDialog roles={roles.data} onClose={close} />
      )}
      {action?.kind === "edit" && roles.data && (
        <EditUserDialog user={action.user} roles={roles.data} onClose={close} />
      )}
      {action?.kind === "password" && <ResetPasswordDialog user={action.user} onClose={close} />}
      {action?.kind === "deactivate" && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && close()}
          title={t("users.deactivate", { name: action.user.username })}
          description={t("users.deactivateBody")}
          confirmLabel={t("users.deactivateGo")}
          onConfirm={async () => {
            await usersApi.update(action.user.id, { is_active: false });
            await qc.invalidateQueries({ queryKey: ["users"] });
          }}
        />
      )}
      {action?.kind === "mfa" && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && close()}
          title={t("users.resetMfa", { name: action.user.username })}
          description={t("users.resetMfaBody")}
          confirmLabel={t("users.resetMfaGo")}
          onConfirm={async () => {
            await usersApi.resetMfa(action.user.id);
            await qc.invalidateQueries({ queryKey: ["users"] });
          }}
        />
      )}
    </div>
  );
}
