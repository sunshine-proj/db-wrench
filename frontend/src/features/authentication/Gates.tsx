import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/misc";
import { t } from "@/i18n";
import { PasswordForm } from "./AccountPage";
import { MfaPanel } from "./MfaPanel";

function CenteredCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">{children}</CardContent>
      </Card>
    </div>
  );
}

/** The user's role requires MFA and none is enrolled yet: nothing else is reachable. */
export function EnrollmentRequired() {
  return (
    <CenteredCard title={t("auth.enrollRequired.title")}>
      <p className="text-sm text-muted-foreground">{t("auth.enrollRequired.body")}</p>
      <MfaPanel />
    </CenteredCard>
  );
}

export function ForcePasswordChange() {
  return (
    <CenteredCard title={t("auth.changeRequired.title")}>
      <p className="text-sm text-muted-foreground">{t("auth.changeRequired.body")}</p>
      <PasswordForm />
    </CenteredCard>
  );
}
