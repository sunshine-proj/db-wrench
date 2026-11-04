import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { Alert, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { cellText } from "@/lib/format";
import { t } from "@/i18n";

export interface PreviewData {
  count: number;
  token: string;
  requires_confirmation: boolean;
  columns?: string[];
  sample: unknown[];
}

interface Props {
  title: string;
  /** what will happen, shown above the preview */
  summary: string;
  /** name the user must type on production connections */
  objectName: string;
  confirmLabel: string;
  preview: () => Promise<PreviewData>;
  execute: (p: { token: string; count: number; confirm?: string }) => Promise<{ affected: number }>;
  onClose: () => void;
  onDone: (affected: number) => void;
}

/**
 * Two-step bulk operation: preview (count + sample) first, then explicit confirmation. The
 * server refuses to run unless the preview token and the previewed count match.
 */
export function BulkDialog({
  title,
  summary,
  objectName,
  confirmLabel,
  preview,
  execute,
  onClose,
  onDone,
}: Props) {
  const [typed, setTyped] = useState("");
  const prev = useMutation({ mutationFn: preview });
  const run = useMutation({
    mutationFn: (p: PreviewData) =>
      execute({
        token: p.token,
        count: p.count,
        confirm: p.requires_confirmation ? typed : undefined,
      }),
    onSuccess: (r) => onDone(r.affected),
  });

  useEffect(() => {
    prev.mutate();
    // run the preview once when the dialog opens
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const data = prev.data;
  const blocked = !data || data.count === 0 || (data.requires_confirmation && typed !== objectName);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={title} description={summary} className="max-w-3xl">
        <div className="space-y-4">
          {prev.isPending && <Spinner label={t("bulk.previewing")} />}
          {prev.error && <ErrorAlert error={prev.error} />}
          {data && (
            <>
              <Alert variant={data.count === 0 ? "info" : "warning"}>
                {data.count === 0
                  ? t("bulk.none")
                  : t("bulk.affects", { count: data.count.toLocaleString() })}
              </Alert>
              {data.sample.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">
                    {t("bulk.sample", { n: data.sample.length })}
                  </p>
                  {data.columns ? (
                    <Table>
                      <thead>
                        <tr>
                          {data.columns.map((c) => (
                            <Th key={c}>{c}</Th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {(data.sample as unknown[][]).map((row, i) => (
                          <tr key={i}>
                            {row.map((v, j) => (
                              <Td key={j} className="max-w-48 truncate font-mono text-xs">
                                {v === null ? "NULL" : cellText(v)}
                              </Td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  ) : (
                    <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 font-mono text-xs">
                      {JSON.stringify(data.sample, null, 2)}
                    </pre>
                  )}
                </div>
              )}
              {data.requires_confirmation && data.count > 0 && (
                <Field
                  label={t("confirm.typeToConfirm", { text: objectName })}
                  hint={t("bulk.productionHint")}
                >
                  {(p) => (
                    <Input
                      {...p}
                      autoComplete="off"
                      value={typed}
                      onChange={(e) => setTyped(e.target.value)}
                    />
                  )}
                </Field>
              )}
            </>
          )}
          {run.error && <ErrorAlert error={run.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={blocked || run.isPending}
              onClick={() => data && run.mutate(data)}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
