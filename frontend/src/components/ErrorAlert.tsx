import { Link } from "react-router-dom";
import { Alert } from "@/components/ui/misc";
import { errorMessage, requestId } from "@/lib/errors";
import { t } from "@/i18n";
import { ApiError } from "@/services/api";

export function ErrorAlert({ error, title }: { error: unknown; title?: string }) {
  const rid = requestId(error);
  if (error instanceof ApiError && error.code === "approval_required") {
    const id = String((error.problem as unknown as { approval_id?: string }).approval_id ?? "");
    return (
      <Alert variant="warning" title={t("approvals.required")}>
        <span>{t("approvals.requestedHint", { id: id.slice(0, 8) })}</span>{" "}
        <Link className="underline" to="/approvals">
          {t("approvals.openList")}
        </Link>
      </Alert>
    );
  }
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
