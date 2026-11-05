import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useState } from "react";
import { ApprovalPicker } from "@/components/ApprovalPicker";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Alert, Table, Td, Th } from "@/components/ui/misc";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { importApi, withApproval, type TablePath } from "@/services/api";
import type { Connection, CsvImportOptions, CsvPreview, DocImportPreview } from "@/types/api";

const MAX_BYTES = 8_000_000;

/** "csv_column=table_column" per line -> mapping (empty = use the header names). */
export function parseMapping(text: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return Object.keys(out).length ? out : null;
}

type Preview = CsvPreview | DocImportPreview;
const isCsv = (p: Preview): p is CsvPreview => "rows" in p;

export function ImportButton({
  connection,
  table,
  mongo,
}: {
  connection: Connection;
  table?: TablePath;
  mongo?: { db: string; collection: string };
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");
  const [delimiter, setDelimiter] = useState<CsvImportOptions["delimiter"]>(",");
  const [hasHeader, setHasHeader] = useState(true);
  const [emptyAsNull, setEmptyAsNull] = useState(true);
  const [mapping, setMapping] = useState("");
  const [format, setFormat] = useState<"json" | "ndjson">("json");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirm, setConfirm] = useState("");
  const [approvalId, setApprovalId] = useState("");
  const [done, setDone] = useState<number | null>(null);

  const objectName = mongo ? mongo.collection : (table?.table ?? "");
  const csvOptions = (): CsvImportOptions => ({
    content: content ?? "",
    delimiter,
    has_header: hasHeader,
    mapping: parseMapping(mapping),
    empty_as_null: emptyAsNull,
  });

  const validate = useMutation({
    mutationFn: async () => {
      setDone(null);
      setPreview(null);
      return mongo
        ? importApi.previewDocs(connection.id, mongo.db, mongo.collection, content ?? "", format)
        : importApi.previewCsv(table as TablePath, csvOptions());
    },
    onSuccess: setPreview,
  });
  const commit = useMutation({
    mutationFn: async () => {
      const token = preview?.token ?? "";
      const conf = confirm || undefined;
      return withApproval(approvalId || undefined, () =>
        mongo
          ? importApi.commitDocs(connection.id, mongo.db, mongo.collection, {
              content: content ?? "",
              format,
              token,
              confirm: conf,
            })
          : importApi.commitCsv(table as TablePath, { ...csvOptions(), token, confirm: conf }),
      );
    },
    onSuccess: async (r) => {
      setDone(r.imported);
      setPreview(null);
      await qc.invalidateQueries();
    },
  });

  if (!canOn(connection, "export.import") || !canOn(connection, "data.insert")) return null;

  async function pick(file: File | undefined) {
    setPreview(null);
    setDone(null);
    setFileError(null);
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setFileError(t("import.tooLarge"));
      return;
    }
    setFileName(file.name);
    setContent(await file.text());
    if (file.name.endsWith(".ndjson") || file.name.endsWith(".jsonl")) setFormat("ndjson");
    if (file.name.endsWith(".tsv")) setDelimiter("tab");
  }

  const count = preview ? (isCsv(preview) ? preview.rows : preview.documents) : 0;
  const problems = preview
    ? isCsv(preview)
      ? preview.errors.map((e) => ({ at: t("import.rowN", { n: e.row }), message: e.message }))
      : preview.errors.map((e) => ({
          at: e.index ? t("import.docN", { n: e.index }) : "—",
          message: e.message,
        }))
    : [];
  const needsName = preview?.requires_confirmation === true;

  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        <Upload /> {t("import.button")}
      </Button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent title={t("import.title", { name: objectName })} className="max-w-3xl">
            <div className="space-y-3">
              <p className="text-xs text-muted-foreground">{t("import.trial")}</p>
              <Field label={t("import.file")}>
                {(p) => (
                  <Input
                    {...p}
                    type="file"
                    accept={mongo ? ".json,.ndjson,.jsonl,.txt" : ".csv,.tsv,.txt"}
                    onChange={(e) => void pick(e.target.files?.[0])}
                  />
                )}
              </Field>
              {fileName && <p className="text-xs text-muted-foreground">{fileName}</p>}
              {fileError && <Alert variant="destructive">{fileError}</Alert>}
              {mongo ? (
                <Field label={t("import.format")}>
                  {(p) => (
                    <Select
                      {...p}
                      value={format}
                      onChange={(e) => setFormat(e.target.value as "json" | "ndjson")}
                    >
                      <option value="json">JSON array</option>
                      <option value="ndjson">NDJSON</option>
                    </Select>
                  )}
                </Field>
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label={t("import.delimiter")}>
                      {(p) => (
                        <Select
                          {...p}
                          value={delimiter}
                          onChange={(e) =>
                            setDelimiter(e.target.value as CsvImportOptions["delimiter"])
                          }
                        >
                          <option value=",">,</option>
                          <option value=";">;</option>
                          <option value="tab">Tab</option>
                          <option value="|">|</option>
                        </Select>
                      )}
                    </Field>
                    <div className="space-y-1 pt-6">
                      <Checkbox
                        label={t("import.hasHeader")}
                        checked={hasHeader}
                        onChange={(e) => setHasHeader(e.target.checked)}
                      />
                      <Checkbox
                        label={t("import.emptyAsNull")}
                        checked={emptyAsNull}
                        onChange={(e) => setEmptyAsNull(e.target.checked)}
                      />
                    </div>
                  </div>
                  <Field label={t("import.mapping")}>
                    {(p) => (
                      <Textarea
                        {...p}
                        rows={3}
                        value={mapping}
                        onChange={(e) => setMapping(e.target.value)}
                      />
                    )}
                  </Field>
                </>
              )}
              {validate.error && <ErrorAlert error={validate.error} />}
              <Button
                disabled={content === null || validate.isPending}
                onClick={() => validate.mutate()}
              >
                {t("import.preview")}
              </Button>

              {preview && (
                <div className="space-y-3 border-t pt-3">
                  <Alert variant={problems.length ? "warning" : "success"}>
                    {t("import.summary", {
                      valid: isCsv(preview) ? preview.valid_rows : preview.valid_documents,
                      total: count,
                    })}
                  </Alert>
                  {problems.length > 0 && (
                    <div>
                      <h3 className="mb-1 text-sm font-semibold">{t("import.problems")}</h3>
                      <ul className="max-h-40 space-y-1 overflow-y-auto text-xs">
                        {problems.map((p, i) => (
                          <li key={i}>
                            <span className="font-medium">{p.at}:</span> {p.message}
                          </li>
                        ))}
                      </ul>
                      <p className="mt-2 text-xs text-muted-foreground">{t("import.noToken")}</p>
                    </div>
                  )}
                  {isCsv(preview) && preview.sample.length > 0 && (
                    <div className="overflow-x-auto">
                      <h3 className="mb-1 text-sm font-semibold">{t("import.sample")}</h3>
                      <Table>
                        <thead>
                          <tr>
                            {preview.columns.map((c) => (
                              <Th key={c}>{c}</Th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {preview.sample.map((r, i) => (
                            <tr key={i}>
                              {r.map((v, j) => (
                                <Td key={j} className="font-mono text-xs">
                                  {v === null ? "NULL" : v}
                                </Td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </Table>
                    </div>
                  )}
                  {preview.token && (
                    <div className="space-y-3">
                      <ApprovalPicker
                        connectionId={connection.id}
                        operation="import.commit"
                        value={approvalId}
                        onChange={setApprovalId}
                      />
                      {needsName && (
                        <Field label={t("confirm.typeToConfirm", { text: objectName })}>
                          {(p) => (
                            <Input
                              {...p}
                              value={confirm}
                              onChange={(e) => setConfirm(e.target.value)}
                            />
                          )}
                        </Field>
                      )}
                      {commit.error && <ErrorAlert error={commit.error} />}
                      <Button
                        disabled={commit.isPending || (needsName && confirm !== objectName)}
                        onClick={() => commit.mutate()}
                      >
                        {t(mongo ? "import.commitDocs" : "import.commit", { count })}
                      </Button>
                    </div>
                  )}
                </div>
              )}
              {done !== null && (
                <Alert variant="success">{t("import.done", { count: done })}</Alert>
              )}
            </div>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
