import { KeyRound, Lock } from "lucide-react";
import { SensitiveColumns, useSensitiveFields } from "@/features/data-editor/SensitiveColumns";
import { canOn } from "@/hooks/useAuth";
import { Badge, Table, Td, Th } from "@/components/ui/misc";
import { t } from "@/i18n";
import type { Connection, TableStructure } from "@/types/api";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

export function StructureView({
  structure,
  connection,
  db,
}: {
  structure: TableStructure;
  connection?: Connection;
  db?: string;
}) {
  const sensitive = useSensitiveFields(
    connection?.id ?? "",
    db ?? "",
    structure.schema ?? undefined,
    structure.name,
  );
  const pk = new Set(structure.primary_key);
  const fkCols = new Set(structure.foreign_keys.flatMap((f) => f.columns));
  return (
    <div className="space-y-6">
      <Section title={t("structure.columns")}>
        <Table>
          <thead>
            <tr>
              <Th>#</Th>
              <Th>{t("structure.name")}</Th>
              <Th>{t("structure.type")}</Th>
              <Th>{t("structure.nullable")}</Th>
              <Th>{t("structure.default")}</Th>
              <Th>{t("structure.comment")}</Th>
            </tr>
          </thead>
          <tbody>
            {structure.columns.map((c) => (
              <tr key={c.name}>
                <Td className="text-muted-foreground">{c.ordinal}</Td>
                <Td className="font-medium">
                  <span className="inline-flex items-center gap-1.5">
                    {pk.has(c.name) && (
                      <KeyRound
                        className="size-3.5 text-warning"
                        aria-label={t("grid.primaryKey")}
                      />
                    )}
                    {c.name}
                    {fkCols.has(c.name) && <Badge variant="outline">FK</Badge>}
                    {c.is_identity && <Badge variant="secondary">identity</Badge>}
                    {c.is_generated && <Badge variant="secondary">generated</Badge>}
                    {sensitive.fields.has(c.name) && (
                      <Badge variant="warning" className="gap-1">
                        <Lock className="size-3" aria-hidden /> {t("sensitive.badge")}
                      </Badge>
                    )}
                  </span>
                </Td>
                <Td className="font-mono text-xs">{c.data_type}</Td>
                <Td>{c.nullable ? t("common.yes") : t("common.no")}</Td>
                <Td className="max-w-64 truncate font-mono text-xs" title={c.default ?? ""}>
                  {c.default ?? "—"}
                </Td>
                <Td className="text-xs text-muted-foreground">{c.comment ?? ""}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Section>

      {structure.foreign_keys.length > 0 && (
        <Section title={t("structure.foreignKeys")}>
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("structure.columns")}</Th>
                <Th>{t("structure.references")}</Th>
                <Th>{t("structure.rules")}</Th>
              </tr>
            </thead>
            <tbody>
              {structure.foreign_keys.map((f) => (
                <tr key={f.name}>
                  <Td className="font-medium">{f.name}</Td>
                  <Td className="font-mono text-xs">{f.columns.join(", ")}</Td>
                  <Td className="font-mono text-xs">
                    {[f.ref_schema, f.ref_table].filter(Boolean).join(".")} (
                    {f.ref_columns.join(", ")})
                  </Td>
                  <Td className="text-xs">
                    ON UPDATE {f.on_update ?? "—"} · ON DELETE {f.on_delete ?? "—"}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Section>
      )}

      <Section title={t("structure.indexes")}>
        {structure.indexes.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("structure.noIndexes")}</p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("structure.columns")}</Th>
                <Th>{t("structure.method")}</Th>
                <Th>{t("structure.flags")}</Th>
              </tr>
            </thead>
            <tbody>
              {structure.indexes.map((i) => (
                <tr key={i.name}>
                  <Td className="font-medium">{i.name}</Td>
                  <Td className="font-mono text-xs">{i.columns.join(", ")}</Td>
                  <Td className="text-xs">{i.method ?? "—"}</Td>
                  <Td className="space-x-1">
                    {i.primary && <Badge>PRIMARY</Badge>}
                    {i.unique && <Badge variant="secondary">UNIQUE</Badge>}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Section>

      {structure.constraints.length > 0 && (
        <Section title={t("structure.constraints")}>
          <Table>
            <thead>
              <tr>
                <Th>{t("structure.name")}</Th>
                <Th>{t("structure.kind")}</Th>
                <Th>{t("structure.definition")}</Th>
              </tr>
            </thead>
            <tbody>
              {structure.constraints.map((c) => (
                <tr key={`${c.kind}-${c.name}`}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td>
                    <Badge variant="outline">{c.kind}</Badge>
                  </Td>
                  <Td className="font-mono text-xs">{c.definition ?? c.columns.join(", ")}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Section>
      )}
      {connection && db && canOn(connection, "connection.edit") && (
        <SensitiveColumns
          cid={connection.id}
          db={db}
          schema={structure.schema ?? undefined}
          object={structure.name}
          columns={structure.columns.map((c) => c.name)}
          primaryKey={structure.primary_key}
        />
      )}
    </div>
  );
}
