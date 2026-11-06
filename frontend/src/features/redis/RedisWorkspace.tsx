import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EnvBanner } from "@/components/EnvBadge";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Alert, Badge, Card, PageHeader, Spinner } from "@/components/ui/misc";
import { canOn } from "@/hooks/useAuth";
import { t } from "@/i18n";
import { connectionsApi, redisApi } from "@/services/api";
import type { Connection, RedisKeyDetail, RedisKeyInfo, RedisKeyType } from "@/types/api";

const TYPES: RedisKeyType[] = ["string", "hash", "list", "set", "zset", "stream"];

/** Human-readable TTL; the server sends milliseconds. */
export function formatTtl(ms: number | null): string {
  if (ms === null) return t("redis.noExpiry");
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 129600) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/** The JSON shown in the editor for a value; text stays text, collections become JSON. */
export function editableText(d: Pick<RedisKeyDetail, "type" | "value">): string {
  const v = d.value.value;
  return d.type === "string" ? String(v ?? "") : JSON.stringify(v, null, 2);
}

/** Parses the editor text back into the value the API expects; throws on invalid JSON. */
export function parseEditable(type: RedisKeyType, text: string): unknown {
  return type === "string" ? text : JSON.parse(text);
}

function Value({ detail }: { detail: RedisKeyDetail }) {
  // Database content is only ever rendered as text (React escapes it); never as HTML.
  const v = detail.value.value;
  return (
    <pre
      className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted p-3 text-xs"
      data-testid="redis-value"
    >
      {typeof v === "string" ? v : JSON.stringify(v, null, 2)}
    </pre>
  );
}

function KeyPanel({
  cid,
  db,
  keyId,
  connection,
  onGone,
}: {
  cid: string;
  db: string;
  keyId: string;
  connection: Connection;
  onGone: () => void;
}) {
  const qc = useQueryClient();
  const [full, setFull] = useState(false);
  const [more, setMore] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["redis-key", cid, db, keyId, full, more],
    queryFn: () => redisApi.key(cid, db, keyId, { full, cursor: more }),
  });
  const [draft, setDraft] = useState<string | null>(null);
  const [ttl, setTtl] = useState("");
  const [del, setDel] = useState(false);
  const d = detail.data;
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["redis-key", cid, db, keyId] });
    void qc.invalidateQueries({ queryKey: ["redis-keys", cid, db] });
  };
  const save = useMutation({
    mutationFn: () => {
      if (!d || draft === null) throw new Error("nothing to save");
      return redisApi.replace(cid, db, keyId, {
        type: d.type,
        value: parseEditable(d.type, draft),
        version: d.version,
      });
    },
    onSuccess: () => {
      setDraft(null);
      refresh();
    },
  });
  const ttlM = useMutation({
    mutationFn: (ms: number | null) => redisApi.setTtl(cid, db, keyId, ms),
    onSuccess: () => {
      setTtl("");
      refresh();
    },
  });
  const writable = !connection.read_only;
  if (detail.isLoading) return <Spinner label={t("common.loading")} />;
  if (detail.error) return <ErrorAlert error={detail.error} />;
  if (!d) return null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 break-all text-base font-semibold">{d.name}</h2>
        <Badge>{d.type}</Badge>
        {d.binary && <Badge variant="outline">{t("redis.binaryKey")}</Badge>}
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs text-muted-foreground">{t("redis.ttl")}</dt>
          <dd>{formatTtl(d.ttl_ms)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("redis.size")}</dt>
          <dd>{d.size_bytes ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("redis.encoding")}</dt>
          <dd>{d.encoding ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("redis.length")}</dt>
          <dd>{d.value.length ?? "—"}</dd>
        </div>
      </dl>
      {draft === null ? (
        <>
          <Value detail={d} />
          {d.value.truncated && (
            <Alert variant="info">
              {t("redis.truncated")}{" "}
              {!full && (
                <Button size="sm" variant="outline" onClick={() => setFull(true)}>
                  {t("redis.moreValues")}
                </Button>
              )}
            </Alert>
          )}
          {d.value.next_cursor && (
            <Button size="sm" variant="outline" onClick={() => setMore(d.value.next_cursor)}>
              {t("redis.moreValues")}
            </Button>
          )}
        </>
      ) : (
        <Field
          label={t("redis.valueEditor")}
          hint={d.type === "string" ? undefined : t("redis.jsonHint")}
        >
          {(p) => (
            <Textarea
              {...p}
              rows={12}
              className="font-mono text-xs"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
          )}
        </Field>
      )}
      {save.error && <ErrorAlert error={save.error} />}
      {ttlM.error && <ErrorAlert error={ttlM.error} />}
      {writable && canOn(connection, "data.update") && (
        <div className="flex flex-wrap items-end gap-2">
          {draft === null ? (
            <Button
              size="sm"
              disabled={!d.editable}
              title={d.editable ? undefined : t("redis.notEditable")}
              onClick={() => setDraft(editableText(d))}
            >
              {t("redis.edit")}
            </Button>
          ) : (
            <>
              <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
                {t("redis.save")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setDraft(null)}>
                {t("redis.cancelEdit")}
              </Button>
            </>
          )}
          <Field label={t("redis.setTtl")} className="w-40">
            {(p) => (
              <Input
                {...p}
                inputMode="numeric"
                value={ttl}
                onChange={(e) => setTtl(e.target.value.replace(/\D/g, ""))}
              />
            )}
          </Field>
          <Button
            size="sm"
            variant="outline"
            disabled={!ttl || ttlM.isPending}
            onClick={() => ttlM.mutate(Number(ttl) * 1000)}
          >
            {t("redis.apply")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={d.ttl_ms === null || ttlM.isPending}
            onClick={() => ttlM.mutate(null)}
          >
            {t("redis.persist")}
          </Button>
        </div>
      )}
      {writable && canOn(connection, "data.delete") && (
        <>
          <Button size="sm" variant="destructive" onClick={() => setDel(true)}>
            {t("redis.deleteKey")}
          </Button>
          <ConfirmDialog
            open={del}
            onOpenChange={setDel}
            title={t("redis.deleteKey")}
            description={t("redis.deleteBody", { name: d.name })}
            confirmLabel={t("common.delete")}
            requireText={connection.environment === "production" ? d.name : undefined}
            onConfirm={async () => {
              await redisApi.remove(cid, db, keyId, {
                version: d.version,
                confirm: connection.environment === "production" ? d.name : undefined,
              });
              onGone();
              refresh();
            }}
          />
        </>
      )}
    </div>
  );
}

function NewKey({
  cid,
  db,
  onCreated,
}: {
  cid: string;
  db: string;
  onCreated: (k: string) => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<RedisKeyType>("string");
  const [value, setValue] = useState("");
  const create = useMutation({
    mutationFn: () =>
      redisApi.create(cid, db, {
        name,
        type,
        value: type === "string" ? value : JSON.parse(value),
      }),
    onSuccess: (d) => {
      setName("");
      setValue("");
      onCreated(d.key);
    },
  });
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <h3 className="text-sm font-medium">{t("redis.newKey")}</h3>
      <Field label={t("redis.newName")}>
        {(p) => <Input {...p} required value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label={t("redis.type")}>
        {(p) => (
          <Select {...p} value={type} onChange={(e) => setType(e.target.value as RedisKeyType)}>
            {TYPES.filter((x) => x !== "stream").map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </Select>
        )}
      </Field>
      <Field label={t("redis.newValue")} hint={type === "string" ? undefined : t("redis.jsonHint")}>
        {(p) => (
          <Textarea
            {...p}
            required
            rows={4}
            className="font-mono text-xs"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        )}
      </Field>
      {create.error && <ErrorAlert error={create.error} />}
      <Button type="submit" size="sm" disabled={create.isPending}>
        {t("redis.createKey")}
      </Button>
    </form>
  );
}

function Keys({ connection, cid, db }: { connection: Connection; cid: string; db: string }) {
  const [pattern, setPattern] = useState("");
  const [type, setType] = useState("");
  const [applied, setApplied] = useState({ pattern: "", type: "" });
  const [selected, setSelected] = useState<string | null>(null);
  const [pages, setPages] = useState<RedisKeyInfo[][]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["redis-keys", cid, db, applied, cursor],
    queryFn: async () => {
      const page = await redisApi.keys(cid, db, {
        pattern: applied.pattern || undefined,
        type: applied.type || undefined,
        cursor,
        limit: 100,
      });
      setPages((prev) => (cursor ? [...prev, page.keys] : [page.keys]));
      setNext(page.next_cursor);
      return page;
    },
  });
  const all = pages.flat();
  const canWrite = !connection.read_only && canOn(connection, "data.insert");
  return (
    <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
      <div className="space-y-3">
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            setCursor(null);
            setPages([]);
            setApplied({ pattern, type });
          }}
        >
          <Field label={t("redis.pattern")} hint={t("redis.patternHint")}>
            {(p) => <Input {...p} value={pattern} onChange={(e) => setPattern(e.target.value)} />}
          </Field>
          <Field label={t("redis.type")}>
            {(p) => (
              <Select {...p} value={type} onChange={(e) => setType(e.target.value)}>
                <option value="">{t("redis.anyType")}</option>
                {TYPES.map((x) => (
                  <option key={x} value={x}>
                    {x}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Button type="submit" size="sm">
            {t("redis.search")}
          </Button>
        </form>
        {q.error && <ErrorAlert error={q.error} />}
        {q.isLoading && <Spinner label={t("common.loading")} />}
        {q.data?.total_estimate != null && (
          <p className="text-xs text-muted-foreground">
            {t("redis.total", { n: q.data.total_estimate })}
          </p>
        )}
        {q.data?.warnings.map((w) => (
          <Alert key={w} variant="warning">
            {w}
          </Alert>
        ))}
        {!q.isLoading && all.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("redis.noKeys")}</p>
        )}
        <ul
          aria-label={t("redis.keyList")}
          className="max-h-[28rem] divide-y overflow-auto rounded-md border"
        >
          {all.map((k) => (
            <li key={k.key}>
              <button
                type="button"
                aria-current={selected === k.key}
                className={`flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted ${
                  selected === k.key ? "bg-accent" : ""
                }`}
                onClick={() => setSelected(k.key)}
              >
                <span className="min-w-0 truncate">{k.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {k.type} · {formatTtl(k.ttl_ms)}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {next && (
          <Button
            size="sm"
            variant="outline"
            disabled={q.isFetching}
            onClick={() => setCursor(next)}
          >
            {t("redis.loadMore")}
          </Button>
        )}
        {canWrite && (
          <Card className="p-3">
            <NewKey cid={cid} db={db} onCreated={(k) => setSelected(k)} />
          </Card>
        )}
      </div>
      <Card className="min-w-0 p-4">
        {selected ? (
          <KeyPanel
            key={selected}
            cid={cid}
            db={db}
            keyId={selected}
            connection={connection}
            onGone={() => setSelected(null)}
          />
        ) : (
          <p className="text-sm text-muted-foreground">{t("redis.pickKey")}</p>
        )}
      </Card>
    </div>
  );
}

function Console({ connection, cid, db }: { connection: Connection; cid: string; db: string }) {
  const [line, setLine] = useState("");
  const [destructive, setDestructive] = useState(false);
  const run = useMutation({
    mutationFn: () =>
      redisApi.execute(cid, {
        database: db,
        command: line,
        confirm_destructive: destructive || undefined,
        confirm: connection.environment === "production" ? connection.name : undefined,
      }),
  });
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        run.mutate();
      }}
    >
      <Field label={t("redis.console")} hint={t("redis.consoleHint")}>
        {(p) => (
          <Input
            {...p}
            className="font-mono"
            autoComplete="off"
            spellCheck={false}
            value={line}
            onChange={(e) => setLine(e.target.value)}
          />
        )}
      </Field>
      <Checkbox
        label={t("redis.confirmDestructive")}
        checked={destructive}
        onChange={(e) => setDestructive(e.target.checked)}
      />
      <Button type="submit" size="sm" disabled={!line.trim() || run.isPending}>
        {t("redis.run")}
      </Button>
      {run.error && <ErrorAlert error={run.error} />}
      {run.data && (
        <div>
          <p className="text-xs text-muted-foreground">
            {t("redis.reply")} · {run.data.command} · {run.data.elapsed_ms} ms
          </p>
          <pre
            className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted p-3 text-xs"
            data-testid="redis-reply"
          >
            {JSON.stringify(run.data.reply, null, 2)}
          </pre>
          {run.data.truncated && <Alert variant="info">{t("redis.truncated")}</Alert>}
        </div>
      )}
    </form>
  );
}

/** `/redis/:cid`: key browser, value viewer/editor and command console for one connection. */
export function RedisWorkspace() {
  const { cid = "" } = useParams();
  const [tab, setTab] = useState<"keys" | "console">("keys");
  const [db, setDb] = useState("");
  const list = useQuery({ queryKey: ["connections"], queryFn: connectionsApi.list });
  const connection = list.data?.find((c) => c.id === cid);
  const dbs = useQuery({
    queryKey: ["redis-dbs", cid],
    queryFn: () => redisApi.databases(cid),
    enabled: connection?.db_type === "redis",
  });
  if (list.isLoading) return <Spinner label={t("common.loading")} />;
  if (list.error) return <ErrorAlert error={list.error} />;
  if (!connection || connection.db_type !== "redis")
    return <Alert variant="destructive">{t("redis.notRedis")}</Alert>;
  if (!canOn(connection, "data.read"))
    return <Alert variant="warning">{t("redis.noPermission")}</Alert>;
  const active = db || dbs.data?.[0]?.name || "0";
  return (
    <div className="flex min-h-screen flex-col">
      <EnvBanner
        env={connection.environment}
        name={connection.name}
        readOnly={connection.read_only}
      />
      <div className="space-y-4 p-4 md:p-6">
        <PageHeader title={t("redis.title")} description={connection.name} />
        {dbs.error && <ErrorAlert error={dbs.error} />}
        <div className="flex flex-wrap items-end gap-3">
          <Field label={t("redis.database")} className="w-40">
            {(p) => (
              <Select {...p} value={active} onChange={(e) => setDb(e.target.value)}>
                {(dbs.data ?? [{ name: "0", keys: 0 }]).map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name} ({d.keys})
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <div role="tablist" className="flex gap-1">
            {(["keys", "console"] as const).map((x) => (
              <Button
                key={x}
                role="tab"
                aria-selected={tab === x}
                size="sm"
                variant={tab === x ? "default" : "outline"}
                onClick={() => setTab(x)}
              >
                {x === "keys" ? t("redis.keysTab") : t("redis.consoleTab")}
              </Button>
            ))}
          </div>
        </div>
        {tab === "keys" ? (
          <Keys key={active} connection={connection} cid={cid} db={active} />
        ) : (
          <Console key={active} connection={connection} cid={cid} db={active} />
        )}
      </div>
    </div>
  );
}
