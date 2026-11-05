import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { EnvBadge } from "@/components/EnvBadge";
import { Alert, Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { t } from "@/i18n";
import { governanceApi } from "@/services/api";
import type { Approval, Environment } from "@/types/api";

const STATUS_VARIANT = {
  pending: "warning",
  approved: "success",
  rejected: "destructive",
  executed: "secondary",
  cancelled: "secondary",
  expired: "secondary",
} as const;

function Details({ a }: { a: Approval }) {
  return (
    <dl className="grid gap-1 text-xs">
      {Object.entries(a.summary).map(([k, v]) => (
        <div key={k} className="flex gap-2">
          <dt className="min-w-24 text-muted-foreground">{k}</dt>
          <dd className="break-all font-mono">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function DecisionDialog({
  approval,
  approve,
  onClose,
}: {
  approval: Approval;
  approve: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: () =>
      approve
        ? governanceApi.approve(approval.id, reason)
        : governanceApi.reject(approval.id, reason),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["approvals"] });
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`${approve ? t("approvals.approve") : t("approvals.reject")}: ${approval.operation}`}
      >
        <div className="space-y-3">
          <Details a={approval} />
          <Field label={t("approvals.reason")}>
            {(p) => <Input {...p} value={reason} onChange={(e) => setReason(e.target.value)} />}
          </Field>
          {decide.error && <ErrorAlert error={decide.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button
              variant={approve ? "default" : "destructive"}
              disabled={decide.isPending}
              onClick={() => decide.mutate()}
            >
              {approve ? t("approvals.approve") : t("approvals.reject")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ApprovalsPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"mine" | "review">("review");
  const [deciding, setDeciding] = useState<{ a: Approval; approve: boolean } | null>(null);
  const q = useQuery({
    queryKey: ["approvals", tab],
    queryFn: () => governanceApi.approvals(tab),
    refetchInterval: 15_000,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => governanceApi.cancel(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
      <PageHeader title={t("approvals.title")} description={t("approvals.description")} />
      <div role="tablist" className="flex gap-1 border-b">
        {(["review", "mine"] as const).map((id) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
              tab === id ? "border-primary" : "border-transparent text-muted-foreground"
            }`}
          >
            {t(id === "review" ? "approvals.review" : "approvals.mine")}
          </button>
        ))}
      </div>
      {q.isLoading && <Spinner label={t("common.loading")} />}
      {q.error && <ErrorAlert error={q.error} />}
      {cancel.error && <ErrorAlert error={cancel.error} />}
      {q.data && q.data.length === 0 && <Alert>{t("approvals.none")}</Alert>}
      {q.data && q.data.length > 0 && (
        <Table>
          <thead>
            <tr>
              <Th>{t("approvals.operation")}</Th>
              <Th>{t("approvals.connection")}</Th>
              <Th>{t("approvals.requester")}</Th>
              <Th>{t("approvals.details")}</Th>
              <Th>{t("approvals.status")}</Th>
              <Th>{t("common.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((a) => (
              <tr key={a.id}>
                <Td className="font-mono text-xs">{a.operation}</Td>
                <Td>
                  <span className="inline-flex items-center gap-2">
                    {a.connection_name}
                    {a.environment && <EnvBadge env={a.environment as Environment} />}
                  </span>
                </Td>
                <Td>{a.requester}</Td>
                <Td>
                  <Details a={a} />
                </Td>
                <Td>
                  <Badge variant={STATUS_VARIANT[a.status]}>{a.status}</Badge>
                  {a.reason && <p className="mt-1 text-xs text-muted-foreground">{a.reason}</p>}
                </Td>
                <Td>
                  <div className="flex gap-2">
                    {tab === "review" && a.status === "pending" && (
                      <>
                        <Button size="sm" onClick={() => setDeciding({ a, approve: true })}>
                          {t("approvals.approve")}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setDeciding({ a, approve: false })}
                        >
                          {t("approvals.reject")}
                        </Button>
                      </>
                    )}
                    {tab === "mine" && (a.status === "pending" || a.status === "approved") && (
                      <Button size="sm" variant="outline" onClick={() => cancel.mutate(a.id)}>
                        {t("approvals.cancel")}
                      </Button>
                    )}
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {deciding && (
        <DecisionDialog
          approval={deciding.a}
          approve={deciding.approve}
          onClose={() => setDeciding(null)}
        />
      )}
    </div>
  );
}
