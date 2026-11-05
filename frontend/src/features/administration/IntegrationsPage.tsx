import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import {
  Alert,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
} from "@/components/ui/misc";
import { t } from "@/i18n";
import { auditSinkApi, rolesApi, ssoApi } from "@/services/api";
import type { AuditSink, OidcConfig } from "@/types/api";

/** "group=Role" per line <-> mapping list. */
export const parseMappings = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const i = l.lastIndexOf("=");
      return { group: l.slice(0, i).trim(), role: l.slice(i + 1).trim() };
    });
export const formatMappings = (m: OidcConfig["role_mappings"]) =>
  m.map((x) => `${x.group}=${x.role}`).join("\n");

function SsoCard() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["oidc"], queryFn: ssoApi.config });
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list });
  const [f, setF] = useState({
    enabled: false,
    issuer: "",
    client_id: "",
    client_secret: "",
    scopes: "openid email profile",
    groups_claim: "groups",
    username_claim: "preferred_username",
    display_name: "Single sign-on",
    mappings: "",
    default_role: "",
  });
  useEffect(() => {
    const d = cfg.data;
    if (d)
      setF({
        enabled: d.enabled,
        issuer: d.issuer,
        client_id: d.client_id,
        client_secret: "",
        scopes: d.scopes,
        groups_claim: d.groups_claim,
        username_claim: d.username_claim,
        display_name: d.display_name,
        mappings: formatMappings(d.role_mappings),
        default_role: d.default_role ?? "",
      });
  }, [cfg.data]);
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((x) => ({ ...x, [k]: v }));
  const save = useMutation({
    mutationFn: () =>
      ssoApi.setConfig({
        enabled: f.enabled,
        issuer: f.issuer,
        client_id: f.client_id,
        ...(f.client_secret ? { client_secret: f.client_secret } : {}),
        scopes: f.scopes,
        groups_claim: f.groups_claim,
        username_claim: f.username_claim,
        display_name: f.display_name,
        role_mappings: parseMappings(f.mappings),
        default_role: f.default_role || null,
      }),
    onSuccess: (d) => {
      qc.setQueryData(["oidc"], d);
      void qc.invalidateQueries({ queryKey: ["oidc-public"] });
    },
  });
  const assignable = (roles.data ?? []).filter((r) => r.name !== "Super Admin");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("sso.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        {cfg.isLoading && <Spinner label={t("common.loading")} />}
        {cfg.error && <ErrorAlert error={cfg.error} />}
        {cfg.data && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Checkbox
              label={t("sso.enabled")}
              checked={f.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("sso.issuer")}>
                {(p) => (
                  <Input
                    {...p}
                    type="url"
                    value={f.issuer}
                    onChange={(e) => set("issuer", e.target.value)}
                  />
                )}
              </Field>
              <Field label={t("sso.clientId")}>
                {(p) => (
                  <Input
                    {...p}
                    value={f.client_id}
                    onChange={(e) => set("client_id", e.target.value)}
                  />
                )}
              </Field>
              <Field
                label={t("sso.clientSecret")}
                hint={cfg.data.client_secret_set ? t("sso.secretKeep") : undefined}
              >
                {(p) => (
                  <Input
                    {...p}
                    type="password"
                    autoComplete="off"
                    value={f.client_secret}
                    onChange={(e) => set("client_secret", e.target.value)}
                  />
                )}
              </Field>
              <Field label={t("sso.buttonLabel")}>
                {(p) => (
                  <Input
                    {...p}
                    value={f.display_name}
                    onChange={(e) => set("display_name", e.target.value)}
                  />
                )}
              </Field>
              <Field label={t("sso.groupsClaim")}>
                {(p) => (
                  <Input
                    {...p}
                    value={f.groups_claim}
                    onChange={(e) => set("groups_claim", e.target.value)}
                  />
                )}
              </Field>
              <Field label={t("sso.usernameClaim")}>
                {(p) => (
                  <Input
                    {...p}
                    value={f.username_claim}
                    onChange={(e) => set("username_claim", e.target.value)}
                  />
                )}
              </Field>
            </div>
            <Field label={t("sso.mappings")} hint={t("sso.noSuperAdmin")}>
              {(p) => (
                <Textarea
                  {...p}
                  rows={4}
                  value={f.mappings}
                  onChange={(e) => set("mappings", e.target.value)}
                />
              )}
            </Field>
            <Field label={t("sso.defaultRole")}>
              {(p) => (
                <Select
                  {...p}
                  value={f.default_role}
                  onChange={(e) => set("default_role", e.target.value)}
                >
                  <option value="">—</option>
                  {assignable.map((r) => (
                    <option key={r.id} value={r.name}>
                      {r.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {cfg.data.redirect_uri && (
              <p className="text-xs text-muted-foreground">
                {t("sso.redirect")}: <code className="font-mono">{cfg.data.redirect_uri}</code>
              </p>
            )}
            {save.error && <ErrorAlert error={save.error} />}
            {save.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
            <Button type="submit" disabled={save.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

function SiemCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["audit-sink"], queryFn: auditSinkApi.get });
  const [f, setF] = useState<Omit<AuditSink, "last_forwarded_id">>({
    enabled: false,
    protocol: "tcp",
    host: "",
    port: 514,
    format: "rfc5424",
    facility: 13,
  });
  const [backfill, setBackfill] = useState(false);
  useEffect(() => {
    if (q.data) {
      const { last_forwarded_id: _ignored, ...rest } = q.data;
      void _ignored;
      setF(rest);
    }
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => auditSinkApi.set({ ...f, backfill }),
    onSuccess: (d) => qc.setQueryData(["audit-sink"], d),
  });
  const test = useMutation({ mutationFn: auditSinkApi.test });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("siem.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        {q.isLoading && <Spinner label={t("common.loading")} />}
        {q.error && <ErrorAlert error={q.error} />}
        {q.data && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Checkbox
              label={t("siem.enabled")}
              checked={f.enabled}
              onChange={(e) => setF({ ...f, enabled: e.target.checked })}
            />
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label={t("siem.host")} className="sm:col-span-2">
                {(p) => (
                  <Input
                    {...p}
                    value={f.host}
                    onChange={(e) => setF({ ...f, host: e.target.value })}
                  />
                )}
              </Field>
              <Field label={t("siem.port")}>
                {(p) => (
                  <Input
                    {...p}
                    type="number"
                    min={1}
                    max={65535}
                    value={f.port}
                    onChange={(e) => setF({ ...f, port: Number(e.target.value) })}
                  />
                )}
              </Field>
              <Field label={t("siem.protocol")}>
                {(p) => (
                  <Select
                    {...p}
                    value={f.protocol}
                    onChange={(e) =>
                      setF({ ...f, protocol: e.target.value as AuditSink["protocol"] })
                    }
                  >
                    <option value="tcp">TCP</option>
                    <option value="tls">TLS</option>
                    <option value="udp">UDP</option>
                  </Select>
                )}
              </Field>
              <Field label={t("siem.format")}>
                {(p) => (
                  <Select
                    {...p}
                    value={f.format}
                    onChange={(e) => setF({ ...f, format: e.target.value as AuditSink["format"] })}
                  >
                    <option value="rfc5424">RFC 5424</option>
                    <option value="json">JSON</option>
                  </Select>
                )}
              </Field>
              <Field label={t("siem.facility")}>
                {(p) => (
                  <Input
                    {...p}
                    type="number"
                    min={0}
                    max={23}
                    value={f.facility}
                    onChange={(e) => setF({ ...f, facility: Number(e.target.value) })}
                  />
                )}
              </Field>
            </div>
            {!q.data.enabled && (
              <Checkbox
                label={t("siem.backfill")}
                checked={backfill}
                onChange={(e) => setBackfill(e.target.checked)}
              />
            )}
            {q.data.enabled && (
              <p className="text-xs text-muted-foreground">
                {t("siem.position", { id: q.data.last_forwarded_id })}
              </p>
            )}
            {save.error && <ErrorAlert error={save.error} />}
            {save.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
            {test.data && (
              <Alert variant={test.data.ok ? "success" : "destructive"}>
                {test.data.ok
                  ? t("siem.testOk")
                  : t("siem.testFailed", { error: test.data.error ?? "" })}
              </Alert>
            )}
            {test.error && <ErrorAlert error={test.error} />}
            <div className="flex gap-2">
              <Button type="submit" disabled={save.isPending}>
                {t("common.save")}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={test.isPending || !q.data.host}
                onClick={() => test.mutate()}
              >
                {t("siem.test")}
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

export function IntegrationsPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-6">
      <PageHeader title={t("integrations.title")} description={t("integrations.description")} />
      <SsoCard />
      <SiemCard />
    </div>
  );
}
