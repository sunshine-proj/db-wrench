import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Alert, Badge, PageHeader, Spinner, Table, Td, Th } from "@/components/ui/misc";
import { useAdminCall } from "@/features/explorer/AdminActions";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { explorerApi, mongoPrincipalsApi, principalsApi } from "@/services/api";
import type { Connection, GrantTarget, MongoRole, MongoUser, Principal } from "@/types/api";

const DB_PRIVS = ["CONNECT", "CREATE", "TEMPORARY"];
const PG_SCHEMA_PRIVS = ["USAGE", "CREATE"];
const PG_TABLE_PRIVS = [
  "SELECT",
  "INSERT",
  "UPDATE",
  "DELETE",
  "TRUNCATE",
  "REFERENCES",
  "TRIGGER",
];
const MY_TABLE_PRIVS = [
  "SELECT",
  "INSERT",
  "UPDATE",
  "DELETE",
  "CREATE",
  "DROP",
  "ALTER",
  "INDEX",
  "REFERENCES",
  "CREATE VIEW",
  "SHOW VIEW",
  "TRIGGER",
];
const MY_DB_PRIVS = [
  ...MY_TABLE_PRIVS,
  "EXECUTE",
  "LOCK TABLES",
  "CREATE TEMPORARY TABLES",
  "CREATE ROUTINE",
  "ALTER ROUTINE",
  "EVENT",
];
const MS_OBJ_PRIVS = [
  "SELECT",
  "INSERT",
  "UPDATE",
  "DELETE",
  "REFERENCES",
  "VIEW DEFINITION",
  "ALTER",
];
const MS_SCHEMA_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "REFERENCES"];
const MS_DB_PRIVS = [
  ...MS_OBJ_PRIVS,
  "EXECUTE",
  "CREATE TABLE",
  "CREATE VIEW",
  "CREATE PROCEDURE",
  "CREATE FUNCTION",
  "CONNECT",
];
const MONGO_ACTIONS = [
  "find",
  "insert",
  "update",
  "remove",
  "createCollection",
  "dropCollection",
  "createIndex",
  "dropIndex",
  "listCollections",
  "listIndexes",
  "collStats",
  "dbStats",
];
const MONGO_BUILTIN = ["read", "readWrite", "dbAdmin", "dbOwner", "userAdmin"];

/** Privileges offered for an object kind on an engine (the server enforces the same allowlists). */
export function privilegesFor(engine: string, kind: GrantTarget["kind"]): string[] {
  if (engine === "mssql") {
    return kind === "database" ? MS_DB_PRIVS : kind === "schema" ? MS_SCHEMA_PRIVS : MS_OBJ_PRIVS;
  }
  if (engine === "mysql" || engine === "mariadb")
    return kind === "database" ? MY_DB_PRIVS : MY_TABLE_PRIVS;
  if (kind === "database") return DB_PRIVS;
  if (kind === "schema") return PG_SCHEMA_PRIVS;
  return PG_TABLE_PRIVS;
}

function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={title} className="max-w-2xl">
        {children}
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------ relational

function CreateDialog({ c, onClose }: { c: Connection; onClose: () => void }) {
  const pg = c.db_type === "postgresql";
  const mssql = c.db_type === "mssql";
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [host, setHost] = useState("%");
  const [login, setLogin] = useState(true);
  const [createdb, setCreatedb] = useState(false);
  const [createrole, setCreaterole] = useState(false);
  const [ssl, setSsl] = useState(false);
  const { m, picker } = useAdminCall(
    "admin.create_principal",
    c,
    () =>
      principalsApi.create(c.id, {
        name,
        ...(password ? { password } : {}),
        ...(pg || mssql ? {} : { host }),
        options: pg ? { login, createdb, createrole } : mssql ? {} : { require_ssl: ssl },
      }),
    onClose,
  );
  return (
    <Modal title={t("access.new")} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate(undefined);
        }}
      >
        <Field label={t("access.user")}>
          {(p) => <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        {!pg && !mssql && (
          <Field label={t("access.host")}>
            {(p) => <Input {...p} value={host} onChange={(e) => setHost(e.target.value)} />}
          </Field>
        )}
        <Field label={t("access.password")} hint={t("access.passwordHint")}>
          {(p) => (
            <Input
              {...p}
              type="password"
              autoComplete="new-password"
              required={!pg || login}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        {pg ? (
          <div className="space-y-1">
            <Checkbox
              label={t("access.login")}
              checked={login}
              onChange={(e) => setLogin(e.target.checked)}
            />
            <Checkbox
              label={t("access.createdb")}
              checked={createdb}
              onChange={(e) => setCreatedb(e.target.checked)}
            />
            <Checkbox
              label={t("access.createrole")}
              checked={createrole}
              onChange={(e) => setCreaterole(e.target.checked)}
            />
          </div>
        ) : (
          <Checkbox
            label={t("access.requireSsl")}
            checked={ssl}
            onChange={(e) => setSsl(e.target.checked)}
          />
        )}
        {picker}
        {m.error != null && <ErrorAlert error={m.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={m.isPending}>
            {t("common.create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function PasswordDialog({ c, p, onClose }: { c: Connection; p: Principal; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const { m, picker } = useAdminCall(
    "admin.alter_principal",
    c,
    () => principalsApi.alter(c.id, p.name, p.host, { password }),
    onClose,
  );
  return (
    <Modal title={`${t("access.changePassword")}: ${p.name}`} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate(undefined);
        }}
      >
        <Field label={t("access.password")} hint={t("access.passwordHint")}>
          {(f) => (
            <Input
              {...f}
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        {picker}
        {m.error != null && <ErrorAlert error={m.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={m.isPending}>
            {t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function GrantsDialog({ c, p, onClose }: { c: Connection; p: Principal; onClose: () => void }) {
  const pg = c.db_type === "postgresql";
  const mssql = c.db_type === "mssql";
  const hasSchemas = pg || mssql;
  const qc = useQueryClient();
  const [database, setDatabase] = useState(c.database ?? "");
  const [kind, setKind] = useState<GrantTarget["kind"]>("table");
  const [schema, setSchema] = useState(pg ? "public" : mssql ? "dbo" : "");
  const [object, setObject] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [confirm, setConfirm] = useState("");
  const needsName = c.environment === "production";
  const grants = useQuery({
    queryKey: ["principal-grants", c.id, p.name, p.host, database],
    queryFn: () => principalsApi.grants(c.id, p.name, p.host, database || undefined),
  });
  const target = (): GrantTarget => ({
    kind,
    database,
    ...(hasSchemas && kind !== "database" ? { schema } : {}),
    ...(kind === "table" ? { object } : {}),
  });
  const grant = useAdminCall(
    "admin.grant_privileges",
    c,
    (g: boolean) =>
      principalsApi.changePrivileges(c.id, p.name, p.host, g, {
        privileges: picked,
        target: target(),
        ...(needsName ? { confirm } : {}),
      }),
    () => void qc.invalidateQueries({ queryKey: ["principal-grants"] }),
  );
  const revoke = useAdminCall(
    "admin.revoke_privileges",
    c,
    (g: { kind: GrantTarget["kind"]; schema?: string; object?: string; privileges: string[] }) =>
      principalsApi.changePrivileges(c.id, p.name, p.host, false, {
        privileges: g.privileges,
        target: {
          kind: g.kind as GrantTarget["kind"],
          database,
          schema: g.schema,
          object: g.object,
        },
        ...(needsName ? { confirm } : {}),
      }),
    () => void qc.invalidateQueries({ queryKey: ["principal-grants"] }),
  );
  const member = useAdminCall(
    "admin.grant_privileges",
    c,
    (a: { role: string; grant: boolean }) =>
      principalsApi.changeMembership(
        c.id,
        p.name,
        a.role,
        a.grant,
        needsName ? confirm : undefined,
      ),
    () => void qc.invalidateQueries({ queryKey: ["principals", c.id] }),
  );
  const [role, setRole] = useState("");
  const options = privilegesFor(c.db_type, kind);
  const kinds: GrantTarget["kind"][] = pg
    ? ["database", "schema", "table", "all_tables"]
    : mssql
      ? ["database", "schema", "table"]
      : ["database", "table"];
  return (
    <Modal title={t("access.grants", { name: p.name })} onClose={onClose}>
      <div className="space-y-4">
        <Field label={t("scope.database")}>
          {(f) => <Input {...f} value={database} onChange={(e) => setDatabase(e.target.value)} />}
        </Field>
        {grants.isLoading && <Spinner label={t("common.loading")} />}
        {grants.error && <ErrorAlert error={grants.error} />}
        {grants.data && grants.data.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("access.noGrants")}</p>
        )}
        {grants.data && grants.data.length > 0 && (
          <Table>
            <thead>
              <tr>
                <Th>{t("access.targetKind")}</Th>
                <Th>{t("access.privileges")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {grants.data.map((g, i) => (
                <tr key={i}>
                  <Td>
                    {t(`access.kind.${g.kind}` as "access.kind.table")}{" "}
                    <span className="font-mono text-xs">
                      {[g.database, g.schema, g.object].filter(Boolean).join(".")}
                    </span>
                  </Td>
                  <Td className="text-xs">{g.privileges.join(", ")}</Td>
                  <Td>
                    {g.kind !== "global" && canOn(c, "admin.manage_privileges") && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={needsName && confirm !== p.name}
                        onClick={() =>
                          revoke.m.mutate({
                            kind: g.kind as GrantTarget["kind"],
                            schema: g.schema ?? undefined,
                            object: g.object ?? undefined,
                            privileges: g.privileges,
                          })
                        }
                      >
                        {t("access.revoke")}
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {canOn(c, "admin.manage_privileges") && (
          <form
            className="space-y-3 border-t pt-3"
            onSubmit={(e) => {
              e.preventDefault();
              grant.m.mutate(true);
            }}
          >
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label={t("access.targetKind")}>
                {(f) => (
                  <Select
                    {...f}
                    value={kind}
                    onChange={(e) => {
                      setKind(e.target.value as GrantTarget["kind"]);
                      setPicked([]);
                    }}
                  >
                    {kinds.map((k) => (
                      <option key={k} value={k}>
                        {t(`access.kind.${k}` as "access.kind.table")}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              {hasSchemas && kind !== "database" && (
                <Field label={t("scope.schema")}>
                  {(f) => (
                    <Input {...f} value={schema} onChange={(e) => setSchema(e.target.value)} />
                  )}
                </Field>
              )}
              {kind === "table" && (
                <Field label={t("scope.object")}>
                  {(f) => (
                    <Input
                      {...f}
                      required
                      value={object}
                      onChange={(e) => setObject(e.target.value)}
                    />
                  )}
                </Field>
              )}
            </div>
            <fieldset className="grid gap-1 sm:grid-cols-3">
              <legend className="sr-only">{t("access.privileges")}</legend>
              {options.map((o) => (
                <Checkbox
                  key={o}
                  label={o}
                  checked={picked.includes(o)}
                  onChange={(e) =>
                    setPicked(e.target.checked ? [...picked, o] : picked.filter((x) => x !== o))
                  }
                />
              ))}
            </fieldset>
            {needsName && (
              <Field label={t("access.confirmName", { name: p.name })}>
                {(f) => (
                  <Input {...f} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
                )}
              </Field>
            )}
            {grant.picker}
            {(grant.m.error ?? revoke.m.error ?? member.m.error) != null && (
              <ErrorAlert error={grant.m.error ?? revoke.m.error ?? member.m.error} />
            )}
            <Button
              type="submit"
              disabled={
                grant.m.isPending || picked.length === 0 || (needsName && confirm !== p.name)
              }
            >
              {t("access.grant")}
            </Button>
          </form>
        )}
        {pg && canOn(c, "admin.manage_privileges") && (
          <div className="space-y-2 border-t pt-3">
            <h3 className="text-sm font-semibold">{t("access.membership")}</h3>
            <div className="flex flex-wrap items-center gap-2">
              {p.member_of.map((r) => (
                <Button
                  key={r}
                  size="sm"
                  variant="outline"
                  disabled={needsName && confirm !== p.name}
                  onClick={() => member.m.mutate({ role: r, grant: false })}
                >
                  {r} ✕
                </Button>
              ))}
              <Input
                aria-label={t("access.addMembership")}
                className="w-40"
                placeholder={t("access.addMembership")}
                value={role}
                onChange={(e) => setRole(e.target.value)}
              />
              <Button
                size="sm"
                disabled={!role || (needsName && confirm !== p.name)}
                onClick={() => member.m.mutate({ role, grant: true })}
              >
                {t("access.grant")}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

function RelationalAccess({ c }: { c: Connection }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["principals", c.id], queryFn: () => principalsApi.list(c.id) });
  const [creating, setCreating] = useState(false);
  const [pwd, setPwd] = useState<Principal | null>(null);
  const [grants, setGrants] = useState<Principal | null>(null);
  const [dropping, setDropping] = useState<Principal | null>(null);
  const lock = useMutation({
    mutationFn: (a: { p: Principal; lock: boolean }) =>
      principalsApi.alter(c.id, a.p.name, a.p.host, { options: { lock: a.lock } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["principals", c.id] }),
  });
  const drop = useAdminCall(
    "admin.drop_principal",
    c,
    (p: Principal) => principalsApi.drop(c.id, p.name, p.host),
    () => void qc.invalidateQueries({ queryKey: ["principals", c.id] }),
  );
  const manage = canOn(c, "admin.manage_users");
  return (
    <div className="space-y-4">
      <PageHeader
        title={t("access.title")}
        description={t("access.description", { name: c.name })}
        actions={manage && <Button onClick={() => setCreating(true)}>{t("access.new")}</Button>}
      />
      {q.isLoading && <Spinner label={t("common.loading")} />}
      {q.error && <ErrorAlert error={q.error} />}
      {lock.error && <ErrorAlert error={lock.error} />}
      {q.data && (
        <Table>
          <thead>
            <tr>
              <Th>{t("access.user")}</Th>
              <Th>{t("access.memberOf")}</Th>
              <Th>{t("access.privileges")}</Th>
              <Th>{t("common.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((p) => (
              <tr key={`${p.name}@${p.host ?? ""}`}>
                <Td>
                  <span className="font-medium">{p.name}</span>
                  {p.host && <span className="text-muted-foreground">@{p.host}</span>}
                  <div className="mt-1 flex flex-wrap gap-1">
                    <Badge variant="outline">{p.kind}</Badge>
                    {!p.can_login && (
                      <Badge variant="secondary">{p.host ? t("access.locked") : "NOLOGIN"}</Badge>
                    )}
                    {p.protected && (
                      <Badge variant="warning" title={t("access.protectedHint")}>
                        {t("access.protected")}
                      </Badge>
                    )}
                    {p.attributes["superuser"] === true && (
                      <Badge variant="destructive">superuser</Badge>
                    )}
                    {p.attributes["createdb"] === true && (
                      <Badge variant="secondary">createdb</Badge>
                    )}
                    {p.attributes["createrole"] === true && (
                      <Badge variant="secondary">createrole</Badge>
                    )}
                  </div>
                </Td>
                <Td className="text-xs">{p.member_of.join(", ") || "—"}</Td>
                <Td>
                  <Button size="sm" variant="outline" onClick={() => setGrants(p)}>
                    {t("access.privileges")}
                  </Button>
                </Td>
                <Td>
                  {manage && !p.protected && (
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="outline" onClick={() => setPwd(p)}>
                        {t("access.changePassword")}
                      </Button>
                      {p.host && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => lock.mutate({ p, lock: p.can_login })}
                        >
                          {p.can_login ? t("access.lock") : t("access.unlock")}
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        aria-label={t("access.drop", { name: p.name })}
                        onClick={() => setDropping(p)}
                      >
                        {t("common.delete")}
                      </Button>
                    </div>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {creating && (
        <CreateDialog
          c={c}
          onClose={() => {
            setCreating(false);
            void qc.invalidateQueries({ queryKey: ["principals", c.id] });
          }}
        />
      )}
      {pwd && <PasswordDialog c={c} p={pwd} onClose={() => setPwd(null)} />}
      {grants && <GrantsDialog c={c} p={grants} onClose={() => setGrants(null)} />}
      {dropping && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDropping(null)}
          title={t("access.drop", { name: dropping.name })}
          description={
            <div className="space-y-2">
              <p>{t("access.dropWarning", { name: dropping.name })}</p>
            </div>
          }
          confirmLabel={t("common.delete")}
          requireText={dropping.name}
          onConfirm={() => drop.m.mutateAsync(dropping)}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- MongoDB

function MongoAccess({ c }: { c: Connection }) {
  const qc = useQueryClient();
  const dbs = useQuery({
    queryKey: ["databases", c.id],
    queryFn: () => explorerApi.mongoDatabases(c.id),
  });
  const [db, setDb] = useState("");
  const [tab, setTab] = useState<"users" | "roles">("users");
  const active =
    db || dbs.data?.find((d) => !["admin", "local", "config"].includes(d.name))?.name || "";
  const users = useQuery({
    queryKey: ["mongo-users", c.id, active],
    queryFn: () => mongoPrincipalsApi.users(c.id, active),
    enabled: !!active,
  });
  const roles = useQuery({
    queryKey: ["mongo-roles", c.id, active],
    queryFn: () => mongoPrincipalsApi.roles(c.id, active),
    enabled: !!active,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["mongo-users"] });
    void qc.invalidateQueries({ queryKey: ["mongo-roles"] });
  };
  const [newUser, setNewUser] = useState(false);
  const [newRole, setNewRole] = useState(false);
  const [pwdFor, setPwdFor] = useState<MongoUser | null>(null);
  const [dropping, setDropping] = useState<{ kind: "user" | "role"; name: string } | null>(null);
  const manage = canOn(c, "admin.manage_users");
  const manageRoles = canOn(c, "admin.manage_privileges");
  const protectedDb = ["admin", "local", "config"].includes(active);
  const change = useAdminCall(
    "admin.grant_privileges",
    c,
    (a: { u: MongoUser; role: string; rdb: string; grant: boolean }) =>
      mongoPrincipalsApi.changeRoles(
        c.id,
        active,
        a.u.name,
        [{ role: a.role, db: a.rdb }],
        a.grant,
      ),
    refresh,
  );
  const dropUser = useAdminCall(
    "admin.drop_principal",
    c,
    (a: { kind: "user" | "role"; name: string }) =>
      a.kind === "user"
        ? mongoPrincipalsApi.dropUser(c.id, active, a.name)
        : mongoPrincipalsApi.dropRole(c.id, active, a.name),
    refresh,
  );
  const [grantRole, setGrantRole] = useState("read");
  const assignable = [
    ...MONGO_BUILTIN,
    ...(roles.data ?? []).filter((r) => !r.builtin).map((r) => r.role),
  ];
  return (
    <div className="space-y-4">
      <PageHeader
        title={t("access.title")}
        description={t("access.description", { name: c.name })}
        actions={
          <>
            {tab === "users" && manage && !protectedDb && (
              <Button onClick={() => setNewUser(true)}>{t("access.newMongo")}</Button>
            )}
            {tab === "roles" && manageRoles && !protectedDb && (
              <Button onClick={() => setNewRole(true)}>{t("access.newRole")}</Button>
            )}
          </>
        }
      />
      <div className="flex flex-wrap items-end gap-3">
        <Field label={t("access.mongoDatabase")} className="w-64">
          {(f) => (
            <Select {...f} value={active} onChange={(e) => setDb(e.target.value)}>
              {(dbs.data ?? []).map((d) => (
                <option key={d.name} value={d.name}>
                  {d.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div role="tablist" className="flex gap-1">
          {(["users", "roles"] as const).map((id) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
              className={`border-b-2 px-3 py-2 text-sm font-medium ${tab === id ? "border-primary" : "border-transparent text-muted-foreground"}`}
            >
              {t(id === "users" ? "access.users" : "access.roles")}
            </button>
          ))}
        </div>
      </div>
      {protectedDb && <Alert variant="info">{t("access.systemDb")}</Alert>}
      {dropUser.m.error != null && <ErrorAlert error={dropUser.m.error} />}
      {change.m.error != null && <ErrorAlert error={change.m.error} />}
      {tab === "users" && (
        <>
          {users.error && <ErrorAlert error={users.error} />}
          <Table>
            <thead>
              <tr>
                <Th>{t("access.user")}</Th>
                <Th>{t("access.rolesOf")}</Th>
                <Th>{t("common.actions")}</Th>
              </tr>
            </thead>
            <tbody>
              {(users.data ?? []).map((u) => (
                <tr key={u.name}>
                  <Td className="font-medium">
                    {u.name}
                    {u.protected && (
                      <Badge className="ml-2" variant="warning">
                        {t("access.protected")}
                      </Badge>
                    )}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.map((r) => (
                        <Button
                          key={`${r.role}@${r.db}`}
                          size="sm"
                          variant="outline"
                          disabled={u.protected || !manageRoles}
                          aria-label={`${t("access.revoke")} ${r.role}`}
                          onClick={() =>
                            change.m.mutate({ u, role: r.role, rdb: r.db, grant: false })
                          }
                        >
                          {r.role}@{r.db} ✕
                        </Button>
                      ))}
                    </div>
                  </Td>
                  <Td>
                    {!u.protected && (
                      <div className="flex flex-wrap items-center gap-2">
                        {manageRoles && (
                          <>
                            <Select
                              aria-label={t("access.addRole")}
                              className="w-36"
                              value={grantRole}
                              onChange={(e) => setGrantRole(e.target.value)}
                            >
                              {assignable.map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </Select>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() =>
                                change.m.mutate({ u, role: grantRole, rdb: active, grant: true })
                              }
                            >
                              {t("access.addRole")}
                            </Button>
                          </>
                        )}
                        {manage && (
                          <Button size="sm" variant="outline" onClick={() => setPwdFor(u)}>
                            {t("access.changePassword")}
                          </Button>
                        )}
                        {manage && (
                          <Button
                            size="sm"
                            variant="outline"
                            aria-label={t("access.drop", { name: u.name })}
                            onClick={() => setDropping({ kind: "user", name: u.name })}
                          >
                            {t("common.delete")}
                          </Button>
                        )}
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </>
      )}
      {tab === "roles" && (
        <Table>
          <thead>
            <tr>
              <Th>{t("access.roles")}</Th>
              <Th>{t("access.privileges")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {(roles.data ?? [])
              .filter((r) => !r.builtin)
              .map((r: MongoRole) => (
                <tr key={r.role}>
                  <Td className="font-medium">
                    {r.role} <Badge variant="secondary">{t("access.custom")}</Badge>
                  </Td>
                  <Td className="text-xs">
                    {r.privileges
                      .map((p) => `${p.resource.collection || "*"}: ${p.actions.join(", ")}`)
                      .join(" · ")}
                  </Td>
                  <Td>
                    {manageRoles && (
                      <Button
                        size="sm"
                        variant="outline"
                        aria-label={t("access.drop", { name: r.role })}
                        onClick={() => setDropping({ kind: "role", name: r.role })}
                      >
                        {t("common.delete")}
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            {(roles.data ?? [])
              .filter((r) => r.builtin && MONGO_BUILTIN.includes(r.role))
              .map((r) => (
                <tr key={r.role}>
                  <Td className="font-medium">
                    {r.role} <Badge variant="outline">{t("access.builtin")}</Badge>
                  </Td>
                  <Td />
                  <Td />
                </tr>
              ))}
          </tbody>
        </Table>
      )}
      {newUser && (
        <MongoUserDialog
          c={c}
          db={active}
          roles={assignable}
          onClose={() => {
            setNewUser(false);
            refresh();
          }}
        />
      )}
      {newRole && (
        <MongoRoleDialog
          c={c}
          db={active}
          onClose={() => {
            setNewRole(false);
            refresh();
          }}
        />
      )}
      {pwdFor && (
        <MongoPasswordDialog c={c} db={active} u={pwdFor} onClose={() => setPwdFor(null)} />
      )}
      {dropping && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDropping(null)}
          title={t("access.drop", { name: dropping.name })}
          description={t("access.dropWarning", { name: dropping.name })}
          confirmLabel={t("common.delete")}
          requireText={dropping.name}
          onConfirm={() => dropUser.m.mutateAsync(dropping)}
        />
      )}
    </div>
  );
}

function MongoUserDialog({
  c,
  db,
  roles,
  onClose,
}: {
  c: Connection;
  db: string;
  roles: string[];
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [picked, setPicked] = useState<string[]>(["read"]);
  const { m, picker } = useAdminCall(
    "admin.create_principal",
    c,
    () =>
      mongoPrincipalsApi.createUser(c.id, db, {
        name,
        password,
        roles: picked.map((role) => ({ role })),
      }),
    onClose,
  );
  return (
    <Modal title={t("access.newMongo")} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate(undefined);
        }}
      >
        <Field label={t("access.user")}>
          {(f) => <Input {...f} required value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label={t("access.password")} hint={t("access.passwordHint")}>
          {(f) => (
            <Input
              {...f}
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <fieldset className="grid gap-1 sm:grid-cols-3">
          <legend className="text-sm font-medium">{t("access.rolesOf")}</legend>
          {roles.map((r) => (
            <Checkbox
              key={r}
              label={r}
              checked={picked.includes(r)}
              onChange={(e) =>
                setPicked(e.target.checked ? [...picked, r] : picked.filter((x) => x !== r))
              }
            />
          ))}
        </fieldset>
        {picker}
        {m.error != null && <ErrorAlert error={m.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={m.isPending || picked.length === 0}>
            {t("common.create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function MongoPasswordDialog({
  c,
  db,
  u,
  onClose,
}: {
  c: Connection;
  db: string;
  u: MongoUser;
  onClose: () => void;
}) {
  const [password, setPassword] = useState("");
  const { m, picker } = useAdminCall(
    "admin.alter_principal",
    c,
    () => mongoPrincipalsApi.setPassword(c.id, db, u.name, password),
    onClose,
  );
  return (
    <Modal title={`${t("access.changePassword")}: ${u.name}`} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate(undefined);
        }}
      >
        <Field label={t("access.password")} hint={t("access.passwordHint")}>
          {(f) => (
            <Input
              {...f}
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        {picker}
        {m.error != null && <ErrorAlert error={m.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={m.isPending}>
            {t("common.save")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function MongoRoleDialog({ c, db, onClose }: { c: Connection; db: string; onClose: () => void }) {
  const [name, setName] = useState("");
  const [collection, setCollection] = useState("");
  const [actions, setActions] = useState<string[]>(["find"]);
  const { m, picker } = useAdminCall(
    "admin.create_principal",
    c,
    () => mongoPrincipalsApi.createRole(c.id, db, { name, privileges: [{ collection, actions }] }),
    onClose,
  );
  return (
    <Modal title={t("access.newRole")} onClose={onClose}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate(undefined);
        }}
      >
        <Field label={t("access.user")}>
          {(f) => <Input {...f} required value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label={t("access.collection")}>
          {(f) => (
            <Input {...f} value={collection} onChange={(e) => setCollection(e.target.value)} />
          )}
        </Field>
        <fieldset className="grid gap-1 sm:grid-cols-3">
          <legend className="text-sm font-medium">{t("access.actions")}</legend>
          {MONGO_ACTIONS.map((a) => (
            <Checkbox
              key={a}
              label={a}
              checked={actions.includes(a)}
              onChange={(e) =>
                setActions(e.target.checked ? [...actions, a] : actions.filter((x) => x !== a))
              }
            />
          ))}
        </fieldset>
        {picker}
        {m.error != null && <ErrorAlert error={m.error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" disabled={m.isPending || actions.length === 0}>
            {t("common.create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function PrincipalsPage() {
  const c = useOutletContext<Connection>();
  if (!canOn(c, "admin.view_users")) {
    return <Alert variant="warning">{t("common.forbiddenBody")}</Alert>;
  }
  return c.db_type === "mongodb" ? <MongoAccess c={c} /> : <RelationalAccess c={c} />;
}
