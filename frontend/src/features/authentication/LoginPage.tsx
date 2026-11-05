import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import { Alert, Card, CardContent, CardHeader, CardTitle, Spinner } from "@/components/ui/misc";
import { SESSION_KEY, useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { authApi, ssoApi } from "@/services/api";

export function LoginPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const location = useLocation();
  const { session, isLoading } = useAuth();
  const from = (location.state as { from?: string; setupDone?: boolean } | null)?.from ?? "/";
  const setupDone = (location.state as { setupDone?: boolean } | null)?.setupDone === true;

  const setup = useQuery({ queryKey: ["setup-status"], queryFn: authApi.setupStatus });
  const sso = useQuery({ queryKey: ["oidc-public"], queryFn: ssoApi.publicConfig, retry: false });
  const ssoFailed = new URLSearchParams(location.search).has("sso_error");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const pendingVerify = session?.mfa_state === "pending_verify";

  const login = useMutation({
    mutationFn: () => authApi.login(username, password),
    onSuccess: async () => {
      setPassword("");
      await qc.invalidateQueries({ queryKey: SESSION_KEY });
    },
  });
  const verify = useMutation({
    mutationFn: () => authApi.verifyMfa(code),
    onSuccess: async () => {
      setCode("");
      await qc.invalidateQueries({ queryKey: SESSION_KEY });
      nav(from, { replace: true });
    },
  });

  if (isLoading || setup.isLoading) return <Spinner label={t("common.loading")} />;
  if (setup.data?.setup_required) return <Navigate to="/setup" replace />;
  if (session && session.mfa_state !== "pending_verify") return <Navigate to={from} replace />;

  function submitCredentials(e: FormEvent) {
    e.preventDefault();
    login.mutate();
  }
  function submitCode(e: FormEvent) {
    e.preventDefault();
    verify.mutate();
  }

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{pendingVerify ? t("auth.mfa.title") : t("auth.login.title")}</CardTitle>
          <p className="text-sm text-muted-foreground">
            {pendingVerify ? t("auth.mfa.prompt") : t("app.tagline")}
          </p>
        </CardHeader>
        <CardContent>
          {pendingVerify ? (
            <form onSubmit={submitCode} className="space-y-3">
              <Field label={t("auth.mfa.code")} hint={t("auth.mfa.codeHint")}>
                {(p) => (
                  <Input
                    {...p}
                    required
                    autoFocus
                    autoComplete="one-time-code"
                    inputMode="text"
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                )}
              </Field>
              {verify.error && <ErrorAlert error={verify.error} />}
              <Button
                type="submit"
                className="w-full"
                disabled={verify.isPending || code.length < 6}
              >
                {t("auth.mfa.verify")}
              </Button>
            </form>
          ) : (
            <form onSubmit={submitCredentials} className="space-y-3">
              {setupDone && <Alert variant="success">{t("setup.done")}</Alert>}
              {ssoFailed && <Alert variant="destructive">{t("sso.failed")}</Alert>}
              {sso.data?.enabled && (
                <Button asChild variant="outline" className="w-full">
                  <a href={ssoApi.loginUrl}>{t("sso.login", { name: sso.data.display_name })}</a>
                </Button>
              )}
              <Field label={t("auth.username")}>
                {(p) => (
                  <Input
                    {...p}
                    required
                    autoFocus
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                )}
              </Field>
              <Field label={t("auth.password")}>
                {(p) => (
                  <Input
                    {...p}
                    type="password"
                    required
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                )}
              </Field>
              {login.error && <ErrorAlert error={login.error} />}
              <Button type="submit" className="w-full" disabled={login.isPending}>
                {t("auth.login.submit")}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
