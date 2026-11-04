import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Alert } from "@/components/ui/misc";
import { DEFAULT_PORT, ENVIRONMENTS } from "@/lib/environment";
import { t } from "@/i18n";
import { connectionsApi } from "@/services/api";
import type { Connection, ConnectionInput, DbType, Environment, TlsMode } from "@/types/api";

const TLS_MODES: TlsMode[] = ["disable", "require", "verify-ca", "verify-full"];

interface FormState {
  name: string;
  db_type: DbType;
  environment: Environment;
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
  tls_mode: TlsMode;
  tls_ca: string;
  connect_timeout_s: string;
  pool_max: string;
  read_only: boolean;
  enabled: boolean;
  auth_source: string;
}

function initial(c?: Connection): FormState {
  return {
    name: c?.name ?? "",
    db_type: c?.db_type ?? "postgresql",
    environment: c?.environment ?? "development",
    host: c?.host ?? "",
    port: String(c?.port ?? DEFAULT_PORT.postgresql),
    database: c?.database ?? "",
    username: c?.username ?? "",
    password: "",
    tls_mode: c?.tls_mode ?? "disable",
    tls_ca: "",
    connect_timeout_s: String(c?.connect_timeout_s ?? 10),
    pool_max: String(c?.pool_max ?? 10),
    read_only: c?.read_only ?? true,
    enabled: c?.enabled ?? true,
    auth_source: String((c?.options?.["auth_source"] as string | undefined) ?? ""),
  };
}

export function toInput(f: FormState, editing: boolean): Partial<ConnectionInput> {
  const options: Record<string, unknown> = {};
  if (f.db_type === "mongodb" && f.auth_source.trim())
    options["auth_source"] = f.auth_source.trim();
  const body: Partial<ConnectionInput> = {
    name: f.name.trim(),
    environment: f.environment,
    host: f.host.trim(),
    port: Number(f.port),
    database: f.database.trim() || null,
    username: f.username.trim(),
    tls_mode: f.tls_mode,
    connect_timeout_s: Number(f.connect_timeout_s),
    pool_max: Number(f.pool_max),
    read_only: f.read_only,
    enabled: f.enabled,
    options,
  };
  if (f.tls_ca.trim()) body.tls_ca = f.tls_ca.trim();
  // On edit a blank password means "keep the stored one"; it is never sent back to us.
  if (f.password || !editing) body.password = f.password;
  if (!editing) body.db_type = f.db_type;
  return body;
}

export function ConnectionFormDialog({
  open,
  onOpenChange,
  connection,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  connection?: Connection;
}) {
  const qc = useQueryClient();
  const editing = connection !== undefined;
  const [f, setF] = useState<FormState>(() => initial(connection));
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((s) => ({ ...s, [k]: v }));

  const save = useMutation({
    mutationFn: () => {
      const body = toInput(f, editing);
      return editing
        ? connectionsApi.update(connection.id, body)
        : connectionsApi.create(body as ConnectionInput);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["connections"] });
      onOpenChange(false);
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={editing ? t("connections.edit") : t("connections.create")}
        className="max-w-2xl"
      >
        <form onSubmit={submit} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("connections.name")} className="sm:col-span-2">
              {(p) => (
                <Input
                  {...p}
                  required
                  value={f.name}
                  onChange={(e) => set("name", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("connections.type")}>
              {(p) => (
                <Select
                  {...p}
                  value={f.db_type}
                  disabled={editing}
                  onChange={(e) => {
                    const type = e.target.value as DbType;
                    setF((s) => ({ ...s, db_type: type, port: String(DEFAULT_PORT[type]) }));
                  }}
                >
                  <option value="postgresql">PostgreSQL</option>
                  <option value="mysql">MySQL</option>
                  <option value="mongodb">MongoDB</option>
                </Select>
              )}
            </Field>
            <Field label={t("connections.environment")}>
              {(p) => (
                <Select
                  {...p}
                  value={f.environment}
                  onChange={(e) => set("environment", e.target.value as Environment)}
                >
                  {ENVIRONMENTS.map((env) => (
                    <option key={env} value={env}>
                      {t(`env.${env}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("connections.host")}>
              {(p) => (
                <Input
                  {...p}
                  required
                  value={f.host}
                  onChange={(e) => set("host", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("connections.port")}>
              {(p) => (
                <Input
                  {...p}
                  type="number"
                  min={1}
                  max={65535}
                  required
                  value={f.port}
                  onChange={(e) => set("port", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("connections.database")} hint={t("connections.databaseHint")}>
              {(p) => (
                <Input
                  {...p}
                  value={f.database}
                  onChange={(e) => set("database", e.target.value)}
                />
              )}
            </Field>
            {f.db_type === "mongodb" && (
              <Field label={t("connections.authSource")} hint={t("connections.authSourceHint")}>
                {(p) => (
                  <Input
                    {...p}
                    placeholder="admin"
                    value={f.auth_source}
                    onChange={(e) => set("auth_source", e.target.value)}
                  />
                )}
              </Field>
            )}
            <Field label={t("connections.username")}>
              {(p) => (
                <Input
                  {...p}
                  autoComplete="off"
                  value={f.username}
                  onChange={(e) => set("username", e.target.value)}
                />
              )}
            </Field>
            <Field
              label={t("connections.password")}
              hint={editing ? t("connections.passwordKeep") : undefined}
            >
              {(p) => (
                <Input
                  {...p}
                  type="password"
                  autoComplete="new-password"
                  value={f.password}
                  onChange={(e) => set("password", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("connections.tls")}>
              {(p) => (
                <Select
                  {...p}
                  value={f.tls_mode}
                  onChange={(e) => set("tls_mode", e.target.value as TlsMode)}
                >
                  {TLS_MODES.map((m) => (
                    <option key={m} value={m}>
                      {t(`tls.${m}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("connections.timeout")}>
              {(p) => (
                <Input
                  {...p}
                  type="number"
                  min={1}
                  max={60}
                  value={f.connect_timeout_s}
                  onChange={(e) => set("connect_timeout_s", e.target.value)}
                />
              )}
            </Field>
            {f.tls_mode !== "disable" && f.tls_mode !== "require" && (
              <Field
                label={t("connections.tlsCa")}
                hint={editing && connection?.has_tls_ca ? t("connections.tlsCaKeep") : undefined}
                className="sm:col-span-2"
              >
                {(p) => (
                  <Textarea
                    {...p}
                    placeholder="-----BEGIN CERTIFICATE-----"
                    value={f.tls_ca}
                    onChange={(e) => set("tls_ca", e.target.value)}
                  />
                )}
              </Field>
            )}
            <Field label={t("connections.poolMax")}>
              {(p) => (
                <Input
                  {...p}
                  type="number"
                  min={1}
                  max={50}
                  value={f.pool_max}
                  onChange={(e) => set("pool_max", e.target.value)}
                />
              )}
            </Field>
          </div>
          <div className="space-y-2">
            <Checkbox
              label={t("connections.readOnly")}
              checked={f.read_only}
              onChange={(e) => set("read_only", e.target.checked)}
            />
            <Checkbox
              label={t("connections.enabled")}
              checked={f.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
            />
          </div>
          {f.environment === "production" && !f.read_only && (
            <Alert variant="warning">{t("connections.productionWrite")}</Alert>
          )}
          {save.error && <ErrorAlert error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
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
