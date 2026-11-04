import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import {
  Alert,
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageHeader,
  Spinner,
  Table,
  Td,
  Th,
} from "@/components/ui/misc";
import { SESSION_KEY, useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { authApi } from "@/services/api";
import { MfaPanel } from "./MfaPanel";

export function PasswordForm() {
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const mismatch = confirm !== "" && next !== confirm;
  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, next),
    onSuccess: async () => {
      setCurrent("");
      setNext("");
      setConfirm("");
      await qc.invalidateQueries({ queryKey: SESSION_KEY });
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!mismatch) change.mutate();
  }
  return (
    <form onSubmit={submit} className="space-y-3">
      <Field label={t("account.currentPassword")}>
        {(p) => (
          <Input
            {...p}
            type="password"
            required
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        )}
      </Field>
      <Field label={t("account.newPassword")} hint={t("auth.passwordPolicy")}>
        {(p) => (
          <Input
            {...p}
            type="password"
            required
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        )}
      </Field>
      <Field
        label={t("auth.confirmPassword")}
        error={mismatch ? t("auth.passwordMismatch") : undefined}
      >
        {(p) => (
          <Input
            {...p}
            type="password"
            required
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        )}
      </Field>
      {change.error && <ErrorAlert error={change.error} />}
      {change.isSuccess && <Alert variant="success">{t("account.passwordChanged")}</Alert>}
      <Button type="submit" disabled={change.isPending || mismatch}>
        {t("account.changePassword")}
      </Button>
    </form>
  );
}

function Sessions() {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: authApi.sessions });
  const revoke = useMutation({
    mutationFn: (id: string) => authApi.revokeSession(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
  if (sessions.isLoading) return <Spinner label={t("common.loading")} />;
  if (sessions.error) return <ErrorAlert error={sessions.error} />;
  return (
    <Table>
      <thead>
        <tr>
          <Th>{t("account.sessions.started")}</Th>
          <Th>{t("account.sessions.lastSeen")}</Th>
          <Th>{t("account.sessions.ip")}</Th>
          <Th>{t("account.sessions.agent")}</Th>
          <Th>
            <span className="sr-only">{t("common.actions")}</span>
          </Th>
        </tr>
      </thead>
      <tbody>
        {sessions.data?.map((s) => (
          <tr key={s.id}>
            <Td>{new Date(s.created_at).toLocaleString()}</Td>
            <Td>{new Date(s.last_seen_at).toLocaleString()}</Td>
            <Td className="font-mono text-xs">{s.ip ?? "—"}</Td>
            <Td className="max-w-56 truncate text-xs" title={s.user_agent ?? ""}>
              {s.user_agent ?? "—"}
            </Td>
            <Td className="text-right">
              {s.current ? (
                <Badge variant="secondary">{t("account.sessions.current")}</Badge>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => revoke.mutate(s.id)}
                  disabled={revoke.isPending}
                >
                  {t("account.sessions.revoke")}
                </Button>
              )}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export function AccountPage() {
  const { session } = useAuth();
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-6">
      <PageHeader title={t("account.title")} description={session?.user.username} />
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("account.password")}</CardTitle>
          </CardHeader>
          <CardContent>
            <PasswordForm />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("account.mfa")}</CardTitle>
          </CardHeader>
          <CardContent>
            <MfaPanel />
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t("account.sessions")}</CardTitle>
        </CardHeader>
        <CardContent>
          <Sessions />
        </CardContent>
      </Card>
    </div>
  );
}
