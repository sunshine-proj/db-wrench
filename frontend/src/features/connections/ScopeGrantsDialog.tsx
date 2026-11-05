import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/form";
import { Spinner, Table, Td, Th } from "@/components/ui/misc";
import { t } from "@/i18n";
import { connectionsApi, rolesApi, usersApi } from "@/services/api";
import type { Connection, ScopeGrant } from "@/types/api";

type Row = Pick<
  ScopeGrant,
  "user_id" | "username" | "role_id" | "role" | "database" | "schema" | "object"
>;

/** Grants of a role on one database, schema or table/collection (additive to connection access). */
export function ScopeGrantsDialog({
  connection,
  onClose,
}: {
  connection: Connection;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const hasSchemas = connection.capabilities.supports_schemas;
  const grants = useQuery({
    queryKey: ["scope-grants", connection.id],
    queryFn: () => connectionsApi.scopeGrants(connection.id),
  });
  const directory = useQuery({ queryKey: ["directory"], queryFn: usersApi.directory });
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list });
  const [rows, setRows] = useState<Row[]>([]);
  const [user, setUser] = useState("");
  const [role, setRole] = useState("");
  const [database, setDatabase] = useState("");
  const [schema, setSchema] = useState("");
  const [object, setObject] = useState("");
  useEffect(() => {
    if (grants.data) setRows(grants.data);
  }, [grants.data]);

  const save = useMutation({
    mutationFn: () =>
      connectionsApi.setScopeGrants(
        connection.id,
        rows.map((r) => ({
          user_id: r.user_id,
          role_id: r.role_id,
          database: r.database,
          schema: r.schema,
          object: r.object,
        })),
      ),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["scope-grants", connection.id] });
      onClose();
    },
  });
  const assignable = (roles.data ?? []).filter((r) => r.name !== "Super Admin");

  function add() {
    const u = directory.data?.find((d) => d.id === user);
    const r = assignable.find((x) => x.id === role);
    if (!u || !r || !database.trim()) return;
    setRows([
      ...rows,
      {
        user_id: u.id,
        username: u.username,
        role_id: r.id,
        role: r.name,
        database: database.trim(),
        schema: schema.trim(),
        object: object.trim(),
      },
    ]);
    setObject("");
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        title={`${t("scope.title")}: ${connection.name}`}
        description={t("scope.description")}
        className="max-w-3xl"
      >
        {(grants.isLoading || directory.isLoading) && <Spinner label={t("common.loading")} />}
        {grants.error && <ErrorAlert error={grants.error} />}
        <div className="space-y-3">
          {rows.length > 0 && (
            <Table>
              <thead>
                <tr>
                  <Th>{t("scope.user")}</Th>
                  <Th>{t("scope.role")}</Th>
                  <Th>{t("scope.database")}</Th>
                  {hasSchemas && <Th>{t("scope.schema")}</Th>}
                  <Th>{t("scope.object")}</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.user_id}-${r.database}-${r.schema}-${r.object}-${i}`}>
                    <Td>{r.username}</Td>
                    <Td>{r.role}</Td>
                    <Td>{r.database}</Td>
                    {hasSchemas && <Td>{r.schema || t("scope.whole")}</Td>}
                    <Td>{r.object || t("scope.whole")}</Td>
                    <Td>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t("common.deleteItem", { name: r.username })}
                        onClick={() => setRows(rows.filter((_, j) => j !== i))}
                      >
                        <Trash2 />
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
          <div className="grid gap-2 sm:grid-cols-2">
            <Select
              aria-label={t("scope.user")}
              value={user}
              onChange={(e) => setUser(e.target.value)}
            >
              <option value="">{t("scope.user")}</option>
              {(directory.data ?? []).map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}
                </option>
              ))}
            </Select>
            <Select
              aria-label={t("scope.role")}
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="">{t("scope.role")}</option>
              {assignable.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </Select>
            <Input
              aria-label={t("scope.database")}
              placeholder={t("scope.database")}
              value={database}
              onChange={(e) => setDatabase(e.target.value)}
            />
            {hasSchemas && (
              <Input
                aria-label={t("scope.schema")}
                placeholder={t("scope.schema")}
                value={schema}
                onChange={(e) => setSchema(e.target.value)}
              />
            )}
            <Input
              aria-label={t("scope.object")}
              placeholder={t("scope.object")}
              value={object}
              onChange={(e) => setObject(e.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              onClick={add}
              disabled={!user || !role || !database.trim()}
            >
              {t("scope.add")}
            </Button>
          </div>
          {save.error && <ErrorAlert error={save.error} />}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button disabled={save.isPending} onClick={() => save.mutate()}>
              {t("common.save")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
