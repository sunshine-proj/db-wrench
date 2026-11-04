export type DbType = "postgresql" | "mysql" | "mongodb";
export type Environment = "production" | "staging" | "development" | "other";
export type TlsMode = "disable" | "require" | "verify-ca" | "verify-full";

export interface Problem {
  type: string;
  title: string;
  status: number;
  code: string;
  detail?: string;
  request_id?: string;
  errors?: { loc: string[]; msg: string }[];
  mfa_state?: string;
  code_hint?: string;
}

export interface User {
  id: string;
  username: string;
  display_name: string;
  email: string | null;
  role_id: string;
  role: string;
  is_active: boolean;
  mfa_enabled: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface SessionInfo {
  authenticated: boolean;
  user: User;
  csrf_token: string;
  mfa_state: "ok" | "pending_verify" | "pending_enroll";
  app_permissions: string[];
  password_change_required: boolean;
}

export interface LoginResponse {
  mfa_state: SessionInfo["mfa_state"];
  csrf_token: string;
  user: User;
}

export interface SessionItem {
  id: string;
  created_at: string;
  last_seen_at: string;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface Role {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  permissions: string[];
  require_mfa: boolean;
}

export interface DirectoryUser {
  id: string;
  username: string;
  display_name: string;
}

export interface Capabilities {
  family: "relational" | "document";
  engine: string;
  supports_schemas: boolean;
  supports_views: boolean;
  supports_sequences: boolean;
  supports_functions: boolean;
  supports_indexes: boolean;
  supports_foreign_keys: boolean;
  supports_keyset_pagination: boolean;
}

export interface Connection {
  id: string;
  name: string;
  db_type: DbType;
  environment: Environment;
  host: string;
  port: number;
  database: string | null;
  username: string;
  has_password: boolean;
  credential_model: string;
  tls_mode: TlsMode;
  has_tls_ca: boolean;
  connect_timeout_s: number;
  pool_max: number;
  read_only: boolean;
  enabled: boolean;
  options: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  permissions: string[];
  capabilities: Capabilities;
}

export interface ConnectionInput {
  name: string;
  db_type: DbType;
  environment: Environment;
  host: string;
  port: number;
  database: string | null;
  username: string;
  password?: string;
  tls_mode: TlsMode;
  tls_ca: string | null;
  connect_timeout_s: number;
  pool_max: number;
  read_only: boolean;
  enabled: boolean;
  options: Record<string, unknown>;
}

export interface TestResult {
  ok: boolean;
  server_version: string | null;
  latency_ms: number | null;
  error: string | null;
  error_code: string | null;
}

export interface Grant {
  user_id: string;
  username: string;
  role_id: string;
  role: string;
}

export interface NetworkPolicy {
  allow_cidrs: string[];
  allow_hosts: string[];
  allow_loopback: boolean;
  always_denied: string[];
}

export interface AuditEvent {
  id: number;
  ts: string;
  actor_user_id: string | null;
  actor_username: string | null;
  ip: string | null;
  request_id: string | null;
  action: string;
  connection_id: string | null;
  target: Record<string, unknown>;
  result: "success" | "failure" | "denied";
  detail: Record<string, unknown>;
  hash: string;
}

// ---- explorer ---------------------------------------------------------------------------

export interface DatabaseInfo {
  name: string;
  comment?: string | null;
  size_on_disk?: number | null;
}

export interface SchemaInfo {
  name: string;
  owner: string | null;
}

export type TableKind =
  "table" | "view" | "materialized_view" | "foreign_table" | "partitioned_table";

export interface TableInfo {
  name: string;
  kind: TableKind;
  estimated_rows: number | null;
  comment: string | null;
}

export interface ColumnInfo {
  name: string;
  data_type: string;
  nullable: boolean;
  default: string | null;
  is_identity: boolean;
  is_generated: boolean;
  comment: string | null;
  ordinal: number;
}

export interface ForeignKey {
  name: string;
  columns: string[];
  ref_schema: string | null;
  ref_table: string;
  ref_columns: string[];
  on_update: string | null;
  on_delete: string | null;
}

export interface IndexInfo {
  name: string;
  columns: string[];
  unique: boolean;
  primary: boolean;
  method: string | null;
  definition: string | null;
}

export interface ConstraintInfo {
  name: string;
  kind: string;
  columns: string[];
  definition: string | null;
}

export interface TableStructure {
  schema: string | null;
  name: string;
  kind: string;
  columns: ColumnInfo[];
  primary_key: string[];
  foreign_keys: ForeignKey[];
  indexes: IndexInfo[];
  constraints: ConstraintInfo[];
  estimated_rows: number | null;
  comment: string | null;
}

export interface SequenceInfo {
  name: string;
  data_type: string;
  start: number;
  increment: number;
  min_value: number;
  max_value: number;
  cycle: boolean;
}

export interface FunctionInfo {
  id: string;
  name: string;
  arguments: string;
  returns: string | null;
  language: string | null;
  kind: string;
  definition: string | null;
}

export interface RowPage {
  columns: { name: string; type: string }[];
  rows: unknown[][];
  limit: number;
  next_cursor: string | null;
  total_estimate: number | null;
  truncated: boolean;
  pagination: "keyset" | "offset" | "unordered_offset";
  warnings: string[];
  row_keys: (string | null)[];
  editable: boolean;
}

export type FilterOp =
  | "eq"
  | "ne"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "contains"
  | "starts_with"
  | "ends_with"
  | "is_null"
  | "is_not_null"
  | "in";

export interface RowFilter {
  column: string;
  op: FilterOp;
  value?: unknown;
}

export interface SortSpec {
  column: string;
  direction: "asc" | "desc";
}

export interface RowRequest {
  limit: number;
  cursor?: string | null;
  sort?: SortSpec[];
  filters?: RowFilter[];
  columns?: string[];
  search?: string;
  searchColumns?: string[];
}

export interface CollectionInfo {
  name: string;
  type: "collection" | "view" | "timeseries";
}

export interface DocIndex {
  name: string;
  keys: [string, number | string][];
  unique: boolean;
  sparse: boolean;
  ttl_seconds: number | null;
  partial_filter: Record<string, unknown> | null;
}

export interface CollectionDetail {
  name: string;
  type: string;
  options: Record<string, unknown>;
  capped: boolean;
  stats: Record<string, number | boolean | null>;
  indexes: DocIndex[];
}

export interface DocPage {
  documents: Record<string, unknown>[];
  limit: number;
  next_cursor: string | null;
  total_estimate: number | null;
  truncated: boolean;
  pagination: "keyset" | "offset";
  warnings: string[];
  doc_keys: (string | null)[];
}

export interface DocRequest {
  limit: number;
  cursor?: string | null;
  filter?: string;
  sort?: string;
  projection?: string;
}

// ---- Phase 3: editing ------------------------------------------------------------------

export interface RowResult {
  columns: { name: string; type: string }[];
  row: unknown[] | null;
  row_key: string | null;
}

export interface Selector {
  row_keys?: string[];
  filters?: RowFilter[];
  search?: string;
  search_columns?: string[];
  all_rows?: boolean;
}

export interface BulkPreview {
  count: number;
  columns: string[];
  sample: unknown[][];
  token: string;
  requires_confirmation: boolean;
}

export interface DocBulkPreview {
  count: number;
  sample: Record<string, unknown>[];
  token: string;
  requires_confirmation: boolean;
}

export interface SensitiveMarker {
  database: string;
  schema: string;
  object: string;
  field: string;
}

export type ExportFormat = "csv" | "json" | "ndjson";

// ---- Phase 4: query workspaces ---------------------------------------------------------

export interface SqlResultSet {
  columns: { name: string; type: string }[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
}

export interface SqlExecuteRequest {
  sql: string;
  database: string;
  schema?: string | null;
  timeout_s?: number;
  max_rows?: number;
  confirm_destructive?: boolean;
  confirm?: string;
  rollback?: boolean;
  operation_id?: string;
}

export interface SqlExecuteResult {
  operation_id: string;
  results: SqlResultSet[];
  statements: string[];
  elapsed_ms: number;
  committed: boolean;
}

export interface MongoExecuteRequest {
  database: string;
  collection: string;
  kind: "find" | "aggregate";
  filter?: unknown;
  projection?: unknown;
  sort?: unknown;
  pipeline?: unknown;
  limit?: number;
  timeout_s?: number;
  operation_id?: string;
}

export interface MongoExecuteResult {
  operation_id: string;
  documents: Record<string, unknown>[];
  count: number;
  truncated: boolean;
  elapsed_ms: number;
}

export interface HistoryEntry {
  id: number;
  kind: "sql" | "mongo";
  database: string | null;
  schema: string | null;
  statement: string;
  redacted: boolean;
  status: "succeeded" | "failed" | "cancelled" | "timeout";
  error_code: string | null;
  duration_ms: number;
  row_count: number | null;
  created_at: string;
}

export interface SavedQuery {
  id: string;
  kind: "sql" | "mongo";
  name: string;
  description: string;
  database: string | null;
  schema: string | null;
  statement: string;
  visibility: "private" | "shared";
  owner_id: string;
  mine: boolean;
  updated_at: string;
}

export interface HistorySettings {
  retention_days: number;
  store_literals: boolean;
}
