import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Select } from "@/components/ui/form";
import { Spinner, Table, Td, Th } from "@/components/ui/misc";
import { t } from "@/i18n";
import { connectionsApi, rolesApi, usersApi } from "@/services/api";
import type { Connection } from "@/types/api";

interface Row {
  user_id: string;
  username: string;
  role_id: string;
}

export function GrantsDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const grants = useQuery({
    queryKey: ["grants", connection.id],
    queryFn: () => connectionsApi.grants(connection.id),
    enabled: open,
  });
  const directory = useQuery({
    queryKey: ["directory"],
    queryFn: usersApi.directory,
    enabled: open,
  });
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list, enabled: open });
  const [rows, setRows] = useState<Row[]>([]);
  const [newUser, setNewUser] = useState("");

  useEffect(() => {
    if (grants.data)
      setRows(
        grants.data.map((g) => ({ user_id: g.user_id, username: g.username, role_id: g.role_id })),
      );
  }, [grants.data]);

  const save = useMutation({
    mutationFn: () =>
      connectionsApi.setGrants(
        connection.id,
        rows.map((r) => ({ user_id: r.user_id, role_id: r.role_id })),
      ),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["grants", connection.id] });
      onOpenChange(false);
    },
  });

  const available = (directory.data ?? []).filter((u) => !rows.some((r) => r.user_id === u.id));
  const defaultRole = roles.data?.find((r) => r.name === "Viewer")?.id ?? roles.data?.[0]?.id ?? "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={t("grants.title", { name: connection.name })}
        description={t("grants.description")}
        className="max-w-2xl"
      >
        {(grants.isLoading || directory.isLoading || roles.isLoading) && (
          <Spinner label={t("common.loading")} />
        )}
        {(grants.error || directory.error || roles.error) && (
          <ErrorAlert error={grants.error ?? directory.error ?? roles.error} />
        )}
        {grants.data && roles.data && (
          <div className="space-y-4">
            <Table>
              <thead>
                <tr>
                  <Th>{t("grants.user")}</Th>
                  <Th>{t("grants.role")}</Th>
                  <Th>
                    <span className="sr-only">{t("common.actions")}</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <Td colSpan={3} className="text-muted-foreground">
                      {t("grants.none")}
                    </Td>
                  </tr>
                )}
                {rows.map((r) => (
                  <tr key={r.user_id}>
                    <Td>{r.username}</Td>
                    <Td>
                      <Select
                        aria-label={t("grants.roleFor", { name: r.username })}
                        value={r.role_id}
                        onChange={(e) =>
                          setRows((rs) =>
                            rs.map((x) =>
                              x.user_id === r.user_id ? { ...x, role_id: e.target.value } : x,
                            ),
                          )
                        }
                      >
                        {roles.data.map((role) => (
                          <option key={role.id} value={role.id}>
                            {role.name}
                          </option>
                        ))}
                      </Select>
                    </Td>
                    <Td className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t("grants.remove", { name: r.username })}
                        onClick={() => setRows((rs) => rs.filter((x) => x.user_id !== r.user_id))}
                      >
                        <Trash2 />
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <div className="flex gap-2">
              <Select
                aria-label={t("grants.addUser")}
                value={newUser}
                onChange={(e) => setNewUser(e.target.value)}
              >
                <option value="">{t("grants.addUser")}…</option>
                {available.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.username}
                  </option>
                ))}
              </Select>
              <Button
                variant="outline"
                disabled={!newUser}
                onClick={() => {
                  const u = available.find((x) => x.id === newUser);
                  if (u)
                    setRows((rs) => [
                      ...rs,
                      { user_id: u.id, username: u.username, role_id: defaultRole },
                    ]);
                  setNewUser("");
                }}
              >
                {t("common.add")}
              </Button>
            </div>
            {save.error && <ErrorAlert error={save.error} />}
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                {t("common.cancel")}
              </Button>
              <Button onClick={() => save.mutate()} disabled={save.isPending}>
                {t("common.save")}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
