import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Textarea } from "@/components/ui/form";
import {
  Alert,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
} from "@/components/ui/misc";
import { t } from "@/i18n";
import { queryApi, settingsApi } from "@/services/api";
import { Checkbox, Input } from "@/components/ui/form";

const lines = (s: string) =>
  s
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

function HistorySettingsCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["history-settings"], queryFn: queryApi.historySettings });
  const [days, setDays] = useState("90");
  const [literals, setLiterals] = useState(true);
  useEffect(() => {
    if (q.data) {
      setDays(String(q.data.retention_days));
      setLiterals(q.data.store_literals);
    }
  }, [q.data]);
  const save = useMutation({
    mutationFn: () =>
      queryApi.setHistorySettings({ retention_days: Number(days), store_literals: literals }),
    onSuccess: (d) => qc.setQueryData(["history-settings"], d),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("historyCfg.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <p className="text-sm text-muted-foreground">{t("historyCfg.description")}</p>
          {q.error && <ErrorAlert error={q.error} />}
          <Field label={t("historyCfg.retention")}>
            {(p) => (
              <Input
                {...p}
                type="number"
                min={1}
                max={3650}
                value={days}
                onChange={(e) => setDays(e.target.value)}
              />
            )}
          </Field>
          <Checkbox
            label={t("historyCfg.literals")}
            checked={literals}
            onChange={(e) => setLiterals(e.target.checked)}
          />
          {save.error && <ErrorAlert error={save.error} />}
          {save.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
          <Button type="submit" disabled={save.isPending}>
            {t("common.save")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export function NetworkPolicyPage() {
  const qc = useQueryClient();
  const policy = useQuery({ queryKey: ["network-policy"], queryFn: settingsApi.networkPolicy });
  const [cidrs, setCidrs] = useState("");
  const [hosts, setHosts] = useState("");
  const [rotating, setRotating] = useState(false);
  const [rotated, setRotated] = useState<string | null>(null);

  useEffect(() => {
    if (policy.data) {
      setCidrs(policy.data.allow_cidrs.join("\n"));
      setHosts(policy.data.allow_hosts.join("\n"));
    }
  }, [policy.data]);

  const save = useMutation({
    mutationFn: () =>
      settingsApi.setNetworkPolicy({ allow_cidrs: lines(cidrs), allow_hosts: lines(hosts) }),
    onSuccess: (data) => qc.setQueryData(["network-policy"], data),
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-6">
      <PageHeader title={t("network.title")} description={t("network.description")} />
      {policy.isLoading && <Spinner label={t("common.loading")} />}
      {policy.error && <ErrorAlert error={policy.error} />}
      {policy.data && (
        <>
          {policy.data.allow_loopback && <Alert variant="warning">{t("network.loopbackOn")}</Alert>}
          <Card>
            <CardHeader>
              <CardTitle>{t("network.allowlist")}</CardTitle>
            </CardHeader>
            <CardContent>
              <form
                className="space-y-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  save.mutate();
                }}
              >
                <Field label={t("network.cidrs")} hint={t("network.cidrsHint")}>
                  {(p) => (
                    <Textarea
                      {...p}
                      rows={5}
                      spellCheck={false}
                      value={cidrs}
                      onChange={(e) => setCidrs(e.target.value)}
                    />
                  )}
                </Field>
                <Field label={t("network.hosts")} hint={t("network.hostsHint")}>
                  {(p) => (
                    <Textarea
                      {...p}
                      rows={3}
                      spellCheck={false}
                      value={hosts}
                      onChange={(e) => setHosts(e.target.value)}
                    />
                  )}
                </Field>
                {save.error && <ErrorAlert error={save.error} />}
                {save.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
                <Button type="submit" disabled={save.isPending}>
                  {t("common.save")}
                </Button>
              </form>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>{t("network.alwaysDenied")}</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="mb-2 text-sm text-muted-foreground">{t("network.alwaysDeniedHint")}</p>
              <ul className="flex flex-wrap gap-1">
                {policy.data.always_denied.map((n) => (
                  <li key={n}>
                    <Badge variant="outline" className="font-mono">
                      {n}
                    </Badge>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </>
      )}
      <HistorySettingsCard />
      <Card>
        <CardHeader>
          <CardTitle>{t("keys.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">{t("keys.description")}</p>
          {rotated && <Alert variant="success">{rotated}</Alert>}
          <Button variant="outline" onClick={() => setRotating(true)}>
            {t("keys.rotate")}
          </Button>
        </CardContent>
      </Card>
      {rotating && (
        <ConfirmDialog
          open
          destructive={false}
          onOpenChange={(o) => !o && setRotating(false)}
          title={t("keys.rotate")}
          description={t("keys.rotateBody")}
          confirmLabel={t("keys.rotateGo")}
          onConfirm={async () => {
            const r = await settingsApi.rotateKeys();
            setRotated(
              t("keys.rotated", {
                kek: r.active_kek,
                connections: r.connections,
                mfa: r.mfa_secrets,
              }),
            );
          }}
        />
      )}
    </div>
  );
}
