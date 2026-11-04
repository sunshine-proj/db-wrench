import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import { Alert, Card, CardContent, CardHeader, CardTitle, Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import { authApi } from "@/services/api";

export function SetupPage() {
  const nav = useNavigate();
  const status = useQuery({ queryKey: ["setup-status"], queryFn: authApi.setupStatus });
  const [form, setForm] = useState({
    token: "",
    username: "admin",
    password: "",
    confirm: "",
    email: "",
  });
  const mismatch = form.confirm !== "" && form.password !== form.confirm;

  const setup = useMutation({
    mutationFn: () =>
      authApi.setup({
        token: form.token.trim(),
        username: form.username.trim(),
        password: form.password,
        email: form.email.trim() || undefined,
      }),
    onSuccess: () => nav("/login", { replace: true, state: { setupDone: true } }),
  });

  if (status.isLoading) return <Spinner label={t("common.loading")} />;
  if (status.data && !status.data.setup_required) return <Navigate to="/login" replace />;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!mismatch) setup.mutate();
  }

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>{t("setup.title")}</CardTitle>
          <p className="text-sm text-muted-foreground">{t("setup.intro")}</p>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-3">
            <Field label={t("setup.token")} hint={t("setup.tokenHint")}>
              {(p) => (
                <Input
                  {...p}
                  required
                  autoComplete="off"
                  value={form.token}
                  onChange={(e) => setForm({ ...form, token: e.target.value })}
                />
              )}
            </Field>
            <Field label={t("auth.username")}>
              {(p) => (
                <Input
                  {...p}
                  required
                  autoComplete="username"
                  value={form.username}
                  onChange={(e) => setForm({ ...form, username: e.target.value })}
                />
              )}
            </Field>
            <Field label={t("setup.email")}>
              {(p) => (
                <Input
                  {...p}
                  type="email"
                  autoComplete="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                />
              )}
            </Field>
            <Field label={t("auth.password")} hint={t("auth.passwordPolicy")}>
              {(p) => (
                <Input
                  {...p}
                  type="password"
                  required
                  autoComplete="new-password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
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
                  value={form.confirm}
                  onChange={(e) => setForm({ ...form, confirm: e.target.value })}
                />
              )}
            </Field>
            {setup.error && <ErrorAlert error={setup.error} />}
            <Button type="submit" className="w-full" disabled={setup.isPending || mismatch}>
              {t("setup.submit")}
            </Button>
            <Alert variant="info">{t("setup.tokenWhere")}</Alert>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
