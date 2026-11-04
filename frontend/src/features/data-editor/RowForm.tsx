import { useState, type FormEvent } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Badge } from "@/components/ui/misc";
import {
  buildExpected,
  columnKind,
  isMasked,
  parseInput,
  rowToObject,
  toInputText,
  type ColumnKind,
} from "@/lib/editing";
import { asMarker } from "@/lib/format";
import { t } from "@/i18n";
import type { ColumnInfo } from "@/types/api";

interface FieldState {
  text: string;
  isNull: boolean;
  /** insert only: leave the column out so the database default applies */
  useDefault: boolean;
}

export interface RowFormResult {
  values: Record<string, unknown>;
  /** previous values of the edited columns (update only) */
  expected?: Record<string, unknown>;
}

interface Props {
  mode: "insert" | "update";
  columns: ColumnInfo[];
  primaryKey: string[];
  /** the row as displayed (update), aligned with `rowColumns` */
  row?: readonly unknown[];
  rowColumns?: { name: string }[];
  onClose: () => void;
  onSubmit: (result: RowFormResult) => Promise<unknown>;
}

function initialField(
  col: ColumnInfo,
  kind: ColumnKind,
  value: unknown,
  mode: Props["mode"],
): FieldState {
  if (mode === "insert") {
    const hasDefault = col.default !== null || col.is_identity || col.nullable;
    return { text: "", isNull: false, useDefault: hasDefault };
  }
  const marker = asMarker(value);
  const text =
    marker?.kind === "truncated" ? marker.preview : marker ? "" : toInputText(kind, value);
  return { text, isNull: value === null || value === undefined, useDefault: false };
}

export function RowForm({ mode, columns, primaryKey, row, rowColumns, onClose, onSubmit }: Props) {
  const original = row && rowColumns ? rowToObject(rowColumns, row) : {};
  const [fields, setFields] = useState<Record<string, FieldState>>(() =>
    Object.fromEntries(
      columns.map((c) => [
        c.name,
        initialField(c, columnKind(c.data_type), original[c.name], mode),
      ]),
    ),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const patch = (name: string, p: Partial<FieldState>) =>
    setFields((f) => ({ ...f, [name]: { ...f[name]!, ...p } }));

  const locked = (c: ColumnInfo) =>
    c.is_generated ||
    (mode === "update" && (primaryKey.includes(c.name) || isMasked(original[c.name]))) ||
    (mode === "update" && asMarker(original[c.name])?.kind === "binary");

  async function submit(e: FormEvent) {
    e.preventDefault();
    const values: Record<string, unknown> = {};
    const errs: Record<string, string> = {};
    for (const c of columns) {
      const f = fields[c.name]!;
      const kind = columnKind(c.data_type);
      if (locked(c)) continue;
      if (mode === "insert") {
        if (f.useDefault) continue;
        try {
          values[c.name] = f.isNull ? null : parseInput(kind, f.text);
        } catch (err) {
          errs[c.name] = (err as Error).message;
        }
      } else {
        const before = original[c.name];
        const beforeText =
          asMarker(before)?.kind === "truncated" ? null : toInputText(kind, before);
        const isNullBefore = before === null || before === undefined;
        if (f.isNull === isNullBefore && (f.isNull || beforeText === null || f.text === beforeText))
          continue;
        try {
          values[c.name] = f.isNull ? null : parseInput(kind, f.text);
        } catch (err) {
          errs[c.name] = (err as Error).message;
        }
      }
    }
    setErrors(errs);
    if (Object.keys(errs).length) return;
    if (mode === "update" && Object.keys(values).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    setSubmitError(null);
    try {
      await onSubmit({
        values,
        expected: mode === "update" ? buildExpected(original, Object.keys(values)) : undefined,
      });
      onClose();
    } catch (err) {
      setSubmitError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={mode === "insert" ? t("editor.insertTitle") : t("editor.updateTitle")}
        description={mode === "update" ? t("editor.updateHint") : t("editor.insertHint")}
        className="max-w-2xl"
      >
        <form onSubmit={submit} className="space-y-4" aria-label={t("editor.form")}>
          <div className="space-y-3">
            {columns.map((c) => {
              const f = fields[c.name]!;
              const kind = columnKind(c.data_type);
              const disabled = locked(c) || f.isNull || (mode === "insert" && f.useDefault);
              const common = {
                disabled,
                value: f.text,
                onChange: (e: { target: { value: string } }) =>
                  patch(c.name, { text: e.target.value }),
              };
              return (
                <div key={c.name} className="rounded-md border p-3">
                  <Field
                    label={c.name}
                    error={errors[c.name]}
                    hint={locked(c) ? lockedReason(c, primaryKey, original[c.name]) : undefined}
                  >
                    {(p) =>
                      kind === "boolean" ? (
                        <Select
                          {...p}
                          disabled={disabled}
                          value={f.text}
                          onChange={common.onChange}
                        >
                          <option value="">—</option>
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </Select>
                      ) : kind === "json" ||
                        (kind === "text" && c.data_type.toLowerCase() === "text") ? (
                        <Textarea
                          {...p}
                          {...common}
                          rows={kind === "json" ? 4 : 2}
                          spellCheck={false}
                        />
                      ) : (
                        <Input {...p} {...common} spellCheck={false} />
                      )
                    }
                  </Field>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <Badge variant="outline" className="font-mono">
                      {c.data_type}
                    </Badge>
                    {primaryKey.includes(c.name) && <Badge>PK</Badge>}
                    {!locked(c) && c.nullable && (
                      <Checkbox
                        label={t("editor.null")}
                        checked={f.isNull}
                        onChange={(e) => patch(c.name, { isNull: e.target.checked })}
                      />
                    )}
                    {!locked(c) &&
                      mode === "insert" &&
                      (c.default !== null || c.is_identity || c.nullable) && (
                        <Checkbox
                          label={t("editor.useDefault")}
                          checked={f.useDefault}
                          onChange={(e) => patch(c.name, { useDefault: e.target.checked })}
                        />
                      )}
                  </div>
                </div>
              );
            })}
          </div>
          {submitError !== null && <ErrorAlert error={submitError} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={busy}>
              {mode === "insert" ? t("editor.insert") : t("common.save")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function lockedReason(c: ColumnInfo, pk: string[], value: unknown): string {
  if (c.is_generated) return t("editor.lockedGenerated");
  if (isMasked(value)) return t("editor.lockedSensitive");
  if (pk.includes(c.name)) return t("editor.lockedPk");
  return t("editor.lockedBinary");
}
