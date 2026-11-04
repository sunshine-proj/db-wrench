import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/form";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/misc";
import { t } from "@/i18n";
import { editorApi } from "@/services/api";
import type { SensitiveMarker } from "@/types/api";

export const markersKey = (cid: string) => ["markers", cid] as const;

/** Sensitive fields of one object (table or collection), as a set of names. */
export function useSensitiveFields(
  cid: string,
  db: string,
  schema: string | undefined,
  object: string,
) {
  const q = useQuery({
    queryKey: markersKey(cid),
    queryFn: () => editorApi.markers(cid),
    retry: false,
  });
  const fields = new Set(
    (q.data ?? [])
      .filter((m) => m.database === db && m.schema === (schema ?? "") && m.object === object)
      .map((m) => m.field),
  );
  return { fields, all: q.data ?? [], isLoading: q.isLoading };
}

/**
 * Admin control: mark columns whose values are masked for users without the
 * data.read_sensitive permission. Primary-key columns cannot be marked.
 */
export function SensitiveColumns({
  cid,
  db,
  schema,
  object,
  columns,
  primaryKey,
}: {
  cid: string;
  db: string;
  schema?: string;
  object: string;
  columns: string[];
  primaryKey: string[];
}) {
  const qc = useQueryClient();
  const { fields, all } = useSensitiveFields(cid, db, schema, object);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  useEffect(() => setChosen(new Set(fields)), [JSON.stringify([...fields].sort())]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: () => {
      const others: SensitiveMarker[] = all.filter(
        (m) => !(m.database === db && m.schema === (schema ?? "") && m.object === object),
      );
      const mine = [...chosen].map((field) => ({
        database: db,
        schema: schema ?? "",
        object,
        field,
      }));
      return editorApi.setMarkers(cid, [...others, ...mine]);
    },
    onSuccess: (data) => qc.setQueryData(markersKey(cid), data),
  });
  const dirty = [...chosen].sort().join() !== [...fields].sort().join();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Lock className="size-4" aria-hidden /> {t("sensitive.title")}
        </CardTitle>
        <p className="text-sm text-muted-foreground">{t("sensitive.description")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
          {columns.map((c) => (
            <Checkbox
              key={c}
              label={c}
              disabled={primaryKey.includes(c)}
              checked={chosen.has(c)}
              onChange={(e) =>
                setChosen((s) => {
                  const n = new Set(s);
                  if (e.target.checked) n.add(c);
                  else n.delete(c);
                  return n;
                })
              }
            />
          ))}
        </div>
        {primaryKey.length > 0 && (
          <p className="text-xs text-muted-foreground">{t("sensitive.pkHint")}</p>
        )}
        {save.error && <ErrorAlert error={save.error} />}
        {save.isSuccess && !dirty && <Alert variant="success">{t("common.saved")}</Alert>}
        <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
          {t("common.save")}
        </Button>
      </CardContent>
    </Card>
  );
}
