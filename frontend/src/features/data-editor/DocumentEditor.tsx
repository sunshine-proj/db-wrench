import { useState, type FormEvent } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Textarea } from "@/components/ui/form";
import { t } from "@/i18n";

/** Edit or create a MongoDB document as canonical Extended JSON (types are preserved). */
export function DocumentEditor({
  mode,
  initial,
  onClose,
  onSubmit,
}: {
  mode: "insert" | "update";
  initial?: Record<string, unknown>;
  onClose: () => void;
  onSubmit: (document: unknown) => Promise<unknown>;
}) {
  const [text, setText] = useState(initial ? JSON.stringify(initial, null, 2) : "{\n  \n}");
  const [error, setError] = useState<unknown>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      setParseError((err as Error).message);
      return;
    }
    setParseError(null);
    setBusy(true);
    setError(null);
    try {
      await onSubmit(parsed);
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={mode === "insert" ? t("docs.insertTitle") : t("docs.editTitle")}
        description={t("docs.editHint")}
        className="max-w-3xl"
      >
        <form onSubmit={submit} className="space-y-3">
          <Field label={t("docs.json")} error={parseError ?? undefined}>
            {(p) => (
              <Textarea
                {...p}
                rows={18}
                spellCheck={false}
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setParseError(null);
                }}
              />
            )}
          </Field>
          {error !== null && <ErrorAlert error={error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={busy}>
              {mode === "insert" ? t("docs.insert") : t("common.save")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
