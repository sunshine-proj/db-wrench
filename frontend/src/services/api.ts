/**
 * The only module that talks to the backend. Components never call `fetch` directly.
 *
 * Authentication is a server-side session in an HttpOnly cookie; the CSRF token returned by
 * the session endpoints is attached to every state-changing request.
 */
import type {
  ApiTokenCreated,
  ApiTokenInfo,
  Approval,
  ApprovalPolicy,
  AuditSink,
  CredentialsStatus,
  CsvImportOptions,
  CsvPreview,
  DocImportPreview,
  OidcConfig,
  OidcPublic,
  PoliciesResponse,
  ScopeGrant,
  AuditEvent,
  BulkPreview,
  DocBulkPreview,
  ExportFormat,
  HistoryEntry,
  HistorySettings,
  MongoExecuteRequest,
  MongoExecuteResult,
  RowResult,
  SavedQuery,
  Selector,
  SensitiveMarker,
  SqlExecuteRequest,
  SqlExecuteResult,
  CollectionDetail,
  CollectionInfo,
  Connection,
  ConnectionInput,
  DatabaseInfo,
  DirectoryUser,
  DocPage,
  DocRequest,
  FunctionInfo,
  Grant,
  LoginResponse,
  NetworkPolicy,
  Problem,
  Role,
  RowPage,
  RowRequest,
  SchemaInfo,
  SequenceInfo,
  SessionInfo,
  SessionItem,
  TableInfo,
  TableStructure,
  TestResult,
  User,
} from "@/types/api";

const BASE = "/api/v1";
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title);
    this.name = "ApiError";
    this.status = problem.status;
    this.code = problem.code;
    this.requestId = problem.request_id;
    this.problem = problem;
  }
}

let csrfToken: string | null = null;
let approvalOverride: string | null = null;

/**
 * Repeat a request with an approved request's id (four-eyes workflow). The header is read
 * synchronously when the request starts, so it never leaks into concurrent requests.
 */
export function withApproval<T>(id: string | undefined, fn: () => Promise<T>): Promise<T> {
  if (!id) return fn();
  approvalOverride = id;
  try {
    return fn();
  } finally {
    approvalOverride = null;
  }
}
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

type Params = Record<string, string | number | boolean | null | undefined>;

function query(params?: Params): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

async function request<T>(
  method: string,
  path: string,
  opts: { body?: unknown; params?: Params } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (!SAFE.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (approvalOverride) headers["Approval-Id"] = approvalOverride;

  let response: Response;
  try {
    response = await fetch(`${BASE}/${path}${query(opts.params)}`, {
      method,
      headers,
      credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw new ApiError({
      type: "about:blank",
      title: "Network error",
      status: 0,
      code: "network_error",
      detail: "The server could not be reached.",
    });
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = undefined;
  }
  if (!response.ok) {
    const problem: Problem = isProblem(data)
      ? data
      : {
          type: "about:blank",
          title: response.statusText || "Request failed",
          status: response.status,
          code: `http_${response.status}`,
        };
    if (response.status === 401 && !path.startsWith("auth/login")) unauthorizedHandler?.();
    throw new ApiError(problem);
  }
  return data as T;
}

function isProblem(v: unknown): v is Problem {
  return typeof v === "object" && v !== null && "status" in v && "code" in v;
}

const seg = encodeURIComponent;
const get = <T>(path: string, params?: Params) => request<T>("GET", path, { params });
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, { body: body ?? {} });
const patch = <T>(path: string, body: unknown) => request<T>("PATCH", path, { body });
const put = <T>(path: string, body: unknown) => request<T>("PUT", path, { body });
const del = <T = void>(path: string, body?: unknown) =>
  request<T>("DELETE", path, body === undefined ? {} : { body });

/** Builds the query string for the row endpoints (complex values travel as JSON). */
export function rowParams(r: RowRequest): Params {
  return {
    limit: r.limit,
    cursor: r.cursor ?? undefined,
    sort: r.sort?.length ? JSON.stringify(r.sort) : undefined,
    filters: r.filters?.length ? JSON.stringify(r.filters) : undefined,
    columns: r.columns?.length ? JSON.stringify(r.columns) : undefined,
    search: r.search || undefined,
    search_columns:
      r.search && r.searchColumns?.length ? JSON.stringify(r.searchColumns) : undefined,
  };
}

export const authApi = {
  setupStatus: () => get<{ setup_required: boolean }>("auth/setup-status"),
  setup: (b: {
    token: string;
    username: string;
    password: string;
    email?: string;
    display_name?: string;
  }) => post<User>("auth/setup", b),
  login: async (username: string, password: string) => {
    const r = await post<LoginResponse>("auth/login", { username, password });
    setCsrfToken(r.csrf_token);
    return r;
  },
  logout: async () => {
    await post<void>("auth/logout");
    setCsrfToken(null);
  },
  session: async () => {
    const s = await get<SessionInfo>("auth/session");
    setCsrfToken(s.csrf_token);
    return s;
  },
  verifyMfa: async (code: string) => {
    const r = await post<LoginResponse>("auth/mfa/verify", { code });
    setCsrfToken(r.csrf_token);
    return r;
  },
  startMfaEnrollment: () => post<{ secret: string; otpauth_uri: string }>("auth/mfa/enroll"),
  confirmMfaEnrollment: async (code: string) => {
    const r = await post<{ recovery_codes: string[]; csrf_token: string }>(
      "auth/mfa/enroll/confirm",
      { code },
    );
    setCsrfToken(r.csrf_token);
    return r;
  },
  disableMfa: (password: string, code: string) =>
    post<void>("auth/mfa/disable", { password, code }),
  changePassword: (current_password: string, new_password: string) =>
    post<void>("auth/password", { current_password, new_password }),
  sessions: () => get<SessionItem[]>("auth/sessions"),
  revokeSession: (id: string) => del(`auth/sessions/${seg(id)}`),
};

export const usersApi = {
  list: () => get<User[]>("users"),
  directory: () => get<DirectoryUser[]>("directory/users"),
  create: (b: {
    username: string;
    password: string;
    role_id: string;
    email?: string;
    display_name?: string;
  }) => post<User>("users", b),
  update: (
    id: string,
    b: Partial<{ display_name: string; email: string; role_id: string; is_active: boolean }>,
  ) => patch<User>(`users/${seg(id)}`, b),
  resetPassword: (id: string, new_password: string) =>
    post<void>(`users/${seg(id)}/password-reset`, { new_password }),
  resetMfa: (id: string) => post<void>(`users/${seg(id)}/mfa/reset`),
  revokeSessions: (id: string) => del(`users/${seg(id)}/sessions`),
};

export const rolesApi = {
  list: () => get<Role[]>("roles"),
  permissions: () => get<string[]>("permissions"),
  create: (b: { name: string; description: string; permissions: string[]; require_mfa: boolean }) =>
    post<Role>("roles", b),
  update: (
    id: string,
    b: Partial<{ description: string; permissions: string[]; require_mfa: boolean }>,
  ) => patch<Role>(`roles/${seg(id)}`, b),
  remove: (id: string) => del(`roles/${seg(id)}`),
};

export const connectionsApi = {
  list: () => get<Connection[]>("connections"),
  get: (id: string) => get<Connection>(`connections/${seg(id)}`),
  create: (b: ConnectionInput) => post<Connection>("connections", b),
  update: (id: string, b: Partial<ConnectionInput>) =>
    patch<Connection>(`connections/${seg(id)}`, b),
  remove: (id: string) => del(`connections/${seg(id)}`),
  test: (id: string) => post<TestResult>(`connections/${seg(id)}/test`),
  grants: (id: string) => get<Grant[]>(`connections/${seg(id)}/grants`),
  setGrants: (id: string, grants: { user_id: string; role_id: string }[]) =>
    put<Grant[]>(`connections/${seg(id)}/grants`, { grants }),
  scopeGrants: (id: string) => get<ScopeGrant[]>(`connections/${seg(id)}/scope-grants`),
  setScopeGrants: (
    id: string,
    grants: {
      user_id: string;
      role_id: string;
      database: string;
      schema: string;
      object: string;
    }[],
  ) => put<ScopeGrant[]>(`connections/${seg(id)}/scope-grants`, { grants }),
  credentials: (id: string) => get<CredentialsStatus>(`connections/${seg(id)}/credentials/me`),
  setCredentials: (id: string, username: string, password: string) =>
    put<CredentialsStatus>(`connections/${seg(id)}/credentials/me`, { username, password }),
  removeCredentials: (id: string) => del(`connections/${seg(id)}/credentials/me`),
};

export interface TablePath {
  cid: string;
  db: string;
  schema?: string;
  table: string;
}

function tableBase(p: TablePath): string {
  const base = `connections/${seg(p.cid)}/databases/${seg(p.db)}`;
  return p.schema !== undefined
    ? `${base}/schemas/${seg(p.schema)}/tables/${seg(p.table)}`
    : `${base}/tables/${seg(p.table)}`;
}

export const explorerApi = {
  databases: (cid: string) => get<DatabaseInfo[]>(`connections/${seg(cid)}/databases`),
  schemas: (cid: string, db: string) =>
    get<SchemaInfo[]>(`connections/${seg(cid)}/databases/${seg(db)}/schemas`),
  tables: (cid: string, db: string, schema?: string) =>
    get<TableInfo[]>(
      schema !== undefined
        ? `connections/${seg(cid)}/databases/${seg(db)}/schemas/${seg(schema)}/tables`
        : `connections/${seg(cid)}/databases/${seg(db)}/tables`,
    ),
  sequences: (cid: string, db: string, schema: string) =>
    get<SequenceInfo[]>(
      `connections/${seg(cid)}/databases/${seg(db)}/schemas/${seg(schema)}/sequences`,
    ),
  functions: (cid: string, db: string, schema: string) =>
    get<FunctionInfo[]>(
      `connections/${seg(cid)}/databases/${seg(db)}/schemas/${seg(schema)}/functions`,
    ),
  fn: (cid: string, db: string, schema: string, id: string) =>
    get<FunctionInfo>(
      `connections/${seg(cid)}/databases/${seg(db)}/schemas/${seg(schema)}/functions/${seg(id)}`,
    ),
  structure: (p: TablePath) => get<TableStructure>(`${tableBase(p)}/structure`),
  rows: (p: TablePath, r: RowRequest) => get<RowPage>(`${tableBase(p)}/rows`, rowParams(r)),

  mongoDatabases: (cid: string) => get<DatabaseInfo[]>(`connections/${seg(cid)}/mongo/databases`),
  mongoCollections: (cid: string, db: string) =>
    get<CollectionInfo[]>(`connections/${seg(cid)}/mongo/databases/${seg(db)}/collections`),
  mongoCollection: (cid: string, db: string, coll: string) =>
    get<CollectionDetail>(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}`,
    ),
  documents: (cid: string, db: string, coll: string, r: DocRequest) =>
    get<DocPage>(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}/documents`,
      {
        limit: r.limit,
        cursor: r.cursor ?? undefined,
        filter: r.filter?.trim() || undefined,
        sort: r.sort?.trim() || undefined,
        projection: r.projection?.trim() || undefined,
      },
    ),
};

export interface AuditQuery {
  before_id?: number;
  limit?: number;
  action?: string;
  actor?: string;
  connection_id?: string;
  result?: string;
}

export const auditApi = {
  events: (q: AuditQuery) =>
    get<{ events: AuditEvent[]; next_before_id: number | null }>("audit/events", { ...q }),
  verify: () =>
    get<{ ok: boolean; checked: number; first_bad_id: number | null; reason: string | null }>(
      "audit/verify",
    ),
};

export const settingsApi = {
  networkPolicy: () => get<NetworkPolicy>("settings/network-policy"),
  setNetworkPolicy: (b: { allow_cidrs: string[]; allow_hosts: string[] }) =>
    put<NetworkPolicy>("settings/network-policy", b),
  rotateKeys: () =>
    post<{ active_kek: string; connections: number; mfa_secrets: number }>(
      "settings/encryption/rotate",
    ),
};

// ---- Phase 3: data editor ----------------------------------------------------------------

export const editorApi = {
  insertRow: (p: TablePath, values: Record<string, unknown>) =>
    post<RowResult>(`${tableBase(p)}/rows`, { values }),
  updateRow: (
    p: TablePath,
    rowKey: string,
    values: Record<string, unknown>,
    expected: Record<string, unknown>,
  ) => patch<RowResult>(`${tableBase(p)}/rows/${seg(rowKey)}`, { values, expected }),
  deleteRow: (
    p: TablePath,
    rowKey: string,
    body: { expected?: Record<string, unknown>; confirm?: string },
  ) => request<void>("DELETE", `${tableBase(p)}/rows/${seg(rowKey)}`, { body }),
  bulkPreview: (
    p: TablePath,
    body: { operation: "delete" | "update"; selector: Selector; values?: Record<string, unknown> },
  ) => post<BulkPreview>(`${tableBase(p)}/rows/bulk/preview`, body),
  bulkDelete: (
    p: TablePath,
    body: { selector: Selector; token: string; expected_count: number; confirm?: string },
  ) => post<{ affected: number }>(`${tableBase(p)}/rows/bulk/delete`, body),
  bulkUpdate: (
    p: TablePath,
    body: {
      selector: Selector;
      values: Record<string, unknown>;
      token: string;
      expected_count: number;
      confirm?: string;
    },
  ) => post<{ affected: number }>(`${tableBase(p)}/rows/bulk/update`, body),
  cell: (p: TablePath, rowKey: string, column: string) =>
    get<{ value: unknown; length: number | null }>(
      `${tableBase(p)}/rows/${seg(rowKey)}/cells/${seg(column)}`,
    ),
  /** Same-origin URL for a browser download (the session cookie authenticates it). */
  exportUrl: (p: TablePath, r: RowRequest, format: ExportFormat) =>
    `${BASE}/${tableBase(p)}/export${query({ ...rowParams({ ...r, limit: 1, cursor: null }), limit: undefined, format })}`,

  markers: (cid: string) => get<SensitiveMarker[]>(`connections/${seg(cid)}/sensitive`),
  setMarkers: (cid: string, markers: SensitiveMarker[]) =>
    put<SensitiveMarker[]>(`connections/${seg(cid)}/sensitive`, { markers }),

  // MongoDB
  insertDocument: (cid: string, db: string, coll: string, document: unknown) =>
    post<{ document: Record<string, unknown>; doc_key: string | null }>(
      `${mongoBase(cid, db, coll)}/documents`,
      { document },
    ),
  replaceDocument: (
    cid: string,
    db: string,
    coll: string,
    docKey: string,
    original: unknown,
    document: unknown,
  ) =>
    put<{ document: Record<string, unknown>; doc_key: string | null }>(
      `${mongoBase(cid, db, coll)}/documents/${seg(docKey)}`,
      { original, document },
    ),
  deleteDocument: (
    cid: string,
    db: string,
    coll: string,
    docKey: string,
    body: { original?: unknown; confirm?: string },
  ) => request<void>("DELETE", `${mongoBase(cid, db, coll)}/documents/${seg(docKey)}`, { body }),
  docBulkPreview: (
    cid: string,
    db: string,
    coll: string,
    body: { operation: "delete" | "update"; filter: unknown; set?: unknown },
  ) => post<DocBulkPreview>(`${mongoBase(cid, db, coll)}/documents/bulk/preview`, body),
  docBulkDelete: (
    cid: string,
    db: string,
    coll: string,
    body: { filter: unknown; token: string; expected_count: number; confirm?: string },
  ) => post<{ affected: number }>(`${mongoBase(cid, db, coll)}/documents/bulk/delete`, body),
  docExportUrl: (
    cid: string,
    db: string,
    coll: string,
    r: { filter?: string; sort?: string; projection?: string },
    format: "json" | "ndjson",
  ) =>
    `${BASE}/${mongoBase(cid, db, coll)}/export${query({
      format,
      filter: r.filter?.trim() || undefined,
      sort: r.sort?.trim() || undefined,
      projection: r.projection?.trim() || undefined,
    })}`,
};

function mongoBase(cid: string, db: string, coll: string): string {
  return `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}`;
}

// ---- Phase 4: query workspaces -------------------------------------------------------------

/** POST a request and save the (streamed) response as a file. */
async function downloadPost(path: string, body: unknown, fallbackName: string): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`${BASE}/${path}`, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data: unknown = await res.json().catch(() => undefined);
    throw new ApiError(
      isProblem(data)
        ? data
        : {
            type: "about:blank",
            title: res.statusText,
            status: res.status,
            code: `http_${res.status}`,
          },
    );
  }
  const name =
    /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export const queryApi = {
  execute: (cid: string, body: SqlExecuteRequest) =>
    post<SqlExecuteResult>(`connections/${seg(cid)}/queries/execute`, body),
  cancel: (cid: string, operationId: string) =>
    post<{ status: string; best_effort: boolean }>(
      `connections/${seg(cid)}/queries/${seg(operationId)}/cancel`,
    ),
  exportQuery: (
    cid: string,
    body: { sql: string; database: string; schema?: string | null; format: ExportFormat },
  ) => downloadPost(`connections/${seg(cid)}/queries/export`, body, `query-result.${body.format}`),
  mongoExecute: (cid: string, body: MongoExecuteRequest) =>
    post<MongoExecuteResult>(`connections/${seg(cid)}/mongo/queries/execute`, body),
  history: (cid: string, params: { kind?: "sql" | "mongo"; before_id?: number; limit?: number }) =>
    get<HistoryEntry[]>(`connections/${seg(cid)}/queries/history`, params),
  deleteHistory: (cid: string, id: number) => del(`connections/${seg(cid)}/queries/history/${id}`),
  clearHistory: (cid: string) => del(`connections/${seg(cid)}/queries/history`),
  saved: (cid: string) => get<SavedQuery[]>(`connections/${seg(cid)}/queries/saved`),
  createSaved: (
    cid: string,
    body: Pick<SavedQuery, "kind" | "name" | "description" | "statement" | "visibility"> & {
      database?: string | null;
      schema?: string | null;
    },
  ) => post<SavedQuery>(`connections/${seg(cid)}/queries/saved`, body),
  deleteSaved: (cid: string, id: string) => del(`connections/${seg(cid)}/queries/saved/${seg(id)}`),
  historySettings: () => get<HistorySettings>("settings/query-history"),
  setHistorySettings: (b: HistorySettings) => put<HistorySettings>("settings/query-history", b),
};

// ------------------------------------------------------------------ Phase 5

/** Where an object-level request goes: with or without the PostgreSQL schema segment. */
function objectBase(p: TablePath): string {
  return tableBase(p);
}

export const adminApi = {
  createDatabase: (cid: string, name: string, options?: Record<string, unknown>) =>
    post<{ name: string }>(`connections/${seg(cid)}/databases`, { name, options: options ?? {} }),
  dropDatabase: (cid: string, db: string) =>
    del(`connections/${seg(cid)}/databases/${seg(db)}`, { confirm: db }),
  createSchema: (cid: string, db: string, name: string) =>
    post<{ name: string }>(`connections/${seg(cid)}/databases/${seg(db)}/schemas`, { name }),
  dropSchema: (cid: string, db: string, schema: string) =>
    del(`connections/${seg(cid)}/databases/${seg(db)}/schemas/${seg(schema)}`, { confirm: schema }),
  createIndex: (p: TablePath, name: string, columns: string[], unique: boolean) =>
    post<{ name: string }>(`${objectBase(p)}/indexes`, { name, columns, unique }),
  dropIndex: (p: TablePath, name: string, confirm?: string) =>
    del(`${objectBase(p)}/indexes/${seg(name)}`, { confirm }),
  mongoCreateDatabase: (cid: string, name: string, collection: string) =>
    post<{ name: string }>(`connections/${seg(cid)}/mongo/databases`, { name, collection }),
  mongoDropDatabase: (cid: string, db: string) =>
    del(`connections/${seg(cid)}/mongo/databases/${seg(db)}`, { confirm: db }),
  mongoCreateCollection: (
    cid: string,
    db: string,
    b: { name: string; capped?: boolean; size_bytes?: number; max_docs?: number },
  ) => post<{ name: string }>(`connections/${seg(cid)}/mongo/databases/${seg(db)}/collections`, b),
  mongoDropCollection: (cid: string, db: string, coll: string) =>
    del(`connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}`, {
      confirm: coll,
    }),
  mongoCreateIndex: (
    cid: string,
    db: string,
    coll: string,
    b: {
      keys: { field: string; direction: number | string }[];
      name?: string;
      unique?: boolean;
      sparse?: boolean;
      ttl_seconds?: number;
    },
  ) =>
    post<{ name: string }>(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}/indexes`,
      b,
    ),
  mongoDropIndex: (cid: string, db: string, coll: string, name: string, confirm?: string) =>
    del(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}/indexes/${seg(name)}`,
      { confirm },
    ),
};

export const importApi = {
  previewCsv: (p: TablePath, b: CsvImportOptions) =>
    post<CsvPreview>(`${objectBase(p)}/import/preview`, b),
  commitCsv: (p: TablePath, b: CsvImportOptions & { token: string; confirm?: string }) =>
    post<{ imported: number }>(`${objectBase(p)}/import/commit`, b),
  previewDocs: (cid: string, db: string, coll: string, content: string, format: string) =>
    post<DocImportPreview>(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}/import/preview`,
      { content, format },
    ),
  commitDocs: (
    cid: string,
    db: string,
    coll: string,
    b: { content: string; format: string; token: string; confirm?: string },
  ) =>
    post<{ imported: number }>(
      `connections/${seg(cid)}/mongo/databases/${seg(db)}/collections/${seg(coll)}/import/commit`,
      b,
    ),
};

export const governanceApi = {
  approvals: (scope: "mine" | "review", status?: string) =>
    get<Approval[]>("approvals", { scope, status }),
  approve: (id: string, reason?: string) =>
    post<Approval>(`approvals/${seg(id)}/approve`, { reason: reason || null }),
  reject: (id: string, reason?: string) =>
    post<Approval>(`approvals/${seg(id)}/reject`, { reason: reason || null }),
  cancel: (id: string) => post<Approval>(`approvals/${seg(id)}/cancel`),
  policies: () => get<PoliciesResponse>("governance/policies"),
  setPolicy: (b: { connection_id?: string; environment?: string; operations: string[] }) =>
    put<ApprovalPolicy>("governance/policies", b),
};

export const tokensApi = {
  list: (scope: "mine" | "all" = "mine") => get<ApiTokenInfo[]>("tokens", { scope }),
  scopes: () => get<{ scopes: string[] }>("tokens/scopes"),
  create: (b: {
    name: string;
    scopes: string[];
    connection_ids?: string[] | null;
    expires_in_days: number;
  }) => post<ApiTokenCreated>("tokens", b),
  revoke: (id: string) => del(`tokens/${seg(id)}`),
};

export const ssoApi = {
  publicConfig: () => get<OidcPublic>("auth/oidc/config"),
  loginUrl: `${BASE}/auth/oidc/login`,
  config: () => get<OidcConfig>("settings/oidc"),
  setConfig: (
    b: Omit<OidcConfig, "client_secret_set" | "redirect_uri"> & { client_secret?: string },
  ) => put<OidcConfig>("settings/oidc", b),
};

export const auditSinkApi = {
  get: () => get<AuditSink>("settings/audit-sink"),
  set: (b: Omit<AuditSink, "last_forwarded_id"> & { backfill?: boolean }) =>
    put<AuditSink>("settings/audit-sink", b),
  test: () => post<{ ok: boolean; error: string | null }>("settings/audit-sink/test"),
  exportUrl: (format: "csv" | "json" | "ndjson", params: Record<string, string> = {}) =>
    `${BASE}/audit/export${query({ format, ...params })}`,
};
