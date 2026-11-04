import { Alert } from "@/components/ui/misc";
import { errorMessage, requestId } from "@/lib/errors";
import { t } from "@/i18n";

export function ErrorAlert({ error, title }: { error: unknown; title?: string }) {
  const rid = requestId(error);
  return (
    <Alert variant="destructive" title={title ?? t("common.error")}>
      <span>{errorMessage(error)}</span>
      {rid && (
        <span className="mt-1 block font-mono text-xs text-muted-foreground">
          {t("common.requestId")}: {rid}
        </span>
      )}
    </Alert>
  );
}
