import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, History, Trash2, Users } from "lucide-react";
import { useState, type FormEvent } from "react";
import { cn } from "@/components/cn";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { Badge, Spinner } from "@/components/ui/misc";
import { t } from "@/i18n";
import { queryApi } from "@/services/api";
import type { HistoryEntry, SavedQuery } from "@/types/api";

export interface LoadedQuery {
  statement: string;
  database?: string | null;
  schema?: string | null;
}

interface Props {
  cid: string;
  kind: "sql" | "mongo";
  current: LoadedQuery;
  onLoad: (q: LoadedQuery) => void;
  canViewHistory: boolean;
  canSave: boolean;
}

const STATUS_VARIANT = {
  succeeded: "success",
  failed: "destructive",
  cancelled: "secondary",
  timeout: "warning",
} as const;

function preview(statement: string): string {
  const oneLine = statement.replace(/\s+/g, " ").trim();
  return oneLine.length > 110 ? `${oneLine.slice(0, 110)}…` : oneLine;
}

function SaveDialog({
  cid,
  kind,
  current,
  onClose,
}: {
  cid: string;
  kind: "sql" | "mongo";
  current: LoadedQuery;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<"private" | "shared">("private");
  const save = useMutation({
    mutationFn: () =>
      queryApi.createSaved(cid, {
        kind,
        name: name.trim(),
        description,
        visibility,
        statement: current.statement,
        database: current.database,
        schema: current.schema,
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["saved-queries", cid] });
      onClose();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={t("panels.saveTitle")} description={t("panels.saveHint")}>
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("roles.name")}>
            {(p) => (
              <Input
                {...p}
                required
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            )}
          </Field>
          <Field label={t("roles.descriptionLabel")}>
            {(p) => (
              <Textarea
                {...p}
                rows={2}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            )}
          </Field>
          <Field label={t("panels.visibility")} hint={t("panels.visibilityHint")}>
            {(p) => (
              <Select
                {...p}
                value={visibility}
                onChange={(e) => setVisibility(e.target.value as "private" | "shared")}
              >
                <option value="private">{t("panels.private")}</option>
                <option value="shared">{t("panels.shared")}</option>
              </Select>
            )}
          </Field>
          {save.error && <ErrorAlert error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending || !name.trim()}>
              {t("common.save")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** History (private to you) and saved queries (private or shared) for a connection. */
export function QueryPanels({ cid, kind, current, onLoad, canViewHistory, canSave }: Props) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"history" | "saved">(canViewHistory ? "history" : "saved");
  const [saving, setSaving] = useState(false);
  const history = useQuery({
    queryKey: ["query-history", cid, kind],
    queryFn: () => queryApi.history(cid, { kind, limit: 50 }),
    enabled: canViewHistory && tab === "history",
  });
  const saved = useQuery({
    queryKey: ["saved-queries", cid],
    queryFn: queryApi.saved.bind(null, cid),
    enabled: tab === "saved",
  });
  const removeHistory = useMutation({
    mutationFn: (id: number) => queryApi.deleteHistory(cid, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["query-history", cid] }),
  });
  const clear = useMutation({
    mutationFn: () => queryApi.clearHistory(cid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["query-history", cid] }),
  });
  const removeSaved = useMutation({
    mutationFn: (id: string) => queryApi.deleteSaved(cid, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["saved-queries", cid] }),
  });

  const entries = (saved.data ?? []).filter((q) => q.kind === kind);

  return (
    <section aria-label={t("panels.label")} className="space-y-2">
      <div role="tablist" className="flex gap-1 border-b">
        {canViewHistory && (
          <button
            role="tab"
            type="button"
            aria-selected={tab === "history"}
            onClick={() => setTab("history")}
            className={cn(
              "-mb-px flex items-center gap-1 border-b-2 px-2 py-1.5 text-sm font-medium",
              tab === "history" ? "border-primary" : "border-transparent text-muted-foreground",
            )}
          >
            <History className="size-4" aria-hidden /> {t("panels.history")}
          </button>
        )}
        <button
          role="tab"
          type="button"
          aria-selected={tab === "saved"}
          onClick={() => setTab("saved")}
          className={cn(
            "-mb-px flex items-center gap-1 border-b-2 px-2 py-1.5 text-sm font-medium",
            tab === "saved" ? "border-primary" : "border-transparent text-muted-foreground",
          )}
        >
          <Bookmark className="size-4" aria-hidden /> {t("panels.saved")}
        </button>
      </div>

      {tab === "history" && (
        <div className="space-y-2">
          {history.isLoading && <Spinner label={t("common.loading")} />}
          {history.error && <ErrorAlert error={history.error} />}
          {history.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">{t("panels.historyEmpty")}</p>
          )}
          <ul className="space-y-1">
            {history.data?.map((h: HistoryEntry) => (
              <li key={h.id} className="group rounded-md border p-2">
                <button
                  type="button"
                  className="block w-full text-left"
                  onClick={() =>
                    onLoad({ statement: h.statement, database: h.database, schema: h.schema })
                  }
                  aria-label={t("panels.loadStatement", { text: preview(h.statement) })}
                >
                  <span className="block truncate font-mono text-xs">{preview(h.statement)}</span>
                </button>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                  <Badge variant={STATUS_VARIANT[h.status]}>{h.status}</Badge>
                  <span>{h.duration_ms} ms</span>
                  {h.row_count !== null && <span>{t("panels.rows", { n: h.row_count })}</span>}
                  {h.redacted && (
                    <span title={t("panels.redactedHint")}>{t("panels.redacted")}</span>
                  )}
                  <span className="ml-auto">{new Date(h.created_at).toLocaleString()}</span>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="size-6"
                    aria-label={t("panels.deleteEntry")}
                    onClick={() => removeHistory.mutate(h.id)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
          {(history.data?.length ?? 0) > 0 && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => clear.mutate()}
              disabled={clear.isPending}
            >
              {t("panels.clearHistory")}
            </Button>
          )}
        </div>
      )}

      {tab === "saved" && (
        <div className="space-y-2">
          {canSave && (
            <Button
              size="sm"
              variant="outline"
              disabled={!current.statement.trim()}
              onClick={() => setSaving(true)}
            >
              <Bookmark /> {t("panels.saveCurrent")}
            </Button>
          )}
          {saved.isLoading && <Spinner label={t("common.loading")} />}
          {saved.error && <ErrorAlert error={saved.error} />}
          {saved.data && entries.length === 0 && (
            <p className="text-sm text-muted-foreground">{t("panels.savedEmpty")}</p>
          )}
          <ul className="space-y-1">
            {entries.map((q: SavedQuery) => (
              <li key={q.id} className="rounded-md border p-2">
                <button
                  type="button"
                  className="block w-full text-left"
                  onClick={() =>
                    onLoad({ statement: q.statement, database: q.database, schema: q.schema })
                  }
                  aria-label={t("panels.loadSaved", { name: q.name })}
                >
                  <span className="flex items-center gap-2 text-sm font-medium">
                    {q.name}
                    {q.visibility === "shared" && (
                      <Badge variant="outline" className="gap-1">
                        <Users className="size-3" aria-hidden />
                        {t("panels.shared")}
                      </Badge>
                    )}
                  </span>
                  {q.description && (
                    <span className="block truncate text-xs text-muted-foreground">
                      {q.description}
                    </span>
                  )}
                  <span className="block truncate font-mono text-[11px] text-muted-foreground">
                    {preview(q.statement)}
                  </span>
                </button>
                {q.mine && canSave && (
                  <Button
                    size="icon"
                    variant="ghost"
                    className="size-6"
                    aria-label={t("common.deleteItem", { name: q.name })}
                    onClick={() => removeSaved.mutate(q.id)}
                  >
                    <Trash2 />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {saving && (
        <SaveDialog cid={cid} kind={kind} current={current} onClose={() => setSaving(false)} />
      )}
    </section>
  );
}
