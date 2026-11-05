import { useQuery } from "@tanstack/react-query";
import { Field, Select } from "@/components/ui/form";
import { t } from "@/i18n";
import { governanceApi } from "@/services/api";

/**
 * Lets the requester attach an already approved request to the operation they are about to
 * run. Only shown when the user has approved requests for this connection and operation
 * (the server re-checks the binding to the exact parameters).
 */
export function ApprovalPicker({
  connectionId,
  operation,
  value,
  onChange,
}: {
  connectionId: string;
  operation: string;
  value: string;
  onChange: (id: string) => void;
}) {
  const q = useQuery({
    queryKey: ["approvals", "mine", "approved"],
    queryFn: () => governanceApi.approvals("mine", "approved"),
  });
  const usable = (q.data ?? []).filter(
    (a) => a.connection_id === connectionId && a.operation === operation,
  );
  if (usable.length === 0) return null;
  return (
    <Field label={t("approvals.use")} hint={t("approvals.useHint")}>
      {(p) => (
        <Select {...p} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">{t("approvals.useNone")}</option>
          {usable.map((a) => (
            <option key={a.id} value={a.id}>
              {Object.values(a.summary).map(String).join(" · ")} ({a.id.slice(0, 8)})
            </option>
          ))}
        </Select>
      )}
    </Field>
  );
}
