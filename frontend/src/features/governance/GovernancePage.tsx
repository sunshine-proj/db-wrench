import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Select } from "@/components/ui/form";
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
import { connectionsApi, governanceApi } from "@/services/api";
import type { ApprovalPolicy } from "@/types/api";

const ENVS = ["production", "staging", "development", "other"] as const;

export function GovernancePage() {
  const qc = useQueryClient();
  const policies = useQuery({ queryKey: ["policies"], queryFn: governanceApi.policies });
  const conns = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const [scope, setScope] = useState("env:production");
  const [ops, setOps] = useState<string[] | null>(null);

  const current: ApprovalPolicy | undefined = policies.data?.policies.find((p) =>
    scope.startsWith("env:")
      ? p.environment === scope.slice(4)
      : p.connection_id === scope.slice(5),
  );
  const selected = ops ?? current?.operations ?? [];
  const save = useMutation({
    mutationFn: () =>
      governanceApi.setPolicy(
        scope.startsWith("env:")
          ? { environment: scope.slice(4), operations: selected }
          : { connection_id: scope.slice(5), operations: selected },
      ),
    onSuccess: async () => {
      setOps(null);
      await qc.invalidateQueries({ queryKey: ["policies"] });
    },
  });

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-6">
      <PageHeader title={t("governance.title")} description={t("governance.description")} />
      {policies.isLoading && <Spinner label={t("common.loading")} />}
      {policies.error && <ErrorAlert error={policies.error} />}
      {policies.data && (
        <Card>
          <CardHeader>
            <CardTitle>{t("governance.operations")}</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate();
              }}
            >
              <Field label={t("governance.scope")}>
                {(p) => (
                  <Select
                    {...p}
                    value={scope}
                    onChange={(e) => {
                      setScope(e.target.value);
                      setOps(null);
                    }}
                  >
                    <optgroup label={t("governance.environment")}>
                      {ENVS.map((e) => (
                        <option key={e} value={`env:${e}`}>
                          {e}
                        </option>
                      ))}
                    </optgroup>
                    <optgroup label={t("governance.connection")}>
                      {(conns.data ?? []).map((c) => (
                        <option key={c.id} value={`conn:${c.id}`}>
                          {c.name}
                        </option>
                      ))}
                    </optgroup>
                  </Select>
                )}
              </Field>
              <fieldset className="grid gap-2 sm:grid-cols-2">
                <legend className="sr-only">{t("governance.operations")}</legend>
                {policies.data.available_operations.map((op) => (
                  <Checkbox
                    key={op}
                    label={op}
                    checked={selected.includes(op) || policies.data.always_approved.includes(op)}
                    disabled={policies.data.always_approved.includes(op)}
                    onChange={(e) =>
                      setOps(
                        e.target.checked ? [...selected, op] : selected.filter((x) => x !== op),
                      )
                    }
                  />
                ))}
              </fieldset>
              <p className="text-xs text-muted-foreground">
                {t("governance.alwaysApproved", { ops: policies.data.always_approved.join(", ") })}
              </p>
              {save.error && <ErrorAlert error={save.error} />}
              {save.isSuccess && <Alert variant="success">{t("common.saved")}</Alert>}
              <Button type="submit" disabled={save.isPending}>
                {t("common.save")}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
