import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/form";
import { ErrorAlert } from "@/components/ErrorAlert";
import { t } from "@/i18n";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  /** When set, the user must type this exact text (used for production objects). */
  requireText?: string;
  destructive?: boolean;
  onConfirm: () => Promise<unknown>;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  requireText,
  destructive = true,
  onConfirm,
}: Props) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const blocked = requireText !== undefined && typed !== requireText;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onOpenChange(false);
      setTyped("");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setTyped("");
          setError(null);
        }
        onOpenChange(o);
      }}
    >
      <DialogContent title={title}>
        <div className="space-y-4">
          <div className="text-sm">{description}</div>
          {requireText !== undefined && (
            <Field label={t("confirm.typeToConfirm", { text: requireText })}>
              {(p) => (
                <Input
                  {...p}
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  autoComplete="off"
                />
              )}
            </Field>
          )}
          {error !== null && <ErrorAlert error={error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant={destructive ? "destructive" : "default"}
              disabled={blocked || busy}
              onClick={run}
            >
              {confirmLabel}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
