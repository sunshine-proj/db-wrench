import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { Alert, Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import { connectionsApi } from "@/services/api";
import type { Connection } from "@/types/api";

/** A user's own database login for a connection that uses personal credentials. */
export function CredentialsDialog({
  connection,
  onClose,
}: {
  connection: Connection;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["credentials", connection.id],
    queryFn: () => connectionsApi.credentials(connection.id),
  });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const save = useMutation({
    mutationFn: () => connectionsApi.setCredentials(connection.id, username, password),
    onSuccess: async () => {
      setPassword("");
      await qc.invalidateQueries();
    },
  });
  const remove = useMutation({
    mutationFn: () => connectionsApi.removeCredentials(connection.id),
    onSuccess: () => qc.invalidateQueries(),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`${t("credentials.title")}: ${connection.name}`}
        description={t("credentials.description")}
      >
        {status.isLoading && <Spinner label={t("common.loading")} />}
        {status.data && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Alert variant={status.data.configured ? "success" : "info"}>
              {status.data.configured
                ? t("credentials.configured", { name: status.data.username ?? "" })
                : t("credentials.missing")}
            </Alert>
            <Field label={t("credentials.username")}>
              {(p) => (
                <Input
                  {...p}
                  required
                  autoComplete="off"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              )}
            </Field>
            <Field label={t("credentials.password")}>
              {(p) => (
                <Input
                  {...p}
                  type="password"
                  required
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              )}
            </Field>
            {save.error && <ErrorAlert error={save.error} />}
            {remove.error && <ErrorAlert error={remove.error} />}
            <div className="flex justify-between gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={!status.data.configured || remove.isPending}
                onClick={() => remove.mutate()}
              >
                {t("credentials.remove")}
              </Button>
              <Button type="submit" disabled={save.isPending}>
                {t("common.save")}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
