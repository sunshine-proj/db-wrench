import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/form";
import { Alert } from "@/components/ui/misc";
import { SESSION_KEY, useAuth } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { authApi } from "@/services/api";

/** TOTP enrollment / disabling. Works for opt-in users and for forced enrollment. */
export function MfaPanel() {
  const qc = useQueryClient();
  const { session } = useAuth();
  const enabled = session?.user.mfa_enabled ?? false;
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);

  const start = useMutation({ mutationFn: authApi.startMfaEnrollment });
  const confirm = useMutation({
    mutationFn: () => authApi.confirmMfaEnrollment(code),
    onSuccess: async (r) => {
      setRecovery(r.recovery_codes);
      setCode("");
      start.reset();
      await qc.invalidateQueries({ queryKey: SESSION_KEY });
    },
  });
  const disable = useMutation({
    mutationFn: () => authApi.disableMfa(password, code),
    onSuccess: async () => {
      setCode("");
      setPassword("");
      await qc.invalidateQueries({ queryKey: SESSION_KEY });
    },
  });

  if (recovery) {
    return (
      <div className="space-y-3">
        <Alert variant="warning" title={t("mfa.recovery.title")}>
          {t("mfa.recovery.body")}
        </Alert>
        <ul
          className="grid grid-cols-2 gap-1 rounded-md border bg-muted p-3 font-mono text-sm"
          aria-label={t("mfa.recovery.title")}
        >
          {recovery.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
        <Button onClick={() => setRecovery(null)}>{t("mfa.recovery.saved")}</Button>
      </div>
    );
  }

  if (enabled) {
    return (
      <div className="space-y-3">
        <Alert variant="success">{t("mfa.enabled")}</Alert>
        <Field label={t("auth.password")}>
          {(p) => (
            <Input
              {...p}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <Field label={t("auth.mfa.code")}>
          {(p) => (
            <Input
              {...p}
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          )}
        </Field>
        {disable.error && <ErrorAlert error={disable.error} />}
        <Button
          variant="destructive"
          disabled={disable.isPending || !password || code.length < 6}
          onClick={() => disable.mutate()}
        >
          {t("mfa.disable")}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {!start.data ? (
        <>
          <p className="text-sm text-muted-foreground">{t("mfa.intro")}</p>
          {start.error && <ErrorAlert error={start.error} />}
          <Button onClick={() => start.mutate()} disabled={start.isPending}>
            {t("mfa.start")}
          </Button>
        </>
      ) : (
        <>
          <p className="text-sm">{t("mfa.scan")}</p>
          <div className="space-y-1 rounded-md border bg-muted p-3">
            <p className="text-xs text-muted-foreground">{t("mfa.secret")}</p>
            <code className="block break-all font-mono text-sm" data-testid="mfa-secret">
              {start.data.secret}
            </code>
            <a className="text-xs text-primary underline" href={start.data.otpauth_uri}>
              {t("mfa.openApp")}
            </a>
          </div>
          <Field label={t("auth.mfa.code")}>
            {(p) => (
              <Input
                {...p}
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            )}
          </Field>
          {confirm.error && <ErrorAlert error={confirm.error} />}
          <Button
            disabled={confirm.isPending || code.length !== 6}
            onClick={() => confirm.mutate()}
          >
            {t("mfa.confirm")}
          </Button>
        </>
      )}
    </div>
  );
}
