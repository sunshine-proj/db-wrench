import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { JsonNode } from "@/features/data-browser/JsonTree";
import { t } from "@/i18n";
import { useAuth } from "@/hooks/useAuth";
import { auditApi, auditSinkApi } from "@/services/api";
import type { AuditEvent } from "@/types/api";

const RESULT_VARIANT = { success: "success", failure: "destructive", denied: "warning" } as const;

export function AuditPage() {
  const { can } = useAuth();
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [result, setResult] = useState("");
  const [applied, setApplied] = useState({ action: "", actor: "", result: "" });
  const [open, setOpen] = useState<AuditEvent | null>(null);

  const q = useInfiniteQuery({
    queryKey: ["audit", applied],
    queryFn: ({ pageParam }) =>
      auditApi.events({
        before_id: pageParam,
        limit: 100,
        action: applied.action || undefined,
        actor: applied.actor || undefined,
        result: applied.result || undefined,
      }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_before_id ?? undefined,
  });
  const verify = useMutation({ mutationFn: auditApi.verify });
  const events = q.data?.pages.flatMap((p) => p.events) ?? [];

  return (
    <div className="space-y-4 p-4 md:p-6">
      <PageHeader
        title={t("audit.title")}
        description={t("audit.description")}
        actions={
          <>
            {can("audit.export") &&
              (["csv", "json", "ndjson"] as const).map((f) => (
                <Button key={f} asChild variant="outline">
                  <a
                    href={auditSinkApi.exportUrl(f, {
                      action: applied.action,
                      result: applied.result,
                      actor: applied.actor,
                    })}
                    download
                  >
                    {t("audit.export")} {f.toUpperCase()}
                  </a>
                </Button>
              ))}
            <Button variant="outline" onClick={() => verify.mutate()} disabled={verify.isPending}>
              <ShieldCheck /> {t("audit.verify")}
            </Button>
          </>
        }
      />
      {verify.data && (
        <Alert
          variant={verify.data.ok ? "success" : "destructive"}
          title={
            verify.data.ok
              ? t("audit.verifyOk", { count: verify.data.checked })
              : t("audit.verifyBroken", { id: verify.data.first_bad_id ?? 0 })
          }
        >
          {verify.data.reason}
        </Alert>
      )}
      {verify.error && <ErrorAlert error={verify.error} />}
      <form
        className="flex flex-wrap items-center gap-2"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({ action, actor, result });
        }}
      >
        <Input
          aria-label={t("audit.action")}
          placeholder={t("audit.actionHint")}
          className="w-48"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
        <Input
          aria-label={t("audit.actor")}
          placeholder={t("audit.actor")}
          className="w-40"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        />
        <Select
          aria-label={t("audit.result")}
          className="w-36"
          value={result}
          onChange={(e) => setResult(e.target.value)}
        >
          <option value="">{t("audit.anyResult")}</option>
          <option value="success">success</option>
          <option value="failure">failure</option>
          <option value="denied">denied</option>
        </Select>
        <Button type="submit" size="sm">
          {t("filter.apply")}
        </Button>
      </form>
      {q.isLoading && <Spinner label={t("common.loading")} />}
      {q.error && <ErrorAlert error={q.error} />}
      {events.length > 0 && (
        <Table>
          <thead>
            <tr>
              <Th>{t("audit.time")}</Th>
              <Th>{t("audit.actor")}</Th>
              <Th>{t("audit.action")}</Th>
              <Th>{t("audit.target")}</Th>
              <Th>{t("audit.result")}</Th>
              <Th>{t("audit.ip")}</Th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr
                key={e.id}
                className="cursor-pointer hover:bg-muted/50"
                onClick={() => setOpen(e)}
              >
                <Td className="whitespace-nowrap text-xs">{new Date(e.ts).toLocaleString()}</Td>
                <Td>{e.actor_username ?? "—"}</Td>
                <Td className="font-mono text-xs">
                  <button type="button" className="hover:underline" onClick={() => setOpen(e)}>
                    {e.action}
                  </button>
                </Td>
                <Td
                  className="max-w-64 truncate font-mono text-xs"
                  title={JSON.stringify(e.target)}
                >
                  {Object.values(e.target)
                    .filter((v) => v !== null)
                    .join(" · ")}
                </Td>
                <Td>
                  <Badge variant={RESULT_VARIANT[e.result]}>{e.result}</Badge>
                </Td>
                <Td className="font-mono text-xs">{e.ip ?? "—"}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {q.hasNextPage && (
        <Button variant="outline" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>
          {t("audit.more")}
        </Button>
      )}
      {q.data && events.length === 0 && <Alert variant="info">{t("audit.empty")}</Alert>}
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(null)}>
          <DialogContent
            title={open.action}
            description={new Date(open.ts).toLocaleString()}
            className="max-w-2xl"
          >
            <JsonNode
              value={{
                actor: open.actor_username,
                ip: open.ip,
                request_id: open.request_id,
                result: open.result,
                connection_id: open.connection_id,
                target: open.target,
                detail: open.detail,
                hash: open.hash,
              }}
              defaultOpenDepth={2}
            />
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
